'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {applyOperations, operations} = require('../mcp/gxp_editor/canvas_operations.cjs');

function design() {
  return {code:'GroupParameterRenameTest',globalVariables:[],actionData:[{
    key:'formatRows',title:'格式化列',actionType:'js',description:'Preserve this group',
    inputParams:[{key:'rowInput',title:'rowData ',type:'array',required:true,
      description:'Rows to format',extension:{nativeFlag:true}},
      {key:'otherInput',title:'options',type:'object',required:false}],
    outputParams:[{key:'result',title:'result',type:'array'}],
    data:[{key:'existingNode',elementKey:'ExitAction',depth:[],paramsValue:{
      inputParams:{returnValue:{paramTypes:'custom',value:'rowData',code:'rowData',label:'rowData',dataType:'array'}},
      outputParams:{}}}],
  },{key:'untouched',title:'Other action',actionType:'js',inputParams:[],data:[]}]};
}
const operation = (extra={}) => ({operation:'rename_group_input_parameter',action_group_key:'formatRows',
  parameter_key:'rowInput',expected_old_name:'rowData ',new_name:'rowData',...extra});
const api = {actionByKey:(canvas,key)=>{
  const group=canvas.actionData.find(item=>item.key===key);
  if(!group)throw Error('group missing');return group;
}};
const apply = (canvas,ops) => applyOperations(canvas,ops,()=>{
  throw Error('Renaming a declaration must not regenerate or replace canvas nodes');
},api);

test('advertises the guarded group input parameter rename operation',()=>{
  assert.ok(operations.includes('rename_group_input_parameter'));
});

test('repairs rowData trailing whitespace while preserving array metadata and every other canvas value',()=>{
  const canvas=design(),expected=structuredClone(canvas),request=operation(),original=structuredClone(request);
  expected.actionData[0].inputParams[0].title='rowData';
  const diff=apply(canvas,[request]);
  assert.deepEqual(canvas,expected);
  assert.deepEqual(request,original);
  assert.equal(diff.length,1);
  assert.equal(diff[0].operation,'rename_group_input_parameter');
  assert.equal(diff[0].action_group_key,'formatRows');
});

test('uses the native parameter key after declaration rows have been reordered',()=>{
  const canvas=design();canvas.actionData[0].inputParams.reverse();
  const expected=structuredClone(canvas);expected.actionData[0].inputParams[1].title='rowData';
  apply(canvas,[operation()]);assert.deepEqual(canvas,expected);
});

test('supports ordinary valid input names without rewriting expressions or other references',()=>{
  const canvas=design();canvas.actionData[0].inputParams[0].title='rowData';
  const oldNodes=structuredClone(canvas.actionData[0].data);
  apply(canvas,[operation({expected_old_name:'rowData',new_name:'rows'})]);
  assert.equal(canvas.actionData[0].inputParams[0].title,'rows');
  assert.deepEqual(canvas.actionData[0].data,oldNodes);
});

test('checks precise old names and rejects ambiguous or missing parameter and group identities without mutation',()=>{
  const cases=[
    [()=>{}, {expected_old_name:'rowData'}],
    [()=>{}, {parameter_key:'missing'}],
    [canvas=>canvas.actionData[0].inputParams.push({...canvas.actionData[0].inputParams[0]}), {}],
    [canvas=>canvas.actionData.push(structuredClone(canvas.actionData[0])), {}],
    [()=>{}, {action_group_key:'missing'}],
    [canvas=>{delete canvas.actionData[0].inputParams;}, {}],
    [canvas=>{canvas.actionData[0].inputParams={rowInput:{title:'rowData '}};}, {}],
    [canvas=>{canvas.actionData[0].actionType='csharp';}, {}],
    [()=>{}, {new_name:'options'}],
  ];
  for(const [prepare,extra] of cases){
    const canvas=design();prepare(canvas);const before=structuredClone(canvas);
    assert.throws(()=>apply(canvas,[operation(extra)]));
    assert.deepEqual(canvas,before);
  }
});

test('rejects invalid identifiers, platform names and code or declaration replacement fields',()=>{
  for(const new_name of ['', 'rowData ', 'row-data', '1row', 'row.data', 'inbiz', 'Inbiz', '__proto__', 'constructor', 'prototype', 'return']){
    const canvas=design(),before=structuredClone(canvas);
    assert.throws(()=>apply(canvas,[operation({new_name})]),new_name);
    assert.deepEqual(canvas,before);
  }
  for(const extra of [{type:'string'},{input_params:[]},{js_code:'injected'},{value:{title:'other'}}]){
    assert.throws(()=>apply(design(),[operation(extra)]));
  }
});

test('sequential renames compare the immediately preceding declaration and retain operation indexes',()=>{
  const canvas=design();
  const diff=apply(canvas,[operation(),operation({expected_old_name:'rowData',new_name:'rows'})]);
  assert.equal(canvas.actionData[0].inputParams[0].title,'rows');
  assert.deepEqual(diff.map(item=>item.operation_index),[0,1]);
  assert.throws(()=>apply(design(),[operation(),operation({new_name:'rows'})]));
});

test('a checked no-op retains the complete canvas and still rejects a stale guard',()=>{
  const canvas=design();canvas.actionData[0].inputParams[0].title='rowData';
  const before=structuredClone(canvas);
  const diff=apply(canvas,[operation({expected_old_name:'rowData'})]);
  assert.deepEqual(canvas,before);assert.equal(diff[0].no_change,true);
  assert.throws(()=>apply(canvas,[operation()]));
});
