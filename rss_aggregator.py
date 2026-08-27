#!/usr/bin/env python3
"""Personal multi-channel RSS publisher.

Phase 1 keeps a narrow boundary: normalized JSON/JSONL items and optional
external RSS sources go in; static RSS files for GitHub Pages come out.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import feedparser
from bs4 import BeautifulSoup
from lxml import etree

CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
DEFAULT_CONFIG_PATH = Path("config.json")
DEFAULT_PROCESSED_LINKS_PATH = Path("processed_links.txt")


@dataclass(frozen=True)
class NormalizedItem:
    id: str
    title: str
    url: str
    category: str
    published_at: datetime
    source: str = ""
    source_url: str = ""
    summary: str = ""
    image_url: str = ""
    content_html: str = ""

    @property
    def normalized_url(self) -> str:
        return normalize_url(self.url)


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/") if parsed.path not in ("", "/") else parsed.path
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            parsed.query,
            "",
        )
    )


def stable_guid(url: str) -> str:
    digest = hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:24]
    return f"url:{digest}"


def parse_datetime(value: Any, fallback: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif value:
        text = str(value).strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            dt = parsedate_to_datetime(text)
    elif fallback is not None:
        dt = fallback
    else:
        raise ValueError("published_at is required")

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_rss_datetime(dt: datetime) -> str:
    return format_datetime(dt.astimezone(timezone.utc), usegmt=True)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    if "feeds" not in config or not isinstance(config["feeds"], dict):
        raise ValueError("config.json must define a feeds object")

    config.setdefault("output_dir", "public")
    config.setdefault("item_inputs", ["items.jsonl", "items.json"])
    config.setdefault("rss_sources", "rss_sources.json")
    config.setdefault("processed_links", str(DEFAULT_PROCESSED_LINKS_PATH))
    config.setdefault("processed_links_retention_days", 365)
    config.setdefault("combined_feed", {"title": "All", "output": "all.xml"})
    config.setdefault("site", {})
    config["site"].setdefault("title", "Personal RSS")
    config["site"].setdefault(
        "description", "Personal multi-channel RSS feeds for an e-ink reader."
    )
    config["site"].setdefault("site_url", "")
    return config


def load_structured_items(paths: list[str], now: datetime) -> list[NormalizedItem]:
    items: list[NormalizedItem] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            continue

        if path.suffix.lower() == ".jsonl":
            with path.open("r", encoding="utf-8") as f:
                for line_number, line in enumerate(f, start=1):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        raw_item = json.loads(stripped)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"{path}:{line_number}: invalid JSONL") from exc
                    items.append(normalize_item(raw_item, now))
        elif path.suffix.lower() == ".json":
            with path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            raw_items = raw.get("items", raw) if isinstance(raw, dict) else raw
            if isinstance(raw_items, dict):
                raw_items = [raw_items]
            if not isinstance(raw_items, list):
                raise ValueError(f"{path} must contain an item, items list, or object")
            items.extend(normalize_item(raw_item, now) for raw_item in raw_items)
        else:
            raise ValueError(f"Unsupported item input format: {path}")
    return items


def normalize_item(raw: dict[str, Any], now: datetime) -> NormalizedItem:
    if not isinstance(raw, dict):
        raise ValueError("Each item must be a JSON object")

    url = str(raw.get("url", "")).strip()
    title = str(raw.get("title", "")).strip()
    category = str(raw.get("category", "")).strip().lower()
    if not url:
        raise ValueError("Item is missing required field: url")
    if not title:
        raise ValueError(f"Item is missing required field: title ({url})")
    if not category:
        raise ValueError(f"Item is missing required field: category ({url})")

    item_id = str(raw.get("id", "")).strip() or stable_guid(url)
    return NormalizedItem(
        id=item_id,
        title=title,
        url=url,
        category=category,
        source=str(raw.get("source", "")).strip(),
        source_url=str(raw.get("source_url", "")).strip(),
        summary=str(raw.get("summary", "")).strip(),
        published_at=parse_datetime(raw.get("published_at"), fallback=now),
        image_url=str(raw.get("image_url", "")).strip(),
        content_html=str(raw.get("content_html", "")).strip(),
    )


def load_external_rss_items(sources_path: str, now: datetime) -> list[NormalizedItem]:
    path = Path(sources_path)
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8") as f:
        raw_sources = json.load(f)

    if isinstance(raw_sources, dict):
        raw_sources = raw_sources.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ValueError(f"{path} must contain a list or a sources object")

    items: list[NormalizedItem] = []
    for source in raw_sources:
        if not source or source.get("enabled", True) is False:
            continue
        items.extend(fetch_rss_source(source, now))
    return items


def fetch_rss_source(source: dict[str, Any], now: datetime) -> list[NormalizedItem]:
    feed_url = str(source.get("url", "")).strip()
    category = str(source.get("category", "")).strip().lower()
    if not feed_url or not category:
        raise ValueError("Each RSS source needs url and category")

    parsed_feed = feedparser.parse(feed_url)
    source_name = str(source.get("source", "")).strip()
    if not source_name:
        source_name = str(parsed_feed.feed.get("title", "")).strip() or feed_url

    items: list[NormalizedItem] = []
    for entry in parsed_feed.entries:
        url = str(entry.get("link", "")).strip()
        title = str(entry.get("title", "")).strip()
        if not url or not title:
            continue

        published_at = rss_entry_datetime(entry, now)
        summary = strip_html(entry.get("summary") or entry.get("description") or "")
        content_html = rss_entry_content_html(entry)
        raw_id = str(entry.get("id", "") or entry.get("guid", "")).strip()

        items.append(
            NormalizedItem(
                id=raw_id or stable_guid(url),
                title=title,
                url=url,
                category=category,
                source=source_name,
                source_url=feed_url,
                summary=summary,
                published_at=published_at,
                image_url=extract_image_url(entry),
                content_html=content_html,
            )
        )
    return items


def rss_entry_datetime(entry: Any, now: datetime) -> datetime:
    date_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if date_struct:
        timestamp = calendar.timegm(date_struct)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return parse_datetime(entry.get("published") or entry.get("updated"), fallback=now)


def rss_entry_content_html(entry: Any) -> str:
    content = entry.get("content")
    if content and isinstance(content, list):
        value = content[0].get("value", "")
        return str(value).strip()
    return ""


def strip_html(value: str) -> str:
    if not value:
        return ""
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


def extract_image_url(entry: Any) -> str:
    media_content = entry.get("media_content") or []
    for media in media_content:
        media_type = str(media.get("type", ""))
        media_url = str(media.get("url", "")).strip()
        if media_url and media_type.startswith("image/"):
            return media_url

    for link in entry.get("links", []):
        link_type = str(link.get("type", ""))
        link_href = str(link.get("href", "")).strip()
        if link_href and link_type.startswith("image/"):
            return link_href
    return ""


def dedupe_items(items: list[NormalizedItem]) -> list[NormalizedItem]:
    by_url: dict[str, NormalizedItem] = {}
    for item in items:
        key = item.normalized_url
        existing = by_url.get(key)
        if existing is None or item.published_at > existing.published_at:
            by_url[key] = item
    return sort_items(list(by_url.values()))


def sort_items(items: list[NormalizedItem]) -> list[NormalizedItem]:
    return sorted(items, key=lambda item: (item.published_at, item.title), reverse=True)


def route_items(
    items: list[NormalizedItem], config: dict[str, Any], now: datetime
) -> tuple[dict[str, list[NormalizedItem]], list[NormalizedItem]]:
    feeds: dict[str, list[NormalizedItem]] = {}
    for feed_name, feed_config in config["feeds"].items():
        category_items = [item for item in items if item.category == feed_name]
        feeds[feed_name] = apply_retention(category_items, feed_config, now)

    combined = dedupe_items([item for feed_items in feeds.values() for item in feed_items])
    combined = apply_retention(combined, config.get("combined_feed", {}), now)
    return feeds, combined


def apply_retention(
    items: list[NormalizedItem], policy: dict[str, Any], now: datetime
) -> list[NormalizedItem]:
    retained = sort_items(items)
    retention_days = policy.get("retention_days")
    if retention_days is not None:
        cutoff = now - timedelta(days=int(retention_days))
        retained = [item for item in retained if item.published_at >= cutoff]

    max_items = policy.get("max_items")
    if max_items is not None:
        retained = retained[: int(max_items)]
    return retained


def write_outputs(
    feeds: dict[str, list[NormalizedItem]],
    combined: list[NormalizedItem],
    config: dict[str, Any],
    now: datetime,
) -> None:
    output_dir = Path(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    for feed_name, feed_items in feeds.items():
        feed_config = config["feeds"][feed_name]
        output_name = feed_config.get("output", f"{feed_name}.xml")
        write_rss_file(output_dir / output_name, feed_config, feed_items, config, now)

    combined_config = config["combined_feed"]
    combined_output = combined_config.get("output", "all.xml")
    write_rss_file(output_dir / combined_output, combined_config, combined, config, now)
    write_index(output_dir / "index.html", config)


def write_rss_file(
    path: Path,
    feed_config: dict[str, Any],
    items: list[NormalizedItem],
    config: dict[str, Any],
    now: datetime,
) -> None:
    site = config["site"]
    site_url = site.get("site_url", "")
    feed_url = urljoin(ensure_trailing_slash(site_url), path.name) if site_url else path.name

    root = etree.Element("rss", version="2.0", nsmap={"content": CONTENT_NS})
    channel = etree.SubElement(root, "channel")
    etree.SubElement(channel, "title").text = feed_config.get("title", path.stem.title())
    etree.SubElement(channel, "link").text = site_url or feed_url
    etree.SubElement(channel, "description").text = feed_config.get(
        "description", site.get("description", "")
    )
    etree.SubElement(channel, "lastBuildDate").text = format_rss_datetime(now)
    etree.SubElement(channel, "generator").text = "personal-rss-publisher"

    for item in items:
        item_el = etree.SubElement(channel, "item")
        etree.SubElement(item_el, "title").text = item.title
        etree.SubElement(item_el, "link").text = item.url
        etree.SubElement(item_el, "guid", isPermaLink="false").text = item.id
        etree.SubElement(item_el, "pubDate").text = format_rss_datetime(item.published_at)
        etree.SubElement(item_el, "category").text = item.category

        if item.source:
            etree.SubElement(item_el, "source", url=item.source_url or item.url).text = (
                item.source
            )

        description = item.summary or strip_html(item.content_html) or item.title
        etree.SubElement(item_el, "description").text = description

        if item.image_url:
            etree.SubElement(
                item_el,
                "enclosure",
                url=item.image_url,
                length="0",
                type=guess_image_type(item.image_url),
            )

        if item.content_html:
            encoded = etree.SubElement(item_el, f"{{{CONTENT_NS}}}encoded")
            encoded.text = etree.CDATA(item.content_html)

    path.write_bytes(
        etree.tostring(
            root,
            pretty_print=True,
            xml_declaration=True,
            encoding="UTF-8",
        )
    )


def guess_image_type(url: str) -> str:
    parsed = urlparse(url)
    guessed, _ = mimetypes.guess_type(parsed.path)
    return guessed if guessed and guessed.startswith("image/") else "image/jpeg"


def ensure_trailing_slash(url: str) -> str:
    return url if not url or url.endswith("/") else f"{url}/"


def write_index(path: Path, config: dict[str, Any]) -> None:
    title = config["site"]["title"]
    rows: list[tuple[str, str]] = []
    for feed_name, feed_config in config["feeds"].items():
        rows.append((feed_config.get("title", feed_name.title()), feed_config.get("output", f"{feed_name}.xml")))
    combined = config["combined_feed"]
    rows.append((combined.get("title", "All"), combined.get("output", "all.xml")))

    feed_rows = "\n".join(
        f'      <li><span>{escape_html(label)}</span><a href="{escape_html(href)}">RSS</a></li>'
        for label, href in rows
    )
    path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape_html(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: Georgia, "Times New Roman", serif;
      background: #f7f7f2;
      color: #171717;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
    }}
    main {{
      width: min(32rem, calc(100% - 2rem));
      padding: 2rem 0;
    }}
    h1 {{
      margin: 0 0 1.25rem;
      font-size: clamp(2rem, 8vw, 3.5rem);
      line-height: 1;
      font-weight: 700;
      letter-spacing: 0;
    }}
    ul {{
      list-style: none;
      margin: 0;
      padding: 0;
      border-top: 1px solid #b8b8ad;
    }}
    li {{
      display: flex;
      justify-content: space-between;
      gap: 1rem;
      padding: 0.9rem 0;
      border-bottom: 1px solid #b8b8ad;
      font-size: 1.15rem;
    }}
    a {{
      color: #004f8b;
      font-weight: 700;
      text-decoration-thickness: 0.08em;
      text-underline-offset: 0.18em;
    }}
  </style>
</head>
<body>
  <main>
    <h1>{escape_html(title)}</h1>
    <ul>
{feed_rows}
    </ul>
  </main>
</body>
</html>
""",
        encoding="utf-8",
    )


def escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class ProcessedLinkStore:
    """Tiny file-backed ledger that can be replaced by SQLite later."""

    def __init__(self, path: Path, retention_days: int = 365):
        self.path = path
        self.retention_days = retention_days
        self.links: dict[str, datetime] = {}
        self.original_urls: dict[str, str] = {}

    def load(self) -> None:
        if not self.path.exists():
            return

        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(maxsplit=1)
                if len(parts) != 2:
                    continue
                try:
                    seen_at = parse_datetime(parts[0])
                except Exception:
                    continue
                original_url = parts[1].strip()
                normalized = normalize_url(original_url)
                existing = self.links.get(normalized)
                if existing is None or seen_at > existing:
                    self.links[normalized] = seen_at
                    self.original_urls[normalized] = original_url

    def contains(self, url: str) -> bool:
        return normalize_url(url) in self.links

    def mark(self, items: list[NormalizedItem]) -> None:
        for item in items:
            normalized = item.normalized_url
            existing = self.links.get(normalized)
            if existing is None or item.published_at > existing:
                self.links[normalized] = item.published_at
                self.original_urls[normalized] = item.url

    def write(self, now: datetime) -> None:
        cutoff = now - timedelta(days=self.retention_days)
        rows = [
            (seen_at, self.original_urls[normalized])
            for normalized, seen_at in self.links.items()
            if seen_at >= cutoff
        ]
        rows.sort(key=lambda row: (row[0], normalize_url(row[1])))

        with self.path.open("w", encoding="utf-8") as f:
            for seen_at, url in rows:
                f.write(f"{seen_at.astimezone(timezone.utc).isoformat()} {url}\n")


def publish(config_path: Path, fetch_rss: bool = True, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    config = load_config(config_path)

    items = load_structured_items(config["item_inputs"], now)
    if fetch_rss:
        items.extend(load_external_rss_items(config["rss_sources"], now))

    deduped = dedupe_items(items)
    feeds, combined = route_items(deduped, config, now)
    write_outputs(feeds, combined, config, now)

    store = ProcessedLinkStore(
        Path(config["processed_links"]),
        int(config.get("processed_links_retention_days", 365)),
    )
    store.load()
    store.mark(combined)
    store.write(now)

    counts = {feed_name: len(feed_items) for feed_name, feed_items in feeds.items()}
    counts["all"] = len(combined)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate personal RSS feeds.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument(
        "--no-fetch-rss",
        action="store_true",
        help="Skip optional external RSS imports from rss_sources.json.",
    )
    args = parser.parse_args()

    counts = publish(args.config, fetch_rss=not args.no_fetch_rss)
    for feed_name, count in counts.items():
        print(f"{feed_name}: {count} item(s)")


if __name__ == "__main__":
    main()
