'use strict';
// Rebuild only the offline compiler, using the configured frontend's native rules.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {createRequire} = require('node:module');
if (!process.argv[2]) throw new Error('Usage: node scripts/build_compiler_bridge.cjs <frontend_repo> [output_directory]');
const root = path.resolve(process.argv[2] || '');
const output = path.resolve(process.argv[3] || path.join(root, 'dist/action-design-compiler'));
const native = path.resolve(__dirname, '../mcp/gxp_editor/native_compiler');
const localRequire = createRequire(path.join(root, 'package.json'));
const webpack = localRequire('webpack');
const started = Date.now();
const deadline = setTimeout(() => { process.stderr.write('Compiler rebuild exceeded 120 seconds\n'); process.exit(1); },120000);
deadline.unref();
const rulesRoot = path.join(root, 'src/core/common/ActionDesign');
function files(directory) {
  return fs.readdirSync(directory, {withFileTypes:true}).flatMap(item => {
    const file = path.join(directory, item.name);
    return item.isDirectory() ? files(file) : /\.[cm]?[jt]sx?$/.test(file) ? [file] : [];
  });
}
const rules = files(rulesRoot).filter(file => !file.startsWith(path.join(rulesRoot, 'compiler') + path.sep));
const sourceManifest = rules.sort().map(file => ({path:path.relative(root, file).split(path.sep).join('/'),
  sha256:crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')}));
const ruleHash = crypto.createHash('sha256').update(JSON.stringify(sourceManifest)).digest('hex');
const config = {
  context:root, mode:'production', target:'node', devtool:false, cache:false,
  entry:path.join(native, 'entry.cjs'),
  output:{path:output, filename:'bridge.js', library:{type:'commonjs2'}},
  optimization:{minimize:false},
  resolve:{extensions:['.tsx','.ts','.js','.cjs'], modules:[path.join(root,'node_modules'), 'node_modules'], alias:{
    '@common':path.join(root,'src/core/common'), '@components':path.join(root,'src/core/components'),
    '@assets':path.join(root,'src/assets'), '@inbiz/basic':[path.join(root,'src/basic'),path.join(root,'src/basic/SyncComponents')],
    '@inbiz/antd':path.join(root,'node_modules/antd'),
    '@inbiz/utils$':path.join(native,'utils_shim.cjs'),
    '@inbiz/hooks$':path.join(native,'sdk_shim.cjs'), '@inbiz/render$':path.join(native,'sdk_shim.cjs'),
    '@inbiz/setters$':path.join(native,'sdk_shim.cjs'),
  }},
  resolveLoader:{modules:[path.join(root,'node_modules')]},
  module:{rules:[
    {test:/\.[jt]sx?$/, exclude:/node_modules/, use:{loader:localRequire.resolve('babel-loader'), options:{
      babelrc:false, configFile:false, cacheDirectory:path.join(output,'.babel-cache'),
      presets:[[localRequire.resolve('@umijs/babel-preset-umi'), {
        presetEnv:{targets:{node:'18'},modules:false}, presetReact:{runtime:'automatic'},
        presetTypeScript:{}, pluginTransformRuntime:{}, pluginLockCoreJS:{},
        pluginDynamicImportNode:false, pluginAutoCSSModules:false,
      }]],
    }}},
    {test:/\.(css|less|scss|svg|png|jpe?g|gif|woff2?|ttf|eot)$/,use:path.join(native,'empty_loader.cjs')},
  ]},
  plugins:[new webpack.DefinePlugin({
    __GXP_FRONTEND_VERSION__:JSON.stringify(localRequire('./package.json').version),
    __GXP_RULE_SOURCE_SHA256__:JSON.stringify(ruleHash),
    __GXP_RULE_SOURCE_FILE_COUNT__:JSON.stringify(sourceManifest.length),
    'process.env.NODE_ENV':JSON.stringify('production'),
  }),new webpack.ProvidePlugin({InbizConfig:path.join(native,'config_shim.cjs')})],
};
webpack(config, (error, stats) => {
  clearTimeout(deadline);
  if (error || stats.hasErrors()) {
    process.stderr.write(error ? String(error.stack || error) : stats.toString({all:false,errors:true}));
    process.exitCode=1; return;
  }
  const dependencies = new Set([...rules, path.join(root,'package.json'),path.join(root,'package-lock.json'),
    ...stats.compilation.fileDependencies].filter(file => file.startsWith(root+path.sep) &&
      !path.relative(root,file).split(path.sep).includes('node_modules') && fs.statSync(file).isFile()));
  const dependenciesManifest = [...dependencies].sort().map(file => {
    if (fs.statSync(file).mtimeMs > started) throw new Error('Frontend source changed during build: '+file);
    return {path:path.relative(root,file).split(path.sep).join('/'),
      sha256:crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')};
  });
  const manifest = {schema_version:1,rule_source_sha256:ruleHash,files:dependenciesManifest};
  const bridge = path.join(output,'bridge.js');
  fs.writeFileSync(bridge,'// GXP_COMPILER_SOURCES_V1='+JSON.stringify(manifest)+'\n'+fs.readFileSync(bridge,'utf8'));
  fs.writeFileSync(path.join(output,'source-manifest.json'),JSON.stringify(manifest,null,2)+'\n');
  process.stdout.write(stats.toString({all:false,assets:true,timings:true})+'\n');
});
