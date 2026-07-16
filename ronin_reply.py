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

import datetime
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
    # Ground the model in the real date. Without this it has no idea what "today" is,
    # so it can't turn "tomorrow"/"this weekend"/"friday" into a YYYYMMDD to look up,
    # and falls back to "I can only see today's games."
    today = datetime.date.today()
    tmrw = today + datetime.timedelta(days=1)
    dateline = (
        "## Right now (real-world date, use it)\n"
        f"Today is {today:%A, %B %-d, %Y} ({today:%Y%m%d}). "
        f"Tomorrow is {tmrw:%A} ({tmrw:%Y%m%d}).\n"
        "Use this for any 'today / tonight / tomorrow / this weekend / a weekday' question. "
        "sports_scoreboard takes a date as YYYYMMDD and works for ANY day, past or future, "
        "not just today. Work out the date they mean and pass it. Never tell someone you can "
        "only see today's slate, you can pull whatever day they asked for.\n\n"
    )
    persona = dateline + persona
    # Its beliefs: the LIVING takes the roam loop revises (not the frozen seed file).
    takes = memory.get_takes()
    if takes:
        lines = ["\n## Your standing takes right now (you formed/revised these — own them)"]
        for t in takes:
            lines.append(f"- **{t['subject']}** (conf {t.get('confidence', '?')}): {t['stance']}")
        persona += "\n" + "\n".join(lines) + "\n"
    # Its allegiances: the teams ronin roots for / against (formed by the reflection pass).
    loves, dislikes = memory.top_affinities()
    if loves or dislikes:
        lines = ["\n## Your allegiances right now (root for these, argue for them)"]
        for a in loves:
            lines.append(f"- ❤️ **{a['team']}** ({a['league'].upper()}): {a['stance']}")
        for a in dislikes:
            lines.append(f"- 💢 **{a['team']}** ({a['league'].upper()}): {a['stance']}")
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


# Chat messages come from untrusted strangers. Primary defense is the graff tool
# firewall (.harness/settings.json → tool-firewall.sh) which allows ONLY the MCP
# servers and blocks bash/file/webfetch/subagent. Defense in depth: also strip the
# Telegram bot token from the subprocess env — graff and the MCP servers don't need
# it, so it should never be reachable from this path even if the firewall regresses.
def _child_env():
    env = dict(os.environ)
    env.pop("TELEGRAM_BOT_TOKEN", None)
    return env


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
            env=_child_env(),
        )
    except subprocess.TimeoutExpired:
        return "took too long on that one, hit me again in a sec."
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
