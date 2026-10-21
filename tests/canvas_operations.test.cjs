'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {applyOperations, componentContract} = require('../mcp/gxp_editor/canvas_operations.cjs');
const {adapt} = require('../mcp/gxp_editor/compiler_runner.cjs');
const catalog = [{type:'Button', title:'按钮', event:[{type:'click', title:'点击', params:[{name:'event',type:'input',dataType:'object',title:'点击事件'}]}]}];
const design = () => ({actionData:[{key:'main',title:'初始化',actionType:'js',data:[]}],globalVariables:[]});
const create = () => ({operation:'create_action_group',group_alias:'handler',group_title:'点击回调',action_type:'js',event_contract:{component_type:'Button',event_name:'click'}});
const bind = () => ({operation:'bind_component_event',action_group_key:'main',component_id:'button1',component_type:'Button',event_name:'click',target_action_group_key:'@handler'});
let sequence = 0;
const api = {
  catalog:() => catalog,
  actionByKey:(d,key) => {const g=d.actionData.find(g=>g.key===key);if(!g)throw Error('group not found');return g;},
  nodeIndex:(g,key) => {const i=g.data.findIndex(n=>n.key===key);if(i<0)throw Error('node not found');return i;},
  elementFor:key => ({levelMarker:key==='IfCondition',endMarker:key==='IfEnd'}),
  blockRange:(g,start) => {
    let depth=0;
    for(let end=start;end<g.data.length;end++) {
      if(g.data[end].elementKey==='IfCondition')depth++;
      if(g.data[end].elementKey==='IfEnd')depth--;
      if(depth===0)return [start,end];
    }
    throw Error('unclosed block');
  },
  generateNodes:(g,key,input,output) => {
    if (key==='Unknown') throw Error('unknown native element');
    if (key==='IfCondition') return [
      {key:'node'+ ++sequence,elementKey:key,paramsValue:{inputParams:input||{},outputParams:output||{}}},
      {key:'node'+ ++sequence,elementKey:'IfEnd',paramsValue:{inputParams:{},outputParams:{}}}];
    return [{key:'node'+ ++sequence,elementKey:key,title:key,paramsValue:{inputParams:input||{},outputParams:output||{}}}];
  },
};
const apply = (d,ops,native=()=>{throw Error('unexpected native call');}) => applyOperations(d,ops,native,api);
test('creates native-shaped callback declarations, appends without anchor, binds generated alias',()=>{
  const d=design();const ops=[create(),{operation:'append_node',action_group_key:'@handler',element_key:'OutputLog'},bind()];
  const original=structuredClone(ops);const diff=apply(d,ops);
  const group=d.actionData[1];assert.match(group.key,/^m[0-9a-f]{8}$/);
  assert.deepEqual(group.inputParams,[{key:'event',title:'event',type:'object',description:'点击事件'}]);
  assert.equal(group.data.length,1);assert.equal(d.actionData[0].data[0].paramsValue.inputParams.action.value,group.key);
  assert.equal(d.actionData[0].data[0].paramsValue.inputParams.name.value,'componentsVariable-button1');
  assert.deepEqual(diff.map(x=>x.operation_index),[0,1,2]);assert.deepEqual(ops,original);
});
test('manual JS and C# group declarations get generated keys',()=>{
  for(const type of ['js','csharp']){const d=design();apply(d,[{operation:'create_action_group',group_alias:'h',group_title:'新分组',action_type:type,input_params:[{title:'recordId',type:'string'}]}]);
    assert.match(d.actionData[1].inputParams[0].key,/^p[0-9a-f]{8}$/);assert.equal(d.actionData[1].data.length,0);}
});
test('unknown or reused aliases and duplicate group titles fail',()=>{
  assert.throws(()=>apply(design(),[{operation:'append_node',action_group_key:'@missing',element_key:'OutputLog'}]),/Unknown action group alias/);
  const d=design();assert.throws(()=>apply(d,[create(),create()]),/duplicate group_alias/);
  assert.throws(()=>apply(design(),[{...create(),group_title:'初始化'}]),/already exists/);
});
test('rejects generated code and unrecognized metadata fields',()=>{
  for(const extra of [{js_code:'x'},{key:'owned'},{depth:[]},{sql:'x'},{raw_code:'x'},{unexpected:true}])assert.throws(()=>apply(design(),[{...create(),...extra}]),/forbidden|Unsupported field/);
});
test('event must exist in unique native catalog; signature must match target',()=>{
  const d=design();apply(d,[create()]);assert.throws(()=>apply(d,[{...bind(),event_name:'fake',target_action_group_key:d.actionData[1].key}]),/Unknown or ambiguous component event/);
  d.actionData[1].inputParams=[];
  assert.throws(()=>apply(d,[{...bind(),target_action_group_key:d.actionData[1].key}]),/does not match/);
  assert.throws(()=>componentContract([...catalog,...catalog],'Button'),/ambiguous/);
});
test('binding rejects C# source, C# callback and self target',()=>{
  for(const which of ['source','callback','self']){const d=design();apply(d,[create()]);const op={...bind(),target_action_group_key:d.actionData[1].key};
    if(which==='source')d.actionData[0].actionType='csharp';if(which==='callback')d.actionData[1].actionType='csharp';if(which==='self')op.target_action_group_key='main';
    assert.throws(()=>apply(d,[op]),/JS/);}
});
test('repeat binding fails; explicit replacement preserves original node key and depth',()=>{
  const d=design();apply(d,[create(),bind()]);const node=d.actionData[0].data[0];node.depth=['parent'];
  const op={...bind(),target_action_group_key:d.actionData[1].key};assert.throws(()=>apply(d,[op]),/already bound/);
  apply(d,[{...op,target_node_key:node.key}]);assert.equal(d.actionData[0].data.length,1);
  assert.equal(d.actionData[0].data[0].key,node.key);assert.deepEqual(d.actionData[0].data[0].depth,['parent']);
});
test('legacy native edits preserve their payload with alias resolution and global diff index',()=>{
  const d=design();let received;const native=(candidate,ops)=>{received=ops[0];return [{operation_index:0,operation:ops[0].operation}];};
  const diff=apply(d,[create(),{operation:'replace_params',action_group_key:'@handler',target_node_key:'existing',input_params:{x:1}}],native);
  assert.equal(received.action_group_key,d.actionData[1].key);assert.deepEqual(received.input_params,{x:1});assert.equal(diff[1].operation_index,1);
});
test('binding can be inserted at an explicit before/after anchor',()=>{
  const d=design();d.actionData[0].data=[{key:'anchor',elementKey:'OutputLog'}];apply(d,[create(),{...bind(),anchor_node_key:'anchor',position:'before'}]);
  assert.equal(d.actionData[0].data[0].elementKey,'ComponentEvent');
  assert.throws(()=>apply(design(),[create(),{...bind(),position:'before'}]),/requires an anchor/);
});
test('unsupported native bundle format fails without executing source',()=>{
  assert.throws(()=>adapt('process.exit(0)'),/Unsupported compiler bundle shape|catalog/);
});

test('incomplete output-only catalog contracts are inspectable but cannot bind',()=>{
  const incomplete=[{type:'Missing',title:'待核实',event:[{type:'before',title:'点击前',params:[{name:'returnValue',type:'output',dataType:'boolean'}]}]}];
  assert.equal(componentContract(incomplete,'Missing').events[0].signature_status,'incomplete');
  const other={...api,catalog:()=>incomplete};
  assert.throws(()=>applyOperations(design(),[{...create(),event_contract:{component_type:'Missing',event_name:'before'}}],()=>{},other),/Incomplete event parameter contract/);
});
test('positional input name is flexible while type and output names stay strict',()=>{
  const d=design();apply(d,[{operation:'create_action_group',group_alias:'handler',group_title:'自定义名称',action_type:'js',input_params:[{title:'payload',type:'object'}]},bind()]);
  assert.equal(d.actionData[1].inputParams[0].title,'payload');
  d.actionData[1].inputParams[0].type='string';
  assert.throws(()=>apply(d,[{...bind(),target_action_group_key:d.actionData[1].key,target_node_key:d.actionData[0].data[0].key}]),/does not match/);
});

test('new blocks accept ordered children and nested blocks through batch-local aliases',()=>{
  const d=design();
  const ops=[create(),
    {operation:'append_node',action_group_key:'@handler',element_key:'IfCondition',node_alias:'outer'},
    {operation:'insert_in_block',action_group_key:'@handler',target_node_key:'@outer',element_key:'DataFilter'},
    {operation:'insert_in_block',action_group_key:'@handler',target_node_key:'@outer',element_key:'IfCondition',node_alias:'inner'},
    {operation:'insert_in_block',action_group_key:'@handler',target_node_key:'@inner',element_key:'DataFilter'},
    {operation:'insert_in_block',action_group_key:'@handler',target_node_key:'@outer',element_key:'OutputLog'}];
  const before=structuredClone(ops),diff=apply(d,ops),nodes=d.actionData[1].data;
  assert.deepEqual(nodes.map(n=>n.elementKey),['IfCondition','DataFilter','IfCondition','DataFilter','IfEnd','OutputLog','IfEnd']);
  assert.equal(diff[2].target_node_key,nodes[0].key);
  assert.equal(diff[4].target_node_key,nodes[2].key);
  assert.equal(diff[1].node_alias,'outer');
  assert.deepEqual(ops,before);
  assert.equal(JSON.stringify(d).includes('node_alias'),false);
  assert.throws(()=>apply(d,[{operation:'insert_in_block',action_group_key:d.actionData[1].key,target_node_key:'@outer',element_key:'OutputLog'}]),/Unknown or forward/);
});

test('block insertion rejects non-block targets, closing nodes, unclosed blocks and fabricated keys',()=>{
  for(const kind of ['OutputLog','IfEnd','IfCondition']) {
    const d=design();d.actionData[0].data=[{key:'existing',elementKey:kind}];
    assert.throws(()=>apply(d,[{operation:'insert_in_block',action_group_key:'main',target_node_key:'existing',element_key:'DataFilter'}]),/opening node|unclosed/);
  }
  assert.throws(()=>apply(design(),[{operation:'append_node',action_group_key:'main',element_key:'IfCondition',node_alias:'block',key:'fake'}]),/forbidden/);
});

test('node aliases reject invalid declarations, forward references, cross-group references and stale nodes',()=>{
  const add={operation:'append_node',action_group_key:'main',element_key:'IfCondition',node_alias:'block'};
  const child={operation:'insert_in_block',action_group_key:'main',target_node_key:'@block',element_key:'DataFilter'};
  for(const alias of ['',null,'@bad','has space',123])assert.throws(()=>apply(design(),[{...add,node_alias:alias}]),/node_alias/);
  assert.throws(()=>apply(design(),[add,add]),/node_alias/);
  assert.throws(()=>apply(design(),[child,add]),/Unknown or forward/);
  assert.throws(()=>apply(design(),[add,create(),{...child,action_group_key:'@handler'}]),/different action group/);
  const remove=(d,ops)=>{d.actionData[0].data=[];return [{operation:ops[0].operation,generated_nodes:[]}];};
  assert.throws(()=>apply(design(),[add,{operation:'delete_block',action_group_key:'main',target_node_key:'@block'},child],remove),/no longer identifies/);
});

test('insertions can declare aliases, and later anchors resolve to native keys without forwarding alias metadata',()=>{
  const d=design();d.actionData[0].data=[{key:'existing',elementKey:'OutputLog'}];
  const native=(candidate,ops)=>{
    const op=ops[0],group=candidate.actionData[0];
    assert.equal(Object.hasOwn(op,'node_alias'),false);
    const generated=api.generateNodes(group,op.element_key,op.input_params,op.output_params);
    group.data.splice(api.nodeIndex(group,op.anchor_node_key)+(op.operation==='insert_after'?1:0),0,...generated);
    return [{operation:op.operation,anchor_node_key:op.anchor_node_key,generated_nodes:generated.map(n=>({key:n.key,element_key:n.elementKey}))}];
  };
  const diff=apply(d,[
    {operation:'insert_before',action_group_key:'main',anchor_node_key:'existing',element_key:'IfCondition',node_alias:'block'},
    {operation:'insert_after',action_group_key:'main',anchor_node_key:'@block',element_key:'DataFilter',node_alias:'filter'},
    {operation:'insert_after',action_group_key:'main',anchor_node_key:'@filter',element_key:'OutputLog'}],native);
  assert.deepEqual(d.actionData[0].data.map(n=>n.elementKey),['IfCondition','DataFilter','OutputLog','IfEnd','OutputLog']);
  assert.equal(diff[2].anchor_node_key,d.actionData[0].data[1].key);
});
