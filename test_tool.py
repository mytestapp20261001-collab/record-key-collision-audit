import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(HERE.name.replace("-", "_"), HERE / "tool.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)

class CommonTests(unittest.TestCase):
    def cli(self, *args):
        return subprocess.run([sys.executable, str(HERE / "tool.py"), *args], cwd=HERE,
                              capture_output=True, text=True, timeout=10)
    def test_cli_pass(self):
        result = self.cli("examples/" + PASS, *ARGS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "pass")
        self.assertEqual(result.stderr, "")
    def test_cli_fail(self):
        result = self.cli("examples/" + FAIL, *ARGS)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "fail")
    def test_cli_malformed(self):
        result = self.cli("examples/" + MALFORMED, *ARGS)
        self.assertEqual(result.returncode, MALFORMED_CODE, result.stdout + result.stderr)
        self.assertIn(json.loads(result.stdout)["status"], ("fail", "error"))
    def test_cli_missing_file(self):
        result = self.cli("nonexistent-input", *ARGS)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"], "unreadable_utf8_input")
    def test_cli_invalid_arguments_are_redacted(self):
        result = self.cli("--unrecognized-secret-value")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"], "invalid_arguments")
        self.assertNotIn("unrecognized-secret-value", result.stdout + result.stderr)
    def test_cli_help(self):
        result = self.cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage:", result.stdout)
    def test_cli_stability(self):
        a = self.cli("examples/" + FAIL, *ARGS)
        b = self.cli("examples/" + FAIL, *ARGS)
        self.assertEqual(a.stdout, b.stdout)
    def test_file_limit_and_utf8(self):
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory) / "input"
            path.write_bytes(b"x" * (tool.MAX_BYTES + 1))
            with self.assertRaises(tool.InputError):
                tool.read_text(path)
            path.write_bytes(b"\xff")
            with self.assertRaises(UnicodeDecodeError):
                tool.read_text(path)
    def test_json_parser_safety(self):
        for text in ('{"x":1,"x":2}', '[NaN]', '[' * 130 + '0' + ']' * 130):
            with self.subTest(text=text[:20]), self.assertRaises(tool.InputError):
                tool.parse_json(text)
    def test_report_cap_preserves_count(self):
        report = tool.Report("test")
        for i in range(101):
            report.add("sample", record=i)
        result = report.finish()
        self.assertEqual(len(result["findings"]), 100)
        self.assertEqual(result["summary"]["finding_count"], 101)
        self.assertTrue(result["findings_truncated"])

    def test_adversarial_cli_and_read_only_corpus(self):
        samples = ['', 'null', 'true', '0', '[]', '{}', '[null]', '[true]', '[1]',
                   '["NaN"]', '[{}]', '[[]]', '{"x":1,"x":2}',
                   '[1e99999999999999999999999999]', '[NaN]', '[Infinity]',
                   '"\\ud800"', '["\\ud800"]', '{', '[', '["a","b"]',
                   '[1e-99999999999999999999999999]']
        before = {path: path.read_bytes() for path in (HERE / 'examples').iterdir()}
        with tempfile.TemporaryDirectory(dir=HERE) as directory:
            path = Path(directory) / 'input.txt'
            for sample in samples:
                with self.subTest(sample=sample):
                    path.write_text(sample, encoding='utf-8')
                    result = self.cli(str(path), *ARGS)
                    self.assertIn(result.returncode, (0, 1, 2), result.stderr)
                    self.assertEqual(result.stderr, '')
                    self.assertIn(json.loads(result.stdout)['status'], ('pass','fail','error'))
                    self.assertEqual(path.read_text(encoding='utf-8'), sample)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

PASS = 'pass.json'
FAIL = 'fail.json'
MALFORMED = 'malformed.json'
MALFORMED_CODE = 2
ARGS = ['--key', 'tenant', '--key', 'id']

class AuditTests(unittest.TestCase):
    def run_audit(self, values, **options): return tool.audit([{'k':v} for v in values],keys=['k'],**options)
    def test_typed_distinctions(self):
        self.assertEqual(self.run_audit([1,tool.Decimal('1.0'),'1',True])['status'],'pass')
    def test_number_normalization(self):
        result=self.run_audit([1,tool.Decimal('1.0'),'01'],normalization='number')
        self.assertEqual(result['summary']['collision_pairs'],3)
    def test_bool_never_number(self): self.assertEqual(self.run_audit([1,True,'true'],normalization='number')['status'],'pass')
    def test_text_normalization(self):
        result=self.run_audit([' CAFÉ ','cafe\u0301'],normalization='text')
        self.assertEqual(result['summary']['duplicate_rows'],1)
    def test_text_numeric_string_distinction(self):
        result=self.run_audit([1,'1','1.0'],normalization='text')
        self.assertEqual(result['summary']['unique_keys'],2)
    def test_composite_not_concatenated(self):
        result=tool.audit([{'a':'ab','b':'c'},{'a':'a','b':'bc'}],keys=['a','b'])
        self.assertEqual(result['status'],'pass')
    def test_null_policies(self):
        self.assertEqual(self.run_audit([None,None])['summary']['eligible_records'],0)
        self.assertEqual(self.run_audit([None,None],nulls='skip')['status'],'pass')
        self.assertEqual(self.run_audit([None,None],nulls='match')['summary']['collision_pairs'],1)
    def test_missing_not_null(self):
        result=tool.audit([{}],keys=['k'],nulls='skip')
        self.assertEqual(result['findings'][0]['code'],'missing_key_field')
    def test_empty_string_not_null(self): self.assertEqual(self.run_audit(['',None],nulls='match')['status'],'pass')
    def test_collision_counts_and_cap(self):
        result=self.run_audit(['same']*150)
        self.assertEqual(result['summary']['collision_pairs'],150*149//2)
        self.assertEqual(result['summary']['duplicate_rows'],149)
        self.assertTrue(result['findings_truncated'])
    def test_nested_key_rejected(self):
        for value in ({},[]):
            with self.assertRaises(tool.InputError): self.run_audit([value])
    def test_bad_keys_and_shape(self):
        for keys in ([],['k','k'],[1]):
            with self.assertRaises(tool.InputError): tool.audit([{}],keys=keys)
        with self.assertRaises(tool.InputError): tool.audit([1],keys=['k'])
    def test_magnitude_limit(self):
        with self.assertRaises(tool.InputError): self.run_audit(['1e101'],normalization='number')
    def test_redaction(self): self.assertNotIn('SUPERSECRET',json.dumps(self.run_audit(['SUPERSECRET']*2)))

if __name__ == '__main__': unittest.main()
