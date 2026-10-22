'use strict';
const assert = require('node:assert/strict');
const {test} = require('node:test');
const modes = require('../mcp/gxp_editor/generation_modes.cjs');
const group = (key, data = [], actionType = 'js') => ({key, title: key, actionType, data});
const node = (key, value = 1) => ({key, elementKey: 'SetVariable', paramsValue: {inputParams: {value}}, depth: []});
const design = (size = 8) => ({code: 'Example', actionData: Array.from({length: size}, (_, i) => group('g' + i)), globalVariables: []});
const baseline = () => ({js_code: 'saved-js', csharp_code: 'saved-csharp', relation_data: {old: 'relation'}});
function changed(before, count = 1) {
  const after = structuredClone(before);
  after.actionData[0].data = Array.from({length: count}, (_, i) => node('node' + i));
  return after;
}
function selection(size = 8, count = 1, mode = 'auto') {
  const before = design(size);
  return modes.select(before, changed(before, count), baseline(), mode);
}
test('full is the default even with saved artifacts; it never calls the splicer', () => {
  const before = design(), after = changed(before);
  const result = modes.compile(before, after, baseline(), undefined, {
    fullCompile(candidate) { assert.equal(candidate, after); return {js_code: 'full-native', generation: {stale: true}}; },
    incrementalCompile() { assert.fail('default full must not splice'); }
  });
  assert.equal(result.js_code, 'full-native');
  assert.equal(result.generation, undefined);
  assert.equal(result.generation_selection.requested_mode, 'full');
  assert.equal(result.generation_selection.selected_mode, 'full');
  assert.equal(result.generation_selection.reason_code, 'explicit_full');
});
test('explicit full ignores unsuitable saved source without parsing it', () => {
  const before = design();
  const result = modes.compile(before, changed(before), {js_code: 'unmappable'}, 'full', {
    fullCompile() { return {js_code: 'complete-native'}; },
    incrementalCompile() { assert.fail('explicit full must not splice'); }
  });
  assert.equal(result.js_code, 'complete-native');
});
test('explicit incremental is honored and its source mapping errors are not downgraded', () => {
  const before = design();
  const expected = new Error('unmappable saved JS');
  assert.throws(() => modes.compile(before, changed(before), baseline(), 'incremental', {
    fullCompile() { assert.fail('incremental cannot silently fall back'); },
    incrementalCompile() { throw expected; }
  }), error => error === expected);
  assert.equal(selection(1, 1, 'incremental').selected_mode, 'incremental');
});
test('invalid requested modes fail instead of choosing a default', () => {
  for (const mode of [null, '', 'FULL', 'fast', false, {}]) {
    assert.throws(() => modes.normalizeMode(mode), /generation_mode must/);
  }
});
test('explicit incremental requires both saved canvas and complete saved artifacts', () => {
  const before = design(), after = changed(before);
  assert.throws(() => modes.select(null, after, baseline(), 'incremental'), /baseline canvas/);
  for (const saved of [null, {}, {...baseline(), csharp_code: null}, {...baseline(), relation_data: []}]) {
    assert.throws(() => modes.select(before, after, saved, 'incremental'), /complete saved baseline artifacts/);
  }
});
test('auto selects incremental for a bounded small change with a full baseline', () => {
  const result = selection();
  assert.equal(result.requested_mode, 'auto');
  assert.equal(result.selected_mode, 'incremental');
  assert.equal(result.reason_code, 'small_change');
  assert.equal(result.policy_version, 'action_groups_v1');
  assert.deepEqual(result.counts, {total_group_count: 8, direct_changed_group_count: 1,
    affected_group_count: 1, dependency_group_count: 0, changed_node_count: 1,
    affected_group_ratio: 0.125, language_changed_group_count: 0});
  assert.deepEqual(result.scope.affected_group_keys, ['g0']);
  assert.equal(result.scope.unaffected_group_keys.length, 7);
  assert.deepEqual(result.thresholds, {max_affected_groups: 3, max_changed_nodes: 30, max_affected_group_ratio: 0.5});
});
test('auto selects full when saved artifacts or canvas are unavailable', () => {
  const before = design(), after = changed(before);
  for (const saved of [null, {}, {...baseline(), relation_data: []}]) {
    assert.equal(modes.select(before, after, saved, 'auto').reason_code, 'baseline_artifacts_unavailable');
    assert.equal(modes.select(before, after, saved, 'auto').selected_mode, 'full');
  }
  assert.equal(modes.select(null, after, baseline(), 'auto').reason_code, 'baseline_unavailable');
});
test('auto selects full for shared settings and language transitions', () => {
  const before = design(), shared = changed(before), language = changed(before);
  shared.globalVariables = [{name: 'sharedValue', type: 'string'}];
  assert.equal(modes.select(before, shared, baseline(), 'auto').reason_code, 'shared_context_changed');
  language.actionData[0].actionType = 'csharp';
  assert.equal(modes.select(before, language, baseline(), 'auto').reason_code, 'language_changed');
  const addedLanguage = changed(before);
  addedLanguage.actionData.push(group('backend', [], 'csharp'));
  assert.equal(modes.select(before, addedLanguage, baseline(), 'auto').reason_code, 'language_changed');
});
test('auto includes transitive dependent groups and cycles in its size decision', () => {
  const before = design();
  for (let i = 1; i < 4; i++) before.actionData[i].data = [{key: 'ref' + i,
    paramsValue: {inputParams: {action: {value: 'g' + (i - 1)}}}}];
  // The cycle must not loop, or remove a dependency from the affected set.
  before.actionData[0].data = [{key: 'cycle', paramsValue: {action: {value: 'g3'}}}];
  const after = structuredClone(before);
  after.actionData[0].title = 'Changed';
  const result = modes.select(before, after, baseline(), 'auto');
  assert.equal(result.selected_mode, 'full');
  assert.equal(result.reason_code, 'affected_group_limit');
  assert.equal(result.counts.direct_changed_group_count, 1);
  assert.equal(result.counts.affected_group_count, 4);
  assert.equal(result.counts.dependency_group_count, 3);
});
test('auto includes deletions and their dependents in the affected union', () => {
  const before = design();
  before.actionData[1].data = [{key: 'call', paramsValue: {action: {value: 'g0'}}}];
  const after = structuredClone(before);
  after.actionData.shift();
  const result = modes.select(before, after, baseline(), 'auto');
  assert.equal(result.selected_mode, 'incremental');
  assert.equal(result.counts.total_group_count, 8);
  assert.equal(result.counts.affected_group_count, 2);
  assert.deepEqual(result.scope.removed_group_keys, ['g0']);
  assert.deepEqual(result.scope.affected_group_keys, ['g1']);
});
test('auto uses actual changed node count, including removals and ordering changes', () => {
  assert.equal(selection(8, 30).selected_mode, 'incremental');
  assert.equal(selection(8, 31).reason_code, 'changed_node_limit');
  const before = changed(design(), 32), after = structuredClone(before);
  after.actionData[0].data.reverse();
  const reordered = modes.select(before, after, baseline(), 'auto');
  assert.equal(reordered.counts.changed_node_count, 32);
  assert.equal(reordered.reason_code, 'changed_node_limit');
  after.actionData[0].data = [];
  assert.equal(modes.select(before, after, baseline(), 'auto').counts.changed_node_count, 32);
});
test('auto ratio must be below 50 percent, including a one-group design', () => {
  assert.equal(selection(1).reason_code, 'affected_group_ratio');
  assert.equal(selection(2).reason_code, 'affected_group_ratio');
  assert.equal(selection(3).reason_code, 'small_change');
});
test('default depth normalization does not invent changes', () => {
  const before = design(), after = structuredClone(before);
  before.actionData[0].data = [{key: 'n', paramsValue: {}}];
  after.actionData[0].data = [{key: 'n', paramsValue: {}, depth: []}];
  const result = modes.select(before, after, baseline(), 'auto');
  assert.equal(result.counts.affected_group_count, 0);
  assert.equal(result.counts.changed_node_count, 0);
});
test('decision is deterministic and bound to canvas and saved source, not claimed scope', () => {
  const before = design(), after = changed(before), saved = baseline();
  const result = modes.select(before, after, saved, 'auto');
  assert.deepEqual(modes.select(structuredClone(before), structuredClone(after), structuredClone(saved), 'auto'), result);
  assert.match(result.baseline_sha256, /^[0-9a-f]{64}$/);
  assert.match(result.candidate_sha256, /^[0-9a-f]{64}$/);
  saved.js_code += '-different';
  assert.notEqual(modes.select(before, after, saved, 'auto').baseline_sha256, result.baseline_sha256);
  after.actionData[0].data[0].paramsValue.inputParams.value = 2;
  assert.notEqual(modes.select(before, after, saved, 'auto').candidate_sha256, result.candidate_sha256);
});
