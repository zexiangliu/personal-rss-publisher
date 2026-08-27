#!/usr/bin/env python3
"""Curate research papers and technical blogs into the RSS item boundary.

The pipeline is deliberately split in two layers:

1. deterministic fetching, dedupe, and transparent scoring;
2. optional OpenAI reviewer for higher-quality judgment when OPENAI_API_KEY is set.

If the optional reviewer is disabled or unavailable, the deterministic scorer keeps
the feed useful and the GitHub Action reliable.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import feedparser

from discover_items import append_jsonl, clean_summary, entry_datetime, load_known_urls
from rss_aggregator import normalize_url, parse_datetime, stable_guid

DEFAULT_CONFIG_PATH = Path("research_topics.json")
ARXIV_API_URL = "https://export.arxiv.org/api/query"
ATOM_NS = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class ResearchCandidate:
    id: str
    title: str
    url: str
    source: str
    source_url: str
    source_type: str
    topic_id: str
    topic_name: str
    summary: str
    published_at: datetime
    authors: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    pdf_url: str = ""


@dataclass(frozen=True)
class Review:
    quality_score: float
    relevance_score: float
    overall_score: float
    decision: str
    reason: str
    reader_summary: str
    keywords: tuple[str, ...]
    reviewer: str = "rules"


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    config.setdefault("output_path", "items.discovered.jsonl")
    config.setdefault("candidate_cache_path", "research_candidates.jsonl")
    config.setdefault("feedback_path", "feedback.jsonl")
    config.setdefault("dedupe_against", ["items.jsonl", "items.discovered.jsonl"])
    config.setdefault("defaults", {})
    config.setdefault("topics", [])

    defaults = config["defaults"]
    defaults.setdefault("enabled", True)
    defaults.setdefault("category", "research")
    defaults.setdefault("lookback_days", 45)
    defaults.setdefault("max_candidates_per_topic", 25)
    defaults.setdefault("max_items_per_topic", 4)
    defaults.setdefault("summary_max_chars", 900)
    defaults.setdefault("publish_threshold", 6.8)
    defaults.setdefault("preferred_categories", ["cs.LG", "cs.AI", "stat.ML"])
    defaults.setdefault("agent_review", {})

    agent = defaults["agent_review"]
    agent.setdefault("enabled", False)
    agent.setdefault("model", "gpt-5-mini")
    agent.setdefault("max_items_per_run", 8)
    agent.setdefault("min_deterministic_score", 5.5)
    agent.setdefault("timeout_seconds", 45)
    return config


def curate(
    config_path: Path = DEFAULT_CONFIG_PATH,
    now: datetime | None = None,
    use_agent: bool | None = None,
) -> int:
    now = now or datetime.now(timezone.utc)
    config = load_config(config_path)
    defaults = config["defaults"]
    output_path = Path(config["output_path"])
    feedback_records = load_jsonl(Path(config["feedback_path"]))
    known_urls = load_known_urls(
        [Path(path) for path in config["dedupe_against"]] + [output_path]
    )

    agent_config = dict(defaults["agent_review"])
    if use_agent is not None:
        agent_config["enabled"] = use_agent
    agent_budget = int(agent_config.get("max_items_per_run", 0))

    cache_items: list[dict[str, Any]] = []
    publishable_items: list[dict[str, Any]] = []

    for topic in config["topics"]:
        if not topic_enabled(topic, defaults):
            continue

        try:
            candidates = discover_topic_candidates(topic, defaults, now)
        except Exception as exc:
            if topic.get("required", False):
                raise
            print(f"Warning: skipped topic {topic.get('name') or topic.get('id')}: {exc}", file=sys.stderr)
            continue

        candidates = dedupe_candidates(candidates)
        reviewed_for_topic: list[tuple[Review, dict[str, Any]]] = []
        for candidate in candidates:
            if normalize_url(candidate.url) in known_urls:
                continue

            review = deterministic_review(candidate, topic, defaults, feedback_records, now)
            if should_call_agent(review, agent_config, agent_budget):
                agent_review = review_with_openai(candidate, topic, review, agent_config)
                if agent_review is not None:
                    review = agent_review
                    agent_budget -= 1

            item = render_item(candidate, topic, defaults, review, now)
            cache_items.append(item)
            reviewed_for_topic.append((review, item))

        reviewed_for_topic.sort(
            key=lambda pair: (
                pair[0].overall_score,
                pair[1].get("published_at", ""),
                pair[1].get("title", ""),
            ),
            reverse=True,
        )

        max_items = int(topic.get("max_items_per_topic", defaults["max_items_per_topic"]))
        selected = [
            item
            for review, item in reviewed_for_topic
            if review.decision == "publish"
        ][:max_items]
        for item in selected:
            known_urls.add(normalize_url(str(item["url"])))
        publishable_items.extend(selected)

    write_jsonl(Path(config["candidate_cache_path"]), cache_items)
    append_jsonl(output_path, publishable_items)
    return len(publishable_items)


def topic_enabled(topic: dict[str, Any], defaults: dict[str, Any]) -> bool:
    return bool(topic.get("enabled", defaults["enabled"]))


def discover_topic_candidates(
    topic: dict[str, Any], defaults: dict[str, Any], now: datetime
) -> list[ResearchCandidate]:
    candidates: list[ResearchCandidate] = []
    max_candidates = int(
        topic.get("max_candidates_per_topic", defaults["max_candidates_per_topic"])
    )

    arxiv_query = str(topic.get("arxiv_query", "")).strip()
    if arxiv_query:
        arxiv_url = build_arxiv_url(arxiv_query, max_candidates)
        xml_text = fetch_text(arxiv_url)
        candidates.extend(parse_arxiv_feed(xml_text, topic, defaults, now, arxiv_url))

    for source in topic.get("rss_sources", []):
        if source.get("enabled", True) is False:
            continue
        candidates.extend(fetch_rss_blog_candidates(source, topic, defaults, now))

    return candidates


def build_arxiv_url(search_query: str, max_results: int) -> str:
    params = {
        "search_query": search_query,
        "start": "0",
        "max_results": str(max_results),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    return f"{ARXIV_API_URL}?{urlencode(params)}"


def fetch_text(url: str, timeout: int = 30) -> str:
    request = Request(
        url,
        headers={
            "Accept": "application/atom+xml, application/rss+xml, text/xml, */*",
            "User-Agent": "personal-rss-publisher/0.3 (research curation)",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset)


def parse_arxiv_feed(
    xml_text: str,
    topic: dict[str, Any],
    defaults: dict[str, Any],
    now: datetime,
    source_url: str,
) -> list[ResearchCandidate]:
    root = ElementTree.fromstring(xml_text)
    candidates: list[ResearchCandidate] = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        raw_id = text_of(entry, "id")
        arxiv_id = arxiv_base_id(raw_id)
        if not arxiv_id:
            continue

        title = collapse_whitespace(text_of(entry, "title"))
        summary = collapse_whitespace(text_of(entry, "summary"))
        if not title or not summary:
            continue

        published_at = parse_datetime(
            text_of(entry, "published") or text_of(entry, "updated"), fallback=now
        )
        if published_at < now - lookback_window(topic, defaults):
            continue

        authors = tuple(
            collapse_whitespace(text_of(author, "name"))
            for author in entry.findall(f"{ATOM_NS}author")
            if text_of(author, "name")
        )
        categories = tuple(
            str(category.attrib.get("term", "")).strip()
            for category in entry.findall(f"{ATOM_NS}category")
            if str(category.attrib.get("term", "")).strip()
        )
        url = f"https://arxiv.org/abs/{arxiv_id}"
        pdf_url = arxiv_pdf_url(entry, arxiv_id)
        topic_name = str(topic.get("name") or topic.get("id") or "Research").strip()

        candidates.append(
            ResearchCandidate(
                id=f"arxiv:{arxiv_id}",
                title=title,
                url=url,
                source="arXiv",
                source_url=source_url,
                source_type="arxiv",
                topic_id=str(topic.get("id") or topic_name).strip(),
                topic_name=topic_name,
                summary=summary,
                published_at=published_at,
                authors=authors,
                categories=categories,
                pdf_url=pdf_url,
            )
        )
    return candidates


def text_of(element: ElementTree.Element, child_name: str) -> str:
    child = element.find(f"{ATOM_NS}{child_name}")
    return child.text.strip() if child is not None and child.text else ""


def arxiv_base_id(raw_id: str) -> str:
    value = raw_id.strip().rstrip("/")
    if not value:
        return ""
    last_part = value.rsplit("/", 1)[-1]
    return re.sub(r"v\d+$", "", last_part)


def arxiv_pdf_url(entry: ElementTree.Element, arxiv_id: str) -> str:
    for link in entry.findall(f"{ATOM_NS}link"):
        href = str(link.attrib.get("href", "")).strip()
        title = str(link.attrib.get("title", "")).strip().casefold()
        link_type = str(link.attrib.get("type", "")).strip().casefold()
        if href and (title == "pdf" or link_type == "application/pdf"):
            return href.replace("http://", "https://", 1)
    return f"https://arxiv.org/pdf/{arxiv_id}"


def fetch_rss_blog_candidates(
    source: dict[str, Any],
    topic: dict[str, Any],
    defaults: dict[str, Any],
    now: datetime,
) -> list[ResearchCandidate]:
    feed_url = str(source.get("url", "")).strip()
    if not feed_url:
        raise ValueError("RSS blog source needs a url")

    parsed = feedparser.parse(feed_url)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"Could not parse RSS source: {feed_url}")

    source_name = str(source.get("name", "")).strip()
    if not source_name:
        source_name = str(parsed.feed.get("title", "")).strip() or feed_url

    candidates: list[ResearchCandidate] = []
    topic_name = str(topic.get("name") or topic.get("id") or "Research").strip()
    for entry in parsed.entries:
        title = collapse_whitespace(str(entry.get("title", "")))
        url = str(entry.get("link", "")).strip()
        if not title or not url:
            continue
        published_at = entry_datetime(entry, now)
        if published_at < now - lookback_window(topic, defaults):
            continue
        summary = clean_summary(
            str(entry.get("summary") or entry.get("description") or ""),
            int(topic.get("summary_max_chars", defaults["summary_max_chars"])),
        )
        candidates.append(
            ResearchCandidate(
                id=str(entry.get("id", "") or entry.get("guid", "")).strip() or stable_guid(url),
                title=title,
                url=url,
                source=source_name,
                source_url=feed_url,
                source_type="rss",
                topic_id=str(topic.get("id") or topic_name).strip(),
                topic_name=topic_name,
                summary=summary,
                published_at=published_at,
            )
        )
    return candidates


def lookback_window(topic: dict[str, Any], defaults: dict[str, Any]) -> timedelta:
    if topic.get("lookback_hours") is not None:
        return timedelta(hours=float(topic["lookback_hours"]))
    if topic.get("lookback_days") is not None:
        return timedelta(days=float(topic["lookback_days"]))
    return timedelta(days=float(defaults["lookback_days"]))


def dedupe_candidates(candidates: list[ResearchCandidate]) -> list[ResearchCandidate]:
    by_url: dict[str, ResearchCandidate] = {}
    for candidate in candidates:
        key = normalize_url(candidate.url)
        existing = by_url.get(key)
        if existing is None or candidate.published_at > existing.published_at:
            by_url[key] = candidate
    return sorted(
        by_url.values(),
        key=lambda candidate: (candidate.published_at, candidate.title),
        reverse=True,
    )


def deterministic_review(
    candidate: ResearchCandidate,
    topic: dict[str, Any],
    defaults: dict[str, Any],
    feedback_records: list[dict[str, Any]],
    now: datetime,
) -> Review:
    text = candidate_text(candidate)
    include_keywords = list(topic.get("include_keywords", []))
    exclude_keywords = list(topic.get("exclude_keywords", []))
    rank_keywords = topic.get("rank_keywords", {})

    if include_keywords and not keyword_matches(text, include_keywords):
        return hold_review(
            candidate,
            "does not match required topic keywords",
            reviewer="rules",
        )
    if keyword_matches(text, exclude_keywords):
        return hold_review(candidate, "matches an excluded keyword", reviewer="rules")

    keyword_points, matched = weighted_keyword_points(candidate, rank_keywords)
    relevance_score = clamp(2.0 + min(keyword_points, 7.0), 0.0, 10.0)

    quality_score = 4.0
    quality_score += 1.2 if candidate.source_type == "arxiv" else 0.7
    quality_score += min(len(candidate.summary) / 750, 1.4)
    quality_score += min(len(candidate.authors) * 0.12, 0.6)
    if preferred_category_match(candidate, topic, defaults):
        quality_score += 0.7
    quality_score = clamp(quality_score, 0.0, 10.0)

    age_days = max((now - candidate.published_at).total_seconds() / 86400, 0.0)
    recency_bonus = clamp(1.4 - (age_days / 28), 0.0, 1.4)
    feedback_bonus = feedback_adjustment(candidate, matched, feedback_records)
    overall_score = clamp(
        (0.58 * relevance_score) + (0.27 * quality_score) + recency_bonus + feedback_bonus,
        0.0,
        10.0,
    )

    threshold = float(topic.get("publish_threshold", defaults["publish_threshold"]))
    decision = "publish" if overall_score >= threshold else "hold"
    reason = review_reason(candidate, matched, feedback_bonus)
    reader_summary = reader_summary_for(candidate, reason, overall_score, defaults, topic)
    return Review(
        quality_score=quality_score,
        relevance_score=relevance_score,
        overall_score=overall_score,
        decision=decision,
        reason=reason,
        reader_summary=reader_summary,
        keywords=tuple(matched),
    )


def hold_review(candidate: ResearchCandidate, reason: str, reviewer: str) -> Review:
    return Review(
        quality_score=0.0,
        relevance_score=0.0,
        overall_score=0.0,
        decision="hold",
        reason=reason,
        reader_summary=reader_summary_for(candidate, reason, 0.0, {}, {}),
        keywords=(),
        reviewer=reviewer,
    )


def candidate_text(candidate: ResearchCandidate) -> str:
    return "\n".join(
        [
            candidate.title,
            candidate.summary,
            " ".join(candidate.categories),
            " ".join(candidate.authors),
        ]
    ).casefold()


def keyword_matches(text: str, keywords: list[str]) -> bool:
    return any(str(keyword).casefold() in text for keyword in keywords)


def weighted_keyword_points(
    candidate: ResearchCandidate,
    rank_keywords: dict[str, int | float] | list[str],
) -> tuple[float, list[str]]:
    if isinstance(rank_keywords, list):
        keyword_weights = {keyword: 1.0 for keyword in rank_keywords}
    else:
        keyword_weights = rank_keywords

    title = candidate.title.casefold()
    body = f"{candidate.summary}\n{' '.join(candidate.categories)}".casefold()
    score = 0.0
    matched: list[str] = []
    for keyword, raw_weight in keyword_weights.items():
        needle = str(keyword).casefold()
        if not needle:
            continue
        weight = float(raw_weight)
        if needle in title:
            score += weight * 1.45
            matched.append(str(keyword))
        elif needle in body:
            score += weight
            matched.append(str(keyword))
    return score, matched


def preferred_category_match(
    candidate: ResearchCandidate, topic: dict[str, Any], defaults: dict[str, Any]
) -> bool:
    preferred = set(topic.get("preferred_categories", defaults["preferred_categories"]))
    return any(category in preferred for category in candidate.categories)


def feedback_adjustment(
    candidate: ResearchCandidate, matched_keywords: list[str], feedback_records: list[dict[str, Any]]
) -> float:
    adjustment = 0.0
    candidate_url = normalize_url(candidate.url)
    matched_set = {keyword.casefold() for keyword in matched_keywords}
    for record in feedback_records:
        rating = float(record.get("rating", 0) or 0)
        if not rating:
            continue

        record_url = str(record.get("url", "")).strip()
        record_id = str(record.get("item_id", "")).strip()
        if record_id == candidate.id or (record_url and normalize_url(record_url) == candidate_url):
            adjustment += rating * 1.6
        if str(record.get("topic_id", "")).strip() == candidate.topic_id:
            adjustment += rating * 0.22
        if str(record.get("source", "")).strip() == candidate.source:
            adjustment += rating * 0.12

        record_keywords = {
            str(keyword).casefold()
            for keyword in record.get("keywords", [])
            if str(keyword).strip()
        }
        overlap = matched_set & record_keywords
        adjustment += min(len(overlap), 4) * rating * 0.12

    return clamp(adjustment, -2.0, 2.0)


def review_reason(
    candidate: ResearchCandidate, matched_keywords: list[str], feedback_bonus: float
) -> str:
    parts: list[str] = []
    if matched_keywords:
        parts.append(f"matches {', '.join(matched_keywords[:4])}")
    if candidate.categories:
        parts.append(f"category {', '.join(candidate.categories[:2])}")
    if feedback_bonus:
        sign = "+" if feedback_bonus > 0 else ""
        parts.append(f"feedback {sign}{feedback_bonus:.1f}")
    return "; ".join(parts) or "topic match with recent publication"


def reader_summary_for(
    candidate: ResearchCandidate,
    reason: str,
    overall_score: float,
    defaults: dict[str, Any],
    topic: dict[str, Any],
) -> str:
    max_chars = int(topic.get("summary_max_chars", defaults.get("summary_max_chars", 900)))
    prefix = f"{candidate.topic_name} | score {overall_score:.1f}/10. Why: {reason}."
    return clean_summary(f"{prefix} {candidate.summary}", max_chars)


def should_call_agent(review: Review, agent_config: dict[str, Any], remaining_budget: int) -> bool:
    if not agent_config.get("enabled", False):
        return False
    if remaining_budget <= 0:
        return False
    if not os.environ.get("OPENAI_API_KEY"):
        return False
    min_score = float(agent_config.get("min_deterministic_score", 0))
    return review.overall_score >= min_score


def review_with_openai(
    candidate: ResearchCandidate,
    topic: dict[str, Any],
    initial_review: Review,
    agent_config: dict[str, Any],
) -> Review | None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None

    payload = {
        "model": str(agent_config.get("model", "gpt-5-mini")),
        "input": [
            {
                "role": "system",
                "content": (
                    "You are a strict research curator for a personal e-ink RSS feed. "
                    "Prefer substantial, topical, technically meaningful papers or blog posts. "
                    "Reject generic listicles, weakly related items, and thin abstracts."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "topic": {
                            "id": topic.get("id"),
                            "name": topic.get("name"),
                            "include_keywords": topic.get("include_keywords", []),
                            "rank_keywords": topic.get("rank_keywords", {}),
                        },
                        "candidate": candidate_to_review_payload(candidate),
                        "rule_review": review_to_dict(initial_review),
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "research_review",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "quality_score": {"type": "number", "minimum": 0, "maximum": 10},
                        "relevance_score": {"type": "number", "minimum": 0, "maximum": 10},
                        "overall_score": {"type": "number", "minimum": 0, "maximum": 10},
                        "decision": {"type": "string", "enum": ["publish", "hold"]},
                        "reason": {"type": "string"},
                        "reader_summary": {"type": "string"},
                        "keywords": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "quality_score",
                        "relevance_score",
                        "overall_score",
                        "decision",
                        "reason",
                        "reader_summary",
                        "keywords",
                    ],
                    "additionalProperties": False,
                },
            }
        },
    }

    try:
        response = fetch_openai_response(payload, api_key, int(agent_config["timeout_seconds"]))
        parsed = json.loads(extract_response_text(response))
    except Exception as exc:
        print(f"Warning: OpenAI review failed for {candidate.id}: {exc}", file=sys.stderr)
        return None

    return Review(
        quality_score=clamp(float(parsed["quality_score"]), 0.0, 10.0),
        relevance_score=clamp(float(parsed["relevance_score"]), 0.0, 10.0),
        overall_score=clamp(float(parsed["overall_score"]), 0.0, 10.0),
        decision=str(parsed["decision"]),
        reason=clean_summary(str(parsed["reason"]), 260),
        reader_summary=clean_summary(str(parsed["reader_summary"]), 900),
        keywords=tuple(str(keyword) for keyword in parsed.get("keywords", [])),
        reviewer=f"openai:{agent_config.get('model', 'gpt-5-mini')}",
    )


def fetch_openai_response(payload: dict[str, Any], api_key: str, timeout: int) -> dict[str, Any]:
    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "personal-rss-publisher/0.3",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset))


def extract_response_text(response: dict[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    for output in response.get("output", []):
        for content in output.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                return str(content["text"])
    raise ValueError("OpenAI response did not contain output text")


def candidate_to_review_payload(candidate: ResearchCandidate) -> dict[str, Any]:
    return {
        "id": candidate.id,
        "title": candidate.title,
        "url": candidate.url,
        "source": candidate.source,
        "source_type": candidate.source_type,
        "summary": candidate.summary,
        "published_at": candidate.published_at.isoformat(),
        "authors": list(candidate.authors),
        "categories": list(candidate.categories),
    }


def review_to_dict(review: Review) -> dict[str, Any]:
    return {
        "quality_score": review.quality_score,
        "relevance_score": review.relevance_score,
        "overall_score": review.overall_score,
        "decision": review.decision,
        "reason": review.reason,
        "keywords": list(review.keywords),
        "reviewer": review.reviewer,
    }


def render_item(
    candidate: ResearchCandidate,
    topic: dict[str, Any],
    defaults: dict[str, Any],
    review: Review,
    now: datetime,
) -> dict[str, Any]:
    category = str(topic.get("category", defaults["category"])).strip().lower()
    curation = {
        "topic_id": candidate.topic_id,
        "topic": candidate.topic_name,
        "reviewer": review.reviewer,
        "quality_score": round(review.quality_score, 2),
        "relevance_score": round(review.relevance_score, 2),
        "overall_score": round(review.overall_score, 2),
        "decision": review.decision,
        "reason": review.reason,
        "keywords": list(review.keywords),
        "reviewed_at": now.astimezone(timezone.utc).isoformat(),
    }
    item = {
        "id": candidate.id,
        "title": candidate.title,
        "url": candidate.url,
        "source": candidate.source,
        "source_url": candidate.source_url,
        "category": category,
        "summary": review.reader_summary,
        "published_at": candidate.published_at.astimezone(timezone.utc).isoformat(),
        "topic_id": candidate.topic_id,
        "topic": candidate.topic_name,
        "curation": curation,
        "content_html": render_content_html(candidate, review),
    }
    return item


def render_content_html(candidate: ResearchCandidate, review: Review) -> str:
    links = f'<p><a href="{html.escape(candidate.url)}">Abstract</a>'
    if candidate.pdf_url:
        links += f' | <a href="{html.escape(candidate.pdf_url)}">PDF</a>'
    links += "</p>"
    authors = ", ".join(candidate.authors[:8])
    if len(candidate.authors) > 8:
        authors += ", et al."

    sections = [
        f"<p><strong>Topic:</strong> {html.escape(candidate.topic_name)}</p>",
        f"<p><strong>Score:</strong> {review.overall_score:.1f}/10</p>",
        f"<p><strong>Why:</strong> {html.escape(review.reason)}</p>",
    ]
    if authors:
        sections.append(f"<p><strong>Authors:</strong> {html.escape(authors)}</p>")
    sections.extend(
        [
            f"<p>{html.escape(candidate.summary)}</p>",
            links,
        ]
    )
    return "\n".join(sections)


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


def write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    if not items:
        if path.exists():
            path.write_text("", encoding="utf-8")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in items) + "\n",
        encoding="utf-8",
    )


def collapse_whitespace(value: str) -> str:
    return " ".join(value.split())


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def main() -> None:
    parser = argparse.ArgumentParser(description="Curate research items for publishing.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    agent_group = parser.add_mutually_exclusive_group()
    agent_group.add_argument(
        "--use-agent",
        action="store_true",
        help="Use the optional OpenAI reviewer when OPENAI_API_KEY is available.",
    )
    agent_group.add_argument(
        "--no-agent",
        action="store_true",
        help="Force deterministic rules only, even if config enables agent review.",
    )
    args = parser.parse_args()

    use_agent = None
    if args.use_agent:
        use_agent = True
    elif args.no_agent:
        use_agent = False

    count = curate(args.config, use_agent=use_agent)
    print(f"Curated {count} new research item(s).")


if __name__ == "__main__":
    main()
