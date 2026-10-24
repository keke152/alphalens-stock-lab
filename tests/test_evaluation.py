import unittest

import numpy as np

from app.data import generate_demo
from app.engine import _config
from app.evaluation import backtest, choose_threshold, data_quality, probability_diagnostics, time_splits


class EvaluationTests(unittest.TestCase):
    def test_walk_forward_is_disjoint_contiguous_and_purged(self):
        for samples in (279, 979):
            for count in (3, 4, 5):
                with self.subTest(samples=samples, folds=count):
                    folds = time_splits(samples, "walk_forward", count)
                    indices = []
                    for fold in folds:
                        self.assertLess(fold["train"][-1] + 1, fold["validation"][0])
                        self.assertLess(fold["validation"][-1] + 1, fold["test"][0])
                        self.assertTrue(np.all(np.diff(fold["test"]) == 1))
                        indices.extend(fold["test"].tolist())
                    self.assertEqual(indices, list(range(int(samples * .6) + 1, samples)))
                    self.assertEqual(len(indices), len(set(indices)))
                    self.assertTrue(all(len(a["train"]) < len(b["train"]) for a, b in zip(folds, folds[1:])))

    def test_holdout_keeps_original_boundaries(self):
        fold, = time_splits(979, "holdout", 3)
        self.assertEqual(len(fold["train"]), int(979 * .6))
        self.assertEqual(fold["test"][0], int(979 * .8) + 1)

    def test_fee_formula_and_cash_days(self):
        settings = _config({"cost_bps": 5, "slippage_bps": 2})
        actual = np.array([.02, -.3])
        result = backtest(actual, np.array([True, False]), settings)
        expected = 1.02 * .9998 * .9995 / (1.0002 * 1.0005)
        self.assertAlmostEqual(result["strategy"][-1], 100000 * expected)
        self.assertEqual(result["daily"][1], 0)
        self.assertEqual(result["metrics"]["trades"], 1)

    def test_no_trades_leaves_cash_and_zero_drawdown(self):
        result = backtest(np.array([.05, -.1]), np.array([False, False]), _config({}))
        self.assertEqual(result["metrics"]["total_return"], 0)
        self.assertEqual(result["metrics"]["max_drawdown"], 0)
        self.assertEqual(result["metrics"]["sharpe"], 0)

    def test_costs_monotonically_reduce_fixed_signal_equity(self):
        actual, active = np.array([.02, -.01, .03]), np.array([True, False, True])
        equities = [backtest(actual, active, _config({"cost_bps": cost}))["strategy"][-1] for cost in (0, 5, 10, 20)]
        self.assertTrue(all(a > b for a, b in zip(equities, equities[1:])))

    def test_validation_selection_excludes_low_trade_candidates(self):
        settings = _config({"threshold_mode": "validation", "cost_bps": 0, "slippage_bps": 0})
        probability = np.array([.51] * 5 + [.7] * 5 + [.95])
        actual = np.array([-.05] * 5 + [.03] * 5 + [.1])
        threshold, analysis = choose_threshold(probability, actual, settings)
        self.assertAlmostEqual(threshold, .55)
        self.assertEqual(analysis["minimum_trades"], 5)
        self.assertTrue(all(not row["eligible"] for row in analysis["candidates"] if row["threshold"] > .7))

    def test_no_eligible_threshold_falls_back_to_manual(self):
        selected, analysis = choose_threshold(np.zeros(30), np.full(30, .02), _config({"threshold_mode": "validation", "threshold": .63}))
        self.assertEqual(selected, .63)
        self.assertTrue(analysis["fallback"])

    def test_manual_mode_never_uses_best_validation_candidate(self):
        selected, analysis = choose_threshold(np.full(30, .6), np.full(30, -.02), _config({"threshold": .51}))
        self.assertEqual(selected, .51)
        self.assertFalse(analysis["fallback"])

    def test_probability_bins_include_zero_one_and_have_no_duplicates(self):
        probability = np.array([0, .2, .4, .6, .8, 1.])
        actual = np.array([-.1, .1, -.1, .1, -.1, .1])
        result = probability_diagnostics(actual, probability)
        self.assertEqual(sum(row["count"] for row in result["reliability_bins"]), 6)
        self.assertEqual(result["reliability_bins"][-1]["count"], 2)
        self.assertEqual(sum(result["confusion"].values()), 6)
        self.assertAlmostEqual(result["brier_score"], np.mean((probability - (actual > 0)) ** 2))

    def test_quality_checks_are_descriptive_and_preserve_frame(self):
        frame = generate_demo(rows=300)
        frame.loc[0, "volume"] = 0
        before = frame.copy(deep=True)
        result = data_quality(frame)
        self.assertEqual(result["checks"][0]["count"], 1)
        self.assertTrue(frame.equals(before))

    def test_invalid_evaluation_config_rejected(self):
        for config in ({"folds": True}, {"folds": 6}, {"evaluation": "random"}, {"threshold_mode": "test"}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                _config(config)
