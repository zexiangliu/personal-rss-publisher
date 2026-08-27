import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree

from rss_aggregator import (
    dedupe_items,
    load_structured_items,
    processed_link_urls,
    promote_candidates,
    prune_item_log,
    publish,
    read_jsonl_raw,
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

    def test_score_defaults_to_zero_and_parses_when_present(self):
        items = load_items_for_test(
            [
                {
                    "id": "unscored",
                    "title": "Unscored",
                    "url": "https://example.com/unscored",
                    "category": "research",
                    "published_at": "2026-08-26T00:00:00Z",
                },
                {
                    "id": "scored",
                    "title": "Scored",
                    "url": "https://example.com/scored",
                    "category": "research",
                    "published_at": "2026-08-26T00:00:00Z",
                    "score": 7.5,
                },
            ]
        )
        by_id = {item.id: item for item in items}
        self.assertEqual(by_id["unscored"].score, 0.0)
        self.assertEqual(by_id["scored"].score, 7.5)

    def test_max_items_is_pure_recency_regardless_of_score(self):
        items = load_items_for_test(
            [
                {
                    "id": "old-high-score",
                    "title": "Old but high score",
                    "url": "https://example.com/old-high-score",
                    "category": "research",
                    "published_at": "2026-08-01T00:00:00Z",
                    "score": 9.0,
                },
                {
                    "id": "new-low-score",
                    "title": "New but low score",
                    "url": "https://example.com/new-low-score",
                    "category": "research",
                    "published_at": "2026-08-27T00:00:00Z",
                    "score": 0.5,
                },
                {
                    "id": "newest-no-score",
                    "title": "Newest, unscored",
                    "url": "https://example.com/newest-no-score",
                    "category": "research",
                    "published_at": "2026-08-27T06:00:00Z",
                },
            ]
        )
        config = {
            "feeds": {"research": {"max_items": 2}},
            "combined_feed": {"max_items": 300},
        }

        feeds, _ = route_items(dedupe_items(items), config, NOW)

        # score is only a promotion-time concept -- once items are archived,
        # a high score never rescues an older item from a max_items cap, so
        # this is the newest two regardless of score
        self.assertEqual(
            [item.id for item in feeds["research"]], ["newest-no-score", "new-low-score"]
        )

    def test_unscored_items_keep_pure_recency_ordering_under_a_cap(self):
        items = load_items_for_test(
            [
                {
                    "id": "older",
                    "title": "Older",
                    "url": "https://example.com/older",
                    "category": "research",
                    "published_at": "2026-08-01T00:00:00Z",
                },
                {
                    "id": "newer",
                    "title": "Newer",
                    "url": "https://example.com/newer",
                    "category": "research",
                    "published_at": "2026-08-27T00:00:00Z",
                },
            ]
        )
        config = {
            "feeds": {"research": {"max_items": 1}},
            "combined_feed": {"max_items": 300},
        }

        feeds, _ = route_items(dedupe_items(items), config, NOW)

        self.assertEqual([item.id for item in feeds["research"]], ["newer"])

    def test_processed_link_urls_reads_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "processed_links.txt"
            path.write_text("2026-08-26T15:00:00+00:00 https://example.com/seen\n", encoding="utf-8")

            urls = processed_link_urls(path)

        self.assertIn("https://example.com/seen", urls)

    def test_processed_link_urls_handles_a_missing_file(self):
        urls = processed_link_urls(Path("/tmp/definitely-does-not-exist.txt"))
        self.assertEqual(urls, set())

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

    def test_publish_promotes_a_pending_candidate_into_the_generated_xml(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            agent_path = tmp_path / "items.agent.jsonl"
            candidates_path = tmp_path / "items.candidates.jsonl"
            write_json(
                tmp_path / "config.json",
                {
                    "site": {"title": "Personal RSS", "site_url": "https://example.com/rss/"},
                    "output_dir": str(tmp_path / "public"),
                    "item_inputs": [str(agent_path)],
                    "rss_sources": str(tmp_path / "rss_sources.json"),
                    "processed_links": str(tmp_path / "processed_links.txt"),
                    "feeds": {"research": {"title": "Research", "output": "research.xml", "max_items": 100}},
                    "combined_feed": {"title": "All", "output": "all.xml", "max_items": 300},
                    "agent_publish": {
                        "output_path": str(agent_path),
                        "candidates_path": str(candidates_path),
                        "default_daily_quota": 6,
                        "daily_quota_by_topic": {},
                        "candidates_max_age_days": 30,
                    },
                },
            )
            write_json(tmp_path / "rss_sources.json", {"sources": []})
            candidates_path.write_text(
                json.dumps(
                    {
                        "id": "candidate-1",
                        "title": "Queued paper",
                        "url": "https://example.com/research/queued",
                        "category": "research",
                        "score": 5.0,
                        "published_at": "2026-08-26T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            counts = publish(tmp_path / "config.json", fetch_rss=False, now=NOW)

            self.assertEqual(counts["research"], 1)
            self.assertEqual(read_jsonl_raw(candidates_path), [])
            root = ElementTree.parse(tmp_path / "public" / "research.xml").getroot()
            titles = [el.text for el in root.findall("./channel/item/title")]
            self.assertIn("Queued paper", titles)


class PromoteCandidatesTests(unittest.TestCase):
    def base_config(self, tmp_path: Path) -> dict:
        return {
            "item_inputs": [str(tmp_path / "items.agent.jsonl")],
            "processed_links": str(tmp_path / "processed_links.txt"),
            "agent_publish": {
                "output_path": str(tmp_path / "items.agent.jsonl"),
                "candidates_path": str(tmp_path / "items.candidates.jsonl"),
                "default_daily_quota": 6,
                "daily_quota_by_topic": {},
                "candidates_max_age_days": 30,
            },
        }

    def write_candidates(self, tmp_path: Path, candidates: list[dict]) -> None:
        (tmp_path / "items.candidates.jsonl").write_text(
            "\n".join(json.dumps(c, ensure_ascii=False) for c in candidates) + "\n",
            encoding="utf-8",
        )

    def test_promotes_top_scored_within_remaining_quota(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = self.base_config(tmp_path)
            config["agent_publish"]["default_daily_quota"] = 2
            self.write_candidates(
                tmp_path,
                [
                    {"id": "low", "title": "Low", "url": "https://example.com/low", "category": "research", "score": 1.0, "published_at": "2026-08-26T00:00:00Z"},
                    {"id": "high", "title": "High", "url": "https://example.com/high", "category": "research", "score": 9.0, "published_at": "2026-08-26T00:00:00Z"},
                    {"id": "mid", "title": "Mid", "url": "https://example.com/mid", "category": "research", "score": 5.0, "published_at": "2026-08-26T00:00:00Z"},
                ],
            )

            promoted = promote_candidates(config, NOW)

            self.assertEqual(promoted, 2)
            published = read_jsonl_raw(tmp_path / "items.agent.jsonl")
            self.assertEqual({item["id"] for item in published}, {"high", "mid"})
            remaining = read_jsonl_raw(tmp_path / "items.candidates.jsonl")
            self.assertEqual([item["id"] for item in remaining], ["low"])

    def test_promotion_overwrites_published_at_to_now(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = self.base_config(tmp_path)
            self.write_candidates(
                tmp_path,
                [{"id": "a", "title": "A", "url": "https://example.com/a", "category": "research", "score": 1.0, "published_at": "2026-08-01T00:00:00Z"}],
            )

            promote_candidates(config, NOW)

            published = read_jsonl_raw(tmp_path / "items.agent.jsonl")
            self.assertEqual(published[0]["published_at"], NOW.isoformat())

    def test_respects_quota_already_used_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = self.base_config(tmp_path)
            config["agent_publish"]["default_daily_quota"] = 1
            (tmp_path / "items.agent.jsonl").write_text(
                json.dumps(
                    {"id": "already", "title": "Already", "url": "https://example.com/already", "category": "research", "published_at": NOW.isoformat()}
                )
                + "\n",
                encoding="utf-8",
            )
            self.write_candidates(
                tmp_path,
                [{"id": "new", "title": "New", "url": "https://example.com/new", "category": "research", "score": 9.0, "published_at": "2026-08-26T00:00:00Z"}],
            )

            promoted = promote_candidates(config, NOW)

            self.assertEqual(promoted, 0)
            remaining = read_jsonl_raw(tmp_path / "items.candidates.jsonl")
            self.assertEqual([item["id"] for item in remaining], ["new"])

    def test_expired_candidate_is_dropped_without_touching_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = self.base_config(tmp_path)
            config["agent_publish"]["candidates_max_age_days"] = 30
            self.write_candidates(
                tmp_path,
                [
                    {
                        "id": "stale",
                        "title": "Stale",
                        "url": "https://example.com/stale",
                        "category": "research",
                        "score": 10.0,
                        "published_at": (NOW - timedelta(days=45)).isoformat(),
                    }
                ],
            )

            promoted = promote_candidates(config, NOW)

            self.assertEqual(promoted, 0)
            self.assertEqual(read_jsonl_raw(tmp_path / "items.candidates.jsonl"), [])
            self.assertEqual(read_jsonl_raw(tmp_path / "items.agent.jsonl"), [])
            self.assertNotIn(
                "https://example.com/stale", processed_link_urls(Path(config["processed_links"]))
            )

    def test_candidate_already_published_elsewhere_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = self.base_config(tmp_path)
            (tmp_path / "items.agent.jsonl").write_text(
                json.dumps(
                    {"id": "dup", "title": "Dup", "url": "https://example.com/dup", "category": "research", "published_at": "2026-08-01T00:00:00Z"}
                )
                + "\n",
                encoding="utf-8",
            )
            self.write_candidates(
                tmp_path,
                [{"id": "dup-candidate", "title": "Dup again", "url": "https://example.com/dup", "category": "research", "score": 9.0, "published_at": "2026-08-26T00:00:00Z"}],
            )

            promoted = promote_candidates(config, NOW)

            self.assertEqual(promoted, 0)
            self.assertEqual(read_jsonl_raw(tmp_path / "items.candidates.jsonl"), [])

    def test_no_candidates_file_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.base_config(Path(tmp))
            self.assertEqual(promote_candidates(config, NOW), 0)


class PruneItemLogTests(unittest.TestCase):
    def test_noop_under_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            path = tmp_path / "items.agent.jsonl"
            path.write_text(
                json.dumps({"id": "a", "title": "A", "url": "https://example.com/a", "category": "research", "published_at": "2026-08-26T00:00:00Z"})
                + "\n",
                encoding="utf-8",
            )

            removed = prune_item_log(path, 5, tmp_path / "processed_links.txt", 365, NOW)

            self.assertEqual(removed, 0)
            self.assertEqual(len(read_jsonl_raw(path)), 1)

    def test_trims_to_newest_and_marks_dropped_in_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            path = tmp_path / "items.agent.jsonl"
            processed_links_path = tmp_path / "processed_links.txt"
            path.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in [
                        {"id": "old", "title": "Old", "url": "https://example.com/old", "category": "research", "published_at": "2026-08-01T00:00:00Z"},
                        {"id": "mid", "title": "Mid", "url": "https://example.com/mid", "category": "research", "published_at": "2026-08-15T00:00:00Z"},
                        {"id": "new", "title": "New", "url": "https://example.com/new", "category": "research", "published_at": "2026-08-27T00:00:00Z"},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            removed = prune_item_log(path, 2, processed_links_path, 365, NOW)

            self.assertEqual(removed, 1)
            remaining_ids = {item["id"] for item in read_jsonl_raw(path)}
            self.assertEqual(remaining_ids, {"mid", "new"})
            self.assertIn("https://example.com/old", processed_link_urls(processed_links_path))


def load_items_for_test(raw_items):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "items.json"
        write_json(path, raw_items)
        return load_structured_items([str(path)], NOW)


if __name__ == "__main__":
    unittest.main()
