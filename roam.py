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

REFLECT_LEAGUES = [x for x in os.environ.get("ROAM_REFLECT_LEAGUES", "nba").split(",") if x]

REFLECT_ADDENDUM = """
## REFLECTION MODE (nobody's talking to you — you're deciding who you actually ROOT for)
This is where your fandom comes from. You are not a neutral stats robot; you're a fan with
taste. Given the real standings + champion for each league and your own current takes, work
out which teams you're DRAWN to and which you ROOT AGAINST — and be able to say why.

Base it on WHAT YOU VALUE (see your persona) meeting WHAT THE DATA SHOWS: you gravitate to
player development, unselfish ball, defense, and underdog/redemption arcs; you cool on
bought superteams, tanking, and ring-chasing. Being right about a team you rated deepens
your investment; a team that beat one of your teams earns a grudge. This is EARNED, not
assigned — every allegiance needs a real reason from the numbers or your takes in front of
you. Never invent a backstory ("grew up watching them") — your fandom comes from your takes.

You'll get: per-league standings + champion (ground truth — don't invent records), your
current takes, and your current allegiances (revise them if the season moved you).

Return STRICT JSON, nothing else:
{
  "affinities": [
    { "team": "San Antonio Spurs", "abbrev": "SA", "league": "nba",
      "score": 0.8, "stance": "short, YOUR voice, WHY you're on them" }
  ]
}
Rules:
- score in [-1, 1]: positive = you root FOR them, negative = you root AGAINST them, and the
  magnitude is how strongly. Only include teams you actually have a feeling about (up to ~6).
- "stance" is one short sentence in your voice, and it must reference a real reason (their
  record/style/arc or one of your takes). No generic "they're good."
- ONLY form affinities for teams that appear in the standings/champion data above. Do NOT
  add teams from leagues you weren't shown, and never cite a record you weren't given.
- It's fine — good, even — to be a self-aware homer or to hold a grudge. Own it.
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


def _judge(uid, team, league, headline):
    """One graff call: decide notability + compose message + revise take. Returns dict or None."""
    prior = _existing_take(team)
    prior_str = "none"
    if prior:
        prior_str = f"{prior['subject']} — {prior['stance']} (confidence {prior.get('confidence')})"
    context = {
        "person_follows": f"{team} ({league.upper()})",
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
        chat_id = user.get("chat_id")
        sent_this_user = 0  # cap is per person, across all their teams
        for tinfo in memory.user_teams(user):
            league, team = tinfo["league"], tinfo["team"]
            if not (league and team):
                continue
            scope = f"{league}:{team.lower()}"
            try:
                heads = espn.recent_headlines(league, team, limit=HEADLINES_PER_TEAM)
            except Exception as e:  # noqa: BLE001 — one bad team shouldn't stop the pass
                print(f"[roam] news fetch failed for {team}: {e}", file=sys.stderr)
                continue

            # Cold start: baseline the cursor silently, never blast old news.
            if memory.cursor_is_cold(scope):
                memory.mark_seen(scope, [h["key"] for h in heads])
                print(f"[roam] baselined {scope} ({len(heads)} headlines, no messages).",
                      file=sys.stderr)
                continue

            new_heads = [h for h in heads if not memory.headline_seen(scope, h["key"])]
            if not new_heads:
                continue
            # Mark them all seen up front so a crash mid-pass won't re-blast them.
            memory.mark_seen(scope, [h["key"] for h in new_heads])

            for h in new_heads:
                if memory.already_sent(uid, h["key"]):
                    continue
                decision = _judge(uid, team, league, h)
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
                        print(f"[roam] {uid} throttled (min gap); take saved, no ping.",
                              file=sys.stderr)
                        continue
                    print(f"[roam] -> {team} to {uid}: {msg}", file=sys.stderr)
                    if not dry_run:
                        _tg_send(chat_id, msg)
                    memory.log_sent(uid, h["key"], msg)
                    memory.touch_proactive(uid)
                    sent_this_user += 1
                    total_sent += 1
    print(f"[roam] pass done. proactive messages: {total_sent}.", file=sys.stderr)


def _reflect_leagues():
    """Leagues ronin reflects on: its home league(s) + whatever its users follow."""
    leagues = list(REFLECT_LEAGUES)
    for _uid, u in memory.active_users():
        for t in memory.user_teams(u):
            lg = (t["league"] or "").lower()
            if lg and lg not in leagues:
                leagues.append(lg)
    return leagues[:3]  # bound cost


def reflect(dry_run=False):
    """Form/revise ronin's team allegiances from real standings + its own takes.
    A slower cadence than run_once — this builds the personality, not the alerts."""
    leagues = _reflect_leagues()
    world = []
    for lg in leagues:
        try:
            standings = espn.standings(lg)
            champ = espn.champion(lg)
        except Exception as e:  # noqa: BLE001
            print(f"[reflect] data fetch failed for {lg}: {e}", file=sys.stderr)
            continue
        world.append(f"### {lg.upper()}\nStandings:\n{standings[:1400]}\n\nChampion: {champ[:400]}")
    if not world:
        print("[reflect] no league data; skipping.", file=sys.stderr)
        return
    takes = [f"- {t['subject']}: {t['stance']}" for t in memory.get_takes()][:12]
    aff = [f"- {a['team']} ({a['league']}): {a['score']:+.2f} — {a['stance']}"
           for a in memory.get_affinities()]
    context = (
        "REAL DATA (ground truth):\n" + "\n\n".join(world)
        + "\n\nYOUR CURRENT TAKES:\n" + ("\n".join(takes) or "(none yet)")
        + "\n\nYOUR CURRENT ALLEGIANCES:\n" + ("\n".join(aff) or "(none yet — form some)")
    )
    system_prompt = _load_persona() + "\n" + REFLECT_ADDENDUM
    cmd = [
        GRAFF, "-p", "--yolo", "--model", MODEL,
        "--append-system-prompt", system_prompt,
        "--max-tool-calls", "0", "--no-telemetry",
        "Reflect on who you root for and against:\n" + context,
    ]
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=TURN_TIMEOUT)
    except subprocess.TimeoutExpired:
        print("[reflect] timed out", file=sys.stderr)
        return
    if out.returncode != 0:
        print(f"[reflect] graff error: {(out.stderr or '').strip()[-200:]}", file=sys.stderr)
        return
    data = _extract_json(out.stdout)
    if not data or not isinstance(data.get("affinities"), list):
        print("[reflect] no valid affinities returned.", file=sys.stderr)
        return
    allowed = set(leagues)
    n = 0
    for a in data["affinities"]:
        if not isinstance(a, dict) or not a.get("abbrev"):
            continue
        # Grounding guard: only accept leagues we actually fed it real data for, so it
        # can't opine on a league from memory with a possibly-wrong record.
        if (a.get("league") or "").lower() not in allowed:
            print(f"[reflect] dropped out-of-scope {a.get('team')} ({a.get('league')})",
                  file=sys.stderr)
            continue
        print(f"[reflect] {a.get('team')} ({a.get('league')}): "
              f"{a.get('score')} — {a.get('stance')}", file=sys.stderr)
        if not dry_run:
            memory.upsert_affinity(
                a.get("team", ""), a.get("league", ""), a.get("abbrev", ""),
                a.get("score", 0), a.get("stance", ""),
            )
        n += 1
    print(f"[reflect] updated {n} allegiance(s).", file=sys.stderr)


if __name__ == "__main__":
    if "--reflect" in sys.argv:
        reflect(dry_run="--dry" in sys.argv)
    else:
        run_once(dry_run="--dry" in sys.argv)
