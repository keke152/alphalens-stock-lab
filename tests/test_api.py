"""Exercise the actual HTTP server with an isolated temporary workspace."""
import csv
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        cls.base = f'http://127.0.0.1:{port}'
        cls.client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        cls.log = open(Path(cls.folder.name) / 'server.log', 'wb')
        env = {**os.environ, 'STOCK_LAB_HOME': cls.folder.name, 'PYTHONUTF8': '1'}
        cls.server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)],
                                      cwd=Path(__file__).resolve().parents[1], env=env, stdout=cls.log, stderr=cls.log)
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    cls.get('/health')
                    break
                except (urllib.error.URLError, ConnectionError):
                    if time.monotonic() > deadline or cls.server.poll() is not None:
                        raise RuntimeError('Test HTTP server did not start')
                    time.sleep(.1)
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=20)
        cls.log.close()
        cls.folder.cleanup()

    @classmethod
    def get(cls, path):
        return cls.client.open(cls.base + path, timeout=10)

    @classmethod
    def post(cls, path, body, headers=None):
        request = urllib.request.Request(cls.base + path, data=json.dumps(body).encode(), method='POST',
                                         headers={'Content-Type': 'application/json', **(headers or {})})
        return cls.client.open(request, timeout=10)

    def test_health_bootstrap_and_script_mime(self):
        with self.get('/health') as response:
            self.assertEqual(json.load(response)['version'], '1.4.0')
        with self.get('/api/bootstrap') as response:
            bootstrap = json.load(response)
            self.assertEqual(bootstrap['defaults']['evaluation'], 'holdout')
        with self.get('/static/robustness.js') as response:
            self.assertIn('text/javascript', response.headers['Content-Type'])
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')

    def test_invalid_fold_and_cross_origin_rejected(self):
        for body in ({'dataset_id': 'demo-a', 'folds': 10}, {'dataset_id': 'demo-a', 'threshold_mode': 'test'}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post('/api/runs', body)
            self.assertEqual(error.exception.code, 422)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post('/api/runs', {'dataset_id': 'demo-a'}, {'Origin': 'https://example.com'})
        self.assertEqual(error.exception.code, 403)

    def test_run_persists_walk_forward_and_exports(self):
        with self.post('/api/runs', {'dataset_id': 'demo-a', 'model': 'logistic', 'evaluation': 'walk_forward',
                                     'threshold_mode': 'validation', 'folds': 3}) as response:
            self.assertEqual(response.status, 202)
            run_id = json.load(response)['id']
        deadline = time.monotonic() + 30
        while True:
            with self.get('/api/runs/' + run_id) as response:
                run = json.load(response)
            if run['status'] in ('completed', 'failed'):
                break
            self.assertLess(time.monotonic(), deadline, 'Training timed out')
            time.sleep(.1)
        self.assertEqual(run['status'], 'completed', run.get('error'))
        self.assertTrue(run['has_model'])
        self.assertEqual(run['report']['evaluation']['fold_count'], 3)
        with self.get(f'/api/runs/{run_id}/export?format=csv') as response:
            rows = list(csv.DictReader(io.StringIO(response.read().decode('utf-8-sig'))))
            self.assertEqual(len(rows), run['report']['metrics']['test_sessions'])
            self.assertTrue({'fold', 'threshold'}.issubset(rows[0]))
        with self.get(f'/api/runs/{run_id}/export?format=html') as response:
            self.assertIn('attachment', response.headers['Content-Disposition'])
            self.assertIn('text/html', response.headers['Content-Type'])
            self.assertIn('成本敏感性', response.read().decode())
        with self.get(f'/api/runs/{run_id}/export?format=json') as response:
            self.assertEqual(json.load(response)['report_version'], 2)
        with self.get(f'/api/runs/{run_id}/model') as response:
            self.assertGreater(len(response.read()), 1000)

    def test_old_bundled_report_html_and_missing_export(self):
        with self.get('/api/runs/bundled-demo/export?format=html') as response:
            self.assertIn('旧版报告', response.read().decode())
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.get('/api/runs/not-found/export?format=html')
        self.assertEqual(error.exception.code, 404)

    def test_tools_analysis_cutoff_and_unavailable_dimensions(self):
        with self.get('/api/datasets/demo-a/analysis?as_of=2026-01-01') as response:
            result = json.load(response)
        self.assertLessEqual(result['as_of'], '2026-01-01')
        self.assertIsNone(result['score']['overall_score'])
        self.assertEqual(result['score']['coverage'], .25)
        self.assertTrue(all(row['date'] <= '2026-01-01' for row in result['series']))
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.get('/api/datasets/demo-a/analysis?as_of=bad-date')
        self.assertEqual(error.exception.code, 422)

    def test_tools_watchlist_dedup_acknowledge_and_monitor(self):
        def request(method, path, body):
            req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method=method,
                                         headers={'Content-Type': 'application/json'})
            with self.client.open(req, timeout=10) as response:
                return json.load(response)
        request('PUT', '/api/watchlist', {'dataset_id': 'demo-a', 'cost_price': 1,
                                           'cost_date': '2025-01-01', 'rules': {'enabled': ['cost']}})
        with self.post('/api/watchlist/scan', {}) as response:
            first = json.load(response)
        with self.post('/api/watchlist/scan', {}) as response:
            second = json.load(response)
        self.assertEqual(first['new_alerts'], 1)
        self.assertEqual(second['new_alerts'], 0)
        alert_id = first['alerts'][0]['id']
        request('PATCH', '/api/alerts/' + alert_id, {'acknowledged': True})
        monitor = request('PUT', '/api/monitor', {'enabled': True, 'interval_seconds': 30})
        self.assertTrue(monitor['enabled'])
        request('PUT', '/api/monitor', {'enabled': False, 'interval_seconds': 60})
        with self.get('/api/research-tools') as response:
            workspace = json.load(response)
        self.assertTrue(next(row for row in workspace['alerts'] if row['id'] == alert_id)['acknowledged'])
        self.assertFalse(workspace['monitor']['enabled'])
        self.assertEqual(len(workspace['watchlist']), 1)
        with self.get('/api/alerts-export') as response:
            self.assertIn('参考成本', response.read().decode('utf-8-sig'))

    def test_tools_scanner_does_not_pass_missing_benchmark(self):
        with self.post('/api/scans', {'as_of': '2026-10-21'}) as response:
            result = json.load(response)
        self.assertEqual(len(result['results']), 3)
        for row in result['results']:
            self.assertIsNone(row['conditions'][4]['passed'])
            self.assertFalse(row['matched'])
            self.assertIn('streak', row['event'])

    def test_tools_review_export_filters_future_trades(self):
        with self.get('/api/research-tools/export?dataset_id=demo-a&as_of=2026-10-20&format=json') as response:
            review = json.load(response)
        self.assertEqual(review['as_of'], '2026-10-20')
        self.assertTrue(all(row['trade_date'] <= '2026-10-20' for row in review['paper']['trades']))
        self.assertIsNone(review['analysis']['score']['overall_score'])
        with self.get('/api/research-tools/export?dataset_id=demo-a&format=html') as response:
            html = response.read().decode()
        self.assertIn('研究日报', html)
        self.assertIn('未接入，不计分', html)
        self.assertIn('@media print', html)
        self.assertNotIn('<script', html)

    def test_tools_paper_tplusone_and_ledger_export(self):
        with self.post('/api/paper/trades', {'dataset_id': 'demo-b', 'side': 'BUY', 'quantity': 10, 'trade_date': '2026-10-20'}) as response:
            self.assertEqual(response.status, 201)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post('/api/paper/trades', {'dataset_id': 'demo-b', 'side': 'SELL', 'quantity': 1, 'trade_date': '2026-10-20'})
        self.assertEqual(error.exception.code, 422)
        with self.post('/api/paper/trades', {'dataset_id': 'demo-b', 'side': 'SELL', 'quantity': 5, 'trade_date': '2026-10-21'}) as response:
            self.assertEqual(json.load(response)['quantity'], 5)
        with self.get('/api/paper') as response:
            account = json.load(response)
        self.assertEqual(len(account['trades']), 2)
        self.assertEqual(account['positions'][0]['quantity'], 5)
        with self.get('/api/paper/export') as response:
            rows = list(csv.DictReader(io.StringIO(response.read().decode('utf-8-sig'))))
        self.assertEqual(len(rows), 2)
