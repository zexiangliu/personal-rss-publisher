import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from feedback import rate_item, recent_items, split_tags


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


class FeedbackTests(unittest.TestCase):
    def test_rate_item_records_curation_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            item_path = tmp_path / "items.discovered.jsonl"
            feedback_path = tmp_path / "feedback.jsonl"
            item_path.write_text(
                json.dumps(
                    {
                        "id": "arxiv:2608.12345",
                        "title": "Safe RL Paper",
                        "url": "https://arxiv.org/abs/2608.12345",
                        "source": "arXiv",
                        "category": "research",
                        "topic_id": "safe-reinforcement-learning",
                        "topic": "Safe Reinforcement Learning",
                        "published_at": NOW.isoformat(),
                        "curation": {
                            "overall_score": 8.4,
                            "keywords": ["safe exploration"],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            record = rate_item(
                "arxiv:2608.12345",
                1,
                ["useful"],
                "Good fit",
                feedback_path=feedback_path,
                item_paths=[item_path],
                now=NOW,
            )

            self.assertEqual(record["rating"], 1)
            self.assertEqual(record["topic_id"], "safe-reinforcement-learning")
            self.assertEqual(record["source"], "arXiv")
            self.assertEqual(record["keywords"], ["safe exploration"])

            written = json.loads(feedback_path.read_text(encoding="utf-8"))
            self.assertEqual(written["item_id"], "arxiv:2608.12345")

    def test_recent_items_filters_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            item_path = tmp_path / "items.jsonl"
            item_path.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in [
                        {
                            "id": "news",
                            "title": "News",
                            "category": "news",
                            "published_at": "2026-08-27T11:00:00+00:00",
                        },
                        {
                            "id": "paper",
                            "title": "Paper",
                            "category": "research",
                            "published_at": "2026-08-27T12:00:00+00:00",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            items = recent_items("research", 10, item_paths=[item_path])

            self.assertEqual([item["id"] for item in items], ["paper"])

    def test_split_tags_accepts_repeated_and_comma_separated_values(self):
        self.assertEqual(split_tags(["useful,safe-rl", "math"]), ["useful", "safe-rl", "math"])


if __name__ == "__main__":
    unittest.main()
