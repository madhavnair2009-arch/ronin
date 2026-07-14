#!/usr/bin/env python3
"""
ronin — transport-independent core.

Given a sender id + an incoming message, produce ronin's reply by driving graff
with the thin-slice persona + seed takes + the espn-nba MCP tools. Per-sender
graff sessions give lightweight conversation memory (a nod at the "it knows you"
lock-in from the design doc).

Any transport (Telegram, Signal, SMS, a web box) just calls reply().

CLI (for local testing, no transport):
    python3 ronin_reply.py <sender_id> "your message"
"""

import os
import re
import subprocess
import sys

import memory

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    # So graff (and the MCP servers it spawns) inherit REDDIT_*/etc. when run locally.
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()
GRAFF = os.path.expanduser("~/bin/graff")
MODEL = os.environ.get("RONIN_MODEL", "claude-opus-4-8")
MAX_TOOL_CALLS = os.environ.get("RONIN_MAX_TOOL_CALLS", "8")  # cost/loop guard
TURN_TIMEOUT = int(os.environ.get("RONIN_TURN_TIMEOUT", "120"))


def _load_system_prompt(sender_id=None):
    with open(os.path.join(ROOT, "persona.md"), encoding="utf-8") as f:
        persona = f.read()
    # Its beliefs: the LIVING takes the roam loop revises (not the frozen seed file).
    takes = memory.get_takes()
    if takes:
        lines = ["\n## Your standing takes right now (you formed/revised these — own them)"]
        for t in takes:
            lines.append(f"- **{t['subject']}** (conf {t.get('confidence', '?')}): {t['stance']}")
        persona += "\n" + "\n".join(lines) + "\n"
    # You: relationship memory so ronin talks like it knows this person.
    if sender_id is not None:
        u = memory.get_user(sender_id)
        if u and u.get("team"):
            persona += (f"\n## Who you're talking to\nThis person follows the "
                        f"**{u['team']}** ({u.get('league', '').upper()}). Talk like you know "
                        f"that — reference their team, rib them about it, remember it's theirs.\n")
    return persona


def _session_name(sender_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(sender_id))[:64] or "anon"
    return f"sess_{safe}"


def reply(sender_id, message):
    """Return ronin's reply string for one incoming message."""
    system_prompt = _load_system_prompt(sender_id)
    cmd = [
        GRAFF, "-p", "--yolo",
        "--model", MODEL,
        "--resume", _session_name(sender_id),
        "--append-system-prompt", system_prompt,
        "--max-tool-calls", str(MAX_TOOL_CALLS),
        "--no-telemetry",
        message,
    ]
    try:
        out = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True, timeout=TURN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return "took too long on that one — hit me again in a sec."
    if out.returncode != 0:
        err = (out.stderr or "").strip().splitlines()
        detail = err[-1] if err else f"exit {out.returncode}"
        return f"(ronin hiccup: {detail})"
    return out.stdout.strip() or "…got nothing back, try again?"


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: ronin_reply.py <sender_id> \"message\"", file=sys.stderr)
        sys.exit(2)
    print(reply(sys.argv[1], " ".join(sys.argv[2:])))
