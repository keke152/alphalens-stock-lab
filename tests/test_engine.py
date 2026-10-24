import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import joblib
import numpy as np

from app import engine
from app.data import generate_demo
from app.evaluation import time_splits


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frame = generate_demo(rows=300)

    def test_features_do_not_see_future_prices(self):
        before = engine.build_features(self.frame)
        changed = self.frame.copy()
        changed.loc[170:, ["open", "high", "low", "close"]] *= 1.5
        changed.loc[170:, "volume"] *= 3
        after = engine.build_features(changed)
        np.testing.assert_allclose(before.iloc[:170], after.iloc[:170], equal_nan=True)

    def test_every_fold_fits_its_own_scaler_using_training_only(self):
        models = []
        original = engine._models

        def factory(config):
            pair = original(config)
            models.append(pair)
            return pair

        with patch.object(engine, "_models", side_effect=factory):
            report = engine.run_experiment(self.frame, {"model": "logistic", "evaluation": "walk_forward", "threshold_mode": "validation"})
        x = engine.build_features(self.frame).iloc[20:-1]
        splits = time_splits(len(x), "walk_forward", 3)
        self.assertEqual(len(models), 3)
        for pair, split in zip(models, splits):
            for estimator in pair[:2]:
                np.testing.assert_allclose(estimator.named_steps["standardscaler"].mean_, x.iloc[split["train"]].mean())
        dates = [row["date"] for row in report["predictions"]]
        self.assertEqual(dates, sorted(set(dates)))
        expected = 100000 * np.prod([1 + row["return"] for row in report["equity"]])
        self.assertAlmostEqual(report["equity"][-1]["strategy"], expected, places=7)
        for row in report["predictions"]:
            self.assertEqual(row["signal"], "BUY" if row["probability_up"] >= row["threshold"] else "WAIT")
        self.assertEqual(report["latest"]["threshold"], report["evaluation"]["folds"][-1]["threshold"])
        json.dumps(report, allow_nan=False)

    def test_test_outcomes_cannot_change_validation_threshold(self):
        config = {"model": "logistic", "threshold_mode": "validation"}
        before = engine.run_experiment(self.frame, config)
        changed = self.frame.copy()
        changed.loc[260:, ["open", "high", "low", "close"]] *= np.linspace(1, 1.5, len(changed) - 260)[:, None]
        after = engine.run_experiment(changed, config)
        self.assertEqual(before["evaluation"]["folds"][0]["threshold_analysis"], after["evaluation"]["folds"][0]["threshold_analysis"])
        self.assertEqual(before["latest"]["model_train_end"], after["latest"]["model_train_end"])

    def test_model_artifact_keeps_effective_threshold_and_time_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'model.joblib'
            report = engine.run_experiment(self.frame, {"model": "logistic", "threshold_mode": "validation"}, path)
            artifact = joblib.load(path)
            self.assertEqual(artifact["effective_threshold"], report["latest"]["threshold"])
            self.assertEqual(artifact["model_train_end"], report["split"]["train"]["target_end"])
            self.assertEqual(artifact["format_version"], 2)

    def test_random_forest_has_all_required_outputs(self):
        report = engine.run_experiment(self.frame, {"model": "random_forest"})
        self.assertEqual(len(report["features"]), 18)
        self.assertEqual(report["evaluation"]["fold_count"], 1)
        self.assertEqual(report["cost_sensitivity"][2]["total_return"], report["metrics"]["total_return"])
        self.assertEqual(report["cost_sensitivity"][2]["trades"], report["metrics"]["trades"])

    def test_same_seed_reproduces_predictions(self):
        first = engine.run_experiment(self.frame, {"model": "logistic"})
        second = engine.run_experiment(self.frame, {"model": "logistic"})
        self.assertEqual(first["predictions"], second["predictions"])
