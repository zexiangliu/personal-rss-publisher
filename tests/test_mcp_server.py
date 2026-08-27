import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from mcp_server import (
    check_duplicate_core,
    count_topic_publishes_today,
    list_recent_core,
    list_topics_core,
    publish_item_core,
)

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def base_config(tmp_path: Path) -> dict:
    return {
        "item_inputs": [
            str(tmp_path / "items.jsonl"),
            str(tmp_path / "items.agent.jsonl"),
        ],
        "feeds": {
            "news": {"title": "News", "retention_hours": 24},
            "research": {"title": "Research", "max_items": 100},
        },
        "default_feed": {"max_items": 150},
        "agent_publish": {
            "output_path": str(tmp_path / "items.agent.jsonl"),
            "default_daily_quota": 3,
            "daily_quota_by_topic": {},
        },
    }


class PublishItemCoreTests(unittest.TestCase):
    def test_publish_succeeds_for_a_free_form_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = base_config(Path(tmp))
            result = publish_item_core(
                "gadgets",
                "A new e-reader",
                "https://example.com/gadgets/reader",
                summary="Short summary",
                config=config,
                now=NOW,
            )

        self.assertEqual(result.status, "published")
        self.assertEqual(result.topic, "gadgets")
        self.assertEqual(result.remaining_quota, 2)

    def test_duplicate_url_is_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = base_config(tmp_path)
            (tmp_path / "items.jsonl").write_text(
                json.dumps(
                    {
                        "id": "existing",
                        "title": "Existing item",
                        "url": "https://example.com/research/paper",
                        "category": "research",
                        "published_at": "2026-08-26T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = publish_item_core(
                "research",
                "Existing item again",
                "https://example.com/research/paper",
                config=config,
                now=NOW,
            )

            self.assertEqual(result.status, "duplicate")
            self.assertFalse((tmp_path / "items.agent.jsonl").exists())

    def test_daily_quota_blocks_the_next_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = base_config(tmp_path)
            config["agent_publish"]["default_daily_quota"] = 2

            first = publish_item_core(
                "research", "Paper one", "https://example.com/paper-1", config=config, now=NOW
            )
            second = publish_item_core(
                "research", "Paper two", "https://example.com/paper-2", config=config, now=NOW
            )
            third = publish_item_core(
                "research", "Paper three", "https://example.com/paper-3", config=config, now=NOW
            )

            self.assertEqual(first.status, "published")
            self.assertEqual(second.status, "published")
            self.assertEqual(third.status, "quota_exceeded")
            self.assertNotIn(
                "paper-3", (tmp_path / "items.agent.jsonl").read_text(encoding="utf-8")
            )

    def test_missing_required_fields_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = base_config(Path(tmp))
            result = publish_item_core("research", "", "", config=config, now=NOW)

        self.assertEqual(result.status, "invalid")


class HelperTests(unittest.TestCase):
    def test_count_topic_publishes_today_only_counts_matching_topic_and_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent_path = Path(tmp) / "items.agent.jsonl"
            agent_path.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in [
                        {"category": "research", "published_at": "2026-08-27T01:00:00+00:00"},
                        {"category": "research", "published_at": "2026-08-26T23:00:00+00:00"},
                        {"category": "news", "published_at": "2026-08-27T02:00:00+00:00"},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertEqual(count_topic_publishes_today("research", agent_path, NOW), 1)
            self.assertEqual(count_topic_publishes_today("news", agent_path, NOW), 1)
            self.assertEqual(count_topic_publishes_today("missing", agent_path, NOW), 0)

    def test_check_duplicate_reports_the_source_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = base_config(tmp_path)
            (tmp_path / "items.jsonl").write_text(
                json.dumps(
                    {
                        "id": "existing",
                        "title": "Existing",
                        "url": "https://example.com/known",
                        "category": "news",
                        "published_at": "2026-08-26T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            found = check_duplicate_core("https://example.com/known", config)
            missing = check_duplicate_core("https://example.com/unknown", config)

        self.assertTrue(found["duplicate"])
        self.assertTrue(found["found_in"].endswith("items.jsonl"))
        self.assertFalse(missing["duplicate"])

    def test_list_recent_filters_by_topic_and_sorts_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = base_config(tmp_path)
            (tmp_path / "items.jsonl").write_text(
                "\n".join(
                    json.dumps(item)
                    for item in [
                        {"id": "a", "title": "A", "category": "research", "published_at": "2026-08-25T00:00:00Z"},
                        {"id": "b", "title": "B", "category": "research", "published_at": "2026-08-26T00:00:00Z"},
                        {"id": "c", "title": "C", "category": "news", "published_at": "2026-08-27T00:00:00Z"},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            recent = list_recent_core("research", 10, config)

        self.assertEqual([item["id"] for item in recent], ["b", "a"])

    def test_list_topics_includes_configured_feeds(self):
        config = base_config(Path("/tmp"))
        topics = list_topics_core(config)
        self.assertEqual([topic["topic"] for topic in topics], ["news", "research"])


if __name__ == "__main__":
    unittest.main()
