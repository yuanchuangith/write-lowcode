from __future__ import annotations

import unittest

from scripts.relax_compiler_references import advisory_references


SOURCE = """function validateReferences(design, action, node, actionKeys) {
  if (globalMissing) fail('missing global');
  if (inputMissing) fail('missing input');
  if (outputMissing) fail('missing output');
  if (targetMissing) fail('missing action');
  if (languageMismatch) fail('action language mismatch');
}
function validateDesign(designInput) {
  var nodeCount = 0;
  if (duplicateKey) fail('duplicate key');
  if (invalidDepth) fail('invalid depth');
  validateReferences(design, _action, node, actionKeys);
  return {validation: {
      valid: true,
      node_count: nodeCount
  }};
}
function compileCode() { fail('native generation failed'); }
"""


class ReferencePolicyTests(unittest.TestCase):
    def test_only_static_references_become_visible_warnings(self):
        result = advisory_references(SOURCE)
        self.assertEqual(result.count('warnings.push('), 5)
        self.assertIn('actionKeys, warnings);', result)
        self.assertIn('      warnings,', result)
        for message in ('duplicate key', 'invalid depth', 'native generation failed'):
            self.assertIn(f"fail('{message}')", result)

    def test_updater_is_idempotent(self):
        result = advisory_references(SOURCE)
        self.assertEqual(advisory_references(result), result)

    def test_unrecognized_validator_is_rejected(self):
        for original in (
            SOURCE.replace("fail('missing input')", "report('missing input')"),
            SOURCE.replace('  var nodeCount = 0;', '  let nodeCount = 0;'),
        ):
            with self.subTest(original=original):
                with self.assertRaises(ValueError):
                    advisory_references(original)


if __name__ == '__main__':
    unittest.main()
