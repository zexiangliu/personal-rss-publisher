#!/usr/bin/env bash
# Create/refresh the local virtualenv used to run this project, including
# the mcp_server.py MCP server. Needed because system Python on most distros
# is "externally managed" and refuses a plain `pip install mcp`.
#
# Then, interactively, register the personal-rss MCP server with whichever
# coding agents (Claude Code / Cursor / Codex CLI) are detected on this
# machine, at whichever scope (this project only, or all projects) the user
# picks for each.
set -euo pipefail
cd "$(dirname "$0")"
REPO_DIR="$(pwd)"
RUN_SCRIPT="$REPO_DIR/run_mcp_server.sh"

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo
echo "Done. .venv/bin/python3 has all dependencies, including mcp."
echo "  - Run scripts directly:   .venv/bin/python3 rss_aggregator.py"
echo "  - Or activate the venv:   source .venv/bin/activate"

if [ ! -t 0 ]; then
  echo
  echo "Non-interactive shell detected; skipping MCP registration prompts."
  echo "See README.md's 'Wiring it into an agent' section to register manually."
  exit 0
fi

# Idempotently ensure {"mcpServers": {"personal-rss": {"command": ...}}} in a
# Cursor-style JSON file at $1, creating the file/parent dir if needed.
write_cursor_json() {
  local target="$1"
  mkdir -p "$(dirname "$target")"
  python3 - "$target" "$RUN_SCRIPT" <<'PYEOF'
import json
import sys
from pathlib import Path

target, run_script = Path(sys.argv[1]), sys.argv[2]
data = {}
if target.exists():
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}
data.setdefault("mcpServers", {})["personal-rss"] = {"command": run_script}
target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"  -> wrote {target}")
PYEOF
}

# Idempotently append a `[mcp_servers.personal-rss]` table to a Codex-style
# TOML file at $1, if it isn't already there.
append_codex_mcp_block() {
  local target="$1"
  mkdir -p "$(dirname "$target")"
  touch "$target"
  if grep -qF '[mcp_servers.personal-rss]' "$target"; then
    echo "  -> $target already has a personal-rss entry, leaving it alone"
  else
    {
      echo ""
      echo "[mcp_servers.personal-rss]"
      echo "command = \"$RUN_SCRIPT\""
    } >>"$target"
    echo "  -> appended personal-rss to $target"
  fi
}

# Idempotently append a `[projects."$REPO_DIR"]` trust entry to $1, if the
# repo isn't already trusted there. Codex only loads a project-local
# .codex/config.toml for trusted projects.
append_codex_trust_entry() {
  local target="$1"
  mkdir -p "$(dirname "$target")"
  touch "$target"
  if grep -qF "[projects.\"$REPO_DIR\"]" "$target"; then
    echo "  -> $target already trusts this project, leaving it alone"
  else
    {
      echo ""
      echo "[projects.\"$REPO_DIR\"]"
      echo "trust_level = \"trusted\""
    } >>"$target"
    echo "  -> trusted $REPO_DIR in $target"
  fi
}

ask_scope() {
  local agent_name="$1" project_path="$2" system_path="$3"
  echo
  echo "$agent_name -- where should personal-rss be available?"
  local PS3="Choose [1-3]: "
  local choice
  select choice in \
    "This project only   -> $project_path" \
    "All projects (system-wide) -> $system_path" \
    "Skip $agent_name"; do
    case "$REPLY" in
      1) SCOPE_CHOICE="project"; break ;;
      2) SCOPE_CHOICE="system"; break ;;
      3) SCOPE_CHOICE="skip"; break ;;
      *) echo "Please choose 1, 2, or 3." ;;
    esac
  done
}

register_claude() {
  ask_scope "Claude Code" "$REPO_DIR/.mcp.json" "~/.claude.json (user scope)"
  case "$SCOPE_CHOICE" in
    project)
      claude mcp remove personal-rss -s project >/dev/null 2>&1 || true
      claude mcp add personal-rss --scope project -- "$RUN_SCRIPT"
      ;;
    system)
      claude mcp remove personal-rss -s user >/dev/null 2>&1 || true
      claude mcp add personal-rss --scope user -- "$RUN_SCRIPT"
      ;;
    skip) echo "Skipped Claude Code." ;;
  esac
}

register_cursor() {
  ask_scope "Cursor" "$REPO_DIR/.cursor/mcp.json" "~/.cursor/mcp.json"
  case "$SCOPE_CHOICE" in
    project) write_cursor_json "$REPO_DIR/.cursor/mcp.json" ;;
    system) write_cursor_json "$HOME/.cursor/mcp.json" ;;
    skip) echo "Skipped Cursor." ;;
  esac
}

register_codex() {
  ask_scope "Codex CLI" "$REPO_DIR/.codex/config.toml (+ trust entry)" "~/.codex/config.toml"
  case "$SCOPE_CHOICE" in
    project)
      append_codex_mcp_block "$REPO_DIR/.codex/config.toml"
      append_codex_trust_entry "$HOME/.codex/config.toml"
      ;;
    system)
      append_codex_mcp_block "$HOME/.codex/config.toml"
      ;;
    skip) echo "Skipped Codex CLI." ;;
  esac
}

echo
echo "Now let's register the personal-rss MCP server with your coding agent(s)."

if command -v claude >/dev/null 2>&1; then
  register_claude
fi

if command -v cursor >/dev/null 2>&1 || [ -d "$HOME/.cursor" ]; then
  register_cursor
fi

if command -v codex >/dev/null 2>&1 || [ -d "$HOME/.codex" ]; then
  register_codex
fi

echo
echo "Setup complete."
