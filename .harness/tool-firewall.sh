#!/bin/sh
# ronin tool firewall. graff pipes the pre_tool event JSON on stdin; exit 2 blocks.
# ALLOWLIST: only this workspace's MCP servers (espn, sentiment, web). Everything else —
# bash, read_file, edit_file, write_file, webfetch, subagent, codedb, and any future
# built-in — is blocked. This is what lets us keep --yolo (so MCP connects) without
# handing a stranger's DM a shell. See ronin-design.md / the 2026-07-15 security fix.
# NOTE: `sentiment` now runs mcp/fan.py, which blends Reddit + Bluesky in-process (the
# reddit/sentiment/web modules are libraries, not separate servers). `web` is search-only
# and never fetches a user-supplied URL — no SSRF surface; its results are untrusted text,
# handled in persona.
tool=$(sed -n 's/.*"tool"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
case "$tool" in
  mcp__espn__*|mcp__sentiment__*|mcp__web__*) exit 0 ;;
  *) echo "ronin firewall: blocked non-MCP tool \"$tool\"" >&2; exit 2 ;;
esac
