'use strict';
// Saved source is an immutable baseline, never a template for new business code.
// Regenerate whole native action functions and splice only proven function spans.
const crypto = require('node:crypto');
const path = require('node:path');
const {createRequire} = require('node:module');
function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
const hash = value => crypto.createHash('sha256').update(typeof value === 'string' ? value : canonical(value)).digest('hex');
const same = (a, b) => canonical(a) === canonical(b);
function failure(message) { throw new Error('Incremental generation: ' + message); }
function scope(before, after) {
  const old = new Map(before.actionData.map(g => [g.key, g]));
  const comparable = group => group && ({...group, data: group.data.map(node => ({...node, depth: node.depth || []}))});
  const changed = new Set(after.actionData.filter(g => !same(comparable(g), comparable(old.get(g.key)))).map(g => g.key));
  const removed = before.actionData.filter(g => !after.actionData.some(n => n.key === g.key)).map(g => g.key);
  // Any factory-level setting may be read by a native generator. Do not guess
  // which shared metadata is irrelevant, or silently preserve a stale header.
  const shared = design => Object.fromEntries(Object.entries(design).filter(([k]) => k !== 'actionData'));
  const sharedChanged = !same(shared(before), shared(after));
  if (sharedChanged) after.actionData.forEach(g => changed.add(g.key));
  const affected = new Set([...changed, ...removed]);
  // Native elements store callback/child-action references as typed records.
  // Follow every local reference (including timers and lifecycle events), not
  // just CallAction. Cycles are bounded by the set of existing group keys.
  function references(value) {
    if (!value || typeof value !== 'object') return false;
    if (!Array.isArray(value) && typeof value.value === 'string' && affected.has(value.value)) return true;
    return Object.values(value).some(references);
  }
  let expanded;
  do {
    expanded = false;
    for (const group of after.actionData) {
      if (!affected.has(group.key) && group.data.some(node => references(node.paramsValue))) {
        affected.add(group.key); changed.add(group.key); expanded = true;
      }
    }
  } while (expanded);
  return {keys: after.actionData.filter(g => changed.has(g.key)).map(g => g.key), removed,
    sharedChanged, preserved: after.actionData.filter(g => !changed.has(g.key)).map(g => g.key)};
}
function commentStart(code, start, floor) {
  const line = code.lastIndexOf('\n', start - 1) + 1;
  if (code.slice(line, start).trim()) return start;
  const previousEnd = line > 0 && code[line - 1] === '\n' ? line - 1 : line;
  const previousStart = code.lastIndexOf('\n', previousEnd - 1) + 1;
  return previousStart >= floor && /^\s*\/\/[^\r\n]*\r?$/.test(code.slice(previousStart, previousEnd)) ? previousStart : line;
}
function jsSpans(code, groups, frontendRoot) {
  const load = createRequire(path.join(frontendRoot, 'package.json'));
  const ast = load('@babel/parser').parse('(' + code + ')', {sourceType: 'script'});
  const factory = ast.program.body[0]?.expression;
  if (factory?.type !== 'FunctionExpression' || factory.body.type !== 'BlockStatement') failure('unsupported saved JS factory');
  const spans = new Map(), expected = new Set(groups.map(g => g.key));
  let floor = factory.body.start, insertion;
  for (const statement of factory.body.body) {
    const expr = statement.type === 'ExpressionStatement' && statement.expression;
    const left = expr?.type === 'AssignmentExpression' && expr.left;
    if (left?.type === 'MemberExpression' && left.object.type === 'Identifier' && left.object.name === 'self') {
      const key = !left.computed && left.property.type === 'Identifier' ? left.property.name : null;
      if (expected.has(key)) {
        if (spans.has(key) || expr.operator !== '=' || expr.right.type !== 'FunctionExpression') failure('ambiguous JS group ' + key);
        spans.set(key, {start: commentStart(code, statement.start - 1, floor), end: statement.end - 1});
      }
    }
    if (statement.type === 'ReturnStatement' && statement.argument?.type === 'Identifier' && statement.argument.name === 'self') insertion = statement.start - 1;
    floor = statement.end - 1;
  }
  if (spans.size !== expected.size || insertion === undefined) failure('saved JS groups do not match the baseline canvas');
  return {spans, insertion};
}
function csharpTokens(code) {
  // A bounded lexer, not a C# parser. Ambiguous string forms fail closed rather
  // than mistaking braces or fake method declarations in literals for methods.
  const result = [];
  function skipLiteral(start) {
    const prefix = /^(?:\$@|@\$|\$|@)?["']/.exec(code.slice(start));
    if (!prefix) return null;
    const head = prefix[0], quote = head.at(-1), verbatim = head.includes('@'), interpolated = head.includes('$');
    const quoteAt = start + head.length - 1;
    if (code.startsWith('"""', quoteAt)) failure('C# raw strings require a source map; saved source was retained');
    let i = quoteAt + 1;
    while (i < code.length) {
      if (!verbatim && code[i] === '\\') { i += 2; continue; }
      if (code[i] === quote) {
        if (verbatim && code[i + 1] === quote) { i += 2; continue; }
        return i + 1;
      }
      if (interpolated && code[i] === '{') {
        if (code[i + 1] === '{') { i += 2; continue; }
        let braces = 1; i++;
        while (i < code.length && braces) {
          const nested = skipLiteral(i);
          if (nested !== null) { i = nested; continue; }
          if (code.startsWith('//', i)) { const end = code.indexOf('\n', i); if (end < 0) failure('unclosed C# interpolation'); i = end; continue; }
          if (code.startsWith('/*', i)) { const end = code.indexOf('*/', i + 2); if (end < 0) failure('unclosed C# interpolation comment'); i = end + 2; continue; }
          if (code[i] === '{') braces++;
          if (code[i] === '}') braces--;
          i++;
        }
        if (braces) failure('unclosed C# interpolation');
        continue;
      }
      i++;
    }
    failure('unterminated C# literal');
  }
  for (let i = 0; i < code.length;) {
    const start = i, c = code[i], next = code[i + 1];
    if (/\s/.test(c)) { i++; continue; }
    if (c === '/' && next === '/') { i = code.indexOf('\n', i); if (i < 0) break; continue; }
    if (c === '/' && next === '*') { i = code.indexOf('*/', i + 2); if (i < 0) failure('unterminated C# comment'); i += 2; continue; }
    const literalEnd = skipLiteral(i);
    if (literalEnd !== null) {
      i = literalEnd;
      result.push({value: '<literal>', start, end: i}); continue;
    }
    const match = /^[A-Za-z_][A-Za-z0-9_]*/.exec(code.slice(i));
    i += match ? match[0].length : 1;
    result.push({value: code.slice(start, i), start, end: i});
  }
  return result;
}
function csharpSpans(code, groups) {
  const tokens = csharpTokens(code), spans = new Map(), expected = new Set(groups.map(g => g.key));
  const signature = 'public async Task < Dictionary < string , object > >'.split(' ');
  let depth = 0, insertion, floor = 0;
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (depth === 2 && signature.every((v, n) => tokens[i + n]?.value === v)) {
      const key = tokens[i + signature.length]?.value;
      if (expected.has(key) && tokens[i + signature.length + 1]?.value === '(') {
        if (spans.has(key)) failure('ambiguous C# group ' + key);
        let j = i + signature.length + 1, parens = 0;
        do { if (tokens[j].value === '(') parens++; if (tokens[j].value === ')') parens--; j++; } while (j < tokens.length && parens);
        if (parens || tokens[j]?.value !== '{') failure('unsupported saved C# group ' + key);
        let braces = 0, end;
        do { if (tokens[j].value === '{') braces++; if (tokens[j].value === '}') braces--; end = tokens[j++].end; } while (j < tokens.length && braces);
        if (braces) failure('unclosed C# group ' + key);
        spans.set(key, {start: commentStart(code, t.start, floor), end});
        floor = end; i = j - 1; continue;
      }
    }
    if (t.value === '{') depth++;
    if (t.value === '}') { if (depth === 2) insertion = t.start; depth--; }
    if (depth < 0) failure('unbalanced saved C# source');
  }
  if (depth || spans.size !== expected.size || insertion === undefined) failure('saved C# groups do not match the baseline canvas');
  return {spans, insertion};
}
function merge(code, oldGroups, newGroups, generated, keys, language, frontendRoot) {
  const selected = new Set(keys), fresh = newGroups.filter(g => selected.has(g.key));
  const removed = oldGroups.filter(g => !newGroups.some(n => n.key === g.key));
  if (!fresh.length && !removed.length) return code;
  if (!oldGroups.length) return generated;
  if (!newGroups.length) return '';
  const parse = language === 'js' ? text => jsSpans(text, oldGroups, frontendRoot) : text => csharpSpans(text, oldGroups);
  const oldMap = parse(code);
  const newMap = fresh.length ? (language === 'js' ? jsSpans(generated, fresh, frontendRoot) : csharpSpans(generated, fresh)) : {spans: new Map()};
  const edits = [];
  for (const group of oldGroups) if (selected.has(group.key) || removed.some(g => g.key === group.key)) {
    const oldSpan = oldMap.spans.get(group.key), newSpan = newMap.spans.get(group.key);
    edits.push({...oldSpan, text: newSpan ? generated.slice(newSpan.start, newSpan.end) : ''});
  }
  const added = fresh.filter(g => !oldMap.spans.has(g.key)).map(g => {
    const span = newMap.spans.get(g.key); return generated.slice(span.start, span.end);
  });
  if (added.length) edits.push({start: oldMap.insertion, end: oldMap.insertion, text: '\n' + added.join('\n') + '\n'});
  edits.sort((a, b) => b.start - a.start);
  for (const edit of edits) code = code.slice(0, edit.start) + edit.text + code.slice(edit.end);
  return code;
}
function generate(before, after, baseline, nativeCompile, api) {
  if (!baseline || typeof baseline.js_code !== 'string' || typeof baseline.csharp_code !== 'string'
      || !baseline.relation_data || typeof baseline.relation_data !== 'object' || Array.isArray(baseline.relation_data)) failure('complete saved baseline artifacts are required');
  const selection = scope(before, after), keys = new Set(selection.keys);
  const selectedDesign = {...after, actionData: after.actionData.filter(g => keys.has(g.key))};
  const compiled = api.reproducibleCompile(selectedDesign, () => nativeCompile(structuredClone(after), selection.keys));
  const result = {relation_data: {}, interface_metadata: api.interfaceMetadata.describe(selectedDesign)};
  for (const language of ['js', 'csharp']) {
    const field = language + '_code', old = before.actionData.filter(g => g.actionType === language), next = after.actionData.filter(g => g.actionType === language);
    result[field] = selection.sharedChanged ? compiled[field]
      : merge(baseline[field], old, next, compiled[field], selection.keys, language, api.frontendRoot);
  }
  const oldNodes = new Set(before.actionData.filter(g => !selection.preserved.includes(g.key)).flatMap(g => g.data.map(n => n.key)));
  for (const [key, value] of Object.entries(baseline.relation_data)) if (!oldNodes.has(key)) result.relation_data[key] = value;
  Object.assign(result.relation_data, compiled.relation_data);
  result.generation = {mode: 'incremental_action_groups', regenerated_group_keys: selection.keys,
    preserved_group_keys: selection.preserved, removed_group_keys: selection.removed,
    shared_context_changed: selection.sharedChanged, baseline_sha256: hash({design: before, artifacts: baseline}),
    candidate_sha256: hash(after), preserved_source_verified: true};
  return result;
}
module.exports = {generate, scope, jsSpans, csharpSpans, merge, hash};
