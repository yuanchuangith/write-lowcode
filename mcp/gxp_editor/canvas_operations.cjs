'use strict';
const crypto = require('node:crypto');
const runtimeContracts = require('./event_contracts.cjs');
const operationContracts = require('./operation_contracts.json');
const EXTENDED = ['create_action_group', 'set_event_callback_contract', 'append_node', 'insert_in_block', 'bind_component_event', 'set_parameter', 'set_array_item'];
const NATIVE = ['insert_before', 'insert_after', 'replace_params', 'replace_block', 'delete_block', 'move_block'];
const TYPES = new Set(['string', 'number', 'double', 'boolean', 'object', 'dictionary', 'array', 'dateTime', 'any']);
const FORBIDDEN = operationContracts.forbidden_fields;
function cloneParameters(params) { return JSON.parse(JSON.stringify(params || [])); }
function text(value, field) {
  if (typeof value !== 'string' || !value.trim()) reject('invalid_operation', field + ' must be a nonblank string');
  return value;
}
function fields(op, names) {
  const contract = operationContracts.operations[op.operation];
  if (names.includes('operation') && contract?.strict_fields) names = [...contract.required, ...contract.optional];
  for (const key of Object.keys(op)) if (!names.includes(key)) reject('invalid_operation', 'Unsupported field for ' + op.operation + ': ' + key);
}
function reject(reason, message, details) { const error = new Error(message); error.reason_code = reason; if (details) error.details = details; throw error; }
function operationFailure(error, op, index) {
  if (!(error instanceof Error)) error = new Error('Canvas operation rejected');
  const bounded = value => typeof value === 'string' ? value.slice(0, 256) : null;
  const prior = object(error.details) ? error.details : {};
  const candidateReason = error.reason_code || prior.reason_code;
  const reason = typeof candidateReason === 'string' && /^[a-z_]{1,64}$/.test(candidateReason) ? candidateReason : 'operation_rejected';
  error.details = {operation_index:index, group_key:bounded(op?.action_group_key),
    node_key:bounded(op?.target_node_key || op?.anchor_node_key), path:bounded(op?.path), reason_code:reason};
  // Preserve only known diagnostics, never arbitrary native error payloads or parameter values.
  const contract = operationContracts.operations[op?.operation];
  error.details.phase = 'canvas_operation';
  if (contract) error.details.expected_shape = {operation:op.operation, required_fields:contract.required, optional_fields:contract.optional, strict_fields:contract.strict_fields};
  error.details.refresh_scope = ['old_value_mismatch','element_type_mismatch','target_not_found','ambiguous_target','native_parameter_drift'].includes(reason) ? 'target_context' : 'submitted_operations';
  if (Array.isArray(prior.diff_paths)) error.details.diff_paths = prior.diff_paths.filter(value=>typeof value === 'string').slice(0, 32).map(bounded);
  if (typeof prior.diff_paths_truncated === 'boolean') error.details.diff_paths_truncated = prior.diff_paths_truncated;
  for (const field of ['mapping_status','identifier']) if (typeof prior[field] === 'string') error.details[field] = bounded(prior[field]);
  for (const field of ['line','column']) if (Number.isSafeInteger(prior[field]) && prior[field] >= 0) error.details[field] = prior[field];
  return error;
}
const UNSAFE_KEYS = new Set(operationContracts.unsafe_keys);
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
  if (!object(value)) reject('invalid_parameter_value', 'Parameter value must be JSON data');
  for (const [key, item] of Object.entries(value)) {
    if (UNSAFE_KEYS.has(key)) reject('invalid_parameter_value', 'Unsafe parameter key: ' + key);
    parameterValue(item);
  }
}
function parameterPath(path) {
  text(path, 'path');
  if (!path.startsWith('/') || /~(?![01])/.test(path)) reject('invalid_parameter_path', 'path must be a valid JSON pointer');
  const parts = path.slice(1).split('/').map(part => part.replace(/~1/g, '/').replace(/~0/g, '~'));
  if (parts.length < 2 || !['inputParams', 'outputParams'].includes(parts[0])) reject('invalid_parameter_path', 'path must select an existing inputParams/outputParams parameter');
  if (parts.some(part => UNSAFE_KEYS.has(part))) reject('invalid_parameter_path', 'Unsafe parameter path');
  return parts;
}
function sameRecordKeys(before, after) {
  if (Array.isArray(before)) {
    if (!Array.isArray(after)) reject('invalid_parameter_value', 'An array parameter must remain an array');
    return; // A complete native array parameter may add/remove rows under its full old-value precondition.
  }
  if (!object(before)) {
    if (object(after) || Array.isArray(after)) reject('invalid_parameter_value', 'A scalar parameter cannot become a container');
    return;
  }
  if (!object(after) || Object.keys(before).length !== Object.keys(after).length || Object.keys(before).some(key => !Object.hasOwn(after, key))) {
    reject('invalid_parameter_value', 'Parameter record replacement must preserve existing keys');
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
  const known = operationContracts.atomic_parameters;
  for (const [section, name] of known[element] || []) {
    const record = params[section]?.[name];
    if (parts[0] === section && parts[1] === name && parts.length !== 2) {
      reject('paraminput_record_required', 'Submit the complete ParamInput record so value/code stay synchronized');
    }
    if (object(record) && record.paramTypes === 'custom' && (typeof record.value !== 'string' || record.value !== record.code)) {
      reject('paraminput_unsynchronized', 'Custom ParamInput value and code must be synchronized');
    }
  }
}
function parameterDiffPaths(before, after) {
  const paths = []; let total = 0;
  const pointer = value => value.replace(/~/g, '~0').replace(/\//g, '~1');
  function walk(left, right, at) {
    if (sameJson(left, right)) return;
    if (object(left) && object(right)) {
      for (const key of new Set([...Object.keys(left), ...Object.keys(right)])) walk(left[key], right[key], at + '/' + pointer(key));
    } else { total++; if (paths.length < 32) paths.push(at.slice(0, 256)); }
  }
  walk(before, after, '');
  return {diff_paths:paths, diff_paths_truncated:total > paths.length};
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
  const nodeAliases = new Map();
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
    let op = operations[index];
    try {
    if (!op || typeof op !== 'object' || Array.isArray(op)) throw new Error('Every canvas operation must be an object');
    for (const f of FORBIDDEN) if (Object.hasOwn(op, f)) throw new Error('Operation contains forbidden field ' + f);
    op = {...op};
    const nodeAlias = op.node_alias;
    if (Object.hasOwn(op, 'node_alias')) {
      if (!['append_node', 'insert_before', 'insert_after', 'insert_in_block'].includes(op.operation)
          || typeof nodeAlias !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(nodeAlias) || nodeAliases.has(nodeAlias)) {
        reject('invalid_node_alias', 'Invalid, duplicate or unsupported node_alias');
      }
      delete op.node_alias;
    }
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
    for (const field of ['target_node_key', 'anchor_node_key']) {
      if (typeof op[field] !== 'string' || !op[field].startsWith('@')) continue;
      const reference = nodeAliases.get(op[field].slice(1));
      if (!reference) reject('unknown_node_alias', 'Unknown or forward node alias: ' + op[field]);
      if (reference.groupKey !== group.key) reject('node_alias_group_mismatch', 'Node alias belongs to a different action group');
      if (group.data.filter(node => node.key === reference.nodeKey).length !== 1) {
        reject('stale_node_alias', 'Node alias no longer identifies one current node');
      }
      op[field] = reference.nodeKey;
    }
    const diffStart = diff.length;
    if (op.operation === 'set_parameter' || op.operation === 'set_array_item') {
      fields(op, ['operation', 'action_group_key', 'target_node_key', 'expected_element_key', 'path', 'expected_old_value', 'value', ...(op.operation === 'set_array_item' ? ['item_key'] : [])]);
      text(op.target_node_key, 'target_node_key');
      text(op.expected_element_key, 'expected_element_key');
      if (!Object.hasOwn(op, 'expected_old_value') || !Object.hasOwn(op, 'value')) reject('invalid_operation', op.operation + ' requires expected_old_value and value');
      if (design.actionData.filter(item => item.key === group.key).length !== 1 || group.data.filter(item => item.key === op.target_node_key).length !== 1) {
        reject(group.data.some(item => item.key === op.target_node_key) ? 'ambiguous_target' : 'target_not_found', op.operation + ' requires an unambiguous group and target node');
      }
      const at = api.nodeIndex(group, op.target_node_key), current = group.data[at];
      if (current.elementKey !== op.expected_element_key) reject('element_type_mismatch', 'Target element type changed; inspect the current node');
      const parts = parameterPath(op.path), params = checkedParameters(current.paramsValue);
      let parent = params;
      for (const part of parts.slice(0, -1)) {
        if (Array.isArray(parent)) reject('array_index_forbidden', 'Array indexes are unsupported; use a unique native row key or submit the complete array parameter');
        if (!object(parent) || !Object.hasOwn(parent, part)) reject('parameter_path_missing', 'Parameter path does not exist');
        parent = parent[part];
      }
      if (Array.isArray(parent)) reject('array_index_forbidden', 'Array indexes are unsupported; use a unique native row key or submit the complete array parameter');
      const leaf = parts.at(-1);
      if (!object(parent) || !Object.hasOwn(parent, leaf)) reject('parameter_path_missing', 'Parameter path does not exist');
      let container = parent, selected = leaf;
      if (op.operation === 'set_array_item') {
        if (!Array.isArray(parent[leaf])) reject('array_parameter_required', 'set_array_item path must select an existing array parameter');
        if (!object(op.item_key)) reject('invalid_item_key', 'item_key must contain a native row field and value');
        fields(op.item_key, ['field', 'value']);
        const {field, value} = op.item_key;
        if (!['id', 'key'].includes(field) || !(typeof value === 'string' && value.trim() || Number.isSafeInteger(value))) {
          reject('invalid_item_key', 'item_key must select a nonblank string or safe integer by native id/key');
        }
        const matches = parent[leaf].map((row, i) => object(row) && Object.hasOwn(row, field) && sameJson(row[field], value) ? i : -1).filter(i => i >= 0);
        if (matches.length !== 1) reject(matches.length ? 'ambiguous_item_key' : 'item_key_not_found', 'Native row key must identify exactly one existing item');
        container = parent[leaf]; selected = matches[0];
        if (!object(op.value) || ['id', 'key'].some(name => Object.hasOwn(container[selected], name) && !sameJson(container[selected][name], op.value[name]))) {
          reject('item_identity_changed', 'Replacement must preserve the native row id/key');
        }
      }
      parameterValue(op.expected_old_value); parameterValue(op.value);
      if (!sameJson(container[selected], op.expected_old_value)) reject('old_value_mismatch', 'Parameter old value changed; inspect the current node');
      sameRecordKeys(container[selected], op.value);
      const unchanged = sameJson(container[selected], op.value);
      container[selected] = JSON.parse(JSON.stringify(op.value));
      checkEditableInput(current.elementKey, parts, params);
      if (unchanged) {
        diff.push({operation_index:index, operation:op.operation, action_group_key:group.key, target_node_key:current.key,
          element_key:current.elementKey, parameter_path:op.path, no_change:true,
          affected_node_keys:[], generated_nodes:[], before_node_count:group.data.length, after_node_count:group.data.length});
        continue;
      }
      // Validate native regeneration before replacing anything on the caller's canvas.
      const candidateGroup = {...group, data: group.data.slice()};
      const candidate = {...design, actionData: design.actionData.map(item => item === group ? candidateGroup : item)};
      const nativeDiff = native(candidate, [{operation:'replace_params', action_group_key:group.key,
        target_node_key:current.key, input_params:params.inputParams, output_params:params.outputParams}]);
      const generated = candidateGroup.data[at];
      if (candidateGroup.data.length !== group.data.length || generated.key !== current.key || generated.elementKey !== current.elementKey ||
          !sameJson(generated.depth, current.depth) || !sameJson(checkedParameters(generated.paramsValue), params)) {
        reject('native_parameter_drift', 'Native regeneration changed parameters outside the requested value; inspect the native contract and use an explicit complete replacement',
          parameterDiffPaths({key:current.key, elementKey:current.elementKey, depth:current.depth, paramsValue:params},
            {key:generated.key, elementKey:generated.elementKey, depth:generated.depth, paramsValue:generated.paramsValue}));
      }
      group.data[at] = generated;
      diff.push(...nativeDiff.map(item => ({...item, operation_index:index, operation:op.operation,
        element_key:current.elementKey, parameter_path:op.path, ...(op.operation === 'set_array_item' ? {item_key:op.item_key} : {})})));
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
    if (['append_node', 'insert_in_block', 'bind_component_event'].includes(op.operation)) {
      const beforeCount = group.data.length;
      let generated, replaced = null, blockEnd = null;
      if (op.operation === 'append_node' || op.operation === 'insert_in_block') {
        fields(op, ['operation', 'action_group_key', 'element_key', 'input_params', 'output_params',
          ...(op.operation === 'insert_in_block' ? ['target_node_key'] : [])]);
        if (op.operation === 'insert_in_block') {
          const target = text(op.target_node_key, 'target_node_key');
          if (group.data.filter(node => node.key === target).length !== 1) reject('target_not_found', 'Block target must identify one current node');
          const start = api.nodeIndex(group, target), element = api.elementFor(group.data[start].elementKey);
          if (!element.levelMarker || element.endMarker) reject('block_start_required', 'insert_in_block requires a native block opening node');
          blockEnd = api.blockRange(group, start)[1];
          // Append to this opening node's branch, before a sibling Else/ElseIf
          // when present, rather than silently placing the child in that branch.
          let nesting = 1;
          for (let at = start + 1; at <= blockEnd; at++) {
            const marker = api.elementFor(group.data[at].elementKey);
            if (marker.endMarker) {
              if (nesting === 1) { blockEnd = at; break; }
              nesting--;
            }
            if (marker.levelMarker) nesting++;
          }
        }
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
        const at = blockEnd !== null ? blockEnd : op.anchor_node_key ? api.nodeIndex(group, op.anchor_node_key) + (op.position === 'after' ? 1 : 0) : group.data.length;
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
    if (nodeAlias) {
      const key = diff[diffStart]?.generated_nodes?.[0]?.key;
      if (!key || group.data.filter(node => node.key === key).length !== 1) {
        reject('invalid_node_alias_target', 'node_alias requires a native generated node');
      }
      nodeAliases.set(nodeAlias, {groupKey: group.key, nodeKey: key});
      diff[diffStart].node_alias = nodeAlias;
    }
    } catch (error) { throw operationFailure(error, op, index); }
  }
  return diff;
}
module.exports = {applyOperations, componentContract, eventContract, operations: [...NATIVE, ...EXTENDED]};
