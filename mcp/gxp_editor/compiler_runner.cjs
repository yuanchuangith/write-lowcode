'use strict';
// Extend only the recognized compiler dispatch in memory. The frontend bundle stays unchanged.
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const extensionPath = path.join(__dirname, 'canvas_operations.cjs');
const checksPath = path.join(__dirname, 'event_binding_checks.cjs');
const crypto = require('node:crypto');
let compilationSeed = null;
let compilationCounter = 0;
function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key=>JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  return JSON.stringify(value);
}
function reproducibleCompile(design, compile) {
  const previousSeed = compilationSeed, previousCounter = compilationCounter;
  compilationSeed = crypto.createHash('sha256').update(canonical(design)).digest();
  compilationCounter = 0;
  try { return compile(); } finally { compilationSeed = previousSeed; compilationCounter = previousCounter; }
}
function uuidBytes(size, fallback) {
  if (!compilationSeed) return fallback();
  const parts = [];
  for (let remaining=size; remaining>0; remaining-=32) {
    parts.push(crypto.createHash('sha256').update(compilationSeed).update(':' + compilationCounter++).digest());
  }
  return Buffer.concat(parts).subarray(0, size);
}
function adapt(source) {
  const replaceOnce = (needle, replacement) => {
    if (source.split(needle).length !== 2) throw new Error('Unsupported compiler bundle shape: ' + needle);
    source = source.replace(needle, () => replacement);
  };
  // Make only compile-time UUIDs reproducible. Canvas node keys keep native random UUIDs.
  // No generated source or business expression is rewritten.
  replaceOnce('const bytes = crypto.randomBytes(size);',
    'const bytes = require(' + JSON.stringify(__filename) + ').uuidBytes(size, () => crypto.randomBytes(size));');
  replaceOnce('function compileCode(design) {',
    'function compileCode(design) { return require(' + JSON.stringify(__filename) + ').reproducibleCompile(design, () => nativeCompileCode(clone(design))); }\nfunction nativeCompileCode(design) {');
  // Locate webpack's actual exported component catalog, independent of its numeric module ID.
  const catalogExport = /([A-Za-z_$][\w$]*):\s*\(\) => \(\/\* binding \*\/ extractComponentsInfo\)/g;
  const matches = [...source.matchAll(catalogExport)];
  if (matches.length !== 1) throw new Error('Compiler component catalog export is missing or ambiguous');
  const headers = [...source.slice(0, matches[0].index).matchAll(/\/\*\*\*\/ (\d+):/g)];
  if (!headers.length) throw new Error('Compiler component catalog module was not found');
  const catalog = '__webpack_require__(' + headers.at(-1)[1] + ')[' + JSON.stringify(matches[0][1]) + ']';
  const api = '{actionByKey, nodeIndex, generateNodes, frontendRoot: ' + JSON.stringify(process.cwd()) + ', catalog: () => ' + catalog + '}';
  const checks = 'require(' + JSON.stringify(checksPath) + ')';
  const ext = 'require(' + JSON.stringify(extensionPath) + ')';
  replaceOnce('function applyOperations(design, operations) {',
    'function applyOperations(design, operations) { return ' + ext + '.applyOperations(design, operations, nativeApplyOperations, ' + api + '); }\nfunction nativeApplyOperations(design, operations) {');
  replaceOnce('error: error instanceof Error ? error.message : String(error),',
    'error: error instanceof Error ? error.message : String(error), details: error && error.details,');
  replaceOnce('function executeCompilerCommand(payload) {',
    'function executeCompilerCommand(payload) {\n' +
    "if (payload && payload.command === 'describe_component_contract') return {command: payload.command, component: " + ext + '.componentContract(' + catalog + ', payload.component_type, ' + api + ')};\n' +
    "if (payload && payload.command === 'compile_design') { const validation = validateDesign(payload.design).validation; return Object.assign({command: payload.command, validation}, compileCode(payload.design)); }\n" +
    "if (payload && payload.command === 'compile_patch') { const result = compilePatch(payload.design, payload.operations); result.no_change = result.diff.length > 0 && result.diff.every(item => item.no_change === true); " +
    checks + '.assertJavascriptSyntax(result.js_code); result.event_binding_validation = ' + checks + '.validateBindings(result.design, ' + checks + '.collectScope(payload.design, result.design), ' + api + ', ' + ext + '.eventContract, result.js_code); return result; }\n' +
    "if (payload && payload.command === 'validate_event_bindings') { " + checks + '.assertJavascriptSyntax(payload.js_code); return {command: payload.command, event_binding_validation: ' + checks + '.validateBindings(payload.design, payload.baseline ? ' + checks + '.collectScope(payload.baseline, payload.design) : payload.binding_node_keys, ' + api + ', ' + ext + '.eventContract, payload.js_code)}; }\n' +
    "if (payload && payload.command === 'verify_candidate') { const validation = validateDesign(payload.design).validation; " + checks + '.assertJavascriptSyntax(payload.js_code); const event_binding_validation = ' + checks + '.validateBindings(payload.design, payload.baseline ? ' + checks + '.collectScope(payload.baseline, payload.design) : payload.binding_node_keys, ' + api + ', ' + ext + '.eventContract, payload.js_code); return Object.assign({command:payload.command, validation, event_binding_validation}, compileCode(payload.design)); }\n');
  return source;
}
function main() {
  const bridge = path.resolve(process.argv[2]);
  const compiler = new Module(bridge, module);
  compiler.filename = bridge;
  compiler.paths = Module._nodeModulePaths(path.dirname(bridge));
  compiler._compile(adapt(fs.readFileSync(bridge, 'utf8')), bridge);
}
module.exports = {adapt, reproducibleCompile, uuidBytes};
if (require.main === module) {
  try { main(); } catch (error) {
    process.stdout.write(JSON.stringify({ok:false, error:error.message, details:error.details}), () => process.exit(1));
  }
}
