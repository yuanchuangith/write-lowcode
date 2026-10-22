'use strict';
const assert = require('node:assert/strict');
const {validateBindings} = require('../mcp/gxp_editor/event_binding_checks.cjs');
const frontendRoot = process.argv[2];
assert.ok(frontendRoot, 'Provide the configured frontend repository for Babel dependencies');

function validate(body, overrides = {}) {
  const group = {key:'main', title:'保留名回归', actionType:'js', data:[],
    inputParams:[{title:'action', type:'object'}], ...overrides};
  const parameter = group.inputParams[0]?.title || '';
  const code = `function (inbiz, relatedAttributes) { var self = {};
    self.main = async function (${parameter}) { ${body} }; return self; }`;
  return validateBindings({actionData:[group], globalVariables:[]}, [], {frontendRoot}, null, code);
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
process.stdout.write('Builtin and reserved platform identifier checks passed\n');
