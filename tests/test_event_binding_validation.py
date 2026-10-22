from __future__ import annotations
import json
import os
import subprocess
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from pathlib import Path
from tests.helpers import editor_config
from gxp_editor.compiler import CompilerBridge, CompilerError


def value(code, data_type="string"):
    return {"paramTypes":"custom", "value":code, "code":code, "label":code, "dataType":data_type}


def variable(name, expression, data_type="string"):
    return {"operation":"append_node", "action_group_key":"@callback", "element_key":"SetVariable",
            "input_params":{"variableType":data_type,"variableValue":value(expression,data_type)},
            "output_params":{"variableName":value(name,data_type)}}


def operations(expression="action.id ?? ''", return_code="true"):
    return [
        {"operation":"create_action_group","group_alias":"callback","group_title":"子窗体自定义按钮点击前","action_type":"js",
         "event_contract":{"component_type":"FormTables","event_name":"onChildrenCustomBeforeClick"}},
        variable("id",expression),
        variable("innerInbiz","action.innerInbiz","any"),
        variable("trainingTopics",'id === "AP5l67Gn" ? (inbiz("trainee_task").getSelectedRows() || []).map(row => row.topic ?? "").join("、") : ""'),
        variable("evaluationTopics",'id === "OjuHwTLK" ? (inbiz("trainee_task").getSelectedRows() || []).map(row => row.topic ?? "").join("、") : ""'),
        variable("evaluationResult",'id === "OjuHwTLK" ? (innerInbiz("assess_status").getText() || "") : ""'),
        {"operation":"append_node","action_group_key":"@callback","element_key":"AddMetaData",
         "input_params":{"config":{"paramSettings":[{"id":name,"key":name,"value":{"paramTypes":"localVariable","value":"localVariable-"+name,"code":name,"label":name,"dataType":"string"}}
                      for name in ("trainingTopics","evaluationTopics","evaluationResult")]}},"output_params":{}},
        {"operation":"append_node","action_group_key":"@callback","element_key":"ExitAction","input_params":{"returnValue":value(return_code,"boolean")},"output_params":{}},
        {"operation":"bind_component_event","action_group_key":"main","component_id":"trainee_task","component_type":"FormTables",
         "event_name":"onChildrenCustomBeforeClick","target_action_group_key":"@callback"},
    ]


@unittest.skipUnless(os.environ.get("GXP_TEST_COMPILER_BRIDGE"),"Native compiler path required")
class EventBindingContractTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ["GXP_TEST_COMPILER_BRIDGE"]).resolve()
        self.bridge = CompilerBridge(replace(editor_config(),frontend_repo=str(path.parents[2]),compiler_bridge=str(path)))
        self.design = {"actionData":[{"key":"main","title":"初始化","actionType":"js","data":[]}],"globalVariables":[]}

    def test_declared_payload_and_metadata_values_in_isolated_vm(self):
        result = self.bridge.compile_patch(self.design,operations())
        self.assertTrue(self.bridge.verify_compiled_result(result)["code_generation_complete"])
        callback = result["design"]["actionData"][1]
        self.assertEqual([(p["title"],p["type"]) for p in callback["inputParams"]],[("action","object")])
        self.assertNotIn("arguments[",result["js_code"])
        evidence=result["event_binding_validation"]
        self.assertEqual(evidence["checks"][0]["signature_status"],"source_verified")
        self.assertFalse(evidence["runtime_verified"])
        script=r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(0,'utf8');const factory=vm.runInNewContext('('+source+')');
let handler,metadata;let rows=[{topic:'培训甲'},{topic:'培训乙'}];
const table={getSelectedRows:()=>rows,configEvent:(name,fn)=>{assert.equal(name,'onChildrenCustomBeforeClick');handler=fn;},on:(name,fn)=>assert.equal(fn,handler)};
const inbiz=id=>{assert.equal(id,'trainee_task');return table;};inbiz.configEvent=(name,fn)=>{assert.equal(name,'getPageAddMetaData');metadata=fn;};
const self=factory(inbiz,{});
// Match OperationButtonClick's actual one-object callsite, including callable innerInbiz.
const child=id=>{assert.equal(id,'assess_status');return {getText:()=> '合格'};};
(async()=>{await self.main();
for(const [id,expected] of [['AP5l67Gn',{trainingTopics:'培训甲、培训乙',evaluationTopics:'',evaluationResult:''}],['OjuHwTLK',{trainingTopics:'',evaluationTopics:'培训甲、培训乙',evaluationResult:'合格'}],['unrelated',{trainingTopics:'',evaluationTopics:'',evaluationResult:''}]]){
 const allowed=await handler({id,action:'custom',name:'按钮',innerInbiz:child});assert.equal(allowed,true);assert.equal(typeof allowed,'boolean');
 const actual=Object.fromEntries(metadata().map(item=>[item.key,item.value]));assert.deepEqual(actual,expected);
}
rows=[];await handler({id:'AP5l67Gn',action:'custom',name:'按钮',innerInbiz:child});assert.equal(metadata()[0].value,'');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        ran=subprocess.run(["node","-e",script],input=result["js_code"],encoding="utf-8",capture_output=True,timeout=15)
        self.assertEqual(ran.returncode,0,ran.stderr)
        # Approval rechecks the scope derived from actual before/after canvases.
        desired={key:result[key] for key in ("js_code","csharp_code","relation_data")}
        verified=self.bridge.verify_candidate_artifact(result["design"],desired,baseline_design=self.design)
        self.assertEqual(verified["event_binding_validation"],evidence)

    def test_hidden_arguments_are_rejected_even_with_declared_context(self):
        with self.assertRaisesRegex(CompilerError,"Hidden event arguments"):
            self.bridge.compile_patch(self.design,operations('arguments[0]?.id ?? ""'))

    def test_existing_in_flight_wrapper_is_not_mistaken_for_callback_declaration(self):
        original = self.bridge.compile_patch(self.design, operations())
        key = original["design"]["actionData"][1]["key"]
        wrapper = """(() => {
            self.KEY = (function(original) {
                let pending;
                return function(action) {
                    if (pending) return pending;
                    pending = Promise.resolve().then(() => original.call(this, action)).finally(() => { pending = null; });
                    return pending;
                };
            })(self.KEY);
            return true;
        })()""".replace("KEY", key)
        operation = variable("wrapped", wrapper, "boolean")
        operation["action_group_key"] = "main"
        compiled = self.bridge.compile_patch(original["design"], [operation])
        evidence = self.bridge.verify_compiled_result(compiled)
        self.assertTrue(evidence["generated_code_consistency_verified"])
        desired = {name: compiled[name] for name in ("js_code", "csharp_code", "relation_data")}
        self.bridge.verify_candidate_artifact(compiled["design"], desired, baseline_design=original["design"])
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
let metadataWrites=0,fail=false;
const table={getSelectedRows:()=>{if(fail)throw new Error('generation failed');return [{topic:'培训甲'}];},configEvent:()=>{},on:()=>{}};
const inbiz=()=>table;inbiz.configEvent=()=>{metadataWrites++;};
const self=vm.runInNewContext('('+fs.readFileSync(0,'utf8')+')')(inbiz,{});
(async()=>{await self.main();const action={id:'AP5l67Gn',innerInbiz:()=>{}};
 const first=self.KEY(action),same=self.KEY(action);assert.equal(first,same);assert.equal(await first,true);assert.equal(metadataWrites,1);
 assert.equal(await self.KEY(action),true);assert.equal(metadataWrites,2);
 fail=true;await assert.rejects(self.KEY(action),/generation failed/);fail=false;
 assert.equal(await self.KEY(action),true);assert.equal(metadataWrites,3);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''.replace("KEY", key)
        ran = subprocess.run(["node", "-e", script], input=compiled["js_code"], encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_undeclared_parameter_read_is_rejected(self):
        with self.assertRaisesRegex(CompilerError,"Undeclared callback identifier missingContext"):
            self.bridge.compile_patch(self.design,operations('missingContext.id'))

    def test_native_output_object_is_preserved_without_inventing_sdk_unwrap(self):
        ops=operations();ops.pop(7)
        result=self.bridge.compile_patch(self.design,ops)
        self.assertIn("return outputParams;",result["js_code"])
        check=result["event_binding_validation"]["checks"][0]
        self.assertEqual(check["return_verification"],"sdk_wrapper_unverified")
        self.assertFalse(check["runtime_verified"])

    def test_explicit_false_return_is_preserved_in_isolated_callsite(self):
        result=self.bridge.compile_patch(self.design,operations(return_code="false"))
        script=r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');let handler;
const inbiz=()=>({getSelectedRows:()=>[],configEvent:(name,fn)=>handler=fn,on:()=>{}});inbiz.configEvent=()=>{};
const self=vm.runInNewContext('('+fs.readFileSync(0,'utf8')+')')(inbiz,{});
(async()=>{await self.main();let clicked=false;const response=await handler({id:'unrelated',innerInbiz:()=>{}});if(response)clicked=true;assert.equal(response,false);assert.equal(clicked,false);})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        ran=subprocess.run(["node","-e",script],input=result["js_code"],encoding="utf-8",capture_output=True,timeout=15)
        self.assertEqual(ran.returncode,0,ran.stderr)

    def test_native_insert_cannot_bypass_event_contract_validation(self):
        ops=operations();ops[-1]={"operation":"append_node","action_group_key":"main","element_key":"ComponentEvent",
            "input_params":{"name":{"paramTypes":"componentsVariable","value":"componentsVariable-trainee_task","dataType":"FormTables","code":'inbiz("trainee_task")'},
            "eventName":"onChildrenCustomBeforeClick","action":{"value":"missing-callback","label":"未声明回调","actionType":"js"}}}
        with self.assertRaisesRegex(CompilerError,"not found"):
            self.bridge.compile_patch(self.design,ops)

    def test_existing_callback_can_receive_visible_contract_and_is_rechecked(self):
        result=self.bridge.compile_patch(self.design,operations())
        broken=deepcopy(result["design"]);callback=broken["actionData"][1];callback["inputParams"]=[]
        ops=[{"operation":"set_event_callback_contract","action_group_key":callback["key"],"component_type":"FormTables","event_name":"onChildrenCustomBeforeClick"}]
        repaired=self.bridge.compile_patch(broken,ops)
        self.assertEqual(repaired["design"]["actionData"][1]["inputParams"][0]["title"],"action")
        self.assertEqual(len(repaired["event_binding_validation"]["checks"]),1)

    def test_manual_canvas_branches_and_named_payload_are_preserved(self):
        # Reduced to the user's callback; no other action or business records are copied.
        callback=json.loads((Path(__file__).parent/'fixtures'/'manual_child_button_callback.json').read_text(encoding='utf-8'))
        design=deepcopy(self.design);design['actionData'].append(callback)
        bind={"operation":"bind_component_event","action_group_key":"main","component_id":"trainee_task","component_type":"FormTables",
              "event_name":"onChildrenCustomBeforeClick","target_action_group_key":callback['key']}
        result=self.bridge.compile_patch(design,[bind])
        group=result['design']['actionData'][1]
        self.assertEqual(group['inputParams'][0]['title'],'action')
        self.assertEqual(len(group['data']),11)
        self.assertEqual([n['elementKey'] for n in group['data']].count('IfCondition'),2)
        self.assertIn('return outputParams;',result['js_code'])
        self.assertEqual(result['event_binding_validation']['checks'][0]['return_verification'],'sdk_wrapper_unverified')
        script=r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
let handler,metadata,registrations=0,selected=0,childReads=0;let rows=[{topic:'主题甲'},{topic:'主题乙'}];
const table={getSelectedRows:()=>{selected++;return rows;},configEvent:(event,fn)=>handler=fn,on:()=>{}};
const inbiz=()=>table;inbiz.configEvent=(event,fn)=>{assert.equal(event,'getPageAddMetaData');registrations++;metadata=fn;};
const self=vm.runInNewContext('('+fs.readFileSync(0,'utf8')+')')(inbiz,{});
const innerInbiz=()=>({getText:()=>{childReads++;return '合格';}});
(async()=>{await self.main();
 for(const [id,expected] of [['AP5l67Gn',{trainingTopics:'主题甲、主题乙'}],['OjuHwTLK',{evaluationTopics:'主题甲、主题乙',evaluationResult:'合格'}],['Jv0Lyar9',{evaluationTopics:'',evaluationResult:''}]]){
  const count=registrations;const output=await handler({id,action:'custom',name:'按钮',innerInbiz});
  assert.equal(registrations,count+1);assert.equal(output.returnValue,null);
  assert.deepEqual(Object.fromEntries(metadata().map(x=>[x.key,x.value])),expected);
 }
 const counts=[registrations,selected,childReads];await handler({id:'unrelated',innerInbiz});
 assert.deepEqual([registrations,selected,childReads],counts); // no new registration for unmatched branch
 rows=[];await handler({id:'AP5l67Gn',innerInbiz});assert.equal(metadata()[0].value,'');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        ran=subprocess.run(['node','-e',script],input=result['js_code'],encoding='utf-8',capture_output=True,timeout=15)
        self.assertEqual(ran.returncode,0,ran.stderr)
        # Positional callback inputs may have a different explicit canvas name.
        renamed=deepcopy(design);g=renamed['actionData'][1];g['inputParams'][0]['title']='context'
        for node in g['data'][:2]:
            param=node['paramsValue']['inputParams']['variableValue']
            for field in ('value','code','label'):param[field]=param[field].replace('action','context')
        checked=self.bridge.compile_patch(renamed,[bind])
        self.assertRegex(checked['js_code'],r'function \(context(?: = \{\})?\)')
        repaired=self.bridge.compile_patch(checked['design'],[{'operation':'set_event_callback_contract','action_group_key':g['key'],
            'component_type':'FormTables','event_name':'onChildrenCustomBeforeClick'}])
        self.assertEqual(repaired['design']['actionData'][1]['inputParams'][0]['title'],'context')

    def test_plain_js_group_undeclared_identifier_is_rejected(self):
        op=variable('result','missingContext.id');op['action_group_key']='main'
        with self.assertRaisesRegex(CompilerError,'Undeclared action identifier missingContext'):
            self.bridge.compile_patch(self.design,[op])

    def test_existing_eval_expressions_survive_full_generation_and_independent_recheck(self):
        ops = operations()
        expressions = ['eval(innerInbiz("file_upload").value)', 'eval(requestResult.Data)']
        ops[3:3] = [variable('files', expressions[0], 'any'),
                    variable('requestResult', '{Data:"[]"}', 'any'),
                    variable('error', expressions[1], 'any')]
        original = self.bridge.compile_patch(self.design, ops)
        edit = variable('changed', 'true', 'boolean'); edit['action_group_key'] = 'main'
        result = self.bridge.compile_patch(original['design'], [edit])
        self.assertEqual(original['design']['actionData'][1], result['design']['actionData'][1])
        for expression in expressions:
            self.assertIn(expression, original['js_code'])
            self.assertIn(expression, result['js_code'])
        evidence = self.bridge.verify_compiled_result(result)
        self.assertTrue(evidence['generated_code_consistency_verified'])
        desired = {name: result[name] for name in ('js_code', 'csharp_code', 'relation_data')}
        verified = self.bridge.verify_candidate_artifact(result['design'], desired, baseline_design=original['design'])
        self.assertTrue(verified['generated_code_consistency_verified'])
        for validation in (result['event_binding_validation'], verified['event_binding_validation']):
            self.assertEqual(validation['javascript_lexical_scope'], 'all_js_groups')
            self.assertEqual(set(validation['javascript_group_keys']), {group['key'] for group in result['design']['actionData']})
            for flag in ('business_scenarios_verified', 'browser_verified', 'runtime_verified'):
                self.assertFalse(validation[flag])

    def test_native_generation_still_rejects_undeclared_eval_argument(self):
        op = variable('result', 'eval(missingContext.value)', 'any'); op['action_group_key'] = 'main'
        with self.assertRaisesRegex(CompilerError, 'Undeclared action identifier missingContext'):
            self.bridge.compile_patch(self.design, [op])

    def test_reserved_identifier_scope_and_mount_regressions(self):
        script = Path(__file__).with_name('event_binding_checks.test.cjs')
        ran = subprocess.run(['node', str(script), str(self.bridge.frontend_repo)],
                             encoding='utf-8', capture_output=True, timeout=15)
        self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_native_generation_rejects_reserved_business_variables(self):
        for name in ('inbiz', 'Inbiz'):
            with self.subTest(name=name):
                op = variable(name, '{}', 'any'); op['action_group_key'] = 'main'
                with self.assertRaisesRegex(CompilerError, 'Reserved platform identifier ' + name) as caught:
                    self.bridge.compile_patch(self.design, [op])
                self.assertEqual(caught.exception.details['reason_code'], 'reserved_platform_identifier')
                self.assertEqual(caught.exception.details['group_key'], 'main')

    def test_platform_entry_and_distinct_local_names_survive_independent_recheck(self):
        result = self.bridge.compile_patch(self.design, operations())
        self.assertIn('function (inbiz,relatedAttributes)', result['js_code'])
        self.assertIn('var innerInbiz = action.innerInbiz;', result['js_code'])
        self.assertTrue(self.bridge.verify_compiled_result(result)['generated_code_consistency_verified'])
        evidence = result['event_binding_validation']
        self.bridge.verify_event_binding_validation(result['design'], result['js_code'], evidence)

    def test_changed_action_checks_existing_child_groups_too(self):
        broken=deepcopy(self.design)
        child={"key":"child","title":"下级组","actionType":"js","data":[]}
        broken['actionData'].append(child)
        # Generate a valid child first, then simulate an existing invalid custom expression.
        op=variable('result','1');op['action_group_key']='child'
        first=self.bridge.compile_patch(broken,[op])['design']
        field=first['actionData'][1]['data'][0]['paramsValue']['inputParams']['variableValue']
        field['code']=field['value']='missingChildContext.id'
        main=variable('ok','1');main['action_group_key']='main'
        with self.assertRaisesRegex(CompilerError,'missingChildContext'):
            self.bridge.compile_patch(first,[main])
