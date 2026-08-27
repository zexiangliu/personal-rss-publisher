# Personal RSS Publisher

This is a small fork of [`hitem/rss-aggregator`](https://github.com/hitem/rss-aggregator) for publishing personal, categorized RSS feeds to GitHub Pages.

The Phase 1 pipeline is intentionally simple:

```text
items.json / items.jsonl
        ↓
rss_aggregator.py
        ↓
public/news.xml
public/research.xml
public/investing.xml
public/all.xml
        ↓
GitHub Pages
        ↓
e-ink RSS reader
```

There is no LLM integration, ranking, database, server, React app, or recommendation system in Phase 1.

## How This Differs From Upstream

The upstream project fetches a hardcoded list of Microsoft RSS/HTML sources, appends new entries into one `aggregated_feed.xml`, records processed URLs in `processed_links.txt`, and deploys the repository with GitHub Pages.

This fork keeps the useful parts:

- a small Python generator
- `feedparser`/`lxml` based RSS handling
- exact URL deduplication
- `processed_links.txt` as a tiny file-backed ledger
- GitHub Actions scheduling and manual runs
- GitHub Pages deployment

It changes the main shape:

- normalized JSON/JSONL items are the primary input
- multiple independent channel feeds are generated
- `all.xml` combines all configured channels
- feed retention is configured per channel
- generated files live under `public/`
- external RSS inputs are optional and normalize into the same item structure

## Repository Layout

```text
.
├── .github/workflows/rss_aggregator.yml
├── config.json
├── items.jsonl
├── processed_links.txt
├── public/
│   ├── index.html
│   ├── news.xml
│   ├── research.xml
│   ├── investing.xml
│   └── all.xml
├── requirements.txt
├── rss_aggregator.py
├── rss_sources.json
└── tests/test_rss_aggregator.py
```

## Install And Run Locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python rss_aggregator.py
python -m unittest discover -s tests -v
```

The generated feeds are written to `public/`.

## Add An Item Manually

Append one JSON object per line to `items.jsonl`:

```json
{"id":"research-2026-08-26-example","title":"Interesting paper","url":"https://example.com/article","source":"Example Lab","category":"research","summary":"Short summary for the reader.","published_at":"2026-08-26T08:00:00-07:00","image_url":"https://example.com/image.jpg"}
```

Supported fields:

| Field | Required | Notes |
|---|---:|---|
| `title` | yes | RSS item title |
| `url` | yes | Original article URL |
| `category` | yes | Must match a key in `config.json` feeds |
| `published_at` | no | ISO 8601 or RFC 2822; defaults to generation time |
| `id` | no | Stable GUID; generated from URL when omitted |
| `source` | no | Human-readable source |
| `source_url` | no | Source feed/site URL |
| `summary` | no | RSS description |
| `image_url` | no | Written as an RSS enclosure URL |
| `content_html` | no | Written as `content:encoded` |

Chinese and other Unicode text are supported because all inputs and outputs use UTF-8.

## Configure Feeds

`config.json` defines channel feeds:

```json
"feeds": {
  "news": {
    "title": "News",
    "output": "news.xml",
    "retention_days": 7
  },
  "research": {
    "title": "Research",
    "output": "research.xml",
    "max_items": 100
  }
}
```

To add a new channel, add one feed entry and start using that category in `items.jsonl`:

```json
"technology": {
  "title": "Technology",
  "description": "Technology notes and articles.",
  "output": "technology.xml",
  "max_items": 100
}
```

Then add items with `"category":"technology"`. No Python code change is required.

## Retention

Each channel can use:

- `retention_days`: remove items older than this many days
- `max_items`: keep only the newest N items

`all.xml` is built from the items that remain in the configured channel feeds, then applies its own optional `max_items` or `retention_days`.

## Deduplication And `processed_links.txt`

The publisher normalizes URLs and keeps only one item per URL in the generated output.

`processed_links.txt` is a simple ledger with one line per emitted URL:

```text
2026-08-26T15:00:00+00:00 https://example.com/article
```

In this Phase 1 fork, `items.jsonl` remains the source of truth, so the ledger does not hide existing structured items on later runs. It records what has been published and is isolated behind a small `ProcessedLinkStore` class so it can later be replaced by SQLite without changing RSS generation.

## Optional External RSS Sources

External RSS feeds can be listed in `rss_sources.json`:

```json
{
  "sources": [
    {
      "enabled": true,
      "url": "https://example.com/feed.xml",
      "source": "Example Feed",
      "category": "news"
    }
  ]
}
```

Fetched entries are normalized into the same internal item representation as `items.jsonl`. Leave `sources` empty if you only want manually or agent-generated structured items.

To skip RSS fetching during a local run:

```bash
python rss_aggregator.py --no-fetch-rss
```

## GitHub Pages

1. Push this repository to GitHub.
2. Go to `Settings -> Pages`.
3. Set `Build and deployment -> Source` to `GitHub Actions`.
4. Edit `config.json` and set `site.site_url`:

```json
"site_url": "https://<username>.github.io/<repo-name>/"
```

5. Go to `Actions -> Personal RSS Publisher -> Run workflow`.

The public URLs will be:

```text
https://<username>.github.io/<repo-name>/
https://<username>.github.io/<repo-name>/news.xml
https://<username>.github.io/<repo-name>/research.xml
https://<username>.github.io/<repo-name>/investing.xml
https://<username>.github.io/<repo-name>/all.xml
```

Subscribe to those `.xml` URLs from your e-ink reader's RSS app.

## Phase 2 Boundary

Future agents only need to produce normalized JSON/JSONL items. They should not need to know RSS, XML, GitHub Pages, or e-ink reader details.

```text
web / RSS / arXiv / search / AI agents
                ↓
          normalized items
                ↓
        rss_aggregator.py
                ↓
          GitHub Pages RSS
```

Keep Phase 2 additions upstream of `items.jsonl` unless the RSS publishing contract itself needs to change.
