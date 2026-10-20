'use strict';
const crypto = require('node:crypto');
const runtimeContracts = require('./event_contracts.cjs');
const EXTENDED = ['create_action_group', 'set_event_callback_contract', 'append_node', 'bind_component_event', 'set_parameter'];
const NATIVE = ['insert_before', 'insert_after', 'replace_params', 'replace_block', 'delete_block', 'move_block'];
const TYPES = new Set(['string', 'number', 'double', 'boolean', 'object', 'dictionary', 'array', 'dateTime', 'any']);
const FORBIDDEN = ['key', 'depth', 'title', 'description', 'js_code', 'csharp_code', 'relation_data', 'sql', 'raw_code'];
function cloneParameters(params) { return JSON.parse(JSON.stringify(params || [])); }
function text(value, field) {
  if (typeof value !== 'string' || !value.trim()) throw new Error(field + ' must be a nonblank string');
  return value;
}
function fields(op, names) {
  for (const key of Object.keys(op)) if (!names.includes(key)) throw new Error('Unsupported field for ' + op.operation + ': ' + key);
}
const UNSAFE_KEYS = new Set(['__proto__', 'constructor', 'prototype']);
function object(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
function sameJson(left, right) {
  if (left === right) return true;
  if (left === null || right === null || typeof left !== typeof right) return false;
  if (Array.isArray(left)) return Array.isArray(right) && left.length === right.length && left.every((item, i) => sameJson(item, right[i]));
  if (!object(left) || !object(right)) return false;
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length && keys.every(key => Object.hasOwn(right, key) && sameJson(left[key], right[key]));
}
function parameterValue(value) {
  if (value === null || ['string', 'boolean'].includes(typeof value)) return;
  if (typeof value === 'number' && Number.isFinite(value)) return;
  if (Array.isArray(value)) { for (const item of value) parameterValue(item); return; }
  if (!object(value)) throw new Error('Parameter value must be JSON data');
  for (const [key, item] of Object.entries(value)) {
    if (UNSAFE_KEYS.has(key)) throw new Error('Unsafe parameter key: ' + key);
    parameterValue(item);
  }
}
function parameterPath(path) {
  text(path, 'path');
  if (!path.startsWith('/') || /~(?![01])/.test(path)) throw new Error('path must be a valid JSON pointer');
  const parts = path.slice(1).split('/').map(part => part.replace(/~1/g, '/').replace(/~0/g, '~'));
  if (parts.length < 2 || !['inputParams', 'outputParams'].includes(parts[0])) throw new Error('path must select an existing inputParams/outputParams parameter');
  if (parts.some(part => UNSAFE_KEYS.has(part))) throw new Error('Unsafe parameter path');
  return parts;
}
function sameRecordKeys(before, after) {
  if (Array.isArray(before)) {
    if (!Array.isArray(after)) throw new Error('An array parameter must remain an array');
    return; // A complete native array parameter may add/remove rows under its full old-value precondition.
  }
  if (!object(before)) {
    if (object(after) || Array.isArray(after)) throw new Error('A scalar parameter cannot become a container');
    return;
  }
  if (!object(after) || Object.keys(before).length !== Object.keys(after).length || Object.keys(before).some(key => !Object.hasOwn(after, key))) {
    throw new Error('Parameter record replacement must preserve existing keys');
  }
  for (const key of Object.keys(before)) sameRecordKeys(before[key], after[key]);
}
function checkedParameters(params) {
  if (!object(params)) throw new Error('Target paramsValue must be an object');
  const result = JSON.parse(JSON.stringify(params));
  for (const section of ['inputParams', 'outputParams']) {
    if (!Object.hasOwn(result, section)) result[section] = {};
    if (!object(result[section])) throw new Error(section + ' must be a parameter dictionary');
  }
  return result;
}
function checkEditableInput(element, parts, params) {
  const known = {
    SetVariable: [['inputParams', 'variableValue'], ['outputParams', 'variableName']],
    SetVariableValue: [['inputParams', 'attributeValue']],
  };
  for (const [section, name] of known[element] || []) {
    const record = params[section]?.[name];
    if (parts[0] === section && parts[1] === name && parts.length !== 2) {
      throw new Error('Submit the complete ParamInput record so value/code stay synchronized');
    }
    if (object(record) && record.paramTypes === 'custom' && (typeof record.value !== 'string' || record.value !== record.code)) {
      throw new Error('Custom ParamInput value and code must be synchronized');
    }
  }
}
function eventContract(catalog, type, event, api, allowIncomplete = false) {
  const components = catalog.filter(c => c.type === type);
  if (components.length !== 1) throw new Error('Unknown or ambiguous component type: ' + type);
  const events = (components[0].event || []).filter(e => e.type === event);
  if (events.length !== 1) throw new Error('Unknown or ambiguous component event: ' + type + '.' + event);
  const params = events[0].params || [];
  if (!Array.isArray(params)) throw new Error('Invalid event parameter contract');
  const declarations = direction => params.filter(p => p.type === direction).map(p => ({
    key: text(p.name, 'event parameter name'), title: p.name,
    type: text(p.dataType, 'event parameter type'), description: p.title || '',
  }));
  for (const p of params) if (!['input', 'output'].includes(p.type)) throw new Error('Unsupported event parameter direction');
  const contract = runtimeContracts.enrich({ component_type: type, component_title: components[0].title, event_name: event,
    event_title: events[0].title, input_params: declarations('input'), output_params: declarations('output') }, events[0], api);
  return allowIncomplete ? contract : runtimeContracts.requireComplete(contract);
}
function componentContract(catalog, type, api) {
  const matches = catalog.filter(c => c.type === type);
  if (matches.length !== 1) throw new Error('Unknown or ambiguous component type: ' + type);
  return { component_type: type, title: matches[0].title,
    events: (matches[0].event || []).map(e => eventContract(catalog, type, e.type, api, true)) };
}
function applyOperations(design, operations, native, api) {
  const aliases = new Map();
  const diff = [];
  const resolve = value => {
    text(value, 'action group');
    if (!value.startsWith('@')) return value;
    if (!aliases.has(value.slice(1))) throw new Error('Unknown action group alias: ' + value);
    return aliases.get(value.slice(1));
  };
  const issuedKeys = new Set();
  const uid = prefix => {
    const used = new Set(design.actionData.flatMap(g => [g.key, ...(g.data || []).map(n => n.key), ...(g.inputParams || []).map(p => p.key), ...(g.outputParams || []).map(p => p.key)]));
    let key; do { key = prefix + crypto.randomBytes(4).toString('hex'); } while (used.has(key) || issuedKeys.has(key));
    issuedKeys.add(key);
    return key;
  };
  function declarations(values, section) {
    if (values === undefined) return [];
    if (!Array.isArray(values)) throw new Error(section + ' must be an array of parameter declarations');
    const titles = new Set();
    return values.map(p => {
      if (!p || typeof p !== 'object' || Array.isArray(p)) throw new Error('Invalid parameter declaration');
      for (const k of Object.keys(p)) if (!['title', 'type', 'description', 'required'].includes(k)) throw new Error('Unsupported declaration field: ' + k);
      const title = text(p.title, 'parameter title');
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(title) || titles.has(title)) throw new Error('Duplicate or invalid parameter name: ' + title);
      titles.add(title);
      if (!TYPES.has(p.type)) throw new Error('Unsupported parameter type: ' + p.type);
      if (p.required !== undefined && typeof p.required !== 'boolean') throw new Error('required must be boolean');
      if (p.description !== undefined && typeof p.description !== 'string') throw new Error('parameter description must be a string');
      return {key: uid('p'), title, type: p.type, description: p.description || '', required: !!p.required};
    });
  }
  for (let index = 0; index < operations.length; index++) {
    const op = operations[index];
    if (!op || typeof op !== 'object' || Array.isArray(op)) throw new Error('Every canvas operation must be an object');
    for (const f of FORBIDDEN) if (Object.hasOwn(op, f)) throw new Error('Operation contains forbidden field ' + f);
    if (op.operation === 'create_action_group') {
      fields(op, ['operation', 'group_alias', 'group_title', 'action_type', 'group_description', 'input_params', 'output_params', 'event_contract']);
      const alias = text(op.group_alias, 'group_alias');
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(alias) || aliases.has(alias)) throw new Error('Invalid or duplicate group_alias: ' + alias);
      if (!['js', 'csharp'].includes(op.action_type)) throw new Error('action_type must be js or csharp');
      if (op.group_description !== undefined && typeof op.group_description !== 'string') throw new Error('group_description must be a string');
      const group = { key: uid('m'), title: text(op.group_title, 'group_title'), actionType: op.action_type,
        description: op.group_description || '', data: [] };
      if (/[\r\n]/.test(group.title)) throw new Error('group_title must be one line');
      if (design.actionData.some(g => g.title === group.title)) throw new Error('Action group title already exists: ' + group.title);
      if (op.event_contract) {
        fields(op.event_contract, ['component_type', 'event_name']);
        if (op.action_type !== 'js' || op.input_params !== undefined || op.output_params !== undefined) throw new Error('event_contract requires a JS group without explicit parameter declarations');
        const contract = eventContract(api.catalog(), op.event_contract.component_type, op.event_contract.event_name, api);
        group.inputParams = contract.input_params; group.outputParams = contract.output_params;
      } else {
        group.inputParams = declarations(op.input_params, 'input_params');
        group.outputParams = declarations(op.output_params, 'output_params');
      }
      design.actionData.push(group); aliases.set(alias, group.key);
      diff.push({operation_index: index, operation: op.operation, action_group_key: group.key,
        group_alias: alias, group_title: group.title, action_type: group.actionType,
        input_params: group.inputParams, output_params: group.outputParams,
        generated_nodes: [], affected_node_keys: [], before_node_count: 0, after_node_count: 0});
      continue;
    }
    const group = api.actionByKey(design, resolve(op.action_group_key));
    if (op.operation === 'set_parameter') {
      fields(op, ['operation', 'action_group_key', 'target_node_key', 'expected_element_key', 'path', 'expected_old_value', 'value']);
      text(op.target_node_key, 'target_node_key');
      text(op.expected_element_key, 'expected_element_key');
      if (!Object.hasOwn(op, 'expected_old_value') || !Object.hasOwn(op, 'value')) throw new Error('set_parameter requires expected_old_value and value');
      if (design.actionData.filter(item => item.key === group.key).length !== 1 || group.data.filter(item => item.key === op.target_node_key).length !== 1) {
        throw new Error('set_parameter requires an unambiguous group and target node');
      }
      const at = api.nodeIndex(group, op.target_node_key), current = group.data[at];
      if (current.elementKey !== op.expected_element_key) throw new Error('Target element type changed; inspect the current node');
      const parts = parameterPath(op.path), params = checkedParameters(current.paramsValue);
      let parent = params;
      for (const part of parts.slice(0, -1)) {
        if (Array.isArray(parent)) throw new Error('Array indexes are unsupported; submit the complete array parameter and its old value');
        if (!object(parent) || !Object.hasOwn(parent, part)) throw new Error('Parameter path does not exist');
        parent = parent[part];
      }
      if (Array.isArray(parent)) throw new Error('Array indexes are unsupported; submit the complete array parameter and its old value');
      const leaf = parts.at(-1);
      if (!object(parent) || !Object.hasOwn(parent, leaf)) throw new Error('Parameter path does not exist');
      parameterValue(op.expected_old_value); parameterValue(op.value);
      if (!sameJson(parent[leaf], op.expected_old_value)) throw new Error('Parameter old value changed; inspect the current node');
      sameRecordKeys(parent[leaf], op.value);
      parent[leaf] = JSON.parse(JSON.stringify(op.value));
      checkEditableInput(current.elementKey, parts, params);
      // Validate native regeneration before replacing anything on the caller's canvas.
      const candidateGroup = {...group, data: group.data.slice()};
      const candidate = {...design, actionData: design.actionData.map(item => item === group ? candidateGroup : item)};
      const nativeDiff = native(candidate, [{operation:'replace_params', action_group_key:group.key,
        target_node_key:current.key, input_params:params.inputParams, output_params:params.outputParams}]);
      const generated = candidateGroup.data[at];
      if (candidateGroup.data.length !== group.data.length || generated.key !== current.key || generated.elementKey !== current.elementKey ||
          !sameJson(generated.depth, current.depth) || !sameJson(checkedParameters(generated.paramsValue), params)) {
        throw new Error('Native regeneration changed parameters outside the requested value; inspect the native contract and use an explicit complete replacement');
      }
      group.data[at] = generated;
      diff.push(...nativeDiff.map(item => ({...item, operation_index:index, operation:op.operation,
        element_key:current.elementKey, parameter_path:op.path})));
      continue;
    }
    if (op.operation === 'set_event_callback_contract') {
      fields(op, ['operation','action_group_key','component_type','event_name']);
      if (group.actionType !== 'js') throw new Error('Event callback contract requires a JS group');
      const contract = eventContract(api.catalog(), op.component_type, op.event_name, api);
      const preserve = (old, params) => params.map(p=>({...p, key:(old||[]).find(item=>item.title===p.title && item.type===p.type)?.key || p.key}));
      const before = {input_params:cloneParameters(group.inputParams),output_params:cloneParameters(group.outputParams)};
      group.inputParams = runtimeContracts.parametersMatch(group.inputParams || [], contract.input_params, contract, 'inputParams') ? cloneParameters(group.inputParams) : preserve(group.inputParams, contract.input_params);
      group.outputParams = preserve(group.outputParams, contract.output_params);
      diff.push({operation_index:index,operation:op.operation,action_group_key:group.key,group_title:group.title,
        before_parameters:before,input_params:group.inputParams,output_params:group.outputParams,
        affected_node_keys:[],generated_nodes:[],before_node_count:group.data.length,after_node_count:group.data.length});
      continue;
    }
    if (op.operation === 'append_node' || op.operation === 'bind_component_event') {
      const beforeCount = group.data.length;
      let generated, replaced = null;
      if (op.operation === 'append_node') {
        fields(op, ['operation', 'action_group_key', 'element_key', 'input_params', 'output_params']);
        generated = api.generateNodes(group, text(op.element_key, 'element_key'), op.input_params, op.output_params);
      } else {
        fields(op, ['operation', 'action_group_key', 'component_id', 'component_type', 'component_title', 'event_name', 'target_action_group_key', 'target_node_key', 'anchor_node_key', 'position']);
        if (group.actionType !== 'js') throw new Error('Component events can only be bound in a JS group');
        const id = text(op.component_id, 'component_id');
        const type = text(op.component_type, 'component_type');
        const event = text(op.event_name, 'event_name');
        const contract = eventContract(api.catalog(), type, event, api);
        const target = api.actionByKey(design, resolve(op.target_action_group_key));
        if (target.actionType !== 'js' || target.key === group.key) throw new Error('Event callback must be a different JS action group');
        for (const [key, expected] of [['inputParams', contract.input_params], ['outputParams', contract.output_params]]) {
          const actual = target[key] || [];
          if (!runtimeContracts.parametersMatch(actual, expected, contract, key)) throw new Error('Event callback ' + key + ' does not match the component contract');
        }
        const duplicates = design.actionData.flatMap(g => g.data || []).filter(n => n.elementKey === 'ComponentEvent' && n.key !== op.target_node_key &&
          (n.paramsValue?.inputParams?.name?.value === 'componentsVariable-' + id || n.paramsValue?.inputParams?.name?.code === 'inbiz(' + JSON.stringify(id) + ')' || n.paramsValue?.inputParams?.name?.code === "inbiz('" + id + "')" || (n.paramsValue?.inputParams?.name?.paramTypes === 'custom' && n.paramsValue?.inputParams?.name?.code === id)) && n.paramsValue?.inputParams?.eventName === event);
        if (duplicates.length) throw new Error('Component event is already bound; select its existing target_node_key');
        if (op.target_node_key) {
          if (op.anchor_node_key || op.position) throw new Error('Replacement cannot have an insertion position');
          replaced = group.data[api.nodeIndex(group, op.target_node_key)];
          if (replaced.elementKey !== 'ComponentEvent') throw new Error('target_node_key must identify a ComponentEvent node');
        } else if (op.anchor_node_key) {
          if (!['before', 'after'].includes(op.position)) throw new Error('Binding anchor requires position before or after');
        } else if (op.position && op.position !== 'append') throw new Error('Binding position requires an anchor');
        const name = {paramTypes: 'componentsVariable', value: 'componentsVariable-' + id,
          label: op.component_title || id, dataType: type, code: 'inbiz(' + JSON.stringify(id) + ')'};
        const action = {value: target.key, label: target.title, actionType: 'js', isActionSelect: true,
          action: 'openAction', payload: {id: target.key}};
        generated = api.generateNodes(group, 'ComponentEvent', {name, eventName: event, action}, {});
        if (generated.length !== 1) throw new Error('ComponentEvent must generate exactly one node');
      }
      if (replaced) {
        generated[0].key = replaced.key; generated[0].depth = replaced.depth;
        group.data[api.nodeIndex(group, replaced.key)] = generated[0];
      } else {
        const at = op.anchor_node_key ? api.nodeIndex(group, op.anchor_node_key) + (op.position === 'after' ? 1 : 0) : group.data.length;
        group.data.splice(at, 0, ...generated);
      }
      diff.push({operation_index: index, operation: op.operation, action_group_key: group.key,
        group_title: group.title, anchor_node_key: op.anchor_node_key || null,
        target_node_key: op.target_node_key || null, element_key: generated[0].elementKey,
        affected_node_keys: generated.map(n => n.key), generated_nodes: generated.map(n => ({key:n.key, element_key:n.elementKey})),
        before_node_count: beforeCount, after_node_count: group.data.length});
    } else {
      if (!NATIVE.includes(op.operation)) throw new Error('Unsupported canvas operation: ' + op.operation);
      const normalized = {...op, action_group_key: group.key};
      diff.push(...native(design, [normalized]).map(d => ({...d, operation_index:index})));
    }
  }
  return diff;
}
module.exports = {applyOperations, componentContract, eventContract, operations: [...NATIVE, ...EXTENDED]};
