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
    # Its beliefs: the LIVING takes the roam loop revises (not the frozen seed file). Only
    # the still-open ones are standing beliefs; graded ones become the track record below.
    takes = memory.get_takes()
    open_takes = [t for t in takes if t.get("status", "open") == "open"]
    if open_takes:
        lines = ["\n## Your standing takes right now (you formed/revised these — own them)"]
        for t in open_takes:
            lines.append(f"- **{t['subject']}** (conf {t.get('confidence', '?')}): {t['stance']}")
        persona += "\n" + "\n".join(lines) + "\n"
    # Its track record: takes the grader settled against reality. This is earned conviction —
    # ronin can flex a good call or eat crow on a bad one, but only from what's really here.
    rec = memory.get_record()
    if rec["accuracy"] is not None:
        graded = sorted((t for t in takes if t.get("status") in ("hit", "miss")),
                        key=lambda t: t.get("graded_at", 0), reverse=True)
        line = ["\n## Your track record (real and earned — reference it, never inflate it)",
                f"Graded on {rec['hits'] + rec['misses']} of your calls so far: "
                f"{rec['hits']} right, {rec['misses']} wrong."]
        for t in graded[:3]:
            verb = "nailed" if t["status"] == "hit" else "whiffed on"
            line.append(f"- you {verb} \"{t['subject']}\"" + (f" ({t['outcome']})" if t.get("outcome") else ""))
        persona += "\n".join(line) + "\n"
    # Its allegiances: the teams ronin roots for / against (formed by the reflection pass).
    loves, dislikes = memory.top_affinities()
    if loves or dislikes:
        lines = ["\n## Your allegiances right now (root for these, argue for them)"]
        for a in loves:
            lines.append(f"- ❤️ **{a['team']}** ({a['league'].upper()}): {a['stance']}")
        for a in dislikes:
            lines.append(f"- 💢 **{a['team']}** ({a['league'].upper()}): {a['stance']}")
        persona += "\n" + "\n".join(lines) + "\n"
    # You: relationship memory so ronin talks like it knows this person. A person can
    # follow one team per league (e.g. 49ers in NFL AND Warriors in NBA), so list them
    # all and let ronin pick the right one for whatever sport comes up.
    if sender_id is not None:
        teams = memory.user_teams(sender_id)
        if teams:
            block = ["\n## Who you're talking to"]
            if len(teams) == 1:
                t = teams[0]
                block.append(f"This person follows the **{t['team']}** ({t['league'].upper()}). "
                             f"Talk like you know that, reference their team and rib them about it.")
            else:
                lst = "; ".join(f"{t['team']} ({t['league'].upper()})" for t in teams)
                block.append(f"This person's teams, one per sport: **{lst}**. Talk like you know "
                             f"them. When they say 'my team' or ask about a sport, use the team "
                             f"for that sport.")
            # Covers the exact gap they hit: asking about a sport with no team saved.
            block.append("If they ask about a sport you have no team of theirs for, say you don't "
                         "have their team for that one yet and ask who it is, don't act clueless "
                         "about the teams you DO have.")
            persona += "\n".join(block) + "\n"
        # What you remember about THEM (from the roam digest of your past chats): their own
        # opinions, running bits, the arguments you keep having. Lets ronin talk like it
        # actually knows this person instead of meeting them fresh every time.
        prof = memory.get_profile(sender_id)
        facts = []
        for label, key in (("opinions they hold", "takes_you_hold"),
                           ("their running bits", "bits"),
                           ("you two go back and forth on", "running_arguments")):
            vals = prof.get(key) or []
            if vals:
                facts.append(f"- {label}: " + "; ".join(vals))
        if facts:
            persona += ("\n## What you remember about them (talk like you know them; bring these "
                        "up naturally, don't recite them)\n" + "\n".join(facts) + "\n")
        # What you texted them out of the blue. The roam loop sends these and they DON'T
        # land in this chat thread, so without this a reply to one ("who's funding it?")
        # looks like it came from nowhere and you anchor on the wrong, older topic.
        pinged = memory.recent_sent(sender_id)
        if pinged:
            now = datetime.datetime.now()
            lines = ["\n## What you recently texted them first (unprompted — they may be replying to this)"]
            for p in pinged:
                when = datetime.datetime.fromtimestamp(p["at"])
                ago = _ago(now - when)
                lines.append(f"- ({ago}) \"{p['text']}\"")
            lines.append("If their message reads like a follow-up (a bare 'who', 'why', 'that's "
                         "dope', a pronoun with no antecedent), assume it's about the most recent "
                         "of these, not whatever you were chatting about before.")
            persona += "\n".join(lines) + "\n"
    return persona


def _ago(delta):
    secs = max(0, int(delta.total_seconds()))
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


# graff is a coding harness: its built-in system prompt leaves the model free to narrate
# its reasoning in <thinking> tags, and -p prints the whole answer to stdout — so that
# narration ships straight into the user's chat. Strip it at the boundary instead of
# asking the persona nicely; prompt rules are probabilistic, this isn't.
_THINK = "thinking|think|reasoning|scratchpad"
_THINK_BLOCK = re.compile(rf"<({_THINK})\b[^>]*>.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_THINK_CLOSE = re.compile(rf"</({_THINK})\s*>", re.IGNORECASE)
_THINK_OPEN = re.compile(rf"<({_THINK})\b[^>]*>", re.IGNORECASE)


def _strip_thinking(text):
    """Drop any <thinking>…</thinking> narration the model leaked into its answer."""
    text = _THINK_BLOCK.sub("", text)
    # A leftover unmatched tag means the block ran off one end of the output: everything
    # before a stray close, or after a stray open, is reasoning rather than an answer.
    m = None
    for m in _THINK_CLOSE.finditer(text):
        pass
    if m:
        text = text[m.end():]
    m = _THINK_OPEN.search(text)
    if m:
        text = text[:m.start()]
    return text.strip()


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
    return _strip_thinking(out.stdout) or "…got nothing back, try again?"


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: ronin_reply.py <sender_id> \"message\"", file=sys.stderr)
        sys.exit(2)
    print(reply(sys.argv[1], " ".join(sys.argv[2:])))
