'use strict';
const assert = require('node:assert/strict');
const {validateBindings, canvasDiagnostics} = require('../mcp/gxp_editor/event_binding_checks.cjs');
const frontendRoot = process.argv[2];
assert.ok(frontendRoot, 'Provide the configured frontend repository for Babel dependencies');

function validate(body, overrides = {}, generation, baselineBody) {
  const group = {key:'main', title:'保留名回归', actionType:'js', data:[],
    inputParams:[{title:'action', type:'object'}], ...overrides};
  const parameter = group.inputParams[0]?.title || '';
  const code = `function (inbiz, relatedAttributes) { var self = {};
    self.main = async function (${parameter}) { ${body} }; return self; }`;
  const baseline = baselineBody === undefined ? undefined : {design:{actionData:[group]},
    js_code:`function (inbiz, relatedAttributes) { var self = {};
      self.main = async function (${parameter}) { ${baselineBody} }; return self; }`};
  return validateBindings({actionData:[group], globalVariables:[]}, [], {frontendRoot}, null, code, generation, baseline);
}

// Platform calls, payload properties, distinct local names and inert keys are valid.
const safe = validate(`const innerInbiz = action.innerInbiz;
  const pageInbiz = innerInbiz;
  const labels = {inbiz:'inbiz', Inbiz:'Inbiz'};
  const saved = window.inbiz;
  await inbiz('parent').getData();
  return pageInbiz('child');`);
assert.equal(safe.javascript_syntax_checked, true);
assert.equal(safe.runtime_verified, false);

// Older globals versions expose eval through builtin, without an es2021 set.
for (const body of [
  'const innerInbiz = action.innerInbiz; const files = eval(innerInbiz("file_upload").value);',
  'const requestResult = {Data:"[]"}; const error = eval(requestResult.Data);',
]) {
  const evidence = validate(body);
  assert.equal(evidence.javascript_syntax_checked, true);
  assert.equal(evidence.javascript_lexical_scope, 'all_js_groups');
  assert.equal(evidence.business_scenarios_verified, false);
  assert.equal(evidence.browser_verified, false);
  assert.equal(evidence.runtime_verified, false);
}
assert.throws(() => validate('return eval(missingContext.value);'), /Undeclared action identifier missingContext/);

for (const body of [
  'var inbiz;',
  'let Inbiz = action.innerInbiz;',
  'const {child: Inbiz} = action;',
  'const [inbiz] = [];',
  'const run = (Inbiz) => Inbiz;',
  'function inbiz() {}',
  'class Inbiz {}',
  'try {} catch (inbiz) {}',
  'inbiz = action.innerInbiz;',
  'Inbiz ||= action.innerInbiz;',
  '[inbiz] = [];',
  '({value: window.inbiz} = action);',
  'window.inbiz = action.innerInbiz;',
  "globalThis['Inbiz'] = action.innerInbiz;",
  'window.inbiz++;',
  'delete window.inbiz;',
  'for (inbiz of []) {}',
  "for (window['inbiz'] in {}) {}",
  'for (const Inbiz of []) {}',
]) {
  assert.throws(() => validate(body), error => {
    assert.match(error.message, /Reserved platform identifier/);
    assert.equal(error.details.reason_code, 'reserved_platform_identifier');
    assert.equal(error.details.group_key, 'main');
    return true;
  }, body);
}

for (const name of ['inbiz', 'Inbiz']) {
  assert.throws(() => validate('return true;', {inputParams:[{title:name, type:'object'}]}), /Reserved platform identifier/);
  assert.throws(() => validate('return true;', {outputParams:[{title:name, type:'object'}]}), /Reserved platform identifier/);
  assert.throws(() => validateBindings({globalVariables:[{name}], actionData:[
    {key:'main', title:'主动作', actionType:'js', data:[]},
  ]}, [], {frontendRoot}, null, 'function (inbiz) { var self = {}; self.main = function () {}; return self; }'), /Reserved platform identifier/);
}
assert.throws(() => validate('return missingContext.id;'), /Undeclared action identifier missingContext/);
assert.throws(() => validate('const one = missingContext.id; return anotherMissing.value + missingContext.name;'), error => {
  assert.equal(error.details.phase,'javascript_lexical');
  assert.equal(error.details.reason_code,'undeclared_identifier');
  assert.equal(error.details.group_key,'main');
  assert.equal(error.details.mapping_status,'group_only');
  assert.equal(error.details.node_key,undefined);
  assert.equal(error.details.issue_count,2);
  assert.deepEqual(error.details.issues.map(issue=>issue.identifier),['missingContext','anotherMissing']);
  assert.ok(error.details.issues.every(issue=>Number.isInteger(issue.line) && Number.isInteger(issue.column)));
  assert.equal(error.details.issues_truncated,false);return true;
});
assert.throws(() => validate(Array.from({length:40},(_,i)=>'missing'+i+'.value;').join('\n')), error => {
  assert.equal(error.details.issue_count,40);
  assert.equal(error.details.issues.length,20);
  assert.equal(error.details.issues_truncated,true);return true;
});
assert.throws(() => validateBindings({globalVariables:[], actionData:[
  {key:'main',title:'Wrong signature',actionType:'js',inputParams:[{title:'params'}],data:[]},
]},[],{frontendRoot},null,'function(inbiz){var self={};self.main=function(action){};return self;}'),error=>{
  assert.equal(error.details.reason_code,'callback_signature_mismatch');
  assert.equal(error.details.mapping_status,'group_only');return true;
});
// Generation scope never proves that old code cannot execute. Even an action
// with no local callers may be invoked by the platform or a page binding.
for (const generation of [undefined, {preserved_group_keys:['main']}]) {
  for (const body of [
    'return missingContext.value;',
    'if (action.disabled) missingContext.value;',
    'if (false) missingContext.value; return missingContext.value;',
    'return legacy(); function legacy() { return missingContext.value; }',
    'if (false) { var enabled = true; } if (enabled) missingContext.value;',
    'for (missingContext.value; false;) {}',
    'do { missingContext.value; } while (false);',
  ]) assert.throws(() => validate(body, {title:'弃用但没有不可达证据'}, generation), /Undeclared action identifier missingContext/);
  for (const [body, reason] of [
    ['if (false) missingContext.value;', 'constant_false_consequent'],
    ['if (true) {} else missingContext.value;', 'constant_true_alternate'],
    ['return false ? missingContext.value : true;', 'constant_false_consequent'],
    ['return true ? true : missingContext.value;', 'constant_true_alternate'],
    ['return false && missingContext.value;', 'literal_short_circuit'],
    ['return true || missingContext.value;', 'literal_short_circuit'],
    ['return 0 ?? missingContext.value;', 'literal_short_circuit'],
    ['while (false) missingContext.value;', 'constant_false_loop'],
    ['for (; !true; missingContext.value) {}', 'constant_false_loop'],
    ['return true; missingContext.value;', 'after_return'],
    ['throw new Error(); missingContext.value;', 'after_throw'],
  ]) {
    const result = validate(body, {}, generation);
    assert.equal(result.javascript_lexical_scope, 'all_js_groups');
    assert.equal(result.unreachable_diagnostics.length, 1, body);
    const finding = result.unreachable_diagnostics[0];
    assert.equal(finding.identifier, 'missingContext');
    assert.equal(finding.reachability.reason_code, reason);
    assert.equal(finding.status, 'unverified');
    assert.equal(finding.blocks_changed_candidate, false);
    assert.ok(finding.line > 0 && finding.column >= 0);
    assert.ok(finding.reachability.line > 0 && finding.reachability.column >= 0);
    assert.equal(result.runtime_verified, false);
    assert.deepEqual(result.baseline_diagnostics || [], []);
  }
}
// An unreachable branch must still parse; reserved platform bindings are
// checked even in dead code because declarations can be hoisted.
assert.throws(() => validate('if (false) { const broken = ; }'), SyntaxError);
assert.throws(() => validate('if (false) { var inbiz; }'), /Reserved platform identifier/);
// Check all groups in every generation mode, then compare individual reachable
// references with the trusted pre-edit source, not just group/name existence.
for (const generation of [undefined, {preserved_group_keys:['main']}, {preserved_group_keys:[]}]) {
  const original = 'return missingContext.value;';
  const result = validate('const changed = true;\n' + original, {}, generation, original);
  assert.equal(result.baseline_diagnostics.length, 1);
  const finding = result.baseline_diagnostics[0];
  assert.equal(finding.status, 'unverified');
  assert.equal(finding.reason_code, 'preexisting_undeclared_identifier');
  assert.equal(finding.blocks_changed_candidate, false);
  assert.match(finding.baseline.source_sha256, /^[a-f0-9]{64}$/);
  assert.match(finding.baseline.reference_sha256, /^[a-f0-9]{64}$/);
  assert.notEqual(finding.line, finding.baseline.line);
  for (const [before, after] of [
    [original, 'missingContext.other; ' + original],
    [original, 'missingContext.value; ' + original],
    ['const missingContext = {}; ' + original, original],
    ['if (false) missingContext.value;', original],
  ]) assert.throws(() => validate(after, {}, generation, before), /Undeclared action identifier missingContext/);
}
// Catalog types are plugin metadata, not a JavaScript runtime type assertion.
// A preexisting any/string mismatch is reported when only callback logic changes.
{
  const declaration = {title:'value', type:'string', key:'value-key'};
  const binding = {key:'binding', elementKey:'ComponentEvent', paramsValue:{inputParams:{
    name:{paramTypes:'componentsVariable', value:'componentsVariable-exam', dataType:'ExamSelect'},
    eventName:'change', action:{value:'callback'}}, outputParams:{}}};
  const before = {globalVariables:[], actionData:[
    {key:'main', title:'初始化', actionType:'js', data:[binding]},
    {key:'callback', title:'试卷变更', actionType:'js', inputParams:[declaration], outputParams:[], data:[]},
  ]};
  const code = 'function(inbiz) { var self={}; self.main=function(){}; self.callback=function(value){ return value; }; return self; }';
  const api = {frontendRoot, catalog:()=>({}), actionByKey:(design,key)=>design.actionData.find(group=>group.key===key)};
  const contract = () => ({input_params:[{title:'value',type:'any'}],output_params:[],signature_status:'catalog_declared'});
  const baseline = {design:structuredClone(before), js_code:code};
  const candidate = structuredClone(before); candidate.actionData[1].data.push({key:'changed',elementKey:'SetVariable'});
  const result = validateBindings(candidate, ['binding'], api, contract, code, undefined, baseline);
  assert.equal(result.preexisting_parameter_issue_count, 1);
  const check = result.checks[0], issue = check.parameter_issues[0];
  assert.equal(check.parameter_status, 'preexisting_mismatch');
  assert.equal(issue.status, 'unverified');
  assert.equal(issue.blocks_changed_candidate, false);
  assert.equal(issue.actual_params[0].type, 'string');
  assert.equal(issue.expected_params[0].type, 'any');
  assert.equal(issue.baseline_signature_sha256, issue.candidate_signature_sha256);
  assert.deepEqual(candidate.actionData[1].inputParams, before.actionData[1].inputParams);
  assert.throws(() => validateBindings(candidate, ['binding'], api, contract, code), /does not match the runtime contract/);
  for (const change of [
    design=>{design.actionData[0].data[0].paramsValue.inputParams.name.value='componentsVariable-another';},
    design=>{design.actionData[0].data[0].paramsValue.inputParams.eventName='newChange';},
    design=>{design.actionData[1].inputParams[0].type='number';},
    design=>{design.actionData[1].inputParams.push({title:'another',type:'string'});},
  ]) {
    const changed = structuredClone(candidate); change(changed);
    assert.throws(() => validateBindings(changed, ['binding'], api, contract, code, undefined, baseline), /does not match the runtime contract/);
  }
  const hidden = code.replace('return value;', 'return arguments[0];');
  const oldHidden = {...baseline, js_code:hidden};
  const hiddenResult = validateBindings(candidate, ['binding'], api, contract, hidden, undefined, oldHidden);
  assert.equal(hiddenResult.baseline_diagnostics[0].reason_code, 'preexisting_hidden_event_arguments');
  for (const modified of [hidden.replace('arguments[0]', 'arguments[1]'), hidden.replace('return arguments', 'arguments[0]; return arguments')])
    assert.throws(() => validateBindings(candidate, ['binding'], api, contract, modified, undefined, oldHidden), /Hidden event arguments/);
  const withoutBinding = structuredClone(before); withoutBinding.actionData[0].data = [];
  assert.throws(() => validateBindings(candidate, ['binding'], api, contract, hidden, undefined,
    {design:withoutBinding,js_code:hidden}), /does not match the runtime contract/);
  const reachable = hidden.replace('return arguments[0];', 'if (false) return arguments[0];');
  assert.equal(validateBindings(candidate, ['binding'], api, contract, reachable, undefined, baseline).unreachable_diagnostics[0].reason_code,
    'hidden_event_arguments');
  assert.throws(() => validateBindings(candidate, ['binding'], api, contract, hidden, undefined,
    {...baseline, js_code:reachable}), /Hidden event arguments/);

  for (const [mutate, reason] of [
    [design => {design.actionData[0].data[0].paramsValue.inputParams.name = {paramTypes:'custom',code:'inbiz("exam")'};}, 'binding_component_identity_required'],
    [design => {design.actionData[0].data[0].paramsValue.inputParams.eventName = 'missing';}, 'event_contract_unavailable'],
    [design => {design.actionData[0].data[0].paramsValue.inputParams.action.value = 'missing';}, 'event_callback_missing'],
    [design => {design.actionData[0].data[0].paramsValue.inputParams.action.value = 'main';}, 'event_callback_group_invalid'],
  ]) {
    const old = structuredClone(before); mutate(old);
    const candidate = structuredClone(old); candidate.actionData[0].data.push({key:'new',elementKey:'SetVariable'});
    const checkedContract = (...args) => {
      if (args[2] === 'missing') {const error = new Error('Unknown event'); error.code = 'event_contract_unavailable'; throw error;}
      return contract();
    };
    const result = validateBindings(candidate, ['binding'], api, checkedContract, code, undefined, {design:old,js_code:code});
    assert.equal(result.checks[0].binding_issue.reason_code, reason);
    assert.equal(result.checks[0].binding_issue.status, 'unverified');
    assert.throws(() => validateBindings(candidate, ['binding'], api, checkedContract, code));
    const changed = structuredClone(candidate);
    changed.actionData[0].data[0].paramsValue.inputParams.name.value = 'componentsVariable-new';
    assert.throws(() => validateBindings(changed, ['binding'], api, checkedContract, code, undefined, {design:old,js_code:code}));
  }
  assert.throws(() => validateBindings(candidate, ['binding'], api, () => {throw new Error('Runtime event contract source changed');},
    code, undefined, baseline), /source changed/);
}
// The same reserved declaration/write is advisory only with concrete old-source
// proof. New writes, changed RHS, and additional occurrences still block.
for (const original of ['var inbiz;', 'window.inbiz = action.innerInbiz;', 'let Inbiz = action.innerInbiz;',
  'const run = (Inbiz) => Inbiz;', 'function inbiz() {}']) {
  const result = validate('const changed = true; ' + original, {}, undefined, original);
  assert.equal(result.baseline_diagnostics.length, 1, original);
  assert.equal(result.baseline_diagnostics[0].reason_code, 'preexisting_reserved_platform_identifier');
  assert.throws(() => validate(original + '\n' + original, {}, undefined, original), /Reserved|already been declared/);
}
assert.throws(() => validate('window.inbiz = action.other;', {}, undefined, 'window.inbiz = action.innerInbiz;'), /Reserved/);
assert.throws(() => validate('const {child:Inbiz} = action.other;', {}, undefined, 'const {child:Inbiz} = action;'), /Reserved/);
// Baseline canvas fields must match exactly; unrelated node changes do not turn
// the old problem into a requirement to fix it.
{
  const before = {globalVariables:[{name:'Inbiz',value:'old'}], actionData:[{key:'main',actionType:'js', outputParams:[{title:'inbiz'}], data:[
    {key:'legacy',elementKey:'SetVariable',paramsValue:{inputParams:{variableValue:{paramTypes:'custom',value:'display',code:'true'}}}},
  ]}]};
  const after = structuredClone(before); after.actionData[0].data.push({key:'changed',elementKey:'SetVariable'});
  const findings = canvasDiagnostics(after, before);
  assert.equal(findings.length, 3);
  assert.ok(findings.every(issue => issue.status === 'unverified' && issue.blocks_changed_candidate === false));
  assert.throws(() => canvasDiagnostics(after), /Reserved|synchronized/);
  after.actionData[0].data[0].paramsValue.inputParams.variableValue.code = 'false';
  assert.throws(() => canvasDiagnostics(after, before), /synchronized/);
}
process.stdout.write('Builtin and reserved platform identifier checks passed\n');
