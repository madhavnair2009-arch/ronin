#!/usr/bin/env python3
"""
ronin — the roam loop (the autonomous half that BUILDS the mind).

One pass (`run_once`) does, per user who's told ronin their team:
  1. WORLD-FACTS pre-filter (cheap, no model): pull the team's news, diff against the
     cursor. Nothing new -> we spend nothing. This is the gate that keeps an always-on
     loop from costing a fortune (design doc: "you pay for silence" is the enemy).
  2. For each genuinely-new headline: ONE expensive graff call in ronin's voice that
     decides three things at once — is this worth interrupting the user? does it move one
     of my takes? and if so, what do I text them? (Returns strict JSON.)
  3. BELIEF revision: upsert the take (with history) whether or not we message.
  4. Anti-annoyance: dedup against the outbound log, cap messages/run, throttle per user.
  5. Push it to Telegram directly (roam is self-contained; doesn't need the bot running).

Cold start is handled: the first time we ever see a team's news we baseline the cursor
WITHOUT messaging, so a new user doesn't get blasted with 8 old headlines.

Run:
  python3 roam.py            one pass, real (may send Telegram messages)
  python3 roam.py --dry      one pass, judge + revise takes but DON'T send
"""

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

import memory
from mcp import espn

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
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
MODEL = os.environ.get("RONIN_ROAM_MODEL", "claude-opus-4-8")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
MAX_PER_USER = int(os.environ.get("ROAM_MAX_PER_USER", "1"))     # msgs per user per pass
PROACTIVE_MIN_GAP = int(os.environ.get("ROAM_MIN_GAP", "21600"))  # 6h between pings/user
HEADLINES_PER_TEAM = int(os.environ.get("ROAM_HEADLINES", "8"))
TURN_TIMEOUT = int(os.environ.get("ROAM_TURN_TIMEOUT", "90"))

ROAM_ADDENDUM = """
## ROAM MODE (you are NOT replying to anyone right now)
You're on your own, scanning fresh news about a team a specific person follows. You are
deciding, unprompted, whether something is worth texting THEM about — and updating your
own beliefs as you go.

You will be given: the person's team, ONE new news item (headline + blurb — this is your
ground-truth fact, don't invent stats beyond it), your current take on this storyline (or
"none"), and things you've recently told them (don't repeat).

Return STRICT JSON, nothing else, in this exact shape:
{
  "notable": true | false,
  "message": "the text you'd send them, in your voice — or \"\" if not notable",
  "take": { "subject": "...", "stance": "...", "confidence": 0.0-1.0, "reasoning": "..." } | null
}

Rules:
- "notable" is TRUE only for things a fan actually wants a text about: a real trade or
  signing (reported, not just rumor-grades), a big injury, a notable win/loss or milestone.
  Routine content, opinion columns, listicles, "power rankings" -> notable: false.
- If notable, "message" is a SHORT text (1-2 sentences), your voice: dry, a little cocky,
  human, lowercase-friendly. React with YOUR read — you are NOT a hive-mind mirror. No
  "Hey!", no "Just wanted to let you know", no emoji spam. Text a friend, not a push
  notification. Reference the actual news; don't state scores/records you weren't given.
- "take": if this news forms or MOVES a belief, return the updated take (revise your prior
  stance if you were given one — it's fine to say your confidence shifted). If it doesn't
  touch a belief, return null. Keep "subject" stable across updates to the same storyline.
- Output ONLY the JSON object. No preamble, no code fence.
"""


def _load_persona():
    with open(os.path.join(ROOT, "persona.md"), encoding="utf-8") as f:
        return f.read()


def _existing_take(subject_team):
    """Best-effort: find a current take whose subject mentions the team."""
    key = subject_team.lower()
    for t in memory.get_takes():
        if key in t.get("subject", "").lower():
            return t
    return None


def _recent_texts(uid, n=5):
    log = memory._read("outbound.json", {}).get("sent", [])
    mine = [s["text"] for s in log if s.get("uid") == str(uid)]
    return mine[-n:]


def _extract_json(text):
    """Pull the first balanced {...} object out of graff's stdout and parse it."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _judge(uid, user, headline):
    """One graff call: decide notability + compose message + revise take. Returns dict or None."""
    team = user["team"]
    prior = _existing_take(team)
    prior_str = "none"
    if prior:
        prior_str = f"{prior['subject']} — {prior['stance']} (confidence {prior.get('confidence')})"
    context = {
        "person_follows": f"{team} ({user.get('league', '').upper()})",
        "new_news_item": f"{headline['headline']} — {headline['desc']}".strip(" —"),
        "your_current_take_on_this_storyline": prior_str,
        "things_you_recently_told_them": _recent_texts(uid) or ["(nothing yet)"],
    }
    system_prompt = _load_persona() + "\n" + ROAM_ADDENDUM
    cmd = [
        GRAFF, "-p", "--yolo",
        "--model", MODEL,
        "--append-system-prompt", system_prompt,
        "--max-tool-calls", "0",  # pure judgment; the fact is already in the prompt
        "--no-telemetry",
        "Here is the news item to assess:\n" + json.dumps(context, indent=2),
    ]
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=TURN_TIMEOUT)
    except subprocess.TimeoutExpired:
        print("[roam] judge timed out", file=sys.stderr)
        return None
    if out.returncode != 0:
        print(f"[roam] graff error: {(out.stderr or '').strip()[-200:]}", file=sys.stderr)
        return None
    return _extract_json(out.stdout)


def _tg_send(chat_id, text):
    if not TOKEN:
        print("[roam] no TELEGRAM_BOT_TOKEN; would have sent:", text, file=sys.stderr)
        return
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data)
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # noqa: BLE001
        print(f"[roam] telegram send failed: {e}", file=sys.stderr)


def run_once(dry_run=False):
    users = memory.active_users()
    if not users:
        print("[roam] no active users (nobody has set a team yet).", file=sys.stderr)
        return
    total_sent = 0
    for uid, user in users:
        league, team = user["league"], user["team"]
        scope = f"{league}:{team.lower()}"
        try:
            heads = espn.recent_headlines(league, team, limit=HEADLINES_PER_TEAM)
        except Exception as e:  # noqa: BLE001 — one bad team shouldn't stop the pass
            print(f"[roam] news fetch failed for {team}: {e}", file=sys.stderr)
            continue

        # Cold start: baseline the cursor silently, never blast old news.
        if memory.cursor_is_cold(scope):
            memory.mark_seen(scope, [h["key"] for h in heads])
            print(f"[roam] baselined {scope} ({len(heads)} headlines, no messages).", file=sys.stderr)
            continue

        new_heads = [h for h in heads if not memory.headline_seen(scope, h["key"])]
        if not new_heads:
            continue
        # Mark them all seen up front so a crash mid-pass won't re-blast them.
        memory.mark_seen(scope, [h["key"] for h in new_heads])

        sent_this_user = 0
        for h in new_heads:
            if memory.already_sent(uid, h["key"]):
                continue
            decision = _judge(uid, user, h)
            if not decision:
                continue
            take = decision.get("take")
            if isinstance(take, dict) and take.get("subject"):
                memory.upsert_take(
                    take["subject"], take.get("stance", ""), take.get("confidence", 0.5),
                    take.get("reasoning", ""), evidence=[h["key"]],
                )
            msg = (decision.get("message") or "").strip()
            if decision.get("notable") and msg and sent_this_user < MAX_PER_USER:
                if not memory.proactive_allowed(uid, PROACTIVE_MIN_GAP):
                    print(f"[roam] {uid} throttled (min gap); take saved, no ping.", file=sys.stderr)
                    continue
                print(f"[roam] -> {team} to {uid}: {msg}", file=sys.stderr)
                if not dry_run:
                    _tg_send(user["chat_id"], msg)
                memory.log_sent(uid, h["key"], msg)
                memory.touch_proactive(uid)
                sent_this_user += 1
                total_sent += 1
    print(f"[roam] pass done. proactive messages: {total_sent}.", file=sys.stderr)


if __name__ == "__main__":
    run_once(dry_run="--dry" in sys.argv)
