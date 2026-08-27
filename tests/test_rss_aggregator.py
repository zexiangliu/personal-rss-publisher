import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from rss_aggregator import (
    dedupe_items,
    load_structured_items,
    publish,
    route_items,
    stable_guid,
)


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class RssPublisherTests(unittest.TestCase):
    def test_parses_jsonl_and_generates_stable_guid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "items.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "title": "Interesting paper",
                        "url": "https://example.com/article",
                        "category": "research",
                        "summary": "Short summary",
                        "published_at": "2026-08-26T08:00:00-07:00",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            items = load_structured_items([str(path)], NOW)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].category, "research")
        self.assertEqual(items[0].id, stable_guid("https://example.com/article"))

    def test_category_routing_duplicate_urls_and_retention(self):
        items = load_items_for_test(
            [
                {
                    "id": "old-news",
                    "title": "Old news",
                    "url": "https://example.com/old",
                    "category": "news",
                    "published_at": "2026-08-01T00:00:00Z",
                },
                {
                    "id": "fresh-news",
                    "title": "Fresh news",
                    "url": "https://example.com/fresh",
                    "category": "news",
                    "published_at": "2026-08-27T00:00:00Z",
                },
                {
                    "id": "stale-hourly-news",
                    "title": "Stale hourly news",
                    "url": "https://example.com/stale-hourly",
                    "category": "news",
                    "published_at": "2026-08-26T11:59:00Z",
                },
                {
                    "id": "duplicate-older",
                    "title": "Duplicate older",
                    "url": "https://example.com/fresh",
                    "category": "news",
                    "published_at": "2026-08-26T23:00:00Z",
                },
                {
                    "id": "paper",
                    "title": "Paper",
                    "url": "https://example.com/paper",
                    "category": "research",
                    "published_at": "2026-08-26T00:00:00Z",
                },
            ]
        )
        config = {
            "feeds": {
                "news": {"retention_hours": 24},
                "research": {"max_items": 100},
                "investing": {"max_items": 100},
            },
            "combined_feed": {"max_items": 300},
        }

        feeds, combined = route_items(dedupe_items(items), config, NOW)

        self.assertEqual([item.id for item in feeds["news"]], ["fresh-news"])
        self.assertEqual([item.id for item in feeds["research"]], ["paper"])
        self.assertEqual(len(combined), 2)

    def test_unconfigured_category_falls_back_to_default_feed_policy(self):
        items = load_items_for_test(
            [
                {
                    "id": "gadget-1",
                    "title": "Gadget one",
                    "url": "https://example.com/gadgets/one",
                    "category": "gadgets",
                    "published_at": "2026-08-27T00:00:00Z",
                },
            ]
        )
        config = {
            "feeds": {"news": {"retention_hours": 24}},
            "combined_feed": {"max_items": 300},
            "default_feed": {"max_items": 1},
        }

        feeds, combined = route_items(dedupe_items(items), config, NOW)

        self.assertIn("gadgets", feeds)
        self.assertEqual([item.id for item in feeds["gadgets"]], ["gadget-1"])
        self.assertEqual([item.id for item in feeds["news"]], [])
        self.assertEqual(len(combined), 1)

    def test_index_lists_dynamically_created_topics(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            write_json(
                tmp_path / "config.json",
                {
                    "site": {"title": "Personal RSS", "site_url": "https://example.com/rss/"},
                    "output_dir": str(tmp_path / "public"),
                    "item_inputs": [str(tmp_path / "items.jsonl")],
                    "rss_sources": str(tmp_path / "rss_sources.json"),
                    "processed_links": str(tmp_path / "processed_links.txt"),
                    "feeds": {"news": {"title": "News", "output": "news.xml"}},
                    "default_feed": {"max_items": 50},
                    "combined_feed": {"title": "All", "output": "all.xml", "max_items": 300},
                },
            )
            write_json(tmp_path / "rss_sources.json", {"sources": []})
            (tmp_path / "items.jsonl").write_text(
                json.dumps(
                    {
                        "id": "gadget-1",
                        "title": "Gadget one",
                        "url": "https://example.com/gadgets/one",
                        "category": "gadgets",
                        "published_at": "2026-08-26T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            publish(tmp_path / "config.json", fetch_rss=False, now=NOW)

            self.assertTrue((tmp_path / "public" / "gadgets.xml").exists())
            index_html = (tmp_path / "public" / "index.html").read_text(encoding="utf-8")
            self.assertIn("gadgets.xml", index_html)

    def test_publish_writes_valid_unicode_rss_and_combined_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            write_json(
                tmp_path / "config.json",
                {
                    "site": {
                        "title": "Personal RSS",
                        "description": "Test feeds",
                        "site_url": "https://example.com/rss/",
                    },
                    "output_dir": str(tmp_path / "public"),
                    "item_inputs": [str(tmp_path / "items.jsonl")],
                    "rss_sources": str(tmp_path / "rss_sources.json"),
                    "processed_links": str(tmp_path / "processed_links.txt"),
                    "feeds": {
                        "news": {"title": "News", "output": "news.xml", "retention_days": 7},
                        "research": {"title": "Research", "output": "research.xml", "max_items": 100},
                        "investing": {"title": "Investing", "output": "investing.xml", "max_items": 100},
                    },
                    "combined_feed": {"title": "All", "output": "all.xml", "max_items": 300},
                },
            )
            write_json(tmp_path / "rss_sources.json", {"sources": []})
            (tmp_path / "items.jsonl").write_text(
                "\n".join(
                    json.dumps(item, ensure_ascii=False)
                    for item in [
                        {
                            "id": "unicode-news",
                            "title": "中文新闻 & XML <escaping>",
                            "url": "https://example.com/news/unicode",
                            "source": "示例新闻",
                            "category": "news",
                            "summary": "中文摘要可以被普通 RSS 阅读器读取。",
                            "published_at": "2026-08-26T08:00:00Z",
                        },
                        {
                            "id": "research-one",
                            "title": "Research one",
                            "url": "https://example.com/research/one",
                            "category": "research",
                            "summary": "Research summary",
                            "published_at": "2026-08-26T07:00:00Z",
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            counts = publish(tmp_path / "config.json", fetch_rss=False, now=NOW)

            self.assertEqual(counts["news"], 1)
            self.assertEqual(counts["research"], 1)
            self.assertEqual(counts["investing"], 0)
            self.assertEqual(counts["all"], 2)

            for filename in ("news.xml", "research.xml", "investing.xml", "all.xml"):
                ElementTree.parse(tmp_path / "public" / filename)

            all_root = ElementTree.parse(tmp_path / "public" / "all.xml").getroot()
            titles = [el.text for el in all_root.findall("./channel/item/title")]
            self.assertIn("中文新闻 & XML <escaping>", titles)
            self.assertIn("Research one", titles)


def load_items_for_test(raw_items):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "items.json"
        write_json(path, raw_items)
        return load_structured_items([str(path)], NOW)


if __name__ == "__main__":
    unittest.main()
