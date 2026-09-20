import json
import unittest
from collections import Counter
from pathlib import Path


class QualityDatasetTests(unittest.TestCase):
    def test_v2_quality_dataset_has_required_thirty_case_distribution(self):
        path = Path(__file__).parent / "fixtures" / "rag_v2_cases.json"
        cases = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(30, len(cases))
        self.assertEqual(30, len({case["id"] for case in cases}))
        self.assertEqual(
            {"knowledge": 8, "web": 8, "mixed": 8, "cold": 2, "insufficient": 2, "injection": 2},
            dict(Counter(case["category"] for case in cases)),
        )
        for case in cases:
            self.assertIn(case["search_mode"], {"knowledge", "web", "auto"})
            self.assertTrue(case["question"].strip())
            self.assertTrue(set(case["expected_source_types"]) <= {"knowledge", "web"})
            if fixture := case.get("document_fixture"):
                self.assertTrue((path.parent / fixture).is_file(), fixture)
            if case["category"] == "knowledge":
                self.assertEqual("knowledge", case["search_mode"])
                self.assertEqual(["knowledge"], case["expected_source_types"])
            if case["category"] == "web":
                self.assertEqual("web", case["search_mode"])
                self.assertEqual(["web"], case["expected_source_types"])
                self.assertTrue(case.get("web_query"))
            if case["category"] == "mixed":
                self.assertEqual("auto", case["search_mode"])
                self.assertEqual({"knowledge", "web"}, set(case["expected_source_types"]))


if __name__ == "__main__":
    unittest.main()
