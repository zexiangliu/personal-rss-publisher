#!/usr/bin/env python3
"""Record lightweight feedback for research curation."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rss_aggregator import normalize_url

DEFAULT_FEEDBACK_PATH = Path("feedback.jsonl")
DEFAULT_ITEM_PATHS = [
    Path("research_candidates.jsonl"),
    Path("items.discovered.jsonl"),
    Path("items.jsonl"),
]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def load_items(paths: list[Path]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in paths:
        items.extend(load_jsonl(path))
    return items


def find_item(identifier: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    normalized_identifier = ""
    if "://" in identifier:
        normalized_identifier = normalize_url(identifier)

    for item in items:
        item_id = str(item.get("id", "")).strip()
        item_url = str(item.get("url", "")).strip()
        if item_id == identifier:
            return item
        if normalized_identifier and item_url and normalize_url(item_url) == normalized_identifier:
            return item
    return None


def rate_item(
    identifier: str,
    rating: int,
    tags: list[str],
    note: str,
    feedback_path: Path = DEFAULT_FEEDBACK_PATH,
    item_paths: list[Path] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    items = load_items(item_paths or DEFAULT_ITEM_PATHS)
    item = find_item(identifier, items) or {}
    curation = item.get("curation", {}) if isinstance(item.get("curation"), dict) else {}

    record = {
        "created_at": now.astimezone(timezone.utc).isoformat(),
        "item_id": str(item.get("id", identifier)).strip(),
        "url": str(item.get("url", identifier if "://" in identifier else "")).strip(),
        "title": str(item.get("title", "")).strip(),
        "source": str(item.get("source", "")).strip(),
        "topic_id": str(item.get("topic_id") or curation.get("topic_id", "")).strip(),
        "topic": str(item.get("topic") or curation.get("topic", "")).strip(),
        "keywords": list(curation.get("keywords", [])),
        "rating": rating,
        "tags": tags,
        "note": note.strip(),
    }
    append_jsonl(feedback_path, record)
    return record


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_leading_newline = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8") as f:
        if needs_leading_newline:
            f.write("\n")
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True))


def recent_items(
    channel: str,
    limit: int,
    item_paths: list[Path] | None = None,
) -> list[dict[str, Any]]:
    items = load_items(item_paths or DEFAULT_ITEM_PATHS)
    if channel:
        items = [item for item in items if str(item.get("category", "")) == channel]
    items.sort(key=lambda item: str(item.get("published_at", "")), reverse=True)
    return items[:limit]


def print_recent_items(items: list[dict[str, Any]]) -> None:
    for item in items:
        curation = item.get("curation", {}) if isinstance(item.get("curation"), dict) else {}
        score = curation.get("overall_score")
        score_text = f" score={score}" if score is not None else ""
        decision = curation.get("decision")
        decision_text = f" decision={decision}" if decision else ""
        topic = item.get("topic") or curation.get("topic")
        topic_text = f" [{topic}]" if topic else ""
        print(f"{item.get('id', '')}{score_text}{decision_text}{topic_text}")
        print(f"  {item.get('title', '')}")
        if item.get("url"):
            print(f"  {item['url']}")


def split_tags(values: list[str]) -> list[str]:
    tags: list[str] = []
    for value in values:
        for tag in value.split(","):
            cleaned = tag.strip()
            if cleaned:
                tags.append(cleaned)
    return tags


def main() -> None:
    parser = argparse.ArgumentParser(description="Record feedback for curated items.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List recent items with IDs.")
    list_parser.add_argument("--channel", default="research")
    list_parser.add_argument("--limit", type=int, default=20)

    rate_parser = subparsers.add_parser("rate", help="Rate an item by id or URL.")
    rate_parser.add_argument("identifier")
    rate_parser.add_argument("--rating", type=int, choices=[-2, -1, 0, 1, 2], required=True)
    rate_parser.add_argument("--tag", action="append", default=[])
    rate_parser.add_argument("--note", default="")
    rate_parser.add_argument("--feedback", type=Path, default=DEFAULT_FEEDBACK_PATH)

    args = parser.parse_args()
    if args.command == "list":
        print_recent_items(recent_items(args.channel, args.limit))
    elif args.command == "rate":
        record = rate_item(
            args.identifier,
            args.rating,
            split_tags(args.tag),
            args.note,
            feedback_path=args.feedback,
        )
        target = record["item_id"] or record["url"] or args.identifier
        print(f"Recorded rating {record['rating']} for {target}.")


if __name__ == "__main__":
    main()
