#!/usr/bin/env python3
"""MCP server for agent-driven RSS publishing.

Curation judgment (what is worth publishing) lives in the calling agent, not
in this repo. This server only owns what the repo must own reliably: URL
dedup against everything already published, feed lifespan (handled by
`rss_aggregator.py`'s per-feed retention config), and a per-topic daily
publish quota so an agent that found many good candidates can only push a
few at a time. Topics are free-form: an unconfigured topic name still works
and gets `config.json`'s `default_feed` retention policy.
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
class PublishResult:
    status: str  # "published" | "duplicate" | "quota_exceeded" | "invalid"
    message: str
    item_id: str = ""
    topic: str = ""
    remaining_quota: int | None = None


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


def agent_publish_settings(config: dict[str, Any]) -> dict[str, Any]:
    settings = dict(config.get("agent_publish", {}))
    settings.setdefault("output_path", "items.agent.jsonl")
    settings.setdefault("default_daily_quota", 3)
    settings.setdefault("daily_quota_by_topic", {})
    return settings


def known_url_paths(config: dict[str, Any]) -> list[Path]:
    return [Path(path) for path in config["item_inputs"]]


def count_topic_publishes_today(topic: str, agent_path: Path, now: datetime) -> int:
    if not agent_path.exists():
        return 0

    today = now.astimezone(timezone.utc).date()
    count = 0
    with agent_path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if str(item.get("category", "")).strip().lower() != topic:
                continue
            try:
                published_at = datetime.fromisoformat(
                    str(item.get("published_at", "")).replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if published_at.astimezone(timezone.utc).date() == today:
                count += 1
    return count


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
    for path in known_url_paths(config):
        if path.exists() and normalized in load_known_urls([path]):
            return {"duplicate": True, "found_in": str(path)}
    processed_path = Path(config["processed_links"])
    if processed_path.exists() and normalized in processed_link_urls(processed_path):
        return {"duplicate": True, "found_in": str(processed_path)}
    return {"duplicate": False, "found_in": ""}


def list_recent_core(topic: str, limit: int, config: dict[str, Any]) -> list[dict[str, Any]]:
    topic = topic.strip().lower()
    items: list[dict[str, Any]] = []
    for raw_path in config["item_inputs"]:
        path = Path(raw_path)
        if path.suffix.lower() != ".jsonl" or not path.exists():
            continue
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

    items.sort(key=lambda item: str(item.get("published_at", "")), reverse=True)
    return items[:limit]


def publish_item_core(
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
) -> PublishResult:
    topic = topic.strip().lower()
    title = title.strip()
    url = url.strip()
    if not topic or not title or not url:
        return PublishResult(status="invalid", message="topic, title, and url are required.")

    known = set()
    for path in known_url_paths(config):
        if path.exists():
            known |= load_known_urls([path])
    processed_path = Path(config["processed_links"])
    if processed_path.exists():
        known |= processed_link_urls(processed_path)
    if normalize_url(url) in known:
        return PublishResult(status="duplicate", message=f"{url} is already published.", topic=topic)

    settings = agent_publish_settings(config)
    agent_path = Path(settings["output_path"])
    quota = int(settings["daily_quota_by_topic"].get(topic, settings["default_daily_quota"]))
    published_today = count_topic_publishes_today(topic, agent_path, now)
    if published_today >= quota:
        return PublishResult(
            status="quota_exceeded",
            message=f"Daily quota of {quota} for topic '{topic}' already reached.",
            topic=topic,
            remaining_quota=0,
        )

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
    append_jsonl(agent_path, [item])

    return PublishResult(
        status="published",
        message=f"Published to '{topic}'.",
        item_id=item_id,
        topic=topic,
        remaining_quota=quota - published_today - 1,
    )


def current_config() -> dict[str, Any]:
    return load_config(DEFAULT_CONFIG_PATH)


@server.tool()
def list_topics() -> list[dict[str, Any]]:
    """List configured RSS topics/feeds. Any other topic name also works with
    publish_item -- an unconfigured topic gets config.json's default_feed
    retention policy instead of a hand-tuned one."""
    return list_topics_core(current_config())


@server.tool()
def check_duplicate(url: str) -> dict[str, Any]:
    """Check whether a URL has already been published to any feed, so the
    agent can skip drafting a summary for something already covered."""
    return check_duplicate_core(url, current_config())


@server.tool()
def list_recent(topic: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """List recently published items, optionally filtered by topic."""
    return list_recent_core(topic, limit, current_config())


@server.tool()
def publish_item(
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
    """Publish one item to a topic feed. Enforces URL dedup across every
    configured item input and a per-topic daily publish quota (see
    config.json's agent_publish.default_daily_quota) -- call check_duplicate
    or list_recent first so quota isn't spent on items already published.
    `score` is an optional importance/interest rating (any scale you like,
    e.g. 0-10 -- just be consistent within a topic): when a feed's max_items
    cap forces items out, higher-scored items are kept over lower-scored
    ones regardless of recency, so you can publish generously and let the
    cap surface the best ones. Items left at the default 0.0 score keep the
    old recency-only behavior. On success this also regenerates the RSS
    feeds and commits + pushes them (plus the new item) to the repo's git
    remote, so GitHub Pages picks it up on the next deploy."""
    config = current_config()
    result = publish_item_core(
        topic,
        title,
        url,
        summary,
        source,
        source_url,
        image_url,
        content_html,
        score,
        config=config,
        now=datetime.now(timezone.utc),
    )
    response = asdict(result)

    if result.status == "published":
        regenerate_feeds()
        settings = agent_publish_settings(config)
        sync_paths = [settings["output_path"], config["output_dir"], config["processed_links"]]
        sync = commit_and_push(
            sync_paths,
            f'agent: publish "{title.strip()}" to {result.topic}',
            repo_dir=REPO_DIR,
            on_rebase=regenerate_feeds,
        )
        response["git"] = asdict(sync)

    return response


if __name__ == "__main__":
    server.run()
