import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from curate_research import (
    ResearchCandidate,
    curate,
    deterministic_review,
    extract_response_text,
    parse_arxiv_feed,
)


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)

SAFE_RL_TOPIC = {
    "id": "safe-reinforcement-learning",
    "name": "Safe Reinforcement Learning",
    "include_keywords": [
        "safe reinforcement learning",
        "constrained reinforcement learning",
        "safe exploration",
    ],
    "rank_keywords": {
        "safe reinforcement learning": 3.2,
        "constrained reinforcement learning": 2.8,
        "safe exploration": 2.4,
    },
    "publish_threshold": 7.0,
}

DEFAULTS = {
    "enabled": True,
    "category": "research",
    "lookback_days": 45,
    "max_candidates_per_topic": 10,
    "max_items_per_topic": 4,
    "summary_max_chars": 900,
    "publish_threshold": 6.8,
    "preferred_categories": ["cs.LG", "cs.AI", "stat.ML"],
    "agent_review": {
        "enabled": False,
        "model": "gpt-5-mini",
        "max_items_per_run": 8,
        "min_deterministic_score": 5.5,
        "timeout_seconds": 45,
    },
}


def arxiv_feed(entries: list[str]) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  {"".join(entries)}
</feed>
"""


def arxiv_entry(
    arxiv_id: str,
    title: str,
    summary: str,
    published: str = "2026-08-26T12:00:00Z",
) -> str:
    return f"""
<entry>
  <id>http://arxiv.org/abs/{arxiv_id}</id>
  <updated>{published}</updated>
  <published>{published}</published>
  <title>{title}</title>
  <summary>{summary}</summary>
  <author><name>Ada Researcher</name></author>
  <author><name>Grace Scientist</name></author>
  <category term="cs.LG" />
  <category term="cs.AI" />
  <link title="pdf" type="application/pdf" href="http://arxiv.org/pdf/{arxiv_id}" />
</entry>
"""


class ResearchCurationTests(unittest.TestCase):
    def test_parse_arxiv_feed_normalizes_recent_entries(self):
        xml = arxiv_feed(
            [
                arxiv_entry(
                    "2608.12345v2",
                    "Safe Exploration for Constrained Reinforcement Learning",
                    "A paper about safe reinforcement learning.",
                )
            ]
        )

        candidates = parse_arxiv_feed(
            xml,
            SAFE_RL_TOPIC,
            DEFAULTS,
            NOW,
            "https://export.arxiv.org/api/query?search_query=test",
        )

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.id, "arxiv:2608.12345")
        self.assertEqual(candidate.url, "https://arxiv.org/abs/2608.12345")
        self.assertEqual(candidate.pdf_url, "https://arxiv.org/pdf/2608.12345v2")
        self.assertEqual(candidate.categories, ("cs.LG", "cs.AI"))

    def test_deterministic_review_publishes_strong_safe_rl_match(self):
        candidate = ResearchCandidate(
            id="arxiv:2608.12345",
            title="Safe Exploration for Constrained Reinforcement Learning",
            url="https://arxiv.org/abs/2608.12345",
            source="arXiv",
            source_url="https://export.arxiv.org/api/query",
            source_type="arxiv",
            topic_id="safe-reinforcement-learning",
            topic_name="Safe Reinforcement Learning",
            summary=(
                "This safe reinforcement learning paper studies constrained "
                "reinforcement learning and safe exploration under practical "
                "policy optimization settings. "
            )
            * 8,
            published_at=NOW,
            authors=("Ada Researcher", "Grace Scientist"),
            categories=("cs.LG",),
        )

        review = deterministic_review(candidate, SAFE_RL_TOPIC, DEFAULTS, [], NOW)

        self.assertEqual(review.decision, "publish")
        self.assertGreaterEqual(review.overall_score, 7.0)
        self.assertIn("safe exploration", review.keywords)

    def test_curate_appends_only_publishable_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_path = tmp_path / "items.discovered.jsonl"
            cache_path = tmp_path / "research_candidates.jsonl"
            config_path = tmp_path / "research_topics.json"
            config_path.write_text(
                json.dumps(
                    {
                        "output_path": str(output_path),
                        "candidate_cache_path": str(cache_path),
                        "feedback_path": str(tmp_path / "feedback.jsonl"),
                        "dedupe_against": [str(tmp_path / "items.jsonl")],
                        "defaults": DEFAULTS,
                        "topics": [
                            {
                                **SAFE_RL_TOPIC,
                                "arxiv_query": "cat:cs.LG AND safe",
                                "max_items_per_topic": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            xml = arxiv_feed(
                [
                    arxiv_entry(
                        "2608.12345v1",
                        "Safe Exploration for Constrained Reinforcement Learning",
                        (
                            "Safe reinforcement learning for constrained "
                            "reinforcement learning and safe exploration. "
                        )
                        * 8,
                    ),
                    arxiv_entry(
                        "2608.99999v1",
                        "A Benchmark for Image Classification",
                        "This paper is about image classification.",
                    ),
                ]
            )

            with patch("curate_research.fetch_text", return_value=xml):
                count = curate(config_path, now=NOW, use_agent=False)

            self.assertEqual(count, 1)
            published = [
                json.loads(line)
                for line in output_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(published[0]["id"], "arxiv:2608.12345")
            self.assertEqual(published[0]["curation"]["decision"], "publish")

            cached = [
                json.loads(line)
                for line in cache_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(cached), 2)

    def test_curate_does_not_backfill_lower_ranked_items_when_top_item_is_known(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_path = tmp_path / "items.discovered.jsonl"
            cache_path = tmp_path / "research_candidates.jsonl"
            output_path.write_text(
                json.dumps(
                    {
                        "id": "arxiv:2608.12345",
                        "title": "Already published",
                        "url": "https://arxiv.org/abs/2608.12345",
                        "category": "research",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config_path = tmp_path / "research_topics.json"
            config_path.write_text(
                json.dumps(
                    {
                        "output_path": str(output_path),
                        "candidate_cache_path": str(cache_path),
                        "feedback_path": str(tmp_path / "feedback.jsonl"),
                        "dedupe_against": [str(tmp_path / "items.jsonl")],
                        "defaults": DEFAULTS,
                        "topics": [
                            {
                                **SAFE_RL_TOPIC,
                                "arxiv_query": "cat:cs.LG AND safe",
                                "max_items_per_topic": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            xml = arxiv_feed(
                [
                    arxiv_entry(
                        "2608.12345v1",
                        "Safe Exploration for Constrained Reinforcement Learning",
                        (
                            "Safe reinforcement learning for constrained "
                            "reinforcement learning and safe exploration. "
                        )
                        * 8,
                    ),
                    arxiv_entry(
                        "2608.54321v1",
                        "Safe Reinforcement Learning with Practical Shields",
                        "Safe reinforcement learning with shielding and safe exploration. "
                        * 8,
                    ),
                ]
            )

            with patch("curate_research.fetch_text", return_value=xml):
                count = curate(config_path, now=NOW, use_agent=False)

            self.assertEqual(count, 0)
            written = [
                json.loads(line)
                for line in output_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([item["id"] for item in written], ["arxiv:2608.12345"])

    def test_extract_response_text_reads_nested_responses_output(self):
        response = {
            "output": [
                {
                    "content": [
                        {
                            "type": "output_text",
                            "text": "{\"decision\":\"publish\"}",
                        }
                    ]
                }
            ]
        }

        self.assertEqual(extract_response_text(response), "{\"decision\":\"publish\"}")


if __name__ == "__main__":
    unittest.main()
