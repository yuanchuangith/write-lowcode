from __future__ import annotations

import os
import subprocess
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from tests.helpers import editor_config
from gxp_editor.compiler import CompilerBridge, CompilerError

ROOT = Path(__file__).resolve().parents[1]


class InterfaceMetadataContractTests(unittest.TestCase):
    def test_offline_metadata_scope(self):
        result = subprocess.run(['node', '--test', str(ROOT / 'tests/interface_metadata.test.cjs')],
                                capture_output=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipUnless(os.environ.get('GXP_TEST_COMPILER_BRIDGE'), 'Native compiler path required')
class NativeInterfaceMetadataTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ['GXP_TEST_COMPILER_BRIDGE']).resolve()
        self.bridge = CompilerBridge(replace(editor_config(), frontend_repo=str(path.parents[2]), compiler_bridge=str(path)))
        self.design = {'actionData': [{'key': 'main', 'title': 'Interface test', 'actionType': 'js', 'data': []}], 'globalVariables': []}
        self.operation = {'operation': 'append_node', 'action_group_key': 'main', 'element_key': 'CallInterface',
            'input_params': {'apiInfo': {'setting': {'id': 'metadata-regression', 'name': 'Saved interface', 'modeType': 'physical',
                'requestType': 'GET', 'requestUrl': 'https://example.invalid/items/{id}', 'requestBody': '{"contentType":"application/json"}'},
                'headers': {}, 'queryParams': {'enabled': {'code': 'true'}}, 'bodyFormData': {}, 'bodyParams': {},
                'pathParams': {'id': {'code': '42'}}, 'rawBodyContent': ''}},
            'output_params': {'requestResult': {'paramTypes': 'custom', 'value': 'response', 'code': 'response', 'label': 'response'}}}

    def test_native_generation_keeps_runtime_request_and_binds_saved_metadata(self):
        original = deepcopy((self.design, self.operation))
        result = self.bridge.compile_patch(self.design, [self.operation])
        evidence = self.bridge.verify_compiled_result(result)
        self.assertEqual((self.design, self.operation), original)
        self.assertEqual(evidence['interface_metadata']['interface_count'], 1)
        self.assertFalse(evidence['interface_metadata']['live_metadata_checked'])
        self.assertIn('await inbiz.request', result['js_code'])
        script = """
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict'),calls=[];
const inbiz={request:async(url,options)=>{calls.push({url,options});return {ok:true};}};
const action=vm.runInNewContext('('+fs.readFileSync(0,'utf8')+')')(inbiz,{});
Promise.resolve(action.main()).then(()=>{assert.equal(calls.length,1);assert.equal(calls[0].url,'https://example.invalid/items/42');
assert.equal(calls[0].options.method,'GET');assert.equal(calls[0].options.params.enabled,true);}).catch(e=>{console.error(e);process.exitCode=1;});
"""
        executed = subprocess.run(['node', '-e', script], input=result['js_code'], capture_output=True, encoding='utf-8', timeout=10)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        tampered = deepcopy(result)
        tampered['interface_metadata']['snapshot_sha256'] = '0' * 64
        with self.assertRaisesRegex(CompilerError, 'interface metadata changed'):
            self.bridge.verify_compiled_result(tampered)

    def test_missing_saved_url_blocks_without_a_network_fallback(self):
        del self.operation['input_params']['apiInfo']['setting']['requestUrl']
        with self.assertRaisesRegex(CompilerError, 'requires saved interface id and requestUrl'):
            self.bridge.compile_patch(self.design, [self.operation])


if __name__ == '__main__':
    unittest.main()
