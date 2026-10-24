import tempfile
import unittest
from pathlib import Path
import uuid
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from app import paper, storage, watching
from app.data import validate_frame
from app.technical import analysis, indicator_frame, scan_templates


def flat_frame():
    dates = pd.bdate_range('2025-01-01', periods=300)
    return validate_frame(pd.DataFrame({'date': dates, 'open': 100., 'high': 101., 'low': 99., 'close': 100., 'volume': 100.}))


def set_close(frame, index, close):
    frame.loc[index, ['open', 'close']] = close
    frame.loc[index, 'high'] = close + 1
    frame.loc[index, 'low'] = close - 1


class IndicatorTests(unittest.TestCase):
    def test_flat_indicators_and_warmup(self):
        result = indicator_frame(flat_frame())
        row = result.iloc[-1]
        self.assertEqual(row['rsi14'], 50)
        self.assertEqual(row['ma60'], 100)
        self.assertEqual(row['macd'], 0)
        self.assertEqual(row['k'], 50)
        self.assertEqual(row['d'], 50)
        self.assertEqual(row['j'], 50)
        self.assertEqual(row['boll_upper'], 100)
        self.assertEqual(row['atr14'], 2)
        self.assertTrue(pd.isna(result.iloc[10]['ma20']))
        self.assertTrue(pd.isna(result.iloc[20]['dif']))

    def test_asof_analysis_cannot_see_future_changes(self):
        frame = flat_frame()
        cutoff = frame.date.iloc[200].strftime('%Y-%m-%d')
        before = analysis(frame, cutoff)
        frame.loc[201:, ['open', 'high', 'low', 'close']] *= 2
        frame.loc[201:, 'volume'] *= 100
        self.assertEqual(before, analysis(frame, cutoff))

    def test_volume_ratio_uses_previous_twenty_excluding_today(self):
        frame = flat_frame();frame.loc[299, 'volume'] = 300
        self.assertEqual(indicator_frame(frame).iloc[-1]['volume_ratio'], 3)

    def test_coverage_does_not_invent_fundamental_or_news_scores(self):
        score = analysis(flat_frame())['score']
        self.assertEqual(score['coverage'], .25)
        self.assertIsNone(score['overall_score'])
        self.assertTrue(all(row['score'] is None for row in score['dimensions'][1:]))

    def test_scan_excludes_current_close_from_high_reference_and_marks_unknown_index(self):
        frame = flat_frame();set_close(frame, 299, 102)
        result = scan_templates(frame)
        self.assertEqual(result['conditions'][0]['reference'], 100)
        self.assertTrue(result['conditions'][0]['passed'])
        self.assertIsNone(result['conditions'][4]['passed'])
        self.assertFalse(result['matched'])

    def test_event_streak_respects_threshold_at_float_boundary(self):
        frame = flat_frame()
        for index, close in zip([297, 298, 299], [110, 121, 133.1]):
            set_close(frame, index, close)
        event = scan_templates(frame, event_threshold=.1)['event']
        self.assertEqual(event['streak'], 3)
        self.assertTrue(event['matched'])


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.previous_home = storage.HOME
        storage.HOME = Path(self.folder.name)
        storage.initialize()
        self.frame = flat_frame()
        storage.register_dataset('a', '观察 A', 'A', 'synthetic', self.frame)
        storage.set_setting('paper_risk', {**paper.RiskSettings().model_dump(), 'fee_bps': 0, 'slippage_bps': 0})

    def tearDown(self):
        storage.HOME = self.previous_home
        self.folder.cleanup()

    def day(self, index):
        return self.frame.date.iloc[index].strftime('%Y-%m-%d')

    def test_watch_scan_deduplicates_alerts_and_preserves_read_state(self):
        watching.save_watch('a', 50, self.day(250), watching.WatchRules(enabled=['cost']))
        first = watching.scan_watchlist();second = watching.scan_watchlist()
        self.assertEqual(first['new_alerts'], 1)
        self.assertEqual(second['new_alerts'], 0)
        alert = watching.list_alerts()[0]
        with storage.connect() as db:
            db.execute('UPDATE alerts SET acknowledged=1 WHERE id=?', (alert['id'],))
        watching.scan_watchlist()
        self.assertEqual(watching.list_alerts(unread_only=True), [])
        self.assertEqual(watching.list_alerts(as_of=self.day(298)), [])
        self.assertEqual(len(watching.list_alerts(as_of=self.day(299))), 1)

    def test_cost_rules_ignore_reference_cost_from_future_date(self):
        result = watching.evaluate_rules(self.frame, watching.WatchRules(enabled=['cost', 'trailing']),
                                        cost_price=50, cost_date=self.day(299), as_of=self.day(270))
        self.assertIsNone(result['cost_return'])
        self.assertEqual(result['events'], [])

    def test_rules_detect_cross_volume_rsi_gap_and_close_change(self):
        frame = self.frame.copy();set_close(frame, 299, 110);frame.loc[299, 'volume'] = 500
        events = watching.evaluate_rules(frame, watching.WatchRules())['events']
        self.assertTrue({'change', 'volume', 'ma_cross', 'rsi', 'gap'}.issubset({event['rule'] for event in events}))

    def test_trailing_peak_only_uses_holding_period_and_past_records(self):
        frame = self.frame.copy();set_close(frame, 290, 130);set_close(frame, 299, 115)
        rules = watching.WatchRules(enabled=['trailing'])
        self.assertEqual(len(watching.evaluate_rules(frame, rules, 100, self.day(280))['events']), 1)
        self.assertEqual(watching.evaluate_rules(frame, rules, 100, self.day(295))['events'], [])

    def test_all_rules_can_be_disabled(self):
        result = watching.evaluate_rules(self.frame, watching.WatchRules(enabled=[]), 1, self.day(250))
        self.assertEqual(result['events'], [])

    def test_same_day_sale_is_blocked_without_changing_ledger(self):
        paper.record_trade('a', 'BUY', 10, self.day(298))
        with self.assertRaisesRegex(ValueError, 'T\+1'):
            paper.record_trade('a', 'SELL', 1, self.day(298))
        self.assertEqual(len(paper.trades()), 1)
        self.assertEqual(paper.portfolio(as_of=self.day(298))['positions'][0]['sellable'], 0)

    def test_tplusone_only_old_lots_can_be_sold(self):
        paper.record_trade('a', 'BUY', 10, self.day(298))
        paper.record_trade('a', 'BUY', 5, self.day(299))
        with self.assertRaises(ValueError):
            paper.record_trade('a', 'SELL', 11, self.day(299))
        paper.record_trade('a', 'SELL', 10, self.day(299))
        position = paper.portfolio()['positions'][0]
        self.assertEqual(position['quantity'], 5)
        self.assertEqual(position['sellable'], 0)

    def test_fifo_partial_sale_cash_and_realized_pnl_reconcile(self):
        set_close(self.frame, 299, 110)
        storage.register_dataset('a', '观察 A', 'A', 'synthetic', self.frame)
        paper.record_trade('a', 'BUY', 10, self.day(298))
        sale = paper.record_trade('a', 'SELL', 6, self.day(299))
        result = paper.portfolio()
        self.assertAlmostEqual(sale['realized_pnl'], 60)
        self.assertAlmostEqual(result['cash'], 99660)
        self.assertAlmostEqual(result['equity'], 100100)
        self.assertAlmostEqual(result['positions'][0]['unrealized_pnl'], 40)
        self.assertAlmostEqual(result['history'][-1]['equity'], result['equity'])

    def test_cash_single_position_total_exposure_and_max_names_are_enforced(self):
        with self.assertRaisesRegex(ValueError, '现金'):
            paper.record_trade('a', 'BUY', 2000, self.day(299))
        with self.assertRaisesRegex(ValueError, '单标的'):
            paper.record_trade('a', 'BUY', 201, self.day(299))
        storage.set_setting('paper_risk', {**paper.settings(), 'max_position': .9, 'max_exposure': .1})
        with self.assertRaisesRegex(ValueError, '总仓位'):
            paper.record_trade('a', 'BUY', 101, self.day(299))
        storage.set_setting('paper_risk', {**paper.settings(), 'max_exposure': .9, 'max_holdings': 1})
        paper.record_trade('a', 'BUY', 10, self.day(298))
        storage.register_dataset('b', 'B', 'B', 'synthetic', self.frame)
        with self.assertRaisesRegex(ValueError, '标的数'):
            paper.record_trade('b', 'BUY', 10, self.day(299))

    def test_record_id_makes_identical_retries_idempotent(self):
        key = str(uuid.uuid4())
        first = paper.record_trade('a', 'BUY', 10, self.day(299), key)
        second = paper.record_trade('a', 'BUY', 10, self.day(299), key)
        self.assertEqual(first, second)
        self.assertEqual(len(paper.trades()), 1)
        with self.assertRaisesRegex(ValueError, '参数不同'):
            paper.record_trade('a', 'BUY', 11, self.day(299), key)

    def test_concurrent_retries_only_create_one_fill(self):
        key = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: paper.record_trade('a', 'BUY', 10, self.day(299), key), range(4)))
        self.assertEqual(len(paper.trades()), 1)
        self.assertTrue(all(row['id'] == key for row in results))

    def test_full_sale_returns_to_cash_and_keeps_history(self):
        paper.record_trade('a', 'BUY', 10, self.day(298))
        paper.record_trade('a', 'SELL', 10, self.day(299))
        result = paper.portfolio()
        self.assertEqual(result['positions'], [])
        self.assertEqual(result['cash'], 100000)
        self.assertEqual(len(result['history']), 2)

    def test_history_cannot_be_backdated_or_valued_before_latest_trade(self):
        paper.record_trade('a', 'BUY', 10, self.day(299))
        with self.assertRaisesRegex(ValueError, '时间顺序'):
            paper.record_trade('a', 'BUY', 1, self.day(298))
        with self.assertRaisesRegex(ValueError, '最后一笔'):
            paper.portfolio(as_of=self.day(298))

    def test_fees_and_slippage_are_charged_and_marked_at_close(self):
        storage.set_setting('paper_risk', {**paper.settings(), 'fee_bps': 5, 'slippage_bps': 2})
        trade = paper.record_trade('a', 'BUY', 10, self.day(299))
        result = paper.portfolio()
        self.assertAlmostEqual(trade['price'], 100.02)
        self.assertAlmostEqual(result['equity'], 100000 - 10 * .02 - trade['fee'])

    def test_drawdown_cooldown_expires_in_observed_record_days(self):
        for index in range(292, 300):
            set_close(self.frame, index, 60)
        storage.register_dataset('a', 'A', 'A', 'synthetic', self.frame)
        storage.set_setting('paper_risk', {**paper.settings(), 'max_position': .9, 'max_exposure': .95})
        paper.record_trade('a', 'BUY', 500, self.day(290))
        during = paper.portfolio(as_of=self.day(293))
        self.assertFalse(during['risk_status']['can_buy'])
        self.assertIn('达到参考止损线', during['positions'][0]['flags'])
        after = paper.portfolio(as_of=self.day(299))
        self.assertTrue(after['risk_status']['can_buy'])

    def test_three_losing_records_halve_effective_position_cap(self):
        for index, close in zip([296, 297, 298, 299], [100, 99, 98, 97]):
            set_close(self.frame, index, close)
        storage.register_dataset('a', 'A', 'A', 'synthetic', self.frame)
        paper.record_trade('a', 'BUY', 10, self.day(296))
        result = paper.portfolio()
        self.assertEqual(result['risk_status']['consecutive_loss_days'], 3)
        self.assertEqual(result['risk_status']['effective_position_cap'], .1)
