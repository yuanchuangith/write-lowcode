'use strict';
// Native generator boundaries are captured in memory. Markers never enter saved JS.
const crypto = require('node:crypto');
const path = require('node:path');
const {createRequire} = require('node:module');
function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key=>JSON.stringify(key)+':'+canonical(value[key])).join(',') + '}';
  return JSON.stringify(value);
}
const hash = value => crypto.createHash('sha256').update(typeof value === 'string' ? value : canonical(value)).digest('hex');
const canvasHash = design => hash({actionData:design.actionData, globalVariables:design.globalVariables || []});
let active;
function markNodeCode(code, node) {
  if (!active || typeof code !== 'string' || !code.trim()) return code;
  const group = active.groups.get(node.key);
  if (!group) return code;
  const index = active.nodes.length;
  active.nodes.push({group_key:group, node_key:node.key, element_key:node.elementKey});
  return `/*${active.token}:${index}:start*/\n${code}\n/*${active.token}:${index}:end*/`;
}
function capture(design, compile) {
  const previous = active;
  const state = {token:'GXP_COVERAGE_'+crypto.randomBytes(16).toString('hex'), nodes:[], groups:new Map()};
  for (const group of design.actionData || []) if (group.actionType === 'js') {
    for (const node of group.data || []) state.groups.set(node.key, group.key);
  }
  active = state;
  let result;
  try { result = compile(); } finally { active = previous; }
  if (!result || typeof result.js_code !== 'string') return result;
  let source = '', opened = null;
  const ranges = [];
  const marker = new RegExp('^[\\t ]*/\\*'+state.token+':(\\d+):(start|end)\\*/\\r?\\n?$');
  for (const line of result.js_code.match(/[^\n]*\n|[^\n]+$/g) || []) {
    const match = marker.exec(line);
    if (!match) { source += line; continue; }
    const index = Number(match[1]);
    if (match[2] === 'start') {
      if (opened !== null || !state.nodes[index]) throw new Error('Ambiguous native execution mapping');
      opened = {index, start:source.length};
    } else {
      if (!opened || opened.index !== index) throw new Error('Invalid native execution mapping');
      ranges.push({...state.nodes[index], start:opened.start, end:source.length}); opened = null;
    }
  }
  if (opened || ranges.length !== state.nodes.length) throw new Error('Incomplete native execution mapping');
  return {...result, js_code:source, execution_coverage:{schema_version:1,
    source_sha256:hash(source), canvas_sha256:canvasHash(design), ranges}};
}
function instrument(source, mapping, frontendRoot, hook) {
  if (!mapping || mapping.schema_version !== 1 || mapping.source_sha256 !== hash(source) || !Array.isArray(mapping.ranges)) return null;
  const load = createRequire(path.join(path.resolve(frontendRoot || process.cwd()), 'package.json'));
  const parser = load('@babel/parser'), traverse = load('@babel/traverse').default;
  const ast = parser.parse('('+source+')', {sourceType:'script'});
  const statements = [], conditions = [];
  traverse(ast, {
    Statement(p) { if (p.inList && p.listKey === 'body' && !p.isBlockStatement() && !p.isFunctionDeclaration()) statements.push(p.node); },
    IfStatement(p) { conditions.push(p.node.test); },
  });
  const edits = [], entries = [];
  for (const range of mapping.ranges) {
    if (!Number.isInteger(range.start) || !Number.isInteger(range.end) || range.start < 0 || range.end > source.length || range.end <= range.start) return null;
    const contains = node => node.start-1 >= range.start && node.end-1 <= range.end;
    const id = entries.length;
    // Structural closing/else markers have no independently executable statement.
    const condition = ['IfCondition','ElseIfCondition'].includes(range.element_key) ? conditions.find(contains) : null;
    if (condition) {
      edits.push({start:condition.start-1,end:condition.start-1,text:`${hook}(${id},(`},
        {start:condition.end-1,end:condition.end-1,text:'))'});
      entries.push({...range, kind:'condition'});
    } else {
      const statement = statements.filter(contains).sort((a,b)=>a.start-b.start)[0];
      if (!statement) continue;
      edits.push({start:statement.start-1,end:statement.start-1,text:`${hook}(${id});`});
      entries.push({...range, kind:'statement'});
    }
  }
  edits.sort((a,b)=>b.start-a.start);
  for (const edit of edits) source = source.slice(0,edit.start)+edit.text+source.slice(edit.end);
  return {source, entries, mapping_sha256:hash(mapping)};
}
module.exports = {capture, markNodeCode, instrument, hash, canvasHash};
