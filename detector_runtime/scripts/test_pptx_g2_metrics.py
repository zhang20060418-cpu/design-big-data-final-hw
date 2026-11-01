"""Independent synthetic fixtures; never reads a benchmark or model result.

Run with the isolated ML Python. An optional --evidence-dir writes fixture-only
test status and source hashes, but no official evaluation is executed here.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import unittest

import numpy as np
from sklearn.metrics import average_precision_score, confusion_matrix

import pptx_g2_metrics as metrics


def repeated_label(y, probabilities):
    return np.tile(np.asarray(y)[:, None], (1, 4)), np.tile(np.asarray(probabilities)[:, None], (1, 4))


def endpoint(result, model, mode, label, metric, stratum="all"):
    matching = [row for row in result["summary"] if row["stratum"] == stratum and row["model"] == model
                and row["mode"] == mode and row["label"] == label and row["metric"] == metric]
    if len(matching) != 1:
        raise AssertionError(f"Endpoint identity is not unique: {matching}")
    row = matching[0]
    return row, result["replicates"][row["replicate_key"]]


class IndependentMetricsFixtures(unittest.TestCase):
    def setUp(self):
        # Class support and probabilities are deliberately invented for fixtures.
        self.y, self.p = repeated_label([1, 0, 1, 0], [0.9, 0.8, 0.7, 0.1])
        self.threshold = [0.75] * 4

    def test_hand_calculated_ap_confusions_precision_recall_f1(self):
        result = metrics.evaluate(self.y, self.p, self.threshold)
        for row in result["labels"]:
            self.assertAlmostEqual(row["ap"], (1.0 + 2.0 / 3.0) / 2.0)
            self.assertEqual((row["tp"], row["fp"], row["tn"], row["fn"]), (1, 1, 1, 1))
            self.assertEqual((row["n_pos"], row["n_neg"], row["predicted_positive"]), (2, 2, 2))
            self.assertEqual((row["precision"], row["recall"], row["f1"], row["fpr"]), (0.5, 0.5, 0.5, 0.5))
        self.assertEqual(result["macro"]["f1"], 0.5)
        self.assertEqual(result["micro"]["f1"], 0.5)
        self.assertEqual(result["page"]["review_trigger_rate"], 0.5)
        self.assertEqual(result["page"]["all_negative_page_fpr"], 0.5)
        self.assertEqual(result["page"]["defect_page_hit_rate"], 0.5)
        self.assertEqual(result["page"]["mean_alerts_per_page"], 2.0)

    def test_threshold_equality_is_positive(self):
        result = metrics.evaluate(self.y, self.p, [0.7] * 4)
        self.assertEqual(result["labels"][0]["tp"], 2)

    def test_class_one_not_column_one(self):
        probability = np.array([[0.8, 0.2], [0.3, 0.7]])
        np.testing.assert_array_equal(metrics.positive_class_probability(probability, [1, 0]), [0.8, 0.3])
        np.testing.assert_array_equal(metrics.positive_class_probability(probability, [0, 1]), [0.2, 0.7])
        with self.assertRaises(ValueError):
            metrics.positive_class_probability(probability, [0, 2])

    def test_missing_class_and_zero_prediction_are_distinct(self):
        truth = self.y.copy()
        truth[:, 0] = 0
        truth[:, 1] = 1
        result = metrics.evaluate(truth, self.p, [1.0] * 4)
        for row in result["labels"][:2]:
            for key in ("ap", "precision", "recall", "f1"):
                self.assertIsNone(row[key])
        self.assertEqual(result["labels"][0]["fpr"], 0.0)
        self.assertIsNone(result["labels"][1]["fpr"])
        self.assertIsNone(result["labels"][2]["precision"])
        self.assertEqual(result["labels"][2]["recall"], 0.0)
        self.assertEqual(result["labels"][2]["f1"], 0.0)
        self.assertIsNotNone(result["labels"][2]["ap"])
        self.assertIsNone(result["macro"]["f1"])
        self.assertIsNone(result["micro"]["f1"])
        self.assertEqual(result["macro_available"]["f1"]["k"], 2)
        self.assertEqual(result["macro_available"]["f1"]["labels"], list(metrics.LABELS[2:]))

    def test_page_defect_detection_requires_correct_label(self):
        truth = np.array([[1, 0, 0, 0], [0, 0, 0, 0], [0, 1, 0, 0]])
        probability = np.array([[0.1, 0.9, 0.1, 0.1], [0.9, 0.1, 0.1, 0.1], [0.1, 0.8, 0.1, 0.1]])
        result = metrics.evaluate(truth, probability, [0.5] * 4)
        self.assertEqual(result["page"]["review_trigger_rate"], 1.0)
        self.assertEqual(result["page"]["all_negative_page_fpr"], 1.0)
        self.assertEqual(result["page"]["defect_page_hit_rate"], 0.5)
        self.assertEqual(result["page"]["n_correct_hit_pages"], 1)

    def test_empty_strata_and_absent_page_denominators(self):
        empty = metrics.evaluate(self.y, self.p, self.threshold, mask=np.zeros(4, dtype=bool))
        self.assertEqual(empty["page"]["n_pages"], 0)
        self.assertIsNone(empty["macro"]["ap"])
        for key in ("review_trigger_rate", "all_negative_page_fpr", "defect_page_hit_rate", "mean_alerts_per_page"):
            self.assertIsNone(empty["page"][key])
        all_negative = metrics.evaluate(np.zeros_like(self.y), self.p, self.threshold)
        self.assertIsNone(all_negative["page"]["defect_page_hit_rate"])
        self.assertEqual(all_negative["page"]["all_negative_page_fpr"], 0.5)
        all_positive = metrics.evaluate(np.ones_like(self.y), self.p, self.threshold)
        self.assertIsNone(all_positive["page"]["all_negative_page_fpr"])

    def test_feature_and_input_validation(self):
        valid = np.ones((3, 512), dtype=np.float32)
        self.assertIs(metrics.validate_feature_matrix(valid, 3, 512), valid)
        for broken in (np.zeros((3, 512)), np.full((3, 512), np.nan), np.ones((3, 511))):
            with self.assertRaises(ValueError):
                metrics.validate_feature_matrix(broken, 3, 512)
        for broken_y, broken_p in ((self.y.astype(float) + 0.5, self.p), (self.y, self.p * np.nan),
                                    (self.y, self.p[:, :3]), (self.y, self.p + 1)):
            with self.assertRaises(ValueError):
                metrics.evaluate(broken_y, broken_p, self.threshold)
        with self.assertRaises(ValueError):
            metrics.evaluate(self.y, self.p, [0.5] * 3)
        with self.assertRaises(ValueError):
            metrics.evaluate(self.y, self.p, self.threshold, mask=[1, 1, 0, 0])
        with self.assertRaises(ValueError):
            metrics.evaluate(self.y, self.p, self.threshold, weights=[1, -1, 1, 1])

    def test_weighted_ap_ties_and_counts_against_sklearn(self):
        rng = np.random.default_rng(177)
        for _ in range(30):
            truth = rng.integers(0, 2, size=(17, 4))
            probability = rng.integers(0, 6, size=(17, 4)) / 5
            weights = rng.integers(0, 4, size=17)
            result = metrics.evaluate(truth, probability, [0.4] * 4, weights=weights)
            for j, row in enumerate(result["labels"]):
                self.assertGreater(row["n_pos"], 0)
                self.assertGreater(row["n_neg"], 0)
                self.assertAlmostEqual(row["ap"], average_precision_score(truth[:, j], probability[:, j], sample_weight=weights))
                tn, fp, fn, tp = confusion_matrix(truth[:, j], probability[:, j] >= 0.4, sample_weight=weights, labels=[0, 1]).ravel()
                self.assertEqual((row["tp"], row["fp"], row["tn"], row["fn"]), (tp, fp, tn, fn))

    def test_bootstrap_exact_against_explicit_repeated_whole_clusters(self):
        rng = np.random.default_rng(9)
        truth = rng.integers(0, 2, size=(12, 4))
        probability = rng.integers(0, 8, size=(12, 4)) / 7
        clusters = np.array(["c", "a", "b", "c", "c", "a", "b", "c", "c", "a", "b", "c"])
        strata = {"all": np.ones(12, dtype=bool), "subset": np.arange(12) % 2 == 0,
                  "empty": np.zeros(12, dtype=bool)}
        bootstrap = metrics.paired_cluster_bootstrap(truth, {"A": probability}, {"A": {"f1": [0.4] * 4}},
                                                     strata, clusters, B=25, seed=42)
        self.assertEqual(bootstrap["cluster_ids"], ["a", "b", "c"])
        np.testing.assert_array_equal(bootstrap["draw_counts"].sum(axis=1), np.full(25, 3))
        for repeat, counts in enumerate(bootstrap["draw_counts"]):
            selected = np.concatenate([np.tile(np.flatnonzero(clusters == cluster), int(count))
                                       for cluster, count in zip(bootstrap["cluster_ids"], counts)])
            for stratum, mask in strata.items():
                point = metrics.evaluate(truth[selected], probability[selected], [0.4] * 4, mask=mask[selected])
                for j, label in enumerate(metrics.LABELS):
                    for metric in ("ap", "precision", "recall", "f1", "fpr"):
                        _, values = endpoint(bootstrap, "A", "shared" if metric == "ap" else "f1", label, metric, stratum)
                        expected = point["labels"][j][metric]
                        actual = values["values"][repeat]
                        if expected is None:
                            self.assertTrue(np.isnan(actual))
                            self.assertNotEqual(values["reason_codes"][repeat], 0)
                        else:
                            self.assertAlmostEqual(actual, expected)
                for label, metric in (("macro", "ap"), ("macro", "precision"), ("macro", "f1"),
                                      ("micro", "precision"), ("micro", "f1"),
                                      ("page", "review_trigger_rate"), ("page", "all_negative_page_fpr"),
                                      ("page", "defect_page_hit_rate"), ("page", "mean_alerts_per_page")):
                    _, values = endpoint(bootstrap, "A", "shared" if metric == "ap" else "f1", label, metric, stratum)
                    expected = point[label][metric]
                    actual = values["values"][repeat]
                    self.assertTrue(np.isnan(actual)) if expected is None else self.assertAlmostEqual(actual, expected)

    def test_reproducibility_pairing_and_ci(self):
        # Each source contains both classes, hence no class-support loss.
        truth, probability = repeated_label([0, 1] * 3, [0.1, 0.8, 0.4, 0.9, 0.2, 0.7])
        clusters = np.array(["source2", "source2", "source1", "source1", "source3", "source3"])
        models = {name: probability.copy() for name in ("A", "C", "H", "C9")}
        thresholds = {name: {"f1": [0.5] * 4, "recall": [0.2] * 4} for name in models}
        args = (truth, models, thresholds, {"all": np.ones(6, dtype=bool)}, clusters)
        first = metrics.paired_cluster_bootstrap(*args, B=200, seed=42)
        second = metrics.paired_cluster_bootstrap(*args, B=200, seed=42)
        np.testing.assert_array_equal(first["draw_counts"], second["draw_counts"])
        self.assertEqual(first["summary"], second["summary"])
        row, values = endpoint(first, "C-A", "shared", "macro", "ap")
        self.assertEqual((row["point"], row["ci_low"], row["ci_high"]), (0, 0, 0))
        self.assertEqual(row["n_valid"], 200)
        self.assertEqual(row["ci_status"], "exploratory_less_than_10_clusters")
        np.testing.assert_array_equal(values["values"], np.zeros(200))
        self.assertEqual(first["reason_definitions"], second["reason_definitions"])

    def test_invalid_replicates_never_redrawn_and_ci_gate(self):
        # Positive and negative pages live in different whole source clusters.
        truth, probability = repeated_label([1, 1, 0, 0], [0.9, 0.7, 0.6, 0.2])
        result = metrics.paired_cluster_bootstrap(truth, {"A": probability}, {"A": {"f1": [0.5] * 4}},
                                                  {"all": np.ones(4, dtype=bool), "one_source": np.array([1, 1, 0, 0], dtype=bool)},
                                                  ["positive", "positive", "negative", "negative"], B=2000, seed=42)
        row, values = endpoint(result, "A", "shared", "macro", "ap")
        expected_valid = (result["draw_counts"] > 0).all(axis=1)
        np.testing.assert_array_equal(values["reason_codes"] == 0, expected_valid)
        self.assertEqual(row["n_valid"], int(expected_valid.sum()))
        self.assertEqual(row["n_invalid"], 2000 - row["n_valid"])
        self.assertEqual(row["ci_status"], "insufficient_valid_fraction")
        self.assertIsNone(row["ci_low"])
        self.assertEqual(sum(item["count"] for item in row["invalid_reason_counts"].values()), row["n_invalid"])
        source_row, _ = endpoint(result, "A", "f1", "page", "review_trigger_rate", "one_source")
        self.assertEqual(source_row["n_clusters"], 1)
        self.assertEqual(source_row["ci_status"], "insufficient_clusters")
        self.assertIsNone(source_row["ci_low"])

    def test_full_output_callback_and_serialization(self):
        captured = []
        result = metrics.paired_cluster_bootstrap(self.y, {"A": self.p}, {"A": {"f1": self.threshold}},
                                                  {"all": np.ones(4, dtype=bool)}, ["g1", "g1", "g2", "g2"],
                                                  B=12, keep_replicates=False,
                                                  callback=lambda row, values, codes: captured.append((row, values.copy(), codes.copy())))
        self.assertEqual(result["replicates"], {})
        # Shared AP: 4 labels + macro; mode: 4*4 label +3macro+3micro+4page.
        self.assertEqual(len(result["summary"]), 5 + 16 + 3 + 3 + 4)
        self.assertEqual(len(captured), len(result["summary"]))
        json.dumps(result["summary"], ensure_ascii=False, allow_nan=False)
        for row, values, codes in captured:
            self.assertEqual(values.shape, (12,))
            self.assertEqual(codes.dtype, np.uint8)
            self.assertEqual(row["n_valid"] + row["n_invalid"], 12)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(IndependentMetricsFixtures)
    names = [test.id() for test in suite]
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if args.evidence_dir:
        args.evidence_dir.mkdir(parents=True, exist_ok=True)
        files = [Path(__file__), Path(metrics.__file__)]
        evidence = {
            "fixture_type": "independent_synthetic_only",
            "formal_benchmark_inputs_read": False, "model_inference": False,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "tests_run": result.testsRun, "success": result.wasSuccessful(), "test_names": names,
            "failures": [{"test": test.id(), "traceback": trace} for test, trace in result.failures],
            "errors": [{"test": test.id(), "traceback": trace} for test, trace in result.errors],
            "source_sha256": {file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in files},
            "numpy_version": np.__version__,
        }
        (args.evidence_dir / "metrics_fixture_results.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
