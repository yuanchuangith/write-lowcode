'use strict';
// Extend only the recognized compiler dispatch in memory. The frontend bundle stays unchanged.
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const extensionPath = path.join(__dirname, 'canvas_operations.cjs');
function adapt(source) {
  const replaceOnce = (needle, replacement) => {
    if (source.split(needle).length !== 2) throw new Error('Unsupported compiler bundle shape: ' + needle);
    source = source.replace(needle, () => replacement);
  };
  // Locate webpack's actual exported component catalog, independent of its numeric module ID.
  const catalogExport = /([A-Za-z_$][\w$]*):\s*\(\) => \(\/\* binding \*\/ extractComponentsInfo\)/g;
  const matches = [...source.matchAll(catalogExport)];
  if (matches.length !== 1) throw new Error('Compiler component catalog export is missing or ambiguous');
  const headers = [...source.slice(0, matches[0].index).matchAll(/\/\*\*\*\/ (\d+):/g)];
  if (!headers.length) throw new Error('Compiler component catalog module was not found');
  const catalog = '__webpack_require__(' + headers.at(-1)[1] + ')[' + JSON.stringify(matches[0][1]) + ']';
  const api = '{actionByKey, nodeIndex, generateNodes, catalog: () => ' + catalog + '}';
  const ext = 'require(' + JSON.stringify(extensionPath) + ')';
  replaceOnce('function applyOperations(design, operations) {',
    'function applyOperations(design, operations) { return ' + ext + '.applyOperations(design, operations, nativeApplyOperations, ' + api + '); }\nfunction nativeApplyOperations(design, operations) {');
  replaceOnce('function executeCompilerCommand(payload) {',
    'function executeCompilerCommand(payload) {\n' +
    "if (payload && payload.command === 'describe_component_contract') return {command: payload.command, component: " + ext + '.componentContract(' + catalog + ', payload.component_type)};\n');
  return source;
}
function main() {
  const bridge = path.resolve(process.argv[2]);
  const compiler = new Module(bridge, module);
  compiler.filename = bridge;
  compiler.paths = Module._nodeModulePaths(path.dirname(bridge));
  compiler._compile(adapt(fs.readFileSync(bridge, 'utf8')), bridge);
}
if (require.main === module) {
  try { main(); } catch (error) {
    process.stdout.write(JSON.stringify({ok:false, error:error.message}));
    process.exitCode = 1;
  }
}
module.exports = {adapt};
