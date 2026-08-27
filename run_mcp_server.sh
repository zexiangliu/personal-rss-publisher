#!/usr/bin/env bash
# Launches mcp_server.py with the local venv's interpreter, regardless of the
# working directory the calling MCP client (Claude Code / Cursor / Codex CLI)
# invokes this script from.
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python3 mcp_server.py
