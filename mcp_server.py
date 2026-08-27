#!/usr/bin/env python3
"""MCP server for agent-driven RSS publishing.

Curation judgment (what is worth publishing) lives in the calling agent, not
in this repo. Publishing is two phases:

- `propose_item` queues a scored candidate into `items.candidates.jsonl`.
  This is a single cheap local file append -- safe for several agent
  sessions to call at once, and it never touches git.
- Promotion -- `rss_aggregator.promote_candidates()`, run as part of every
  `rss_aggregator.py` invocation (the hourly discovery Action, or this
  server's `publish_pending` tool) -- ranks each topic's pending candidates
  by score and promotes up to that topic's remaining daily quota into the
  published archive (`items.agent.jsonl`). Once promoted, score no longer
  matters: the item ages out by plain recency like anything else, so an old
  high-scored item can never block a new one forever.

This server only owns what the repo must own reliably: URL dedup against
everything already published or pending, feed lifespan (handled by
`rss_aggregator.py`'s per-feed retention config), and the per-topic daily
quota enforced at promotion time. Topics are free-form: an unconfigured
topic name still works and gets `config.json`'s `default_feed` retention
policy.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from discover_items import append_jsonl, load_known_urls
from rss_aggregator import (
    DEFAULT_CONFIG_PATH,
    agent_publish_settings,
    load_config,
    normalize_url,
    processed_link_urls,
    stable_guid,
)
from rss_aggregator import publish as rss_publish

REPO_DIR = Path(__file__).resolve().parent

# Resolve every relative path (config.json, items.*.jsonl, ...) against the
# repo root regardless of the working directory the MCP client launched us
# from.
os.chdir(REPO_DIR)

server = MCPServer("personal-rss-publisher")


@dataclass(frozen=True)
class ProposeResult:
    status: str  # "proposed" | "duplicate" | "invalid"
    message: str
    item_id: str = ""
    topic: str = ""


@dataclass(frozen=True)
class SyncResult:
    committed: bool
    pushed: bool
    detail: str


def run_git(args: list[str], repo_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo_dir, capture_output=True, text=True)


def commit_and_push(
    paths: list[str],
    message: str,
    *,
    repo_dir: Path,
    on_rebase: Callable[[], None] | None = None,
) -> SyncResult:
    """Commit `paths` and push. If the push is rejected by a concurrent
    remote update (e.g. the hourly discovery Action), fetch, rebase onto it,
    call `on_rebase` to regenerate any build artifacts among `paths` against
    the merged inputs, and retry once."""
    add = run_git(["add", "--", *paths], repo_dir)
    if add.returncode != 0:
        return SyncResult(False, False, f"git add failed: {add.stderr.strip()}")

    if run_git(["diff", "--cached", "--quiet"], repo_dir).returncode == 0:
        return SyncResult(False, False, "no changes to commit")

    commit = run_git(["commit", "-m", message], repo_dir)
    if commit.returncode != 0:
        return SyncResult(False, False, f"git commit failed: {commit.stderr.strip()}")

    push = run_git(["push"], repo_dir)
    if push.returncode == 0:
        return SyncResult(True, True, "pushed")

    branch_result = run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir)
    branch = branch_result.stdout.strip()
    if branch_result.returncode != 0 or not branch:
        return SyncResult(True, False, f"push failed: {push.stderr.strip()}")

    if run_git(["fetch", "origin"], repo_dir).returncode != 0:
        return SyncResult(True, False, f"push failed and fetch failed: {push.stderr.strip()}")

    rebase = run_git(["rebase", f"origin/{branch}"], repo_dir)
    if rebase.returncode != 0:
        run_git(["rebase", "--abort"], repo_dir)
        return SyncResult(
            True,
            False,
            f"push rejected by a concurrent update and rebase failed -- "
            f"resolve manually: {push.stderr.strip()}",
        )

    if on_rebase is not None:
        on_rebase()
        run_git(["add", "--", *paths], repo_dir)
        if run_git(["diff", "--cached", "--quiet"], repo_dir).returncode != 0:
            run_git(["commit", "--amend", "--no-edit"], repo_dir)

    retry = run_git(["push"], repo_dir)
    if retry.returncode == 0:
        return SyncResult(True, True, "pushed after rebasing onto a concurrent update")
    return SyncResult(True, False, f"push failed after rebase retry: {retry.stderr.strip()}")


def regenerate_feeds() -> None:
    rss_publish(DEFAULT_CONFIG_PATH, fetch_rss=False, now=datetime.now(timezone.utc))


def known_url_paths(config: dict[str, Any]) -> list[Path]:
    return [Path(path) for path in config["item_inputs"]]


def list_topics_core(config: dict[str, Any]) -> list[dict[str, Any]]:
    topics = [
        {
            "topic": name,
            "title": feed_config.get("title", name.title()),
            "description": feed_config.get("description", ""),
        }
        for name, feed_config in config["feeds"].items()
    ]
    topics.sort(key=lambda entry: entry["topic"])
    return topics


def check_duplicate_core(url: str, config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_url(url)
    candidates_path = Path(agent_publish_settings(config)["candidates_path"])
    for path in known_url_paths(config) + [candidates_path]:
        if path.exists() and normalized in load_known_urls([path]):
            return {"duplicate": True, "found_in": str(path)}
    processed_path = Path(config["processed_links"])
    if processed_path.exists() and normalized in processed_link_urls(processed_path):
        return {"duplicate": True, "found_in": str(processed_path)}
    return {"duplicate": False, "found_in": ""}


def _read_topic_items(path: Path, topic: str) -> list[dict[str, Any]]:
    if path.suffix.lower() != ".jsonl" or not path.exists():
        return []
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not topic or str(item.get("category", "")).strip().lower() == topic:
                items.append(item)
    return items


def list_recent_core(
    topic: str, limit: int, config: dict[str, Any], status: str = "published"
) -> list[dict[str, Any]]:
    topic = topic.strip().lower()
    status = status.strip().lower()
    items: list[dict[str, Any]] = []

    if status in ("published", "all"):
        for raw_path in config["item_inputs"]:
            items.extend(_read_topic_items(Path(raw_path), topic))
    if status in ("pending", "all"):
        candidates_path = Path(agent_publish_settings(config)["candidates_path"])
        items.extend(_read_topic_items(candidates_path, topic))

    items.sort(key=lambda item: str(item.get("published_at", "")), reverse=True)
    return items[:limit]


def propose_item_core(
    topic: str,
    title: str,
    url: str,
    summary: str = "",
    source: str = "",
    source_url: str = "",
    image_url: str = "",
    content_html: str = "",
    score: float = 0.0,
    *,
    config: dict[str, Any],
    now: datetime,
) -> ProposeResult:
    """Queue a candidate for the next promotion round. No quota check here --
    quota is enforced once, at promotion time, across all pending candidates
    for the topic (see rss_aggregator.promote_candidates)."""
    topic = topic.strip().lower()
    title = title.strip()
    url = url.strip()
    if not topic or not title or not url:
        return ProposeResult(status="invalid", message="topic, title, and url are required.")

    settings = agent_publish_settings(config)
    candidates_path = Path(settings["candidates_path"])

    known = set()
    for path in known_url_paths(config) + [candidates_path]:
        if path.exists():
            known |= load_known_urls([path])
    processed_path = Path(config["processed_links"])
    if processed_path.exists():
        known |= processed_link_urls(processed_path)
    if normalize_url(url) in known:
        return ProposeResult(status="duplicate", message=f"{url} is already published or pending.", topic=topic)

    item_id = stable_guid(url)
    item = {
        "id": item_id,
        "title": title,
        "url": url,
        "category": topic,
        "source": source.strip(),
        "source_url": source_url.strip(),
        "summary": summary.strip(),
        "published_at": now.astimezone(timezone.utc).isoformat(),
        "image_url": image_url.strip(),
        "content_html": content_html.strip(),
        "score": float(score),
    }
    append_jsonl(candidates_path, [item])

    return ProposeResult(
        status="proposed",
        message=f"Queued for topic '{topic}'; promoted on the next publish cycle if it ranks high enough.",
        item_id=item_id,
        topic=topic,
    )


def current_config() -> dict[str, Any]:
    return load_config(DEFAULT_CONFIG_PATH)


@server.tool()
def list_topics() -> list[dict[str, Any]]:
    """List configured RSS topics/feeds. Any other topic name also works with
    propose_item -- an unconfigured topic gets config.json's default_feed
    retention policy instead of a hand-tuned one."""
    return list_topics_core(current_config())


@server.tool()
def check_duplicate(url: str) -> dict[str, Any]:
    """Check whether a URL is already published or already pending, so the
    agent can skip drafting a summary for something already covered."""
    return check_duplicate_core(url, current_config())


@server.tool()
def list_recent(topic: str = "", limit: int = 20, status: str = "published") -> list[dict[str, Any]]:
    """List recent items, optionally filtered by topic. `status` is
    "published" (default, what's actually live), "pending" (queued
    candidates awaiting promotion), or "all"."""
    return list_recent_core(topic, limit, current_config(), status)


@server.tool()
def propose_item(
    topic: str,
    title: str,
    url: str,
    summary: str = "",
    source: str = "",
    source_url: str = "",
    image_url: str = "",
    content_html: str = "",
    score: float = 0.0,
) -> dict[str, Any]:
    """Queue a candidate item for a topic. This does NOT publish it and does
    NOT touch git -- it's a cheap local append, safe to call many times in a
    row (or from several agent sessions at once) while you're still
    evaluating candidates. `score` is an importance/interest rating (any
    scale you like, e.g. 0-10 -- just be consistent within a topic): at the
    next promotion round (the hourly discovery Action, or call
    publish_pending to do it now) each topic's pending candidates are ranked
    by score and the top ones are promoted into the live feed, up to that
    topic's remaining daily quota (config.json's
    agent_publish.default_daily_quota). Once promoted, score no longer
    matters -- the item just ages out by plain recency like everything else,
    so a good old item can never permanently block a new one. Call
    check_duplicate or list_recent first so you don't waste effort on
    something already published or already queued."""
    result = propose_item_core(
        topic,
        title,
        url,
        summary,
        source,
        source_url,
        image_url,
        content_html,
        score,
        config=current_config(),
        now=datetime.now(timezone.utc),
    )
    return asdict(result)


@server.tool()
def publish_pending() -> dict[str, Any]:
    """Run a publish cycle right now instead of waiting for the next hourly
    discovery Action: promotes each topic's highest-scored pending
    candidates (up to its remaining daily quota) into the live feed,
    regenerates the RSS files, and commits + pushes everything (the
    promoted items, the updated pending queue, and the generated feeds) to
    the repo's git remote so GitHub Pages picks it up."""
    config = current_config()
    counts = rss_publish(DEFAULT_CONFIG_PATH, fetch_rss=False, now=datetime.now(timezone.utc))

    settings = agent_publish_settings(config)
    sync_paths = [
        settings["output_path"],
        settings["candidates_path"],
        config["output_dir"],
        config["processed_links"],
    ]
    sync = commit_and_push(
        sync_paths,
        "agent: publish pending candidates",
        repo_dir=REPO_DIR,
        on_rebase=regenerate_feeds,
    )
    return {"counts": counts, "git": asdict(sync)}


if __name__ == "__main__":
    server.run()
