'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {before, after, entries} = JSON.parse(fs.readFileSync(0, 'utf8'));
const factories = [before, after].map(source => vm.runInNewContext('(' + source + ')', {}, {timeout:1000}));
const cases = [
  [{}, true], [{mode:'edit'}, true], [{processId:'review:15'}, false], [{processId:'other:16'}, false],
  [{processId:'review:x'}, false], [{processId:''}, false], [{mode:'edit', task:'approve'}, false],
  [{mode:'edit', source:'other'}, false], [{mode:'edit', status:'closed'}, false], [{execution:'none'}, false],
  [{missingReviser:true}, false], [{reviser:['existing']}, false], [{reviser:'owner'}, false], [{reviser:' [] '}, true],
  [{reviser:' null '}, true], [{reviser:null}, true], [{reviser:false}, true], [{reviser:'\t'}, true],
  [{missingUser:true}, false], [{user:'  '}, false], [{user:0}, false], [{user:'  alice  '}, true],
];

async function observe(factory, entry, params) {
  const trace = [];
  const writes = [];
  const person = {value:params.reviser === undefined ? [] : params.reviser, async setValueToID(value) {
    trace.push('write');
    await Promise.resolve();
    writes.push(JSON.parse(JSON.stringify(value)));
    trace.push('write-complete');
  }};
  const fields = {FlowStatus:{value:params.status ?? 'inprocess'}, ExecMode:{value:params.execution ?? 'revised'},
    Reviser:params.missingReviser ? undefined : person,
    UserId:params.missingUser ? undefined : {value:params.user ?? 'initiator'}};
  const inbiz = id => (trace.push('field:' + id), fields[id]);
  inbiz.getPageMode = () => (trace.push('page-mode'), params.mode ?? 'add');
  inbiz.queryData = {get processId() {trace.push('process-id'); return params.processId ?? 'review:16';},
    get source() {trace.push('source'); return params.source ?? 'inbox';}};
  const actions = factory(inbiz, {getWorkFlowInfo: (_inbiz, field) => (trace.push('workflow:' + field), params.task ?? 'firstTask')});
  await actions[entry]();
  return {trace,writes};
}

(async () => {
  for (const entry of entries) for (const [params, shouldWrite] of cases) {
    const observations = await Promise.all(factories.map(factory => observe(factory, entry, params)));
    assert.deepEqual(observations[1], observations[0], JSON.stringify({entry,params}));
    const {trace,writes} = observations[1];
    assert.deepEqual(writes, shouldWrite ? [[{memberType:'user',memberIds:[String(params.user ?? 'initiator').trim()]}]] : []);
    if (!params.mode || params.mode === 'add') assert.ok(!trace.includes('workflow:activityNo'));
    if (shouldWrite) assert.deepEqual(trace.slice(-2), ['write','write-complete']);
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
