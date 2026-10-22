'use strict';
const path = require('node:path');
const vm = require('node:vm');
const {parametersMatch} = require('./event_contracts.cjs');
const {createRequire} = require('node:module');
const GLOBALS = new Set(['console','JSON','Math','Date','Array','Object','String','Number','Boolean','RegExp','Map','Set','WeakMap','WeakSet','Promise','Error','TypeError','RangeError','Symbol','BigInt','Intl','URL','URLSearchParams','undefined','NaN','Infinity','parseInt','parseFloat','isNaN','isFinite','encodeURIComponent','decodeURIComponent','setTimeout','clearTimeout','setInterval','clearInterval','window','document','navigator','localStorage','sessionStorage']);
const RESERVED_PLATFORM_IDENTIFIERS = new Set(['inbiz', 'Inbiz']);
function rejectReservedName(name, group) {
  if (!RESERVED_PLATFORM_IDENTIFIERS.has(name)) return;
  const error = new Error('Reserved platform identifier ' + name + ' cannot be declared or overwritten in ' + group.title);
  error.details = {reason_code:'reserved_platform_identifier', group_key:group.key};
  throw error;
}
function inspectWriteTarget(node, group) {
  if (!node) return;
  if (node.type === 'Identifier') rejectReservedName(node.name, group);
  else if (node.type === 'MemberExpression') {
    const owner = node.object;
    const name = !node.computed && node.property.type === 'Identifier' ? node.property.name
      : node.computed && node.property.type === 'StringLiteral' ? node.property.value : null;
    if (owner.type === 'Identifier' && ['window', 'globalThis'].includes(owner.name)) rejectReservedName(name, group);
  } else if (node.type === 'ObjectPattern') {
    for (const property of node.properties) inspectWriteTarget(property.type === 'RestElement' ? property.argument : property.value, group);
  } else if (node.type === 'ArrayPattern') {
    for (const element of node.elements) inspectWriteTarget(element, group);
  } else if (node.type === 'AssignmentPattern') inspectWriteTarget(node.left, group);
  else if (node.type === 'RestElement') inspectWriteTarget(node.argument, group);
}
function assertJavascriptSyntax(code) {
  if (code.trim()) new vm.Script('(' + code + ')', {filename:'generated-action.js'});
}
function inspectFunctions(code, groups, frontendRoot, eventTargets, preserved = new Set(), diagnostics = []) {
  if (!groups.length) return;
  for (const group of groups) for (const parameter of [...(group.inputParams || []), ...(group.outputParams || [])]) {
    rejectReservedName(parameter.title, group);
  }
  assertJavascriptSyntax(code);
  const load = createRequire(path.join(frontendRoot, 'package.json'));
  const parser = load('@babel/parser');
  const traverse = load('@babel/traverse').default;
  const standard = load('globals');
  const allowed = new Set([...GLOBALS, ...Object.keys(standard.builtin || {}), ...Object.keys(standard.browser || {}), ...Object.keys(standard.es2021 || {})]);
  const ast = parser.parse('(' + code + ')', {sourceType:'script'});
  const factory = ast.program.body[0]?.expression;
  const pending = new Map(groups.map(group=>[group.key, group]));
  traverse(ast, {AssignmentExpression(p) {
    // Native group declarations belong to the generated factory. Assignments
    // inside an action (including an IIFE wrapping another group for in-flight
    // request reuse) are business code, not that group's declaration. They are
    // still traversed by the owning group's lexical identifier checks below.
    if (p.getFunctionParent()?.node !== factory) return;
    const left = p.node.left;
    if (left.type !== 'MemberExpression' || left.computed || left.object.type !== 'Identifier' || left.object.name !== 'self' || left.property.type !== 'Identifier') return;
    const group = pending.get(left.property.name);
    if (!group) return;
    const fn = p.get('right');
    if (!fn.isFunctionExpression()) throw new Error('Event callback is not the declared native function: ' + group.key);
    const names = fn.node.params.map(param=>param.type === 'AssignmentPattern' ? param.left.name : param.name);
    const expected = (group.inputParams || []).map(param=>param.title);
    if (JSON.stringify(names) !== JSON.stringify(expected)) throw new Error('Generated callback signature does not match canvas parameters: ' + group.title);
    fn.traverse({
      BindingIdentifier(ref) { rejectReservedName(ref.node.name, group); },
      AssignmentExpression(ref) { inspectWriteTarget(ref.node.left, group); },
      UpdateExpression(ref) { inspectWriteTarget(ref.node.argument, group); },
      UnaryExpression(ref) { if (ref.node.operator === 'delete') inspectWriteTarget(ref.node.argument, group); },
      'ForInStatement|ForOfStatement'(ref) { inspectWriteTarget(ref.node.left, group); },
      ReferencedIdentifier(ref) {
      const name = ref.node.name;
      if (name === 'arguments') {
        const owner = ref.findParent(parent=>parent.isFunction() && !parent.isArrowFunctionExpression());
        if (owner === fn && eventTargets.has(group.key)) throw new Error('Hidden event arguments are forbidden in ' + group.title + '; declare the callback input and use its named parameter');
      }
      if (!ref.scope.hasBinding(name, true) && !allowed.has(name) && name !== 'arguments') {
        if (preserved.has(group.key)) {
          if (!diagnostics.some(d => d.group_key === group.key && d.identifier === name)) diagnostics.push({
            group_key: group.key, identifier: name, reason_code: 'preserved_undeclared_identifier',
            status: 'unverified', blocks_changed_candidate: false});
        } else throw new Error('Undeclared ' + (eventTargets.has(group.key) ? 'callback' : 'action') + ' identifier ' + name + ' in ' + group.title);
      }
    }});
    pending.delete(group.key);
  }});
  if (pending.size) throw new Error('Generated event callback function is missing: ' + [...pending.keys()].join(', '));
}
function collectScope(before, design) {
  const old = new Map(before.actionData.map(group=>[group.key,group]));
  const comparable = group => JSON.stringify({input:group.inputParams||[], output:group.outputParams||[],
    type:group.actionType, nodes:group.data.map(node=>({key:node.key,element:node.elementKey,params:node.paramsValue,depth:node.depth||[]}))});
  const changed = new Set(design.actionData.filter(group=>!old.has(group.key) || comparable(group)!==comparable(old.get(group.key))).map(group=>group.key));
  const nodes = [];
  for (const group of design.actionData) for (const node of group.data) {
    const target = node.paramsValue?.inputParams?.action?.value;
    if (node.elementKey === 'ComponentEvent' && (changed.has(group.key) || changed.has(target))) nodes.push(node.key);
  }
  return nodes;
}
function validateBindings(design, scope, api, getContract, code, generation) {
  if (!Array.isArray(scope) || new Set(scope).size !== scope.length) throw new Error('Invalid event binding validation scope');
  const checks = [], groups = [];
  for (const key of scope) {
    const owners = design.actionData.flatMap(group=>group.data.filter(n=>n.key===key).map(node=>({group,node})));
    if (owners.length !== 1 || owners[0].node.elementKey !== 'ComponentEvent') throw new Error('Event binding validation target is missing or ambiguous: ' + key);
    const {group,node} = owners[0];
    const input = node.paramsValue?.inputParams;
    const name = input?.name;
    if (name?.paramTypes !== 'componentsVariable' || !name.dataType || typeof name.value !== 'string' || !name.value.startsWith('componentsVariable-')) throw new Error('Event binding needs an explicit component ID and type from page evidence');
    const contract = getContract(api.catalog(), name.dataType, input.eventName, api);
    const target = api.actionByKey(design, input.action?.value);
    if (group.actionType !== 'js' || target.actionType !== 'js' || target.key===group.key) throw new Error('Event callback must be a different JS group');
    for (const [section, expected] of [['inputParams',contract.input_params],['outputParams',contract.output_params]]) {
      const actual = target[section] || [];
      if (!parametersMatch(actual, expected, contract, section)) throw new Error('Event callback ' + section + ' does not match the runtime contract: ' + target.title);
    }
    groups.push(target);
    checks.push({binding_node_key:key, source_group_key:group.key, target_group_key:target.key,
      component_id:name.value.slice('componentsVariable-'.length), component_type:name.dataType, event_name:input.eventName,
      input_params:target.inputParams || [], output_params:target.outputParams || [], signature_status:contract.signature_status,
      return_mode:contract.return_mode || 'native', return_verification:contract.return_verification || 'catalog_only', evidence:contract.evidence || [],
      parameters_declared:true, hidden_arguments_allowed:false, runtime_verified:false});
  }
  const jsGroups = design.actionData.filter(group=>group.actionType === 'js');
  if (jsGroups.length) for (const variable of design.globalVariables || []) {
    rejectReservedName(variable.name, {title:'JS global variables', key:null});
  }
  const diagnostics = [];
  inspectFunctions(code, jsGroups, api.frontendRoot, new Set(groups.map(group=>group.key)), new Set(generation?.preserved_group_keys || []), diagnostics);
  return {binding_node_keys:scope, checks, javascript_syntax_checked:true, javascript_lexical_scope:'all_js_groups', javascript_group_keys:jsGroups.map(group=>group.key), business_scenarios_verified:false, browser_verified:false, runtime_verified:false,
    ...(generation ? {generation, baseline_diagnostics: diagnostics} : {})};
}
module.exports = {assertJavascriptSyntax, collectScope, validateBindings};
