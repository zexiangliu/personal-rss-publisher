import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from discover_items import discover, hk01_zone_id_from_url, normalize_hk01_article


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


class DiscoverItemsTests(unittest.TestCase):
    def test_extracts_hk01_zone_id_from_url(self):
        url = "https://www.hk01.com/zone/4/%E5%9C%8B%E9%9A%9B"

        self.assertEqual(hk01_zone_id_from_url(url), "4")

    def test_normalizes_hk01_article_to_boundary_schema(self):
        article = {
            "articleId": 123,
            "title": "國際新聞標題",
            "canonicalUrl": "/即時國際/123/example",
            "description": "中文摘要",
            "publishTime": int(NOW.timestamp()),
            "mainImage": {"cdnUrl": "https://cdn.hk01.com/example.jpg"},
        }
        source = {
            "url": "https://www.hk01.com/zone/4/%E5%9C%8B%E9%9A%9B",
            "category": "news",
            "lookback_days": 7,
        }
        defaults = {
            "lookback_days": 7,
            "summary_max_chars": 600,
            "include_keywords": [],
            "exclude_keywords": [],
            "rank_keywords": {},
            "min_score": 0,
        }

        candidate = normalize_hk01_article(article, source, defaults, "香港01 國際", NOW)

        self.assertIsNotNone(candidate)
        item = candidate.item
        self.assertEqual(item["id"], "hk01:123")
        self.assertEqual(item["category"], "news")
        self.assertEqual(item["source"], "香港01 國際")
        self.assertEqual(item["url"], "https://www.hk01.com/即時國際/123/example")
        self.assertEqual(item["image_url"], "https://cdn.hk01.com/example.jpg")

    def test_hk01_article_respects_lookback_hours(self):
        article = {
            "articleId": 789,
            "title": "Older article",
            "canonicalUrl": "/即時國際/789/example",
            "description": "Older summary",
            "publishTime": int(datetime(2026, 8, 26, 11, 59, tzinfo=timezone.utc).timestamp()),
        }
        source = {
            "url": "https://www.hk01.com/zone/4/%E5%9C%8B%E9%9A%9B",
            "category": "news",
            "lookback_hours": 24,
        }
        defaults = {
            "lookback_days": 7,
            "lookback_hours": None,
            "summary_max_chars": 600,
            "include_keywords": [],
            "exclude_keywords": [],
            "rank_keywords": {},
            "min_score": 0,
        }

        candidate = normalize_hk01_article(article, source, defaults, "香港01 國際", NOW)

        self.assertIsNone(candidate)

    def test_discovers_hk01_items_and_skips_known_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_path = tmp_path / "items.discovered.jsonl"
            known_path = tmp_path / "items.jsonl"
            known_path.write_text(
                json.dumps(
                    {
                        "title": "Already known",
                        "url": "https://www.hk01.com/即時國際/123/example",
                        "category": "news",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            config_path = tmp_path / "discovery_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "output_path": str(output_path),
                        "dedupe_against": [str(known_path)],
                        "defaults": {
                            "enabled": False,
                            "lookback_days": 7,
                            "max_items_per_source": 20,
                            "summary_max_chars": 600,
                            "min_score": 0,
                            "include_keywords": [],
                            "exclude_keywords": [],
                            "rank_keywords": {},
                        },
                        "sources": [
                            {
                                "enabled": True,
                                "type": "hk01_zone",
                                "name": "香港01 國際",
                                "url": "https://www.hk01.com/zone/4/%E5%9C%8B%E9%9A%9B",
                                "category": "news",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            payload = {
                "items": [
                    {
                        "data": {
                            "articleId": 123,
                            "title": "Already known",
                            "canonicalUrl": "https://www.hk01.com/即時國際/123/example",
                            "description": "中文摘要",
                            "publishTime": int(NOW.timestamp()),
                        }
                    },
                    {
                        "data": {
                            "articleId": 456,
                            "title": "New item",
                            "canonicalUrl": "https://www.hk01.com/即時國際/456/example",
                            "description": "Fresh summary",
                            "publishTime": int(NOW.timestamp()),
                        }
                    },
                ]
            }

            with patch("discover_items.fetch_json", return_value=payload):
                count = discover(config_path, now=NOW)

            self.assertEqual(count, 1)
            written = [
                json.loads(line)
                for line in output_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(written), 1)
            self.assertEqual(written[0]["id"], "hk01:456")


if __name__ == "__main__":
    unittest.main()
