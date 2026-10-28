'use strict';
// Runs trusted compiler output against data-only fixtures. This is not an arbitrary-code security sandbox.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const {createRequire} = require('node:module');
const coverageAdapter = require('./execution_coverage.cjs');

function checkEligibility(source, frontendRoot) {
  const load = createRequire(path.join(path.resolve(frontendRoot || process.cwd()), 'package.json'));
  const parser = load('@babel/parser');
  const traverse = load('@babel/traverse').default;
  const ast = parser.parse('(' + source + ')', {sourceType:'script'});
  const allowed = new Set(['JSON','String','Number','Boolean','Object','Array','Promise','Error','TypeError','RangeError','Map','Set','undefined','NaN','Infinity','console','parseInt','parseFloat','isNaN','isFinite','encodeURIComponent','decodeURIComponent']);
  const forbidden = new Set(['constructor','prototype','__proto__','getPrototypeOf','setPrototypeOf','defineProperty','defineProperties','__defineGetter__','__defineSetter__','caller','callee']);
  traverse(ast, {
    ReferencedIdentifier(p) {
      if (!p.scope.hasBinding(p.node.name, true) && !allowed.has(p.node.name)) {
        throw new Error('Unsupported global: ' + p.node.name);
      }
    },
    MemberExpression(p) {
      const property = p.node.computed ? p.node.property.value : p.node.property.name;
      if (p.node.computed && !['StringLiteral','NumericLiteral'].includes(p.node.property.type)) throw new Error('Dynamic property access is outside the supported execution scope');
      if (forbidden.has(property)) throw new Error('Unsupported property: ' + property);
      if (p.node.object.name === 'Math' && property === 'random') throw new Error('Nondeterministic random values are unsupported');
    },
    OptionalMemberExpression(p) {
      const property = p.node.computed ? p.node.property.value : p.node.property.name;
      if (p.node.computed && !['StringLiteral','NumericLiteral'].includes(p.node.property.type)) throw new Error('Dynamic property access is outside the supported execution scope');
      if (forbidden.has(property)) throw new Error('Unsupported property: ' + property);
    },
    Import(p) { throw new Error('Dynamic imports are unsupported'); },
    NewExpression(p) {
      if (!['Error','TypeError','RangeError','Array','Object','Map','Set'].includes(p.node.callee.name)) throw new Error('Unsupported constructor');
    },
    MetaProperty() { throw new Error('Meta properties are unsupported'); },
    ThisExpression() { throw new Error('Dynamic this access is unsupported'); },
    WithStatement() { throw new Error('Dynamic scope access is unsupported'); },
  });
}

// All callbacks, objects and proxies are created inside the context; no host SDK is passed in.
function executeScenario(spec, factory, executionCoverage) {
  const unsupportedReasons = [];
  const unsupported = reason => { unsupportedReasons.push(reason); throw new Error(reason); };
  const clone = (value, seen=new Set()) => {
    if (value === undefined || value === null || typeof value === 'string' || typeof value === 'boolean') return value;
    if (typeof value === 'number' && Number.isFinite(value)) {
      if (Number.isInteger(value) && !Number.isSafeInteger(value)) unsupported('Integer observations outside JavaScript\'s safe range must use strings');
      return value;
    }
    if (typeof value !== 'object') unsupported('Only finite JSON values are supported in scenario observations');
    const prototype = Object.getPrototypeOf(value);
    if (![Object.prototype,Array.prototype,null].includes(prototype)) unsupported('Non-JSON object observation is unsupported');
    if (seen.has(value)) unsupported('Circular scenario observations are unsupported');
    seen.add(value);
    const array = Array.isArray(value), keys = Reflect.ownKeys(value);
    const isIndex = key => typeof key === 'string' && String(Number(key)) === key && Number.isInteger(Number(key)) && Number(key) >= 0 && Number(key) < value.length;
    if (array && keys.filter(isIndex).length !== value.length) unsupported('Sparse array observations are unsupported');
    const result = array ? new Array(value.length) : Object.create(null);
    for (const key of keys) {
      if (typeof key !== 'string') unsupported('Symbol-valued observations are unsupported');
      const descriptor = Object.getOwnPropertyDescriptor(value,key);
      if (descriptor.get || descriptor.set) unsupported('Accessor observations are unsupported');
      if (key === 'toJSON') unsupported('Custom JSON serialization is unsupported');
      if (array && isIndex(key) && !descriptor.enumerable) unsupported('Non-enumerable array elements are unsupported');
      if (descriptor.enumerable) result[key] = clone(descriptor.value,seen);
    }
    seen.delete(value);
    return result;
  };
  const state = {fields:clone(spec.environment?.fields || {}), child_fields:clone(spec.environment?.child_fields || {}),
    page_mode:spec.environment?.page_mode, trace:[], counts:{}, unsupported:unsupportedReasons, metadata:null,
    metadataVersion:0, snapshotVersion:-1, metadataSnapshot:null, lastReturn:undefined};
  const log = (api, detail={}) => {
    if (state.trace.length >= 1000) unsupported('Scenario trace limit reached');
    state.counts[api] = (state.counts[api] || 0) + 1;
    state.trace.push({api, ...clone(detail)});
  };
  for (const level of ['log','warn','error']) console[level] = (...args) => log('console.' + level, {args});
  Object.freeze(console);
  const events = new Map();
  const fieldCache = new Map();
  function field(id, child=false) {
    const section = child ? 'child_fields' : 'fields';
    if (typeof id !== 'string' || !Object.hasOwn(state[section], id)) unsupported('Undeclared ' + section + ' field: ' + String(id));
    const cacheKey = section + ':' + id;
    if (fieldCache.has(cacheKey)) return fieldCache.get(cacheKey);
    const name = (child ? 'child.' : '') + id;
    const proxy = new Proxy(Object.create(null), {
      get(target, property) {
        if (property === 'value') {
          if (!Object.hasOwn(state[section][id], 'value')) unsupported('Undeclared value for ' + name);
          const value = state[section][id].value; log(name + '.value.read', {value}); return value;
        }
        if (property === 'getText' || property === 'getSelectedRows') return () => {
          const key = property === 'getText' ? 'text' : 'selected_rows';
          if (!Object.hasOwn(state[section][id], key)) unsupported('Undeclared ' + key + ' for ' + name);
          const value = clone(state[section][id][key]); log(name + '.' + property, {value}); return value;
        };
        if (!child && (property === 'configEvent' || property === 'on')) return (event, handler) => {
          if (typeof event !== 'string' || typeof handler !== 'function') unsupported('Invalid event registration');
          const key = id + ':' + event;
          if (property === 'on' && events.has(key) && events.get(key) !== handler) unsupported('SDK event composition is unverified');
          events.set(key, handler); log(name + '.' + property, {event});
        };
        unsupported('Unsupported field API: ' + name + '.' + String(property));
      },
      set(target, property, value) {
        if (property !== 'value') unsupported('Unsupported field write: ' + name + '.' + String(property));
        state[section][id].value = clone(value); log(name + '.value.write', {value}); return true;
      },
    });
    fieldCache.set(cacheKey, proxy); return proxy;
  }
  const innerInbiz = id => field(id, true);
  const inbiz = new Proxy(id => field(id), {
    get(target, property) {
      if (property === 'getPageMode') return () => {
        if (state.page_mode === undefined) unsupported('Page mode was not declared');
        log('inbiz.getPageMode', {value:state.page_mode}); return state.page_mode;
      };
      if (property === 'configEvent') return (event, handler) => {
        if (event !== 'getPageAddMetaData' || typeof handler !== 'function') unsupported('Unsupported page event: ' + String(event));
        state.metadata = handler; state.metadataVersion++; log('metadata.register', {event, version:state.metadataVersion});
      };
      unsupported('Unsupported page API: ' + String(property));
    },
  });
  const related = new Proxy(Object.create(null), {get(target, property) {unsupported('Unsupported relatedAttributes API: ' + String(property));}});
  const readMetadata = () => {
    log('metadata.read', {version:state.metadataVersion});
    state.metadataSnapshot = state.metadata ? clone(state.metadata()) : null;
    state.snapshotVersion = state.metadataVersion;
    return state.metadataSnapshot;
  };
  const assertions = [];
  const equal = (actual, expected) => {
    if (actual === expected) return true;
    if (actual === null || expected === null || typeof actual !== 'object' || typeof expected !== 'object') return false;
    if (Array.isArray(actual) !== Array.isArray(expected)) return false;
    if (Array.isArray(actual) && actual.length !== expected.length) return false;
    const left = Object.keys(actual).sort(), right = Object.keys(expected).sort();
    return JSON.stringify(left) === JSON.stringify(right) && left.every(key=>equal(actual[key],expected[key]));
  };
  const check = (name, actual, expected, present=true) => assertions.push({name, passed:present && equal(actual, expected),
    actual_present:present, actual_kind:!present ? 'missing' : actual === undefined ? 'undefined' : actual === null ? 'null' : typeof actual,
    ...(actual === undefined ? {} : {actual:clone(actual)}), expected:clone(expected)});
  const inspect = () => ({fields:clone(state.fields), metadata:clone(state.metadataSnapshot),
    field_value_presence:Object.fromEntries(Object.entries(state.fields).map(([id,item])=>[id,
      !Object.hasOwn(item,'value') ? 'missing' : item.value === undefined ? 'undefined' : item.value === null ? 'null' : 'value'])),
    metadata_registrations:state.metadataVersion, call_counts:clone(state.counts),
    ...(state.lastReturn === undefined ? {last_return_undefined:true} : {last_return:clone(state.lastReturn)}),
    model_assumptions:['metadata:last_registration_wins','events:direct_callback_without_sdk_wrapper']});
  return (async()=>{
    let runtimeError;
    try {
      const self = factory(inbiz, related);
      if (!self || typeof self !== 'object') throw new Error('Generated JavaScript did not return action groups');
      for (let index=0; index<spec.steps.length; index++) {
        const step = spec.steps[index];
        if (step.kind === 'set_inputs') {
          for (const section of ['fields','child_fields']) for (const [id, value] of Object.entries(step[section] || {})) {
            if (!Object.hasOwn(state[section], id)) unsupported('Cannot update undeclared field: ' + id);
            Object.assign(state[section][id], clone(value));
          }
          if (Object.hasOwn(step,'page_mode')) state.page_mode = step.page_mode;
          state.snapshotVersion = -1;
          continue;
        }
        if (step.kind === 'read_metadata') { readMetadata(); continue; }
        const args = clone(step.args || []);
        let fn;
        if (step.kind === 'call_group') {
          fn = self[step.group_key];
          if (typeof fn !== 'function') throw new Error('Action group not found: ' + step.group_key);
          log('group.' + step.group_key, {step:index});
        } else {
          fn = events.get(step.component_id + ':' + step.event_name);
          if (typeof fn !== 'function') throw new Error('Event handler was not registered: ' + step.component_id + '.' + step.event_name);
          if (step.inject_inner_inbiz) {
            if (!args.length || !args[0] || typeof args[0] !== 'object' || Array.isArray(args[0])) unsupported('innerInbiz injection requires an object first argument');
            args[0].innerInbiz = innerInbiz;
          }
          log('event.' + step.component_id + '.' + step.event_name, {step:index});
        }
        state.lastReturn = await fn(...args);
        state.snapshotVersion = -1;
      }
      const expected = spec.expect || {};
      if (Object.hasOwn(expected, 'metadata') && state.snapshotVersion !== state.metadataVersion) readMetadata();
      for (const [id, value] of Object.entries(expected.fields || {})) check('fields.' + id + '.value', state.fields[id]?.value, value.value,
        Object.hasOwn(state.fields,id) && Object.hasOwn(state.fields[id],'value'));
      if (Object.hasOwn(expected,'metadata')) check('metadata',state.metadataSnapshot,expected.metadata);
      if (Object.hasOwn(expected,'metadata_registrations')) check('metadata_registrations',state.metadataVersion,expected.metadata_registrations);
      if (Object.hasOwn(expected,'last_return')) check('last_return',state.lastReturn,expected.last_return);
      if (expected.last_return_undefined) check('last_return_undefined',state.lastReturn === undefined,true);
      for (const [api,count] of Object.entries(expected.call_counts || {})) check('call_counts.' + api,state.counts[api] || 0,count);
      if (Object.hasOwn(expected,'call_order')) check('call_order',state.trace.map(entry=>entry.api),expected.call_order);
    } catch(error) { runtimeError = error.message; }
    const hasExpected = Object.keys(spec.expect || {}).length > 0;
    const status = state.unsupported.length || !hasExpected ? 'unverified' : runtimeError ? 'failed' : !assertions.length ? 'unverified' : assertions.some(item=>!item.passed) ? 'failed' : 'passed';
    return {status, actual:inspect(), trace:state.trace, assertions,
      ...(executionCoverage ? {execution_coverage:executionCoverage} : {}),
      ...(state.unsupported.length ? {reason:state.unsupported[0]} : runtimeError ? {reason:runtimeError} : !assertions.length ? {reason:'No explicit expectations were declared'} : {})};
  })();
}

async function main(request) {
  const hash = crypto.createHash('sha256').update(request.js_code || '', 'utf8').digest('hex');
  let result;
  try {
    if (request.protocol_version !== 1 || request.source_sha256 !== hash || !request.scenario) throw new Error('Invalid scenario protocol');
    checkEligibility(request.js_code, request.frontend_root);
    // The original code is checked before adding a private, unguessable lexical hook.
    // Counts stay separate from business API logs and never affect call_order.
    const hook = '__gxpCoverage_'+crypto.randomBytes(16).toString('hex');
    const instrumented = request.execution_mapping
      ? coverageAdapter.instrument(request.js_code, request.execution_mapping, request.frontend_root, hook) : null;
    if (request.execution_mapping && !instrumented) throw new Error('Invalid native execution mapping');
    const coverage = instrumented ? {mapping_sha256:instrumented.mapping_sha256,
      nodes:instrumented.entries.map(entry=>({group_key:entry.group_key,node_key:entry.node_key,
        element_key:entry.element_key,kind:entry.kind,count:0,true_count:0,false_count:0}))} : null;
    const context = vm.createContext(Object.create(null), {codeGeneration:{strings:false,wasm:false}});
    const bootstrap = 'const console = {log(){},warn(){},error(){}};\n' +
      // Keep the oracle's JSON/Object/Array operations independent of candidate monkey patches.
      '(function(){const names=["Object","Array","Function","String","Number","Boolean","Promise","Map","Set","Error","TypeError","RangeError","JSON","Reflect","RegExp","Symbol","Date","Math","parseInt","parseFloat","isNaN","isFinite","encodeURIComponent","decodeURIComponent"];' +
      'for(const name of names){const value=globalThis[name];if(value && (typeof value==="object" || typeof value==="function")){if(value.prototype)Object.freeze(value.prototype);Object.freeze(value);}Object.defineProperty(globalThis,name,{value,writable:false,configurable:false});}})();\n' +
      '(function(){const receipt=JSON.parse('+JSON.stringify(JSON.stringify(coverage))+');' +
      'const '+hook+'=function(id,value){const node=receipt.nodes[id];if(node.count<1000000){node.count++;if(arguments.length>1){const key=value?"true_count":"false_count";node[key]++;}}return value;};' +
      'return (' + executeScenario.toString() + ')(JSON.parse(' + JSON.stringify(JSON.stringify(request.scenario)) + '),(' +
      (instrumented ? instrumented.source : request.js_code) + '),receipt);})()';
    result = await new vm.Script(bootstrap, {filename:'generated-scenario.js'}).runInContext(context, {timeout:1500});
  } catch(error) {
    result = {status:'unverified', reason:String(error.message).slice(0,500), actual:null, trace:[], assertions:[]};
  }
  const response = JSON.stringify({protocol_version:1,source_sha256:hash,result});
  if (Buffer.byteLength(response) > 2*1024*1024) {
    process.stdout.write(JSON.stringify({protocol_version:1,source_sha256:hash,result:{status:'unverified',reason:'Scenario output exceeded its limit',actual:null,trace:[],assertions:[]}}));
  } else process.stdout.write(response);
}
if (require.main === module) {
  let input='';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data',chunk=>{input+=chunk;if(Buffer.byteLength(input)>4*1024*1024)process.exit(2);});
  process.stdin.on('end',()=>{
    try { main(JSON.parse(input)).catch(()=>process.exit(2)); } catch(error) { process.exitCode=2; }
  });
}
