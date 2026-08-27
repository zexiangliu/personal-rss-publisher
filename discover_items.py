#!/usr/bin/env python3
"""Phase 2 discovery pipeline.

This script is intentionally small: it reads configured RSS sources, filters and
ranks entries with transparent rules, then appends normalized JSONL items for
the existing RSS publisher to consume.
"""

from __future__ import annotations

import argparse
import calendar
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import feedparser
from bs4 import BeautifulSoup

from rss_aggregator import extract_image_url, normalize_url, stable_guid

DEFAULT_CONFIG_PATH = Path("discovery_config.json")


@dataclass(frozen=True)
class Candidate:
    item: dict[str, Any]
    score: float
    matched_keywords: tuple[str, ...]


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    config.setdefault("output_path", "items.discovered.jsonl")
    config.setdefault("dedupe_against", ["items.jsonl", "items.discovered.jsonl"])
    config.setdefault("defaults", {})
    config.setdefault("sources", [])
    defaults = config["defaults"]
    defaults.setdefault("enabled", False)
    defaults.setdefault("lookback_days", 7)
    defaults.setdefault("max_items_per_source", 10)
    defaults.setdefault("summary_max_chars", 600)
    defaults.setdefault("min_score", 0)
    defaults.setdefault("include_keywords", [])
    defaults.setdefault("exclude_keywords", [])
    defaults.setdefault("rank_keywords", {})
    return config


def discover(config_path: Path = DEFAULT_CONFIG_PATH, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    config = load_config(config_path)
    output_path = Path(config["output_path"])
    known_urls = load_known_urls(
        [Path(path) for path in config["dedupe_against"]] + [output_path]
    )

    new_items: list[dict[str, Any]] = []
    for source in config["sources"]:
        if not source_enabled(source, config["defaults"]):
            continue
        try:
            candidates = discover_source(source, config["defaults"], now)
        except Exception as exc:
            if not source.get("required", False):
                print(f"Warning: skipped source {source.get('name') or source.get('url')}: {exc}", file=sys.stderr)
                continue
            raise
        for candidate in candidates:
            url_key = normalize_url(candidate.item["url"])
            if url_key in known_urls:
                continue
            known_urls.add(url_key)
            new_items.append(candidate.item)

    append_jsonl(output_path, new_items)
    return len(new_items)


def source_enabled(source: dict[str, Any], defaults: dict[str, Any]) -> bool:
    return bool(source.get("enabled", defaults.get("enabled", False)))


def discover_source(
    source: dict[str, Any], defaults: dict[str, Any], now: datetime
) -> list[Candidate]:
    source_type = str(source.get("type", "rss")).strip().lower()
    if source_type == "hk01_zone":
        return discover_hk01_zone_source(source, defaults, now)
    if source_type != "rss":
        raise ValueError(f"Unsupported discovery source type: {source_type}")

    feed_url = str(source.get("url", "")).strip()
    category = str(source.get("category", "")).strip().lower()
    if not feed_url or not category:
        raise ValueError("Each enabled discovery source needs url and category")

    parsed = feedparser.parse(feed_url)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"Could not parse RSS source: {feed_url}")

    source_name = str(source.get("name", "")).strip()
    if not source_name:
        source_name = str(parsed.feed.get("title", "")).strip() or feed_url

    candidates: list[Candidate] = []
    for entry in parsed.entries:
        candidate = normalize_entry(entry, source, defaults, source_name, now)
        if candidate is not None:
            candidates.append(candidate)

    max_items = int(source.get("max_items_per_source", defaults["max_items_per_source"]))
    candidates.sort(
        key=lambda candidate: (
            candidate.score,
            candidate.item["published_at"],
            candidate.item["title"],
        ),
        reverse=True,
    )
    return candidates[:max_items]


def discover_hk01_zone_source(
    source: dict[str, Any], defaults: dict[str, Any], now: datetime
) -> list[Candidate]:
    page_url = str(source.get("url", "")).strip()
    zone_id = str(source.get("zone_id", "")).strip() or hk01_zone_id_from_url(page_url)
    category = str(source.get("category", "")).strip().lower()
    if not zone_id or not category:
        raise ValueError("HK01 zone sources need url or zone_id, plus category")

    api_url = str(source.get("api_url", "")).strip()
    if not api_url:
        api_url = f"https://web-data.api.hk01.com/v2/feed/zone/{zone_id}"

    payload = fetch_json(api_url)
    articles: list[dict[str, Any]] = []
    collect_hk01_articles(payload, articles)

    source_name = str(source.get("name", "")).strip() or "香港01 國際"
    candidates: list[Candidate] = []
    for article in articles:
        candidate = normalize_hk01_article(article, source, defaults, source_name, now)
        if candidate is not None:
            candidates.append(candidate)

    max_items = int(source.get("max_items_per_source", defaults["max_items_per_source"]))
    candidates.sort(
        key=lambda candidate: (
            candidate.score,
            candidate.item["published_at"],
            candidate.item["title"],
        ),
        reverse=True,
    )
    return candidates[:max_items]


def hk01_zone_id_from_url(url: str) -> str:
    path_parts = [part for part in urlparse(url).path.split("/") if part]
    for index, part in enumerate(path_parts):
        if part == "zone" and index + 1 < len(path_parts):
            return path_parts[index + 1]
    return ""


def fetch_json(url: str) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "personal-rss-publisher/0.2",
        },
    )
    with urlopen(request, timeout=20) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset))


def collect_hk01_articles(value: Any, articles: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        if "articleId" in value and "title" in value:
            articles.append(value)
        for child in value.values():
            collect_hk01_articles(child, articles)
    elif isinstance(value, list):
        for child in value:
            collect_hk01_articles(child, articles)


def normalize_hk01_article(
    article: dict[str, Any],
    source: dict[str, Any],
    defaults: dict[str, Any],
    source_name: str,
    now: datetime,
) -> Candidate | None:
    url = str(article.get("canonicalUrl") or article.get("publishUrl") or "").strip()
    title = str(article.get("title", "")).strip()
    category = str(source.get("category", "")).strip().lower()
    if not url or not title:
        return None

    if url.startswith("/"):
        url = f"https://www.hk01.com{url}"

    publish_time = article.get("publishTime") or article.get("lastModifyTime")
    if publish_time:
        published_at = datetime.fromtimestamp(int(publish_time), tz=timezone.utc)
    else:
        published_at = now

    lookback_days = int(source.get("lookback_days", defaults["lookback_days"]))
    if published_at < now - timedelta(days=lookback_days):
        return None

    summary = clean_summary(
        str(article.get("description") or ""),
        int(source.get("summary_max_chars", defaults["summary_max_chars"])),
    )
    text = f"{title}\n{summary}".casefold()

    include_keywords = list(source.get("include_keywords", defaults["include_keywords"]))
    exclude_keywords = list(source.get("exclude_keywords", defaults["exclude_keywords"]))
    rank_keywords = source.get("rank_keywords", defaults["rank_keywords"])
    if include_keywords and not keyword_matches(text, include_keywords):
        return None
    if keyword_matches(text, exclude_keywords):
        return None

    score, matched = score_entry(text, rank_keywords, published_at, now)
    min_score = float(source.get("min_score", defaults["min_score"]))
    if score < min_score:
        return None

    item = {
        "id": f"hk01:{article['articleId']}",
        "title": title,
        "url": url,
        "source": source_name,
        "source_url": str(source.get("url", "")).strip() or url,
        "category": category,
        "summary": summary,
        "published_at": published_at.isoformat(),
    }

    image_url = hk01_image_url(article)
    if image_url:
        item["image_url"] = image_url

    return Candidate(item=item, score=score, matched_keywords=tuple(matched))


def hk01_image_url(article: dict[str, Any]) -> str:
    for key in ("mainImage", "originalImage"):
        image = article.get(key)
        if isinstance(image, dict) and image.get("cdnUrl"):
            return str(image["cdnUrl"])
    return ""


def normalize_entry(
    entry: Any,
    source: dict[str, Any],
    defaults: dict[str, Any],
    source_name: str,
    now: datetime,
) -> Candidate | None:
    url = str(entry.get("link", "")).strip()
    title = str(entry.get("title", "")).strip()
    category = str(source.get("category", "")).strip().lower()
    if not url or not title:
        return None

    published_at = entry_datetime(entry, now)
    lookback_days = int(source.get("lookback_days", defaults["lookback_days"]))
    if published_at < now - timedelta(days=lookback_days):
        return None

    raw_summary = entry.get("summary") or entry.get("description") or ""
    summary = clean_summary(str(raw_summary), int(source.get("summary_max_chars", defaults["summary_max_chars"])))
    text = f"{title}\n{summary}".casefold()

    include_keywords = list(source.get("include_keywords", defaults["include_keywords"]))
    exclude_keywords = list(source.get("exclude_keywords", defaults["exclude_keywords"]))
    rank_keywords = source.get("rank_keywords", defaults["rank_keywords"])

    if include_keywords and not keyword_matches(text, include_keywords):
        return None
    if keyword_matches(text, exclude_keywords):
        return None

    score, matched = score_entry(text, rank_keywords, published_at, now)
    min_score = float(source.get("min_score", defaults["min_score"]))
    if score < min_score:
        return None

    item = {
        "id": str(entry.get("id", "") or entry.get("guid", "")).strip() or stable_guid(url),
        "title": title,
        "url": url,
        "source": source_name,
        "source_url": str(source.get("url", "")).strip(),
        "category": category,
        "summary": summary,
        "published_at": published_at.isoformat(),
    }
    image_url = extract_image_url(entry)
    if image_url:
        item["image_url"] = image_url

    return Candidate(item=item, score=score, matched_keywords=tuple(matched))


def entry_datetime(entry: Any, now: datetime) -> datetime:
    date_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if date_struct:
        timestamp = calendar.timegm(date_struct)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)

    raw_date = entry.get("published") or entry.get("updated")
    if raw_date:
        try:
            dt = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
        except ValueError:
            dt = parsedate_to_datetime(str(raw_date))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return now


def clean_summary(value: str, max_chars: int) -> str:
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1].rstrip()}…"


def keyword_matches(text: str, keywords: list[str]) -> bool:
    return any(str(keyword).casefold() in text for keyword in keywords)


def score_entry(
    text: str, rank_keywords: dict[str, int | float] | list[str], published_at: datetime, now: datetime
) -> tuple[float, list[str]]:
    if isinstance(rank_keywords, list):
        keyword_weights = {keyword: 1 for keyword in rank_keywords}
    else:
        keyword_weights = rank_keywords

    score = 0.0
    matched: list[str] = []
    for keyword, weight in keyword_weights.items():
        if str(keyword).casefold() in text:
            score += float(weight)
            matched.append(str(keyword))

    age_days = max((now - published_at).total_seconds() / 86400, 0)
    recency_bonus = max(0.0, 2.0 - (age_days / 3.5))
    return score + recency_bonus, matched


def load_known_urls(paths: list[Path]) -> set[str]:
    known: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        if path.suffix.lower() == ".jsonl":
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        item = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    url = str(item.get("url", "")).strip()
                    if url:
                        known.add(normalize_url(url))
        elif path.suffix.lower() == ".json":
            with path.open("r", encoding="utf-8") as f:
                try:
                    raw = json.load(f)
                except json.JSONDecodeError:
                    continue
            raw_items = raw.get("items", raw) if isinstance(raw, dict) else raw
            if isinstance(raw_items, dict):
                raw_items = [raw_items]
            if isinstance(raw_items, list):
                for item in raw_items:
                    if isinstance(item, dict) and item.get("url"):
                        known.add(normalize_url(str(item["url"])))
    return known


def append_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_leading_newline = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8") as f:
        if needs_leading_newline:
            f.write("\n")
        for index, item in enumerate(items):
            if index:
                f.write("\n")
            f.write(json.dumps(item, ensure_ascii=False, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover RSS items for publishing.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args()
    count = discover(args.config)
    print(f"Discovered {count} new item(s).")


if __name__ == "__main__":
    main()
