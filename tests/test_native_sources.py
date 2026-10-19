from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.helpers import editor_config
from gxp_editor.compiler import CompilerBridge, CompilerError


class NativeSourceBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rule = self.root / 'src/core/common/ActionDesign/Element/index.tsx'
        self.rule.parent.mkdir(parents=True)
        self.rule.write_text('export const ElementListData = [];', encoding='utf-8')
        self.path = self.root / 'bridge.js'
        self.bridge = CompilerBridge(replace(editor_config(), frontend_repo=str(self.root)))

    def manifest(self):
        entries = [{'path': self.rule.relative_to(self.root).as_posix(),
                    'sha256': hashlib.sha256(self.rule.read_bytes()).hexdigest()}]
        digest = hashlib.sha256(json.dumps(entries, separators=(',', ':')).encode()).hexdigest()
        manifest = {'schema_version': 1, 'rule_source_sha256': digest, 'files': entries}
        self.path.write_text('// GXP_COMPILER_SOURCES_V1=' + json.dumps(manifest) + '\n', encoding='utf-8')
        return digest

    def test_current_source_is_accepted_and_changes_are_blocked(self):
        expected = self.manifest()
        self.assertEqual(self.bridge._verify_native_sources(), expected)

    def test_changed_native_generator_is_blocked(self):
        self.manifest()
        self.rule.write_text('export const ElementListData = [SyncLock];', encoding='utf-8')
        with self.assertRaisesRegex(CompilerError, 'native source changed'):
            self.bridge._verify_native_sources()

    def test_new_native_rule_requires_rebuild(self):
        self.manifest()
        (self.rule.parent / 'SyncLock.tsx').write_text('new rule', encoding='utf-8')
        with self.assertRaisesRegex(CompilerError, 'rule file set changed'):
            self.bridge._verify_native_sources()

    def test_missing_native_rule_directory_is_not_treated_as_an_unbound_fixture(self):
        self.manifest()
        self.rule.unlink()
        self.rule.parent.rmdir()
        self.rule.parent.parent.rmdir()
        with self.assertRaisesRegex(CompilerError, 'Invalid compiler native source manifest'):
            self.bridge._verify_native_sources()

    def test_changed_dependency_outside_native_rules_is_blocked(self):
        self.manifest()
        dependency = self.root / 'package.json'
        dependency.write_text('{}', encoding='utf-8')
        prefix = '// GXP_COMPILER_SOURCES_V1='
        manifest = json.loads(self.path.read_text(encoding='utf-8')[len(prefix):])
        manifest['files'].append({'path': 'package.json', 'sha256': hashlib.sha256(dependency.read_bytes()).hexdigest()})
        self.path.write_text(prefix + json.dumps(manifest) + '\n', encoding='utf-8')
        self.bridge._verify_native_sources()
        dependency.write_text('{"version":"changed"}', encoding='utf-8')
        with self.assertRaisesRegex(CompilerError, 'native source changed: package.json'):
            self.bridge._verify_native_sources()

    def test_manifest_paths_cannot_escape_frontend_root(self):
        self.manifest()
        prefix = '// GXP_COMPILER_SOURCES_V1='
        manifest = json.loads(self.path.read_text(encoding='utf-8')[len(prefix):])
        manifest['files'].append({'path': '../outside.js', 'sha256': '0' * 64})
        self.path.write_text(prefix + json.dumps(manifest) + '\n', encoding='utf-8')
        with self.assertRaisesRegex(CompilerError, 'invalid or duplicate source path'):
            self.bridge._verify_native_sources()

    def test_legacy_bridge_requires_explicit_rebuild(self):
        self.path.write_text('// legacy bridge', encoding='utf-8')
        with self.assertRaisesRegex(CompilerError, 'build_compiler_bridge'):
            self.bridge._verify_native_sources()

    def test_legacy_bridge_cannot_skip_verification_without_frontend_sources(self):
        self.path.write_text('// legacy bridge', encoding='utf-8')
        self.rule.unlink()
        self.rule.parent.rmdir()
        self.rule.parent.parent.rmdir()
        with self.assertRaisesRegex(CompilerError, 'no verified native source manifest'):
            self.bridge._verify_native_sources()

    def cache_registry(self):
        digest = self.manifest()
        response = {'ok': True, 'command': 'describe_registry', 'rule_source_sha256': digest,
                    'elements': [{'key': 'SyncLock'}]}
        with patch.object(self.bridge, '_call', return_value=response), \
                patch.object(self.bridge, 'source_provenance', return_value={}):
            self.bridge.describe_registry()

    def test_cached_registry_cannot_hide_source_changes(self):
        self.cache_registry()
        self.rule.write_text('changed native generator', encoding='utf-8')
        with self.assertRaisesRegex(CompilerError, 'native source changed'):
            self.bridge.describe_registry()

    def test_cached_registry_cannot_hide_bridge_replacement(self):
        self.cache_registry()
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write('// changed bridge implementation\n')
        with self.assertRaisesRegex(CompilerError, 'Compiler bridge changed'):
            self.bridge.compiler_binding()

    def test_missing_source_and_tampered_hash_are_blocked(self):
        self.manifest()
        original = self.path.read_text(encoding='utf-8')
        self.path.write_text(original.replace('"rule_source_sha256": "', '"rule_source_sha256": "0'), encoding='utf-8')
        with self.assertRaisesRegex(CompilerError, 'rule source hash mismatch'):
            self.bridge._verify_native_sources()
        self.path.write_text(original, encoding='utf-8')
        self.rule.unlink()
        with self.assertRaisesRegex(CompilerError, 'Invalid compiler native source manifest'):
            self.bridge._verify_native_sources()


@unittest.skipUnless(os.environ.get('GXP_TEST_COMPILER_BRIDGE'), 'Native compiler path required')
class NativeElementCoverageTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ['GXP_TEST_COMPILER_BRIDGE']).resolve()
        root = Path(os.environ.get('GXP_TEST_FRONTEND_REPO', str(path.parents[2]))).resolve()
        self.bridge = CompilerBridge(replace(editor_config(), frontend_repo=str(root), compiler_bridge=str(path)))

    def test_full_native_registry_including_new_elements(self):
        registry = self.bridge.describe_registry()
        elements = {element['key']: element for element in registry['elements']}
        index = self.bridge.frontend_repo / 'src/core/common/ActionDesign/Element/index.tsx'
        source = index.read_text(encoding='utf-8')
        body = source.split('export const ElementListData = [', 1)[1].split('];', 1)[0]
        names = re.findall(r'^\s*(\w+),', body, re.M)
        imports = dict(re.findall(r"import (\w+) from '([^']+)';", source))
        keys = set()
        for name in names:
            text = (index.parent / (imports[name] + '.tsx')).read_text(encoding='utf-8')
            # Default-import aliases need not match the element's local name.
            definition = re.search(r'\bconst\s+\w+\s*:\s*ActionDesignElementType\s*=\s*\{', text)
            self.assertIsNotNone(definition, name)
            keys.add(re.search(r"\bkey:\s*['\"]([^'\"]+)", text[definition.end():]).group(1))
        self.assertEqual(set(elements), keys)
        self.assertEqual(len(registry['elements']), len(names))
        self.assertTrue({'SyncLock', 'SyncLockEnd', 'AddChangeData', 'VersionFormat', 'FileRelease',
                         'FileCopyVersion', 'FileEditingCheck'} <= set(elements))
        self.assertFalse(elements['SyncLock']['supports_js'])
        self.assertTrue(elements['SyncLock']['supports_csharp'])
        self.assertEqual(elements['SyncLock']['end_element_key'], 'SyncLockEnd')

    def test_native_lock_generation_and_structural_rejections(self):
        design = {'code': 'OfflineLockCheck', 'actionData': [{'key': 'backend', 'title': '锁验证',
                  'actionType': 'csharp', 'inputParams': [], 'outputParams': [], 'data': []}], 'globalVariables': []}
        key = {'paramTypes': 'custom', 'value': 'offline-lock', 'code': 'offline-lock', 'label': 'offline-lock', 'dataType': 'string'}
        result = self.bridge.compile_patch(design, [{'operation': 'append_node', 'action_group_key': 'backend',
                  'element_key': 'SyncLock', 'input_params': {'lockKey': key}, 'output_params': {}}])
        nodes = result['design']['actionData'][0]['data']
        self.assertEqual([node['elementKey'] for node in nodes], ['SyncLock', 'SyncLockEnd'])
        self.assertIn('ExecuteWithLockAsync<object>("offline-lock", async () => {', result['csharp_code'])
        self.assertTrue(self.bridge.verify_compiled_result(result)['generated_code_consistency_verified'])
        self.assertEqual(self.bridge.validate_design(result['design'])['validation']['valid'], True)
        for mutate, message in (
            (lambda d: d['actionData'][0]['data'].pop(), 'unclosed block'),
            (lambda d: d['actionData'][0].update(actionType='js'), 'not valid in JS'),
            (lambda d: d['actionData'][0]['data'][0].update(elementKey='UnknownCustomElement'), 'Unknown'),
        ):
            with self.subTest(message=message):
                invalid = json.loads(json.dumps(result['design']))
                mutate(invalid)
                with self.assertRaisesRegex(CompilerError, message):
                    self.bridge.validate_design(invalid)

    def test_other_previously_missing_elements_generate_consistent_artifacts(self):
        def custom(value, data_type='string'):
            return {'paramTypes': 'custom', 'value': value, 'code': value,
                    'label': value, 'dataType': data_type}

        cases = (
            ('js', 'AddChangeData', {'config': {'changeSettings': [], 'isIgnoredPageChange': False}},
             {}, "configEvent('getPageAddChangeData'"),
            ('js', 'VersionFormat', {'versionValue': custom('"1.0"')},
             {'variableName': custom('formattedVersion')}, 'relatedAttributes.formatVersion('),
            ('csharp', 'VersionFormat', {'versionValue': custom('"1.0"')},
             {'variableName': custom('formattedVersion')}, 'GetCpmSettingValue("VersionFormat")'),
            ('csharp', 'FileRelease', {'fileId': custom('offline-file')},
             {'requestResult': custom('releaseResult', 'object')}, '_service.ReleaseFile('),
            ('js', 'FileCopyVersion', {'versionId': custom('offline-version')},
             {'copyFileId': custom('copiedFileId'), 'copyVersionId': custom('copiedVersionId')},
             'relatedAttributes.copyFileVersion('),
            ('csharp', 'FileCopyVersion', {'versionId': custom('offline-version')},
             {'copyFileId': custom('copiedFileId'), 'copyVersionId': custom('copiedVersionId')},
             '_service.CopyFileVersion('),
            ('js', 'FileEditingCheck', {'fileId': custom('offline-file')},
             {'hasEditingFiles': custom('hasEditingFiles', 'boolean')}, '/api/files/editing'),
        )
        design = {'code': 'OfflineElementCheck', 'actionData': [
            {'key': language, 'title': language, 'actionType': language,
             'inputParams': [], 'outputParams': [], 'data': []}
            for language in ('js', 'csharp')], 'globalVariables': []}
        operations = [{'operation': 'append_node', 'action_group_key': language,
                       'element_key': element, 'input_params': inputs, 'output_params': outputs}
                      for language, element, inputs, outputs, _ in cases]
        result = self.bridge.compile_patch(design, operations)
        for language, element, _, _, expected in cases:
            with self.subTest(language=language, element=element):
                self.assertIn(expected, result['js_code' if language == 'js' else 'csharp_code'])
        self.assertTrue(self.bridge.verify_compiled_result(result)['generated_code_consistency_verified'])


if __name__ == '__main__':
    unittest.main()
