'use strict';
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const {parametersMatch} = require('./event_contracts.cjs');
const {createRequire} = require('node:module');
const {hash} = require('./incremental_codegen.cjs');
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
function rejectReservedName(name, group, node, report) {
  if (!RESERVED_PLATFORM_IDENTIFIERS.has(name)) return;
  if (report) return report(name, node);
  const error = new Error('Reserved platform identifier ' + name + ' cannot be declared or overwritten in ' + group.title);
  error.details = diagnostic('reserved_platform_identifier', group, node, {identifier:name});
  throw error;
}
function inspectWriteTarget(node, group, report) {
  if (!node) return;
  if (node.type === 'Identifier') rejectReservedName(node.name, group, node, report);
  else if (node.type === 'MemberExpression') {
    const owner = node.object;
    const name = !node.computed && node.property.type === 'Identifier' ? node.property.name
      : node.computed && node.property.type === 'StringLiteral' ? node.property.value : null;
    if (owner.type === 'Identifier' && ['window', 'globalThis'].includes(owner.name)) rejectReservedName(name, group, node, report);
  } else if (node.type === 'ObjectPattern') {
    for (const property of node.properties) inspectWriteTarget(property.type === 'RestElement' ? property.argument : property.value, group, report);
  } else if (node.type === 'ArrayPattern') {
    for (const element of node.elements) inspectWriteTarget(element, group, report);
  } else if (node.type === 'AssignmentPattern') inspectWriteTarget(node.left, group, report);
  else if (node.type === 'RestElement') inspectWriteTarget(node.argument, group, report);
}
function assertJavascriptSyntax(code) {
  if (code.trim()) new vm.Script('(' + code + ')', {filename:'generated-action.js'});
}
// Use only literal control flow, never runtime values or a lack of callers.
// In particular, every action can be called externally by the platform.
function literalValue(node) {
  if (['BooleanLiteral', 'NumericLiteral', 'StringLiteral'].includes(node?.type)) return {known:true, value:node.value};
  if (node?.type === 'NullLiteral') return {known:true, value:null};
  if (node?.type === 'UnaryExpression' && node.operator === '!') {
    const argument = literalValue(node.argument);
    if (argument.known) return {known:true, value:!argument.value};
  }
  return {known:false};
}
function unreachableProof(ref) {
  const proof = (reason_code, node) => ({reason_code, line:node.loc.start.line,
    column:Math.max(0, node.loc.start.column - (node.loc.start.line === 1 ? 1 : 0))});
  for (let child = ref; child.parentPath; child = child.parentPath) {
    const parent = child.parentPath;
    // Hoisted function declarations after return can still be called. Do not
    // transfer a containing statement's reachability across function boundaries.
    if (parent.isFunction()) return null;
    if (parent.isBlockStatement() || parent.isSwitchCase()) {
      const statements = parent.isBlockStatement() ? parent.node.body : parent.node.consequent;
      if (child.inList) for (const previous of statements.slice(0, child.key)) {
        if (previous.type === 'ReturnStatement') return proof('after_return', previous);
        if (previous.type === 'ThrowStatement') return proof('after_throw', previous);
      }
    }
    if (parent.isIfStatement() || parent.isConditionalExpression()) {
      const test = literalValue(parent.node.test);
      if (test.known && (child.key === 'consequent' && !test.value || child.key === 'alternate' && test.value)) {
        return proof(test.value ? 'constant_true_alternate' : 'constant_false_consequent', parent.node.test);
      }
    }
    if ((parent.isWhileStatement() && child.key === 'body')
        || (parent.isForStatement() && ['body', 'update'].includes(child.key))) {
      const test = literalValue(parent.node.test);
      if (test.known && !test.value) return proof('constant_false_loop', parent.node.test);
    }
    if (parent.isLogicalExpression() && child.key === 'right') {
      const left = literalValue(parent.node.left), operator = parent.node.operator;
      if (left.known && (operator === '&&' && !left.value || operator === '||' && left.value
          || operator === '??' && left.value !== null)) return proof('literal_short_circuit', parent.node.left);
    }
  }
  return null;
}
function referenceHash(ref, groupKey) {
  let expression = ref;
  while (expression.parentPath && (
    ['MemberExpression', 'OptionalMemberExpression'].includes(expression.parentPath.type) && expression.key === 'object'
    || ['CallExpression', 'OptionalCallExpression'].includes(expression.parentPath.type) && expression.key === 'callee')) {
    expression = expression.parentPath;
  }
  const metadata = new Set(['start', 'end', 'loc', 'extra', 'leadingComments', 'innerComments', 'trailingComments']);
  return crypto.createHash('sha256').update(JSON.stringify([groupKey, ref.node.name, expression.node],
    (key, value) => metadata.has(key) ? undefined : value)).digest('hex');
}
function baselineReferences(baseline, parser, traverse, allowed) {
  const references = new Map();
  if (!baseline || typeof baseline.js_code !== 'string') return references;
  let ast;
  try { ast = parser.parse('(' + baseline.js_code + ')', {sourceType:'script'}); }
  catch { return references; } // An unreadable baseline cannot prove an issue preexists.
  const factory = ast.program.body[0]?.expression;
  const groups = new Map((baseline.design?.actionData || []).filter(g => g.actionType === 'js').map(g => [g.key, g]));
  const source_sha256 = crypto.createHash('sha256').update(baseline.js_code).digest('hex');
  traverse(ast, {AssignmentExpression(p) {
    if (p.getFunctionParent()?.node !== factory) return;
    const left = p.node.left, fn = p.get('right');
    if (left.type !== 'MemberExpression' || left.computed || left.object.type !== 'Identifier'
        || left.object.name !== 'self' || left.property.type !== 'Identifier' || !fn.isFunctionExpression()) return;
    const group = groups.get(left.property.name);
    if (!group) return;
    fn.traverse({ReferencedIdentifier(ref) {
      const name = ref.node.name;
      if (ref.scope.hasBinding(name, true) || allowed.has(name) || name === 'arguments' || unreachableProof(ref)) return;
      const reference_sha256 = referenceHash(ref, group.key);
      if (!references.has(reference_sha256)) references.set(reference_sha256, []);
      references.get(reference_sha256).push({source_sha256, reference_sha256, line:ref.node.loc.start.line,
        column:Math.max(0, ref.node.loc.start.column - (ref.node.loc.start.line === 1 ? 1 : 0))});
    }});
  }});
  return references;
}
function eventTargetKeys(design) {
  return new Set((design?.actionData || []).flatMap(group => (group.data || [])
    .filter(node => node.elementKey === 'ComponentEvent').map(node => node.paramsValue?.inputParams?.action?.value)));
}
function policyIssues(ast, groups, traverse, eventTargets, design) {
  const found = [], seen = new Set(), factory = ast.program.body[0]?.expression;
  const byKey = new Map(groups.map(group => [group.key, group]));
  const add = (reason, group, ref, identifier, node = ref.node) => {
    const identity = JSON.stringify([reason, group.key, node.start, node.end]);
    if (seen.has(identity)) return;
    seen.add(identity);
    // Bind declarations independently of surrounding business statements. For
    // writes include the complete operation, so a changed RHS is a new finding.
    let expression = ref, expressionNode;
    if (reason === 'hidden_event_arguments') {
      while (expression.parentPath?.isMemberExpression() && expression.key === 'object') expression = expression.parentPath;
    } else if (ref.isBindingIdentifier()) {
      for (let parent = ref.parentPath; parent; parent = parent.parentPath) {
        if (parent.isVariableDeclarator()) { expression = parent; break; }
        if (parent.isFunction()) {
          expressionNode = {type:parent.node.type, id:parent.node.id, params:parent.node.params, async:parent.node.async, generator:parent.node.generator}; break;
        }
        if (parent.isCatchClause()) { expressionNode = {type:parent.node.type, param:parent.node.param}; break; }
        if (parent.isClass()) { expressionNode = {type:parent.node.type, id:parent.node.id, superClass:parent.node.superClass}; break; }
        if (parent.isStatement()) break;
      }
    }
    const metadata = new Set(['start','end','loc','extra','leadingComments','innerComments','trailingComments']);
    const bindingContext = reason === 'hidden_event_arguments' ? (design?.actionData || []).flatMap(owner => (owner.data || [])
      .filter(node => node.elementKey === 'ComponentEvent' && node.paramsValue?.inputParams?.action?.value === group.key)
      .map(node => eventDeclarationSnapshot(design, node.key))) : null;
    const reference_sha256 = crypto.createHash('sha256').update(JSON.stringify([reason, group.key, identifier, expressionNode || expression.node, bindingContext],
      (key, value) => metadata.has(key) ? undefined : value)).digest('hex');
    found.push({reason, group, ref, node, identifier, reference_sha256});
  };
  traverse(ast, {AssignmentExpression(p) {
    const left = p.node.left;
    if (p.getFunctionParent()?.node !== factory || left.type !== 'MemberExpression' || left.computed
        || left.object.type !== 'Identifier' || left.object.name !== 'self' || left.property.type !== 'Identifier') return;
    const group = byKey.get(left.property.name), fn = p.get('right');
    if (!group || !fn.isFunctionExpression()) return;
    fn.traverse({
      BindingIdentifier(ref) {
        // Babel also labels assignment targets as BindingIdentifier; the write
        // visitor records those with their RHS instead.
        for (let parent = ref.parentPath; parent && !parent.isFunction() && !parent.isVariableDeclarator() && !parent.isStatement(); parent = parent.parentPath)
          if (parent.isAssignmentExpression() || parent.isUpdateExpression()) return;
        if (RESERVED_PLATFORM_IDENTIFIERS.has(ref.node.name)) add('reserved_platform_identifier', group, ref, ref.node.name);
      },
      'AssignmentExpression|UpdateExpression|UnaryExpression|ForInStatement|ForOfStatement'(ref) {
        const n = ref.node;
        if (n.type === 'UnaryExpression' && n.operator !== 'delete') return;
        inspectWriteTarget(n.left || n.argument, group,
          (name, node) => add('reserved_platform_identifier', group, ref, name, node));
      },
      ReferencedIdentifier(ref) {
        if (ref.node.name === 'arguments' && eventTargets.has(group.key)
            && ref.findParent(parent => parent.isFunction() && !parent.isArrowFunctionExpression()) === fn)
          add('hidden_event_arguments', group, ref, 'arguments');
      },
    });
  }});
  return found;
}
function inspectFunctions(code, groups, frontendRoot, eventTargets, diagnostics = [], baseline, oldDiagnostics = [], design) {
  if (!groups.length) return;
  assertJavascriptSyntax(code);
  const load = createRequire(path.join(frontendRoot, 'package.json'));
  const parser = load('@babel/parser');
  const traverse = load('@babel/traverse').default;
  const standard = load('globals');
  const allowed = new Set([...GLOBALS, ...Object.keys(standard.builtin || {}), ...Object.keys(standard.browser || {}), ...Object.keys(standard.es2021 || {})]);
  const ast = parser.parse('(' + code + ')', {sourceType:'script'});
  const oldReferences = baselineReferences(baseline, parser, traverse, allowed);
  const oldPolicy = new Map();
  if (typeof baseline?.js_code === 'string') {
    let before;
    try { before = parser.parse('(' + baseline.js_code + ')', {sourceType:'script'}); } catch {}
    if (before) for (const issue of policyIssues(before, (baseline.design?.actionData || []).filter(group => group.actionType === 'js'), traverse, eventTargetKeys(baseline.design), baseline.design)) {
      if (issue.reason === 'hidden_event_arguments' && unreachableProof(issue.ref)) continue;
      if (!oldPolicy.has(issue.reference_sha256)) oldPolicy.set(issue.reference_sha256, []);
      oldPolicy.get(issue.reference_sha256).push({
        ...diagnostic(issue.reason, issue.group, issue.node), reference_sha256:issue.reference_sha256,
        source_sha256:crypto.createHash('sha256').update(baseline.js_code).digest('hex')});
    }
  }
  for (const issue of policyIssues(ast, groups, traverse, eventTargets, design)) {
    const finding = diagnostic(issue.reason, issue.group, issue.node, {identifier:issue.identifier});
    const proof = issue.reason === 'hidden_event_arguments' && unreachableProof(issue.ref);
    if (proof) diagnostics.push({...finding, status:'unverified', blocks_changed_candidate:false, reachability:proof});
    else {
      const old = oldPolicy.get(issue.reference_sha256)?.shift();
      if (old) oldDiagnostics.push({...finding, reason_code:'preexisting_' + issue.reason,
        status:'unverified', blocks_changed_candidate:false, baseline:old});
      else rejectWithDetails(issue.reason === 'reserved_platform_identifier'
        ? 'Reserved platform identifier ' + issue.identifier + ' cannot be declared or overwritten in ' + issue.group.title
        : 'Hidden event arguments are forbidden in ' + issue.group.title + '; declare the callback input and use its named parameter', finding);
    }
  }
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
      ReferencedIdentifier(ref) {
      const name = ref.node.name;
      if (!ref.scope.hasBinding(name, true) && !allowed.has(name) && name !== 'arguments') {
        const reachability = unreachableProof(ref);
        if (reachability) {
          diagnostics.push(diagnostic('undeclared_identifier', group, ref.node, {identifier:name,
            status:'unverified', blocks_changed_candidate:false, reachability}));
        } else {
          const old = oldReferences.get(referenceHash(ref, group.key))?.shift();
          if (old) {
            oldDiagnostics.push(diagnostic('preexisting_undeclared_identifier', group, ref.node, {identifier:name,
              status:'unverified', blocks_changed_candidate:false, baseline:old}));
            return;
          }
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
function eventDeclarationSnapshot(design, key) {
  const owners = (design?.actionData || []).flatMap(group => (group.data || [])
    .filter(node => node.key === key && node.elementKey === 'ComponentEvent').map(node => ({group,node})));
  if (owners.length !== 1) return null;
  const {group,node} = owners[0], input = node.paramsValue?.inputParams;
  const targets = design.actionData.filter(target => target.key === input?.action?.value);
  if (targets.length > 1) return null;
  const target = targets[0];
  return {binding_node_key:key, source_group_key:group.key, target_group_key:target?.key || null,
    source_action_type:group.actionType, target_action_type:target?.actionType || null,
    binding_params:node.paramsValue, input_params:target?.inputParams || [], output_params:target?.outputParams || []};
}
function canvasPolicyEntries(design) {
  const entries = [];
  const add = (reason, snapshot, identifier) => entries.push({reason_code:reason, snapshot, ...(identifier ? {identifier} : {})});
  for (const group of design?.actionData || []) {
    if (group.actionType === 'js') for (const [index, param] of (group.outputParams || []).entries()) {
      if (RESERVED_PLATFORM_IDENTIFIERS.has(param.title)) add('reserved_platform_identifier',
        {group_key:group.key, node_key:null, path:'/outputParams/' + index, value:param}, param.title);
    }
    for (const node of group.data || []) {
      const fields = {SetVariable:[['inputParams','variableValue'],['outputParams','variableName']],
        SetVariableValue:[['inputParams','attributeValue']]}[node.elementKey] || [];
      for (const [section, field] of fields) {
        const value = node.paramsValue?.[section]?.[field];
        if (value?.paramTypes === 'custom' && (typeof value.value !== 'string' || value.value !== value.code))
          add('paraminput_unsynchronized', {group_key:group.key, node_key:node.key,
            element_key:node.elementKey, path:'/' + section + '/' + field, value});
      }
    }
  }
  if ((design?.actionData || []).some(group => group.actionType === 'js'))
    for (const [index, variable] of (design.globalVariables || []).entries())
      if (RESERVED_PLATFORM_IDENTIFIERS.has(variable.name)) add('reserved_platform_identifier',
        {group_key:null, node_key:null, path:'/globalVariables/' + index, value:variable}, variable.name);
  return entries;
}
function canvasDiagnostics(design, baseline) {
  const before = new Set(canvasPolicyEntries(baseline).map(entry => hash(entry.snapshot)));
  return canvasPolicyEntries(design).map(entry => {
    const snapshotHash = hash(entry.snapshot);
    if (!before.has(snapshotHash)) rejectWithDetails(entry.reason_code === 'paraminput_unsynchronized'
      ? 'Custom ParamInput value and code must be synchronized'
      : 'Reserved platform identifier ' + entry.identifier + ' cannot be declared',
      {phase:'canvas_contract', ...entry.snapshot, reason_code:entry.reason_code});
    return {...entry.snapshot, reason_code:'preexisting_' + entry.reason_code, ...(entry.identifier ? {identifier:entry.identifier} : {}),
      status:'unverified', blocks_changed_candidate:false, baseline_snapshot_sha256:snapshotHash, candidate_snapshot_sha256:snapshotHash};
  });
}
function validateBindings(design, scope, api, getContract, code, generation, baseline) {
  if (!Array.isArray(scope) || new Set(scope).size !== scope.length) rejectWithDetails('Invalid event binding validation scope',
    {phase:'event_binding',reason_code:'invalid_binding_scope',mapping_status:'unresolved'});
  const checks = [];
  for (const key of scope) {
    const owners = design.actionData.flatMap(group=>group.data.filter(n=>n.key===key).map(node=>({group,node})));
    if (owners.length !== 1 || owners[0].node.elementKey !== 'ComponentEvent') rejectWithDetails('Event binding validation target is missing or ambiguous: ' + key,
      {phase:'event_binding',reason_code:'binding_target_missing',node_key:typeof key === 'string' ? key.slice(0,256) : null,mapping_status:'unresolved'});
    const {group,node} = owners[0];
    const bindingDetails = (reason, path) => ({phase:'event_binding',reason_code:reason,group_key:group.key,node_key:node.key,
      mapping_status:'node',...(path ? {path} : {}),refresh_scope:'target_context'});
    const oldBindingIssue = (reason, path) => {
      const before = eventDeclarationSnapshot(baseline?.design, key), after = eventDeclarationSnapshot(design, key);
      if (!before || !after || hash(before) !== hash(after)) return false;
      checks.push({binding_node_key:key, source_group_key:group.key, target_group_key:after.target_group_key,
        binding_issue:{...bindingDetails(reason,path), status:'unverified', blocks_changed_candidate:false,
          baseline_signature_sha256:hash(before), candidate_signature_sha256:hash(after)}, runtime_verified:false});
      return true;
    };
    const input = node.paramsValue?.inputParams;
    const name = input?.name;
    if (name?.paramTypes !== 'componentsVariable' || !name.dataType || typeof name.value !== 'string' || !name.value.startsWith('componentsVariable-')) {
      if (oldBindingIssue('binding_component_identity_required','/inputParams/name')) continue;
      rejectWithDetails('Event binding needs an explicit component ID and type from page evidence',bindingDetails('binding_component_identity_required','/inputParams/name'));
    }
    let contract, target;
    try { contract = getContract(api.catalog(), name.dataType, input.eventName, api); }
    catch (error) {
      if (error.code === 'event_contract_unavailable' && oldBindingIssue('event_contract_unavailable','/inputParams/eventName')) continue;
      error.details = bindingDetails('event_contract_unavailable','/inputParams/eventName'); throw error;
    }
    try { target = api.actionByKey(design, input.action?.value); }
    catch (error) {
      if (!design.actionData.some(group => group.key === input.action?.value)
          && oldBindingIssue('event_callback_missing','/inputParams/action')) continue;
      error.details = bindingDetails('event_callback_missing','/inputParams/action'); throw error;
    }
    if (!target) {
      if (oldBindingIssue('event_callback_missing','/inputParams/action')) continue;
      rejectWithDetails('Event callback is missing',bindingDetails('event_callback_missing','/inputParams/action'));
    }
    if (group.actionType !== 'js' || target.actionType !== 'js' || target.key===group.key) {
      if (oldBindingIssue('event_callback_group_invalid','/inputParams/action')) continue;
      rejectWithDetails('Event callback must be a different JS group',bindingDetails('event_callback_group_invalid','/inputParams/action'));
    }
    const parameterIssues = [];
    for (const [section, expected] of [['inputParams',contract.input_params],['outputParams',contract.output_params]]) {
      const actual = target[section] || [];
      if (!parametersMatch(actual, expected, contract, section)) {
        const before = eventDeclarationSnapshot(baseline?.design, key), after = eventDeclarationSnapshot(design, key);
        if (!before || hash(before) !== hash(after)) rejectWithDetails('Event callback ' + section + ' does not match the runtime contract: ' + target.title,
          {...bindingDetails('callback_parameter_mismatch','/inputParams/action'),callback_group_key:target.key});
        parameterIssues.push({reason_code:'preexisting_callback_parameter_mismatch', section,
          actual_params:actual, expected_params:expected, status:'unverified', blocks_changed_candidate:false,
          baseline_signature_sha256:hash(before), candidate_signature_sha256:hash(after)});
      }
    }
    checks.push({binding_node_key:key, source_group_key:group.key, target_group_key:target.key,
      component_id:name.value.slice('componentsVariable-'.length), component_type:name.dataType, event_name:input.eventName,
      input_params:target.inputParams || [], output_params:target.outputParams || [], signature_status:contract.signature_status,
      parameter_status:parameterIssues.length ? 'preexisting_mismatch' : 'matched', parameter_issues:parameterIssues,
      return_mode:contract.return_mode || 'native', return_verification:contract.return_verification || 'catalog_only', evidence:contract.evidence || [],
      parameters_declared:true, hidden_arguments_allowed:false, runtime_verified:false});
  }
  const jsGroups = design.actionData.filter(group=>group.actionType === 'js');
  const canvas_diagnostics = canvasDiagnostics(design, baseline?.design);
  const diagnostics = [], oldDiagnostics = [];
  inspectFunctions(code, jsGroups, api.frontendRoot, eventTargetKeys(design), diagnostics, baseline, oldDiagnostics, design);
  return {binding_node_keys:scope, checks, javascript_syntax_checked:true, javascript_lexical_scope:'all_js_groups', javascript_group_keys:jsGroups.map(group=>group.key), business_scenarios_verified:false, browser_verified:false, runtime_verified:false,
    preexisting_parameter_issue_count:checks.reduce((count, check) => count + (check.parameter_issues || []).length, 0),
    canvas_diagnostics, unreachable_diagnostics:diagnostics, baseline_diagnostics:oldDiagnostics, ...(generation ? {generation} : {})};
}
module.exports = {assertJavascriptSyntax, collectScope, validateBindings, canvasDiagnostics};
