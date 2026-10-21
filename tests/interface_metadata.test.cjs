'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {withSnapshot, describe} = require('../mcp/gxp_editor/interface_metadata.cjs');
const node = (id='interface1',requestUrl='https://example.invalid/saved') => ({elementKey:'CallInterface',
  paramsValue:{inputParams:{apiInfo:{setting:{id,requestUrl}}}}});
const design = (...nodes) => ({actionData:[{actionType:'js',data:nodes}]});

test('native metadata lookup reads the saved URL without consulting an existing network implementation',()=>{
  const previous=Object.getOwnPropertyDescriptor(globalThis,'XMLHttpRequest');
  globalThis.XMLHttpRequest=class {constructor(){throw Error('must not use network');}};
  const original=globalThis.XMLHttpRequest;
  try {
    const source=design(node());
    const copy=structuredClone(source);
    withSnapshot(source,()=>{
      const xhr=new XMLHttpRequest();xhr.open('GET','/gxp/api/interfaces/interface1',false);xhr.send(null);
      assert.equal(xhr.status,200);
      assert.deepEqual(JSON.parse(xhr.responseText),{data:[{requestUrl:'https://example.invalid/saved'}]});
    });
    assert.equal(globalThis.XMLHttpRequest,original);
    assert.deepEqual(source,copy);
  } finally {if(previous)Object.defineProperty(globalThis,'XMLHttpRequest',previous);else delete globalThis.XMLHttpRequest;}
});

test('missing/conflicting metadata and all non-metadata HTTP operations fail closed',()=>{
  assert.throws(()=>withSnapshot(design(node('id','')),()=>{}),/requires saved/);
  assert.throws(()=>withSnapshot(design(node('same','first'),node('same','second')),()=>{}),/Conflicting/);
  withSnapshot(design(node()),()=>{
    for(const [method,url,async] of [['POST','/api/interfaces/interface1',false],['GET','/api/interfaces/interface1',true],
      ['GET','/api/interfaces/missing',false],['GET','/api/system/datetime/now',false],['GET','/api/interfaces/interface1?extra=1',false]]) {
      assert.throws(()=>new XMLHttpRequest().open(method,url,async),/network access is unavailable/);
    }
    const request=()=>{const xhr=new XMLHttpRequest();xhr.open('GET','/api/interfaces/interface1',false);return xhr;};
    assert.throws(()=>request().setRequestHeader('X-Test','value'),/network access/);
    assert.throws(()=>request().send('body'),/network access/);
    assert.throws(()=>new XMLHttpRequest().send(null),/network access/);
  });
});

test('metadata scope and descriptor are restored on errors and nested compilation',()=>{
  const previous=Object.getOwnPropertyDescriptor(globalThis,'XMLHttpRequest');
  assert.throws(()=>withSnapshot(design(node()),()=>{
    const outer=globalThis.XMLHttpRequest;
    withSnapshot(design(node('inner','inner-url')),()=>{
      assert.notEqual(globalThis.XMLHttpRequest,outer);
      assert.throws(()=>new XMLHttpRequest().open('GET','/api/interfaces/interface1',false),/network access/);
    });
    assert.equal(globalThis.XMLHttpRequest,outer);
    throw Error('generator failed');
  }),/generator failed/);
  assert.deepEqual(Object.getOwnPropertyDescriptor(globalThis,'XMLHttpRequest'),previous);
});

test('legacy metadata works and evidence is ordered, fingerprinted and explicitly not live',()=>{
  const legacy=node('a','saved-a');legacy.paramsValue.inputParams.apiInfo=legacy.paramsValue.inputParams.apiInfo.setting;
  const first=describe(design(legacy,node('b','saved-b'),node('b','saved-b')));
  assert.deepEqual(first,describe(design(node('b','saved-b'),legacy)));
  assert.equal(first.interface_count,2);
  assert.equal(first.live_metadata_checked,false);
  assert.equal(first.network_access,false);
  assert.match(first.snapshot_sha256,/^[0-9a-f]{64}$/);
  assert.notEqual(first.snapshot_sha256,describe(design(node('a','changed'),node('b','saved-b'))).snapshot_sha256);
});
