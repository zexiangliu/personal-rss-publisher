import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from mcp_server import (
    check_duplicate_core,
    commit_and_push,
    list_recent_core,
    list_topics_core,
    propose_item_core,
)
from rss_aggregator import publish as rss_aggregator_publish
from rss_aggregator import read_jsonl_raw

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
        "output_dir": str(tmp_path / "public"),
        "processed_links": str(tmp_path / "processed_links.txt"),
        "feeds": {
            "news": {"title": "News", "retention_hours": 24},
            "research": {"title": "Research", "max_items": 100},
        },
        "default_feed": {"max_items": 150},
        "agent_publish": {
            "output_path": str(tmp_path / "items.agent.jsonl"),
            "candidates_path": str(tmp_path / "items.candidates.jsonl"),
            "default_daily_quota": 3,
            "daily_quota_by_topic": {},
            "candidates_max_age_days": 30,
        },
    }


class ProposeItemCoreTests(unittest.TestCase):
    def test_proposal_succeeds_for_a_free_form_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = base_config(tmp_path)
            result = propose_item_core(
                "gadgets",
                "A new e-reader",
                "https://example.com/gadgets/reader",
                summary="Short summary",
                config=config,
                now=NOW,
            )

            self.assertEqual(result.status, "proposed")
            self.assertEqual(result.topic, "gadgets")
            candidates = (tmp_path / "items.candidates.jsonl").read_text(encoding="utf-8")
            self.assertIn("gadgets/reader", candidates)
            self.assertFalse((tmp_path / "items.agent.jsonl").exists())

    def test_proposing_the_same_url_twice_is_rejected_the_second_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = base_config(Path(tmp))
            first = propose_item_core(
                "research", "Paper", "https://example.com/paper", config=config, now=NOW
            )
            second = propose_item_core(
                "research", "Paper again", "https://example.com/paper", config=config, now=NOW
            )

            self.assertEqual(first.status, "proposed")
            self.assertEqual(second.status, "duplicate")

    def test_duplicate_url_already_published_is_rejected_without_writing(self):
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

            result = propose_item_core(
                "research",
                "Existing item again",
                "https://example.com/research/paper",
                config=config,
                now=NOW,
            )

            self.assertEqual(result.status, "duplicate")
            self.assertFalse((tmp_path / "items.candidates.jsonl").exists())

    def test_duplicate_url_recorded_only_in_the_processed_links_ledger_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = base_config(tmp_path)
            # Not in any items.*.jsonl (e.g. it aged out already) -- only in
            # the permanent ledger.
            Path(config["processed_links"]).write_text(
                "2026-08-20T00:00:00+00:00 https://example.com/research/old-paper\n",
                encoding="utf-8",
            )

            result = propose_item_core(
                "research",
                "Old paper again",
                "https://example.com/research/old-paper",
                config=config,
                now=NOW,
            )

            self.assertEqual(result.status, "duplicate")
            self.assertFalse((tmp_path / "items.candidates.jsonl").exists())

    def test_score_is_recorded_on_the_proposed_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = base_config(tmp_path)

            propose_item_core(
                "research",
                "Important paper",
                "https://example.com/research/important",
                score=8.5,
                config=config,
                now=NOW,
            )

            written = json.loads((tmp_path / "items.candidates.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(written["score"], 8.5)

    def test_proposing_past_todays_quota_still_succeeds(self):
        # Quota is only enforced at promotion time, not when proposing --
        # score is what decides who wins a limited quota, and that decision
        # needs to see every candidate first.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = base_config(tmp_path)
            config["agent_publish"]["default_daily_quota"] = 1

            first = propose_item_core(
                "research", "Paper one", "https://example.com/paper-1", config=config, now=NOW
            )
            second = propose_item_core(
                "research", "Paper two", "https://example.com/paper-2", config=config, now=NOW
            )

            self.assertEqual(first.status, "proposed")
            self.assertEqual(second.status, "proposed")
            candidates = (tmp_path / "items.candidates.jsonl").read_text(encoding="utf-8")
            self.assertIn("paper-1", candidates)
            self.assertIn("paper-2", candidates)

    def test_missing_required_fields_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = base_config(Path(tmp))
            result = propose_item_core("research", "", "", config=config, now=NOW)

        self.assertEqual(result.status, "invalid")


class HelperTests(unittest.TestCase):
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

    def test_check_duplicate_finds_urls_recorded_only_in_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = base_config(tmp_path)
            Path(config["processed_links"]).write_text(
                "2026-08-20T00:00:00+00:00 https://example.com/aged-out\n",
                encoding="utf-8",
            )

            found = check_duplicate_core("https://example.com/aged-out", config)

        self.assertTrue(found["duplicate"])
        self.assertTrue(found["found_in"].endswith("processed_links.txt"))

    def test_check_duplicate_finds_urls_already_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = base_config(tmp_path)
            propose_item_core(
                "research", "Pending", "https://example.com/pending", config=config, now=NOW
            )

            found = check_duplicate_core("https://example.com/pending", config)

        self.assertTrue(found["duplicate"])
        self.assertTrue(found["found_in"].endswith("items.candidates.jsonl"))

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

    def test_list_recent_status_pending_reads_candidates_and_all_reads_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = base_config(tmp_path)
            (tmp_path / "items.jsonl").write_text(
                json.dumps({"id": "published", "title": "Published", "category": "research", "published_at": "2026-08-26T00:00:00Z"})
                + "\n",
                encoding="utf-8",
            )
            propose_item_core(
                "research", "Pending", "https://example.com/pending", config=config, now=NOW
            )

            pending_only = list_recent_core("research", 10, config, status="pending")
            all_items = list_recent_core("research", 10, config, status="all")

        self.assertEqual([item["title"] for item in pending_only], ["Pending"])
        self.assertEqual({item["title"] for item in all_items}, {"Published", "Pending"})

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


class PublishPendingIntegrationTests(unittest.TestCase):
    """Exercises the same composition publish_pending() does (promote +
    regenerate + commit + push) against an isolated repo, since the real
    tool function is hardcoded to this repo's own config.json/REPO_DIR."""

    def test_a_pending_candidate_is_promoted_committed_and_pushed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            origin = tmp_path / "origin.git"
            init_bare_origin(origin)

            work = tmp_path / "work"
            clone_with_identity(origin, work)
            commit_file(work, "seed.txt", "seed\n", "seed")
            run(["push", "-u", "origin", "main"], work)

            agent_path = work / "items.agent.jsonl"
            candidates_path = work / "items.candidates.jsonl"
            processed_links_path = work / "processed_links.txt"
            output_dir = work / "public"
            config_path = work / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "site": {"title": "Personal RSS", "site_url": "https://example.com/rss/"},
                        "output_dir": str(output_dir),
                        "item_inputs": [str(agent_path)],
                        "rss_sources": str(work / "rss_sources.json"),
                        "processed_links": str(processed_links_path),
                        "feeds": {"research": {"title": "Research", "output": "research.xml", "max_items": 100}},
                        "combined_feed": {"title": "All", "output": "all.xml", "max_items": 300},
                        "agent_publish": {
                            "output_path": str(agent_path),
                            "candidates_path": str(candidates_path),
                            "default_daily_quota": 6,
                            "daily_quota_by_topic": {},
                            "candidates_max_age_days": 30,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (work / "rss_sources.json").write_text(json.dumps({"sources": []}), encoding="utf-8")
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

            rss_aggregator_publish(config_path, fetch_rss=False, now=NOW)
            sync = commit_and_push(
                [str(agent_path), str(candidates_path), str(output_dir), str(processed_links_path)],
                "agent: publish pending candidates",
                repo_dir=work,
            )

            self.assertTrue(sync.pushed)
            self.assertEqual(read_jsonl_raw(candidates_path), [])
            promoted = read_jsonl_raw(agent_path)
            self.assertEqual([item["id"] for item in promoted], ["candidate-1"])

            origin_log = subprocess.run(
                ["git", "--git-dir", str(origin), "log", "--oneline"],
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("agent: publish pending candidates", origin_log)


if __name__ == "__main__":
    unittest.main()
