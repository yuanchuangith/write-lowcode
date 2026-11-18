'use strict';

const fs = require('node:fs');
const path = require('node:path');
const {createRequire} = require('node:module');
const root = process.argv[2];
const load = root ? createRequire(path.join(path.resolve(root), 'package.json')) : require;
const parser = load('@babel/parser');
const traverse = load('@babel/traverse').default;
const comparators = new Set(['===', '!==', '==', '!=', '>', '>=', '<', '<=']);
const members = new Set(['MemberExpression', 'OptionalMemberExpression']);
const calls = new Set(['CallExpression', 'OptionalCallExpression']);
const componentMethods = new Set(['setValueToID', 'setValue', 'setValues']);
const shapeKeys = new Set(['start', 'end', 'loc', 'extra', 'leadingComments', 'trailingComments', 'innerComments']);

function expression(code) {
  return parser.parseExpression(code, {allowAwaitOutsideFunction: true});
}
function parse(code) {
  if (code.length > 16000) throw new Error('Expression too large');
  const node = expression(code);
  return {node, ast: {type:'File', program: {type:'Program', sourceType:'script',
    interpreter:null, directives:[], body: [{type:'ExpressionStatement', expression:node}]}}};
}
function canonical(n) {
  return JSON.stringify(n, (key, value) => shapeKeys.has(key) ? undefined : value);
}
function property(node) {
  if (!node || !members.has(node.type)) return '';
  if (!node.computed && node.property.type === 'Identifier') return node.property.name;
  return node.property.type === 'StringLiteral' ? node.property.value : '';
}
function isInbiz(node, scope) {
  return node?.type === 'Identifier' && node.name === 'inbiz' && !scope.getBinding('inbiz');
}
function component(node, scope) {
  return calls.has(node?.type) && isInbiz(node.callee, scope);
}
function hiddenWrite(ast) {
  let hidden = false;
  const detect = (p) => {
    const m = p.node.callee;
    const name = property(m);
    if ((name === 'setValues' && isInbiz(m.object, p.scope)) ||
        (componentMethods.has(name) && component(m.object, p.scope))) hidden = true;
  };
  const assignment = (p) => {
    const m = p.node.left || p.node.argument;
    if (property(m) === 'value' && component(m.object, p.scope)) hidden = true;
  };
  traverse(ast, {Function(p) {
    // A stored callback is not a write during value calculation. An invoked
    // IIFE is, and must not be used to hide platform calls.
    if (!calls.has(p.parent.type) || p.parent.callee !== p.node) p.skip();
  }, CallExpression: detect, OptionalCallExpression: detect,
    AssignmentExpression: assignment, UpdateExpression: assignment});
  return hidden;
}
function alwaysBoolean(n) {
  if (n.type === 'ConditionalExpression') return alwaysBoolean(n.consequent) && alwaysBoolean(n.alternate);
  if (n.type === 'CallExpression' && property(n.callee) === 'includes' && n.callee.object.type === 'ArrayExpression') return true;
  return n.type === 'BooleanLiteral' || (n.type === 'BinaryExpression' && comparators.has(n.operator)) ||
    (n.type === 'UnaryExpression' && n.operator === '!');
}
function nativeReady(n) {
  if (n.type === 'LogicalExpression' && ['&&','||'].includes(n.operator)) {
    const left = nativeReady(n.left), right = nativeReady(n.right);
    return left && right ? left + right : 0;
  }
  // A boolean leaf can be one parenthesized native target. The generator joins
  // child filters with &&/||, preserving guarded evaluation.
  return alwaysBoolean(n) ? 1 : 0;
}
function branchCount(n) {
  if (n.type === 'LogicalExpression') return branchCount(n.left) + branchCount(n.right);
  if (n.type === 'ConditionalExpression') return branchCount(n.consequent) + branchCount(n.alternate);
  return 1;
}
function inspect(job) {
  const row = (rule, status, reason) => ({rule, status, reason, group_key:job.group_key,
    node_key:job.node_key, field_path:job.field_path});
  try {
    const {node, ast} = parse(job.code);
    if (job.value_expression && hiddenWrite(ast)) return row('hidden_platform_write', 'failed',
      '变量值里包含平台写入。改用等价的原生赋值或调用元件，保留原分支和执行顺序。');
    const count = branchCount(node);
    if (job.wrapper && count >= 3) {
      let moved = false;
      if (job.previous_predicate) {
        try { moved = canonical(node) === canonical(expression(job.previous_predicate)); } catch {}
      }
      if (moved || nativeReady(node) >= 3) return row('wrapped_single_use_predicate', 'failed',
        '相邻 IF 只读取这个变量，复合判断仍藏在初值。把可等价的条件放回画布，保留短路顺序。');
    }
    if (count >= 4 || job.code.length >= 200) return row('complex_expression', 'warning',
      '此表达式含多个判断或较长计算；优先拆出可等价的原生条件，说明必须保留的计算。');
  } catch {
    return row('expression_unverified', 'warning', '表达式无法静态分析；核对原生等价性和执行顺序。');
  }
  return null;
}

const jobs = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(jobs.map(inspect).filter(Boolean)));
