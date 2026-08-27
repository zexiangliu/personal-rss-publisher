import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from mcp_server import (
    check_duplicate_core,
    commit_and_push,
    count_topic_publishes_today,
    list_recent_core,
    list_topics_core,
    publish_item_core,
)

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def run(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def init_bare_origin(path: Path) -> None:
    subprocess.run(["git", "init", "--bare", "-b", "main", str(path)], check=True, capture_output=True)


def clone_with_identity(origin: Path, dest: Path) -> None:
    subprocess.run(["git", "clone", str(origin), str(dest)], check=True, capture_output=True)
    run(["config", "user.email", "test@example.com"], dest)
    run(["config", "user.name", "Test"], dest)


def commit_file(repo: Path, name: str, content: str, message: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    run(["add", name], repo)
    run(["commit", "-m", message], repo)


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


class CommitAndPushTests(unittest.TestCase):
    def test_pushes_cleanly_with_no_divergence(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            origin = tmp_path / "origin.git"
            init_bare_origin(origin)

            work = tmp_path / "work"
            clone_with_identity(origin, work)
            commit_file(work, "seed.txt", "seed\n", "seed")
            run(["push", "-u", "origin", "main"], work)

            (work / "items.agent.jsonl").write_text('{"id": "x"}\n', encoding="utf-8")
            result = commit_and_push(["items.agent.jsonl"], "agent: publish test", repo_dir=work)

            self.assertTrue(result.committed)
            self.assertTrue(result.pushed)

    def test_reports_no_changes_without_committing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            origin = tmp_path / "origin.git"
            init_bare_origin(origin)

            work = tmp_path / "work"
            clone_with_identity(origin, work)
            commit_file(work, "seed.txt", "seed\n", "seed")
            run(["push", "-u", "origin", "main"], work)

            result = commit_and_push(["seed.txt"], "no-op", repo_dir=work)

            self.assertFalse(result.committed)
            self.assertFalse(result.pushed)

    def test_rebases_past_a_concurrent_push_and_regenerates_before_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            origin = tmp_path / "origin.git"
            init_bare_origin(origin)

            work = tmp_path / "work"
            clone_with_identity(origin, work)
            commit_file(work, "seed.txt", "seed\n", "seed")
            run(["push", "-u", "origin", "main"], work)

            # Simulate the hourly discovery Action pushing in the background
            # while our agent is about to publish.
            other = tmp_path / "other"
            clone_with_identity(origin, other)
            commit_file(other, "other.txt", "concurrent\n", "concurrent commit")
            run(["push"], other)

            (work / "items.agent.jsonl").write_text('{"id": "x"}\n', encoding="utf-8")
            (work / "public.txt").write_text("stale\n", encoding="utf-8")
            regenerated = {"calls": 0}

            def on_rebase():
                regenerated["calls"] += 1
                (work / "public.txt").write_text("regenerated\n", encoding="utf-8")

            result = commit_and_push(
                ["items.agent.jsonl", "public.txt"],
                "agent: publish test",
                repo_dir=work,
                on_rebase=on_rebase,
            )

            self.assertTrue(result.committed)
            self.assertTrue(result.pushed)
            self.assertEqual(regenerated["calls"], 1)

            log = subprocess.run(
                ["git", "-C", str(work), "log", "--oneline"], capture_output=True, text=True
            ).stdout
            self.assertIn("concurrent commit", log)
            self.assertIn("agent: publish test", log)
            self.assertEqual((work / "public.txt").read_text(encoding="utf-8"), "regenerated\n")


if __name__ == "__main__":
    unittest.main()
