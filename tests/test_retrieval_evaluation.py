import unittest

from scripts.evaluate_retrieval import score_rankings


class RetrievalEvaluationTests(unittest.TestCase):
    def test_scores_recall_hit_rate_and_mrr(self):
        rankings = [
            (["a", "b", "c"], {"b", "c"}),
            (["d", "e", "f"], {"x"}),
        ]

        self.assertEqual(
            {
                "recall@1": 0.0,
                "hit_rate@1": 0.0,
                "recall@3": 0.5,
                "hit_rate@3": 0.5,
                "mrr": 0.25,
            },
            score_rankings(rankings, [1, 3]),
        )


if __name__ == "__main__":
    unittest.main()
