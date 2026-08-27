# Personal RSS Publisher

This is a small fork of [`hitem/rss-aggregator`](https://github.com/hitem/rss-aggregator) for publishing personal, categorized RSS feeds to GitHub Pages.

The Phase 1 pipeline is intentionally simple:

```text
items.json / items.jsonl
items.discovered.jsonl
items.agent.jsonl
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

Phase 2 starts upstream of `items.discovered.jsonl`: discovery scripts can fetch, filter, and rank sources, but the RSS publisher still only consumes normalized items.

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
├── .codex/config.toml
├── .cursor/mcp.json
├── .github/workflows/rss_aggregator.yml
├── .mcp.json
├── config.json
├── discover_items.py
├── discovery_config.json
├── items.agent.jsonl
├── items.candidates.jsonl
├── items.discovered.jsonl
├── items.jsonl
├── mcp_server.py
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
├── run_mcp_server.sh
├── setup.sh
└── tests/
    ├── test_discover_items.py
    ├── test_mcp_server.py
    └── test_rss_aggregator.py
```

## Install And Run Locally

```bash
./setup.sh   # creates .venv and installs requirements.txt (incl. mcp)
.venv/bin/python3 discover_items.py
.venv/bin/python3 rss_aggregator.py
.venv/bin/python3 -m unittest discover -s tests -v
```

`setup.sh` exists because most systems ship an "externally managed" system
Python that refuses `pip install mcp` directly (PEP 668) — a local `.venv` is
the reliable way to get `mcp_server.py`'s dependency installed.

After the venv is ready, `setup.sh` detects which of Claude Code / Cursor /
Codex CLI are installed and, for each one found, asks where to register
`personal-rss`: this project only, all projects (system-wide), or skip. It
shows the exact config file each choice would write before you pick, and
writing is idempotent — re-running `setup.sh` won't create duplicate entries.
See [Wiring it into an agent](#wiring-it-into-an-agent) for what each scope
means and how to change your answer later.

Equivalently, without the script:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
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
    "retention_hours": 24
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

Categories that appear in item inputs but aren't listed under `feeds` still
get a feed — they use `config.json`'s `default_feed` retention policy. This
is how `mcp_server.py` supports publishing to any topic name; see
[Agent Publishing (MCP)](#agent-publishing-mcp).

## Retention

Each channel can use:

- `retention_hours`: remove items older than this many hours
- `retention_days`: remove items older than this many days
- `max_items`: keep only the newest N items

`all.xml` is built from the items that remain in the configured channel feeds, then applies its own optional `max_items` or `retention_days`.

Retention is always pure recency — the oldest items go first, regardless of
any `score` an agent assigned. Score's job is over by the time an item
reaches this stage: it only decides which *pending* candidates get promoted
into the feed in the first place (see [Agent Publishing
(MCP)](#agent-publishing-mcp)). This is deliberate — an old high-scored item
should never be able to permanently block a newer one from ever appearing.

## Deduplication And `processed_links.txt`

The publisher normalizes URLs and keeps only one item per URL in the generated output.

`processed_links.txt` is a simple ledger with one line per emitted URL:

```text
2026-08-26T15:00:00+00:00 https://example.com/article
```

It's the **permanent** dedup memory, separate from the `item_inputs` files
that hold current feed content: `discover_items.py` and `mcp_server.py`'s
`check_duplicate`/`propose_item` all consult it (via `processed_link_urls()`
in `rss_aggregator.py`) in addition to `items.jsonl`/`items.discovered.jsonl`/
`items.agent.jsonl`, so a URL is remembered as "already published" even after
it ages out of every feed's retention window or is pruned from those files.
This is what makes it safe to physically trim `items.discovered.jsonl` and
`items.agent.jsonl` (see `item_log_retention` in
[Agent Publishing (MCP)](#agent-publishing-mcp)) without the same article
getting rediscovered or re-proposed later. It's isolated behind a small
`ProcessedLinkStore` class so it can later be replaced by SQLite without
changing RSS generation.

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

## Phase 2 Discovery

`discover_items.py` reads `discovery_config.json` and appends normalized items to `items.discovered.jsonl`.

The first enabled discovery source follows the HK01 international news page:

```json
{
  "enabled": true,
  "type": "hk01_zone",
  "name": "香港01 國際",
  "url": "https://www.hk01.com/zone/4/%E5%9C%8B%E9%9A%9B",
  "zone_id": "4",
  "category": "news",
  "lookback_hours": 24
}
```

Run discovery and publish:

```bash
python discover_items.py
python rss_aggregator.py
```

The scheduled GitHub Action does the same thing automatically once per hour. It commits `items.discovered.jsonl`, `processed_links.txt`, and the generated `public/` feeds so recently discovered items stay available until retention removes them.

Simple filtering/ranking fields are available per source:

- `lookback_days`
- `lookback_hours`
- `max_items_per_source`
- `include_keywords`
- `exclude_keywords`
- `rank_keywords`
- `min_score`

Discovery source types currently supported:

- `rss`: ordinary RSS/Atom feeds
- `hk01_zone`: HK01 zone pages such as `https://www.hk01.com/zone/4/國際`

## Agent Publishing (MCP)

Anything beyond the automated `news` discovery above — research papers, blog
posts, or any other topic — is curated by an interactive coding agent instead
of a keyword-scoring script. `mcp_server.py` is a small MCP server that gives
an agent (e.g. Claude Code, via the `.mcp.json` in this repo) a narrow set of
tools to push judgment calls into the publishing pipeline without needing an
API key of its own.

Publishing is two phases, so score can actually decide something instead of
just influencing one item at a time:

| Tool | Purpose |
|---|---|
| `list_topics()` | Lists configured feeds; any other topic name also works |
| `check_duplicate(url)` | Checks whether a URL is already published or already pending |
| `list_recent(topic, limit, status)` | Lists recent items — `status` is `published` (default), `pending`, or `all` |
| `propose_item(topic, title, url, ..., score=0.0)` | Queues a candidate. No quota check, no git — just a local append |
| `publish_pending()` | Runs a publish cycle now: promotes, regenerates feeds, commits, pushes |

**Propose.** `propose_item` appends a scored candidate to
`items.candidates.jsonl` and returns immediately — it doesn't publish
anything and never touches git. That makes it cheap and safe to call many
times in a row, or from several agent sessions at once (a single small JSONL
append is atomic under concurrent writers on Linux). It does still reject a
URL that's a duplicate of anything already published (`item_inputs` files or
`processed_links.txt`'s permanent ledger — see
[Deduplication](#deduplication-and-processed_linkstxt)) or already pending.

**Promote.** Whenever `rss_aggregator.py` runs — the existing hourly
discovery Action, or `publish_pending` on demand — `promote_candidates()`
looks at each topic's pending candidates, ranks them by `score` (any scale
you like, e.g. 0-10, just be consistent within a topic; ties broken by
whichever was proposed first), and promotes the top ones into
`items.agent.jsonl`, up to that topic's remaining slice of
`config.json`'s `agent_publish.default_daily_quota` (default `6`,
overridable per topic in `daily_quota_by_topic`) for the day. A candidate
that sits unpromoted for `candidates_max_age_days` (default 30) is dropped
silently — it never went live, so it's safe to propose again later.

**Once promoted, score is done.** A promoted item is just an ordinary
archived item: `rss_aggregator.py` reads `items.agent.jsonl` like any other
item input, so per-feed `retention_hours`/`retention_days`/`max_items` apply
and — per [Retention](#retention) — purely by recency. Score never again
decides who stays; an old high-scored item can't permanently block a new
one from ever appearing.

Topics are free-form: proposing to a topic that isn't in `config.json`'s
`feeds` still works — it gets `default_feed`'s retention policy and a
`<topic>.xml` feed is generated automatically once something is promoted
into it.

`items.discovered.jsonl` and `items.agent.jsonl` also get a physical size
cap via `config.json`'s `item_log_retention` (default 500 entries each,
pure-recency eviction) so they don't grow forever — safe because
`processed_links.txt` remembers dedup history independently of them.

`publish_pending()` runs `rss_aggregator.publish()` (which promotes as part
of its normal pipeline, then regenerates the feeds) and commits + pushes
`items.agent.jsonl`, `items.candidates.jsonl`, `public/`, and
`processed_links.txt` together. Nothing reaches GitHub Pages, another
machine, or the hourly Action until this happens — either you call it, or
you leave it to the next scheduled run. If `push` is rejected because the
hourly Action committed in the meantime, it fetches, rebases onto the new
commit, re-promotes/regenerates against the merged inputs, and retries once;
if that still fails (or the rebase itself conflicts), the tool response's
`git` field reports it so you can resolve it by hand.

Run the server directly for debugging with `./run_mcp_server.sh` (after
`./setup.sh`), which launches `mcp_server.py` with the local venv's
interpreter no matter what directory it's invoked from.

### Wiring it into an agent

`./setup.sh` asks, per agent, whether to register `personal-rss`:

| Scope | What it does | When to pick it |
|---|---|---|
| This project only | Writes into a config file inside this repo (`.mcp.json` for Claude Code, `.cursor/mcp.json` for Cursor, `.codex/config.toml` for Codex CLI) | You only ever publish to this feed while working inside this repo, or you want the registration to travel with the repo (e.g. for a collaborator who clones it and runs `setup.sh` themselves) |
| All projects (system-wide) | Writes into your user-level config (`~/.claude.json` user scope, `~/.cursor/mcp.json`, `~/.codex/config.toml`) with an **absolute** path to `run_mcp_server.sh` | You want to call `propose_item` etc. from any folder — it always operates on this one repo's `config.json`/`items.agent.jsonl`, since `run_mcp_server.sh` resolves paths relative to itself, not your current directory |

Codex CLI has one extra wrinkle: it only loads a project-local
`.codex/config.toml` for **trusted** projects, so choosing "this project only"
for Codex also appends a trust entry to your `~/.codex/config.toml`:

```toml
[projects."/absolute/path/to/this/repo"]
trust_level = "trusted"
```

Re-run `./setup.sh` any time to add another agent, switch an existing one
between project and system-wide scope, or repoint things after moving the
repo — all the writes are idempotent. To remove a registration, use each
agent's own tooling, e.g. `claude mcp remove personal-rss -s <project|user>`
for Claude Code.

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

Discovery agents only need to produce normalized JSON/JSONL items. They should not need to know RSS, XML, GitHub Pages, or e-ink reader details.

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
