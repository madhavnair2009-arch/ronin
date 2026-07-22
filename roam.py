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

import datetime
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

import memory
from mcp import espn
from mcp import fan

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
  "take": {
    "topic": "short-kebab-slug",
    "subject": "...", "stance": "...", "confidence": 0.0-1.0, "reasoning": "...",
    "resolves_when": "the real outcome that will later prove this right or wrong",
    "deadline": "YYYYMMDD"
  } | null
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
  touch a belief, return null.
- "topic" is the STABLE identity of the storyline — same slug every time you revisit it
  (e.g. "curry-legacy", "okc-title-odds"). If this news is about a storyline you already
  have a take on (see the topics you'll be given), REUSE that exact topic so you revise it
  instead of starting a duplicate. Only mint a new slug for a genuinely new storyline.
- "resolves_when" + "deadline": a take is a prediction you can be graded on. Say plainly
  what future result would settle it (e.g. "OKC eliminated before the conference finals, or
  wins the title") and the YYYYMMDD by which you'd expect to know. The deadline is a REAL
  future date (see today's date above) — for an outcome that lands at the end of a season or
  tournament, use that event's actual date in the RIGHT year, never one already past. If it's
  a timeless opinion with no checkable outcome, use "" and null — don't force one.
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


def _dateline():
    """Ground the roam passes in the real date. Without it the judge has no idea what year
    it is and sets deadlines by guessing — it put a 'next NBA season' take's deadline in the
    PAST (June 2026 instead of 2027), which would churn the grader forever."""
    today = datetime.date.today()
    return (
        "## Right now (ground every date in this)\n"
        f"Today is {today:%A, %B %-d, %Y} ({today:%Y%m%d}).\n"
        "Any deadline you set MUST come AFTER today and use the correct year. Picture when the "
        "outcome actually resolves: a take about next season is settled at the END of next "
        f"season, which is a date well past {today:%Y%m%d} — never one that has already gone by.\n\n"
    )


def _existing_take(subject_team):
    """Best-effort: find a current take whose subject mentions the team."""
    key = subject_team.lower()
    for t in memory.get_takes():
        if key in t.get("subject", "").lower():
            return t
    return None


def _recent_texts(uid, n=5):
    return [p["text"] for p in memory.recent_sent(uid, n=n)]


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
    existing = [f"{t.get('topic') or memory._slug(t['subject'])} — {t['subject']}"
                for t in memory.get_takes()]
    context = {
        "person_follows": f"{team} ({league.upper()})",
        "new_news_item": f"{headline['headline']} — {headline['desc']}".strip(" —"),
        "your_current_take_on_this_storyline": prior_str,
        "your_existing_take_topics_reuse_the_slug_if_it_fits": existing or ["(none yet)"],
        "things_you_recently_told_them": _recent_texts(uid) or ["(nothing yet)"],
    }
    system_prompt = _dateline() + _load_persona() + "\n" + ROAM_ADDENDUM
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
            # Per USER, not per team. This cursor is read and written inside the per-user
            # loop, so a team-only key let the first user's pass mark the headlines seen
            # and the second follower of that team never heard them (their cursor also
            # looked warm, so they never even baselined). Same silent-news-loss family as
            # the judge-timeout bug.
            scope = f"{uid}:{league}:{team.lower()}"
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

            for h in new_heads:
                if memory.already_sent(uid, h["key"]):
                    memory.mark_seen(scope, [h["key"]])
                    continue
                decision = _judge(uid, team, league, h)
                if not decision:
                    # Judge timed out or emitted garbage. Leave the headline UNSEEN so the
                    # next pass retries it — marking it here would drop that news forever.
                    # Re-blasting isn't a risk: already_sent is the guard against that.
                    continue
                # Judged (notable or not), so we're done with it: don't pay to re-judge.
                memory.mark_seen(scope, [h["key"]])
                take = decision.get("take")
                if isinstance(take, dict) and take.get("subject"):
                    dl = take.get("deadline")
                    try:
                        dl = int(dl) if dl else None
                    except (TypeError, ValueError):
                        dl = None
                    # A fresh take can't have already-resolved: a past deadline means the
                    # model slipped the year (it did — "next season" -> a June already gone).
                    # Drop it rather than create a take that churns the grader forever;
                    # better ungraded than graded on a season that hasn't happened.
                    if dl is not None and dl <= memory._today_int():
                        print(f"[roam] dropped past deadline {dl} on '{take.get('subject')}'",
                              file=sys.stderr)
                        dl = None
                    memory.upsert_take(
                        take["subject"], take.get("stance", ""), take.get("confidence", 0.5),
                        take.get("reasoning", ""), evidence=[h["key"]],
                        topic=take.get("topic"), resolves_when=take.get("resolves_when"),
                        deadline=dl,
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
            recent = ""
            # A cup's group tables go stale the moment the knockouts start, so feed the
            # actual recent + upcoming results (who won, who's out, who's in the final) —
            # otherwise allegiances form on group-stage form and miss a semifinal upset.
            if lg in getattr(espn, "SOCCER_CUPS", set()):
                today = datetime.date.today()
                lo = (today - datetime.timedelta(days=12)).strftime("%Y%m%d")
                hi = (today + datetime.timedelta(days=5)).strftime("%Y%m%d")
                recent = espn.scoreboard(lg, f"{lo}-{hi}")
        except Exception as e:  # noqa: BLE001
            print(f"[reflect] data fetch failed for {lg}: {e}", file=sys.stderr)
            continue
        block = f"### {lg.upper()}\n"
        if recent:
            block += f"Recent + upcoming results (ground truth, weigh these most):\n{recent[:1200]}\n\n"
        block += f"Standings:\n{standings[:1000]}\n\nChampion: {champ[:400]}"
        world.append(block)
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
    system_prompt = _dateline() + _load_persona() + "\n" + REFLECT_ADDENDUM
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
    reaffirmed = set()
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
        reaffirmed.add(f"{a.get('league','').lower()}:{a.get('abbrev','').upper()}")
        if not dry_run:
            memory.upsert_affinity(
                a.get("team", ""), a.get("league", ""), a.get("abbrev", ""),
                a.get("score", 0), a.get("stance", ""),
            )
        n += 1
    # Fade allegiances this pass didn't reaffirm, but only in the leagues we actually
    # looked at. This is what finally retires a stale allegiance (the World Cup grudge that
    # outlived the team's elimination) instead of leaving it to editorialize forever.
    dropped = memory.decay_affinities(leagues, reaffirmed) if not dry_run else []
    if dropped:
        print(f"[reflect] retired stale allegiance(s): {', '.join(dropped)}", file=sys.stderr)
    print(f"[reflect] updated {n} allegiance(s), retired {len(dropped)}.", file=sys.stderr)


# ---------------------------------------------------------------------------
# GRADE: settle old takes against reality so being right earns conviction.
# ---------------------------------------------------------------------------
GRADE_ADDENDUM = """
## GRADE MODE (you are checking whether an old prediction of yours came true)
You made a call a while ago. Now you find out if you were right. Use the sports tools to
look up the REAL current standings / champion / results — never guess the outcome.

You'll get: your old take, and what you said would settle it (resolves_when). Check reality
with your tools, then judge honestly. Being wrong is fine and useful; don't grade yourself
generously.

Return STRICT JSON, nothing else:
{
  "resolved": true | false,
  "verdict": "hit" | "miss",
  "note": "one short line, in your voice, on what actually happened"
}
- "resolved": true only if reality now clearly settles the take one way or the other. If the
  season/event hasn't reached the point that would decide it yet, return false (verdict is
  then ignored) and you'll check again later.
- "verdict": "hit" if you were right, "miss" if you were wrong. Judge against what you
  actually claimed, not a charitable reading.
- Output ONLY the JSON object.
"""


def _grade_one(take):
    context = {
        "your_take_subject": take.get("subject", ""),
        "your_stance": take.get("stance", ""),
        "resolves_when": take.get("resolves_when") or "(none recorded — judge from the stance)",
        "you_made_this_call_on": time.strftime("%Y-%m-%d", time.localtime(take.get("formed_at", 0))),
    }
    cmd = [
        GRAFF, "-p", "--yolo", "--model", MODEL,
        "--append-system-prompt", _dateline() + _load_persona() + "\n" + GRADE_ADDENDUM,
        "--max-tool-calls", "6",  # it needs the sports tools to check reality
        "--no-telemetry",
        "Grade this old take of yours against what really happened:\n" + json.dumps(context, indent=2),
    ]
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=TURN_TIMEOUT)
    except subprocess.TimeoutExpired:
        print("[grade] timed out", file=sys.stderr)
        return None
    if out.returncode != 0:
        print(f"[grade] graff error: {(out.stderr or '').strip()[-200:]}", file=sys.stderr)
        return None
    return _extract_json(out.stdout)


def grade(dry_run=False):
    """Settle overdue takes against reality. A hit earns confidence, a miss cuts it, and both
    roll into ronin's record — so its conviction is something it's built, not just asserted."""
    due = memory.takes_due()
    if not due:
        print("[grade] nothing due.", file=sys.stderr)
        return
    graded = 0
    for t in due:
        key = memory.take_key(t)
        res = _grade_one(t)
        if not res or not res.get("resolved") or res.get("verdict") not in ("hit", "miss"):
            # Can't call it yet (or the judge failed): push the deadline out and retry later,
            # rather than leaving it due every pass or grading it on no information.
            if not dry_run:
                memory.defer_take(key)
            print(f"[grade] {t.get('subject','?')}: not settled yet, deferred", file=sys.stderr)
            continue
        note = res.get("note", "")
        if not dry_run:
            nc = memory.resolve_take(key, res["verdict"], note)
        else:
            nc = None
        print(f"[grade] {t.get('subject','?')}: {res['verdict'].upper()} "
              f"(conf->{nc}) — {note}", file=sys.stderr)
        graded += 1
    rec = memory.get_record()
    print(f"[grade] done. settled {graded} this pass. record: "
          f"{rec['hits']}-{rec['misses']}.", file=sys.stderr)


# ---------------------------------------------------------------------------
# SENTIMENT SWEEP: ping when the VIBE around a team shifts, not just on news.
# News roaming reacts to events; this reacts to the mood turning (fans souring on a
# coach, hype building). It reads the blended fan sentiment, compares it to the mood it
# saw last time, and only pings on a real move — then stores the new mood so it won't
# re-ping a steady vibe.
# ---------------------------------------------------------------------------
SENTIMENT_ADDENDUM = """
## VIBE MODE (you're checking whether the MOOD around a team has shifted)
Not news — mood. You're reading what fans are saying right now and comparing it to how the
room felt last time you checked, deciding whether the vibe has MOVED enough to text someone
about, unprompted.

You'll get: the person's team, the fan sentiment right now (Reddit + Bluesky — sentiment,
NOT fact, and some of it may be stale, weigh it), the mood you logged last time (or "first
time"), and things you recently told them (don't repeat).

Return STRICT JSON, nothing else:
{
  "mood": "one line capturing the vibe RIGHT NOW (always fill this in, even if unchanged)",
  "shifted": true | false,
  "notable": true | false,
  "message": "the text you'd send, your voice — or \"\" if not notable"
}
Rules:
- "mood" is your read of the current vibe in a short phrase — this gets stored as the new
  baseline, so make it honest and specific.
- "shifted" is true only if the mood MEANINGFULLY moved from last time (souring, hype
  building, panic setting in, turning a corner) — not day-to-day noise or the same vibe
  reworded.
- "notable" is true only if that shift is something a fan actually wants an unprompted text
  about. A steady mood, or a tiny wobble, is notable: false.
- "message" (only if notable): SHORT, your voice, dry and human, lowercase-friendly. Name the
  shift and give YOUR read — you are NOT a hive-mind mirror. Don't state scores/records; if a
  post claims a transaction, that's for a news check, not this.
- Output ONLY the JSON object.
"""


def _vibe_judge(uid, team, league, sentiment_text, prior_mood):
    context = {
        "person_follows": f"{team} ({league.upper()})",
        "fan_sentiment_right_now": sentiment_text,
        "the_mood_last_time_you_checked": prior_mood or "(first time checking)",
        "things_you_recently_told_them": _recent_texts(uid) or ["(nothing yet)"],
    }
    system_prompt = _dateline() + _load_persona() + "\n" + SENTIMENT_ADDENDUM
    cmd = [
        GRAFF, "-p", "--yolo", "--model", MODEL,
        "--append-system-prompt", system_prompt,
        "--max-tool-calls", "0",  # the sentiment is already in the prompt
        "--no-telemetry",
        "Read the vibe and decide if it shifted:\n" + json.dumps(context, indent=2),
    ]
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=TURN_TIMEOUT)
    except subprocess.TimeoutExpired:
        print("[vibe] judge timed out", file=sys.stderr)
        return None
    if out.returncode != 0:
        print(f"[vibe] graff error: {(out.stderr or '').strip()[-200:]}", file=sys.stderr)
        return None
    return _extract_json(out.stdout)


def sentiment_sweep(dry_run=False):
    """Ping on a mood shift around a user's team. Same anti-annoyance rails as the news pass
    (per-user cap + the cross-pass min-gap throttle), and a cold-start that baselines the mood
    silently so a new team never gets blasted."""
    users = memory.active_users()
    if not users:
        print("[vibe] no active users.", file=sys.stderr)
        return
    total_sent = 0
    for uid, user in users:
        chat_id = user.get("chat_id")
        sent_this_user = 0
        for tinfo in memory.user_teams(user):
            league, team = tinfo["league"], tinfo["team"]
            if not (league and team):
                continue
            # Per user, same reason as the news cursor. A team's mood is arguably one
            # shared fact, but this is the record of what THIS person was last told, and
            # it's written mid-loop: shared, the first user's set_mood became the second
            # user's "prior", so their judge saw no shift and they never got the ping.
            # Costs nothing to split — the fetch and the judge already run per user (the
            # judge personalizes off what it recently texted them).
            scope = f"{uid}:{league}:{team.lower()}"
            try:
                vibe = fan.fan_sentiment(league, team)
            except Exception as e:  # noqa: BLE001 — one team's sentiment failing isn't fatal
                print(f"[vibe] sentiment fetch failed for {team}: {e}", file=sys.stderr)
                continue
            prior = memory.get_mood(scope)
            prior_mood = prior.get("mood") if prior else None
            decision = _vibe_judge(uid, team, league, vibe, prior_mood)
            if not decision:
                continue
            mood = (decision.get("mood") or "").strip()
            if mood and not dry_run:
                memory.set_mood(scope, mood)
            # Cold start: nothing to compare against yet, so baseline silently, never ping.
            if prior_mood is None:
                print(f"[vibe] baselined {scope}: {mood[:60]}", file=sys.stderr)
                continue
            msg = (decision.get("message") or "").strip()
            if not (decision.get("shifted") and decision.get("notable") and msg):
                continue
            if sent_this_user >= MAX_PER_USER:
                continue
            key = "vibe:" + memory._slug(mood)[:48]
            if memory.already_sent(uid, key):
                continue
            if not memory.proactive_allowed(uid, PROACTIVE_MIN_GAP):
                print(f"[vibe] {uid} throttled (min gap); mood saved, no ping.", file=sys.stderr)
                continue
            print(f"[vibe] -> {team} to {uid}: {msg}", file=sys.stderr)
            if not dry_run:
                _tg_send(chat_id, msg)
            memory.log_sent(uid, key, msg)
            memory.touch_proactive(uid)
            sent_this_user += 1
            total_sent += 1
    print(f"[vibe] pass done. mood pings: {total_sent}.", file=sys.stderr)


# ---------------------------------------------------------------------------
# DIGEST: distill what ronin knows about a PERSON from their recent chats.
# ---------------------------------------------------------------------------
DIGEST_MIN_USER_TURNS = 3

DIGEST_ADDENDUM = """
## DIGEST MODE (you're remembering a person, not scores)
You're skimming your recent chats with ONE person so you remember what makes THEM them next
time — their opinions, their running jokes, the arguments you two keep having. Not sports
facts. Facts about the person.

You'll get the recent transcript and what you already remember. Return an updated memory.

Return STRICT JSON, nothing else:
{
  "takes_you_hold":     ["short, third-person: an opinion THEY hold, e.g. 'thinks load management ruined the league'"],
  "bits":               ["a running joke or phrase that's theirs, e.g. 'calls the Lakers the retirement home'"],
  "running_arguments":  ["a topic you two go back and forth on, e.g. 'Steph vs LeBron GOAT'"]
}
Rules:
- Durable only. Skip one-off factual questions ("who won last night") — those aren't about them.
- MERGE with what you already remember: keep what still holds, revise what changed, drop what
  they've clearly moved off. Don't just append.
- Each item a short phrase, third-person about them. Empty lists are fine. JSON only.
"""

# Must match ronin_reply._session_name — both address the same graff session file.
def _session_name(sender_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(sender_id))[:64] or "anon"
    return f"sess_{safe}"


def _session_transcript(uid, max_turns=16):
    """Recent (role, text) turns from a user's graff session, plus its updated_ms."""
    path = os.path.join(ROOT, f"{_session_name(uid)}.session.json")
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return [], 0
    out = []
    for m in d.get("messages", [])[-max_turns:]:
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in c)
        c = (c or "").strip()
        if m.get("role") in ("user", "assistant") and c:
            out.append((m["role"], c))
    return out, d.get("updated_ms", 0)


def _digest_one(uid, profile, turns):
    context = {
        "recent_chat": [f"{r}: {c}" for r, c in turns],
        "what_you_already_remember": {k: profile.get(k, []) for k in memory.PROFILE_CAPS},
    }
    cmd = [
        GRAFF, "-p", "--yolo", "--model", MODEL,
        "--append-system-prompt", _load_persona() + "\n" + DIGEST_ADDENDUM,
        "--max-tool-calls", "0",  # pure distillation; no facts to look up
        "--no-telemetry",
        "Update what you remember about this person:\n" + json.dumps(context, indent=2),
    ]
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=TURN_TIMEOUT)
    except subprocess.TimeoutExpired:
        print("[digest] timed out", file=sys.stderr)
        return None
    if out.returncode != 0:
        print(f"[digest] graff error: {(out.stderr or '').strip()[-200:]}", file=sys.stderr)
        return None
    return _extract_json(out.stdout)


def digest(dry_run=False):
    """Refresh ronin's memory of each active user from their recent chats. Gated: skip a
    conversation that hasn't moved since we last read it, so we don't pay to re-digest."""
    updated = 0
    for uid, u in memory.active_users():
        turns, updated_ms = _session_transcript(uid)
        profile = memory.get_profile(uid)
        if updated_ms and updated_ms <= profile.get("digested_ms", 0):
            continue  # nothing new said since the last digest
        if sum(1 for r, _ in turns if r == "user") < DIGEST_MIN_USER_TURNS:
            continue  # too little to bother
        data = _digest_one(uid, profile, turns)
        if not isinstance(data, dict):
            continue
        n = sum(len(data.get(k) or []) for k in memory.PROFILE_CAPS)
        print(f"[digest] {uid}: {n} remembered detail(s)", file=sys.stderr)
        if not dry_run:
            memory.set_profile(uid, data, digested_ms=updated_ms)
        updated += 1
    print(f"[digest] done. refreshed {updated} profile(s).", file=sys.stderr)


if __name__ == "__main__":
    if "--grade" in sys.argv:
        grade(dry_run="--dry" in sys.argv)
    elif "--digest" in sys.argv:
        digest(dry_run="--dry" in sys.argv)
    elif "--sentiment" in sys.argv:
        sentiment_sweep(dry_run="--dry" in sys.argv)
    elif "--reflect" in sys.argv:
        reflect(dry_run="--dry" in sys.argv)
    else:
        run_once(dry_run="--dry" in sys.argv)
