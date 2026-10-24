import unittest
import hashlib
import json
from pathlib import Path

from app.data import canonical_csv, generate_demo, parse_csv
from app.engine import run_experiment
from app.reporting import render_html_report


class ReportingTests(unittest.TestCase):
    def test_canonical_hash_is_newline_independent_and_matches_bundled_report(self):
        root = Path(__file__).resolve().parents[1]
        raw = (root / 'data/demo-a.csv').read_bytes().replace(b'\r\n', b'\n')
        lf = canonical_csv(parse_csv(raw))
        crlf = canonical_csv(parse_csv(raw.replace(b'\n', b'\r\n')))
        self.assertEqual(lf, crlf)
        self.assertNotIn(b'\n', lf.replace(b'\r\n', b''))
        report = json.loads((root / 'examples/demo-report.json').read_text(encoding='utf-8'))
        self.assertEqual(hashlib.sha256(lf).hexdigest(), report['dataset_sha256'])

    def test_export_escapes_untrusted_text_and_has_no_remote_dependencies(self):
        report = run_experiment(generate_demo(rows=300), {"model": "logistic"})
        report["dataset_name"] = '<img src=x onerror=alert(1)>'
        report["notes"].append('<script>alert(1)</script>')
        result = render_html_report(report)
        self.assertNotIn('<img src=x', result)
        self.assertNotIn('<script>', result)
        self.assertNotIn('src="http', result)
        self.assertNotIn('href="http', result)
        self.assertIn('&lt;img', result)
        self.assertIn('成本敏感性', result)
        self.assertIn('@media print', result)
        self.assertIn('<svg', result)

    def test_old_report_can_still_be_exported(self):
        report = run_experiment(generate_demo(rows=300), {"model": "logistic"})
        for key in ('evaluation', 'data_quality', 'diagnostics', 'cost_sensitivity'):
            report.pop(key)
        self.assertIn('旧版报告', render_html_report(report))

    def test_csv_rejects_duplicate_dates_and_inconsistent_ohlc(self):
        frame = generate_demo(rows=300)
        for column, value in [('date', frame.date.iloc[0]), ('high', .01)]:
            with self.subTest(column=column):
                bad = frame.copy()
                bad.loc[1, column] = value
                with self.assertRaises(ValueError):
                    parse_csv(bad.to_csv(index=False).encode())
