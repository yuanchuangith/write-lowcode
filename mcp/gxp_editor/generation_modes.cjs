'use strict';
// Choose a generation strategy from the actual canvas delta, never from an
// agent's description of the size of its change. Verification repeats this
// calculation using the saved baseline and the complete candidate.
const incremental = require('./incremental_codegen.cjs');
const POLICY_VERSION = 'action_groups_v1';
const THRESHOLDS = Object.freeze({max_affected_groups: 3, max_changed_nodes: 30, max_affected_group_ratio: 0.5});
const MODES = new Set(['full', 'incremental', 'auto']);
function normalizeMode(mode) {
  const requested = mode === undefined ? 'full' : mode;
  if (!MODES.has(requested)) throw new Error('generation_mode must be full, incremental, or auto');
  return requested;
}
function completeArtifacts(baseline) {
  return !!baseline && typeof baseline.js_code === 'string' && typeof baseline.csharp_code === 'string'
    && !!baseline.relation_data && typeof baseline.relation_data === 'object' && !Array.isArray(baseline.relation_data);
}
const same = (left, right) => left === undefined || right === undefined
  ? left === right : incremental.hash(left) === incremental.hash(right);
const comparableNode = node => node && ({...node, depth: node.depth || []});
const comparableGroup = group => group && ({...group, data: group.data.map(comparableNode)});
function describeScope(before, after) {
  const old = new Map((before?.actionData || []).map(group => [group.key, group]));
  const next = new Map(after.actionData.map(group => [group.key, group]));
  const all = new Set([...old.keys(), ...next.keys()]);
  const selected = before ? incremental.scope(before, after)
    : {keys: [...next.keys()], preserved: [], removed: [], sharedChanged: false};
  const direct = new Set(), languageChanged = new Set();
  const oldLanguages = new Set([...old.values()].map(group => group.actionType));
  const nextLanguages = new Set([...next.values()].map(group => group.actionType));
  let changedNodes = 0;
  for (const key of all) {
    const previous = old.get(key), candidate = next.get(key);
    if (!same(comparableGroup(previous), comparableGroup(candidate))) direct.add(key);
    if (previous && candidate && previous.actionType !== candidate.actionType
        || !previous && candidate && !oldLanguages.has(candidate.actionType)
        || previous && !candidate && !nextLanguages.has(previous.actionType)) languageChanged.add(key);
    const previousNodes = new Map((previous?.data || []).map(node => [node.key, node]));
    const candidateNodes = new Map((candidate?.data || []).map(node => [node.key, node]));
    const previousOrder = new Map((previous?.data || []).map((node, index) => [node.key, index]));
    const candidateOrder = new Map((candidate?.data || []).map((node, index) => [node.key, index]));
    for (const nodeKey of new Set([...previousNodes.keys(), ...candidateNodes.keys()])) {
      if (previousOrder.get(nodeKey) !== candidateOrder.get(nodeKey)
          || !same(comparableNode(previousNodes.get(nodeKey)), comparableNode(candidateNodes.get(nodeKey)))) changedNodes++;
    }
  }
  const affected = new Set([...selected.keys, ...selected.removed]);
  return {
    counts: {total_group_count: all.size, direct_changed_group_count: direct.size,
      affected_group_count: affected.size, dependency_group_count: [...affected].filter(key => !direct.has(key)).length,
      changed_node_count: changedNodes, affected_group_ratio: all.size ? affected.size / all.size : 0,
      language_changed_group_count: languageChanged.size},
    scope: {affected_group_keys: selected.keys, unaffected_group_keys: selected.preserved,
      removed_group_keys: selected.removed, shared_context_changed: selected.sharedChanged}
  };
}
function select(before, after, baseline, mode) {
  const requested = normalizeMode(mode);
  const {counts, scope} = describeScope(before, after);
  let selected = requested, reason;
  if (requested === 'full') reason = 'explicit_full';
  else if (requested === 'incremental') {
    if (!before) throw new Error('Incremental generation: the saved baseline canvas is required');
    if (!completeArtifacts(baseline)) throw new Error('Incremental generation: complete saved baseline artifacts are required');
    reason = 'explicit_incremental';
  } else {
    selected = 'full';
    if (!before) reason = 'baseline_unavailable';
    else if (!completeArtifacts(baseline)) reason = 'baseline_artifacts_unavailable';
    else if (scope.shared_context_changed) reason = 'shared_context_changed';
    else if (counts.language_changed_group_count) reason = 'language_changed';
    else if (counts.affected_group_count > THRESHOLDS.max_affected_groups) reason = 'affected_group_limit';
    else if (counts.changed_node_count > THRESHOLDS.max_changed_nodes) reason = 'changed_node_limit';
    else if (counts.affected_group_ratio >= THRESHOLDS.max_affected_group_ratio) reason = 'affected_group_ratio';
    else { selected = 'incremental'; reason = 'small_change'; }
  }
  return {requested_mode: requested, selected_mode: selected, reason_code: reason,
    policy_version: POLICY_VERSION, counts, thresholds: {...THRESHOLDS}, scope,
    baseline_sha256: incremental.hash({design: before || null, artifacts: baseline || null}),
    candidate_sha256: incremental.hash(after)};
}
function compile(before, after, baseline, mode, api) {
  const selection = select(before, after, baseline, mode);
  // In particular, supplying saved artifacts does not opt a full request into
  // splicing. Explicit incremental failures are never caught or downgraded.
  const compiled = selection.selected_mode === 'incremental'
    ? api.incrementalCompile(before, after, baseline) : api.fullCompile(after);
  const result = {...compiled, generation_selection: selection};
  if (selection.selected_mode === 'full') delete result.generation;
  return result;
}
module.exports = {normalizeMode, completeArtifacts, describeScope, select, compile, POLICY_VERSION, THRESHOLDS};
