import json
import unittest
from collections import Counter

from agent_knowledge_bridge.live_benchmark import (
    build_live_benchmark,
    extended_paired_metrics,
    score_model_output,
)


class LiveBenchmarkTests(unittest.TestCase):
    def test_dataset_has_exactly_100_cases_and_expected_categories(self):
        dataset = build_live_benchmark()
        self.assertEqual(len(dataset["cases"]), 100)
        self.assertEqual(len({case["id"] for case in dataset["cases"]}), 100)
        self.assertEqual(Counter(case["category"] for case in dataset["cases"]), {
            "exact_retrieval": 35,
            "cross_language_paraphrase": 20,
            "project_scope_isolation": 15,
            "cross_agent_user_scope": 10,
            "conflict_and_staleness": 10,
            "irrelevant_no_memory": 10,
        })
        keys = {item["key"] for item in dataset["knowledge"]}
        self.assertTrue(all(case["expected_key"] in keys
                            for case in dataset["cases"] if case["expected_key"]))

    def test_output_scoring_separates_task_success_from_memory_reporting(self):
        output = json.dumps({
            "policy_code": "MW-E001", "result": 7,
            "used_memory_ids": ["kn_wrong"],
        })
        score = score_model_output(output, {"policy_code": "MW-E001", "result": 7},
                                   ["kn_right"], "kn_right")
        self.assertTrue(score["success"])
        self.assertFalse(score["expected_memory_reported"])
        self.assertEqual(score["hallucinated_memory_ids"], ["kn_wrong"])
        invalid = score_model_output("not json", {"policy_code": "x", "result": 1}, [], None)
        self.assertFalse(invalid["success"])
        self.assertIsNotNone(invalid["parse_error"])

    def test_extended_metrics_report_retrieval_gap_and_categories(self):
        rows = []
        for task_id, category, results in (
            ("a", "exact", (False, True, True)),
            ("b", "semantic", (False, False, True)),
        ):
            for mode, success in zip(("baseline", "auto", "oracle"), results, strict=True):
                rows.append({"task_id": task_id, "repeat": 1, "category": category,
                             "mode": mode, "success": success, "failure_kind": None})
        metrics = extended_paired_metrics(rows)
        self.assertEqual(metrics["complete_triples"], 2)
        self.assertEqual(metrics["success_rate"]["auto"], .5)
        self.assertEqual(metrics["oracle_minus_auto"], .5)
        self.assertEqual(metrics["helped"], 1)
        self.assertEqual(metrics["by_category"]["semantic"]["success_rate"]["oracle"], 1)


if __name__ == "__main__":
    unittest.main()
