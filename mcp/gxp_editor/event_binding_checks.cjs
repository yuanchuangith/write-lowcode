'use strict';
const path = require('node:path');
const vm = require('node:vm');
const {parametersMatch} = require('./event_contracts.cjs');
const {createRequire} = require('node:module');
const GLOBALS = new Set(['console','JSON','Math','Date','Array','Object','String','Number','Boolean','RegExp','Map','Set','WeakMap','WeakSet','Promise','Error','TypeError','RangeError','Symbol','BigInt','Intl','URL','URLSearchParams','undefined','NaN','Infinity','parseInt','parseFloat','isNaN','isFinite','encodeURIComponent','decodeURIComponent','setTimeout','clearTimeout','setInterval','clearInterval','window','document','navigator','localStorage','sessionStorage']);
const RESERVED_PLATFORM_IDENTIFIERS = new Set(['inbiz', 'Inbiz']);
const MAX_DIAGNOSTIC_ISSUES = 20;
function diagnostic(reason, group, node, extra = {}) {
  const result = {phase:'javascript_lexical', reason_code:reason, group_key:typeof group?.key === 'string' ? group.key.slice(0,256) : null,
    mapping_status:'group_only', ...extra};
  if (node?.loc?.start) {
    result.line = node.loc.start.line;
    result.column = Math.max(0,node.loc.start.column - (result.line === 1 ? 1 : 0));
  }
  return result;
}
function rejectWithDetails(message, details) { const error = new Error(message); error.details = details; throw error; }
function rejectReservedName(name, group, node) {
  if (!RESERVED_PLATFORM_IDENTIFIERS.has(name)) return;
  const error = new Error('Reserved platform identifier ' + name + ' cannot be declared or overwritten in ' + group.title);
  error.details = diagnostic('reserved_platform_identifier', group, node, {identifier:name});
  throw error;
}
function inspectWriteTarget(node, group) {
  if (!node) return;
  if (node.type === 'Identifier') rejectReservedName(node.name, group, node);
  else if (node.type === 'MemberExpression') {
    const owner = node.object;
    const name = !node.computed && node.property.type === 'Identifier' ? node.property.name
      : node.computed && node.property.type === 'StringLiteral' ? node.property.value : null;
    if (owner.type === 'Identifier' && ['window', 'globalThis'].includes(owner.name)) rejectReservedName(name, group, node);
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
  const lexicalIssues = [], seenIssues = new Set(); let issueCount = 0;
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
    if (!fn.isFunctionExpression()) rejectWithDetails('Event callback is not the declared native function: ' + group.key, diagnostic('native_function_expected', group, fn.node));
    const names = fn.node.params.map(param=>param.type === 'AssignmentPattern' ? param.left.name : param.name);
    const expected = (group.inputParams || []).map(param=>param.title);
    if (JSON.stringify(names) !== JSON.stringify(expected)) rejectWithDetails('Generated callback signature does not match canvas parameters: ' + group.title, diagnostic('callback_signature_mismatch', group, fn.node));
    fn.traverse({
      BindingIdentifier(ref) { rejectReservedName(ref.node.name, group, ref.node); },
      AssignmentExpression(ref) { inspectWriteTarget(ref.node.left, group); },
      UpdateExpression(ref) { inspectWriteTarget(ref.node.argument, group); },
      UnaryExpression(ref) { if (ref.node.operator === 'delete') inspectWriteTarget(ref.node.argument, group); },
      'ForInStatement|ForOfStatement'(ref) { inspectWriteTarget(ref.node.left, group); },
      ReferencedIdentifier(ref) {
      const name = ref.node.name;
      if (name === 'arguments') {
        const owner = ref.findParent(parent=>parent.isFunction() && !parent.isArrowFunctionExpression());
        if (owner === fn && eventTargets.has(group.key)) rejectWithDetails('Hidden event arguments are forbidden in ' + group.title + '; declare the callback input and use its named parameter', diagnostic('hidden_event_arguments', group, ref.node));
      }
      if (!ref.scope.hasBinding(name, true) && !allowed.has(name) && name !== 'arguments') {
        if (preserved.has(group.key)) {
          if (!diagnostics.some(d => d.group_key === group.key && d.identifier === name)) diagnostics.push({
            group_key: group.key, identifier: name, reason_code: 'preserved_undeclared_identifier',
            status: 'unverified', blocks_changed_candidate: false});
        } else {
          const identity = JSON.stringify([group.key,name]);
          if (!seenIssues.has(identity)) {
            seenIssues.add(identity); issueCount++;
            if (lexicalIssues.length < MAX_DIAGNOSTIC_ISSUES) lexicalIssues.push(diagnostic('undeclared_identifier', group, ref.node,
              {identifier:name.slice(0,256), callback:eventTargets.has(group.key)}));
          }
        }
      }
    }});
    pending.delete(group.key);
  }});
  if (pending.size) rejectWithDetails('Generated event callback function is missing: ' + [...pending.keys()].join(', '),
    diagnostic('native_function_missing', pending.values().next().value));
  if (lexicalIssues.length) {
    const first = lexicalIssues[0], group = groups.find(group=>group.key === first.group_key);
    rejectWithDetails('Undeclared ' + (first.callback ? 'callback' : 'action') + ' identifier ' + first.identifier + ' in ' + group.title,
      {...first, issues:lexicalIssues, issue_count:issueCount, issues_truncated:issueCount > lexicalIssues.length, refresh_scope:'group_context'});
  }
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
  if (!Array.isArray(scope) || new Set(scope).size !== scope.length) rejectWithDetails('Invalid event binding validation scope',
    {phase:'event_binding',reason_code:'invalid_binding_scope',mapping_status:'unresolved'});
  const checks = [], groups = [];
  for (const key of scope) {
    const owners = design.actionData.flatMap(group=>group.data.filter(n=>n.key===key).map(node=>({group,node})));
    if (owners.length !== 1 || owners[0].node.elementKey !== 'ComponentEvent') rejectWithDetails('Event binding validation target is missing or ambiguous: ' + key,
      {phase:'event_binding',reason_code:'binding_target_missing',node_key:typeof key === 'string' ? key.slice(0,256) : null,mapping_status:'unresolved'});
    const {group,node} = owners[0];
    const bindingDetails = (reason, path) => ({phase:'event_binding',reason_code:reason,group_key:group.key,node_key:node.key,
      mapping_status:'node',...(path ? {path} : {}),refresh_scope:'target_context'});
    const input = node.paramsValue?.inputParams;
    const name = input?.name;
    if (name?.paramTypes !== 'componentsVariable' || !name.dataType || typeof name.value !== 'string' || !name.value.startsWith('componentsVariable-')) rejectWithDetails('Event binding needs an explicit component ID and type from page evidence',bindingDetails('binding_component_identity_required','/inputParams/name'));
    let contract, target;
    try { contract = getContract(api.catalog(), name.dataType, input.eventName, api); }
    catch (error) { error.details = bindingDetails('event_contract_unavailable','/inputParams/eventName'); throw error; }
    try { target = api.actionByKey(design, input.action?.value); }
    catch (error) { error.details = bindingDetails('event_callback_missing','/inputParams/action'); throw error; }
    if (group.actionType !== 'js' || target.actionType !== 'js' || target.key===group.key) rejectWithDetails('Event callback must be a different JS group',bindingDetails('event_callback_group_invalid','/inputParams/action'));
    for (const [section, expected] of [['inputParams',contract.input_params],['outputParams',contract.output_params]]) {
      const actual = target[section] || [];
      if (!parametersMatch(actual, expected, contract, section)) rejectWithDetails('Event callback ' + section + ' does not match the runtime contract: ' + target.title,
        {...bindingDetails('callback_parameter_mismatch','/inputParams/action'),callback_group_key:target.key});
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
