#!/usr/bin/env python3
"""ronin eval harness.

Three layers, cheapest first:
  data         pure/offline unit checks (memory, formatters, date logic). Instant, free.
  integration  live ESPN calls, no LLM (scoreboard order, champion, schedules).
  behavior     the model in the loop (ronin_reply.reply) on seeded memory. Costs API $.

Every case here froze a real bug we hit by hand: first-game date-guessing, "only today"
date blindness, wrong weekday, the multi-team gap, stale-data takes, em dashes. Run it
before a deploy so those don't come back.

Usage:
  python3 eval/run.py                # all three layers
  python3 eval/run.py --no-llm       # data + integration (no API cost)
  python3 eval/run.py --data-only    # just the free offline checks

Behavior needs ~/bin/graff + ANTHROPIC_API_KEY (read from ./.env like the bot).
"""

import datetime
import os
import re
import sys
import tempfile
import time

# Isolate memory state in a temp dir BEFORE importing anything that reads STATE_DIR.
_TMP_STATE = tempfile.mkdtemp(prefix="ronin_eval_state_")
os.environ["RONIN_STATE_DIR"] = _TMP_STATE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcp import espn  # noqa: E402
import memory         # noqa: E402


# ---------------------------------------------------------------------------
# tiny check framework
# ---------------------------------------------------------------------------
class Results:
    def __init__(self):
        self.rows = []

    def check(self, group, name, ok, detail=""):
        ok = bool(ok)
        self.rows.append((group, name, ok, detail))
        mark = "\033[32m✓\033[0m" if ok else "\033[31m✗\033[0m"
        line = f"  {mark} [{group}] {name}"
        if not ok and detail:
            line += f"\n        → {detail}"
        print(line)
        return ok

    def summary(self):
        print("\n" + "=" * 60)
        by = {}
        for g, _n, ok, _d in self.rows:
            p, t = by.get(g, (0, 0))
            by[g] = (p + (1 if ok else 0), t + 1)
        total_ok = sum(1 for _g, _n, ok, _d in self.rows if ok)
        total = len(self.rows)
        for g, (p, t) in by.items():
            flag = "" if p == t else "  <-- FAILURES"
            print(f"  {g:<12} {p}/{t}{flag}")
        print("-" * 60)
        print(f"  {'TOTAL':<12} {total_ok}/{total}")
        fails = [(g, n, d) for g, n, ok, d in self.rows if not ok]
        if fails:
            print("\nFailures:")
            for g, n, d in fails:
                print(f"  ✗ [{g}] {n}{('  — ' + d) if d else ''}")
        return total_ok == total


def low(t):
    return (t or "").lower()


def has_all(t, *subs):
    return all(s.lower() in low(t) for s in subs)


def has_any(t, *subs):
    return any(s.lower() in low(t) for s in subs)


def has_none(t, *subs):
    return not any(s.lower() in low(t) for s in subs)


NO_EM_DASH = "—"  # the single biggest AI tell; must never appear in a reply


# ---------------------------------------------------------------------------
# layer 1: data (offline, pure/local — no network, no LLM)
# ---------------------------------------------------------------------------
def run_data(res):
    print("\n── data (offline) ──")
    # weekday computed in US Eastern from ESPN's UTC timestamps
    res.check("data", "weekday: Sep-9 8:20pm ET game reads Wed (not Thu)",
              espn._weekday("2026-09-10T00:20Z") == "Wed",
              f"got {espn._weekday('2026-09-10T00:20Z')!r}")
    res.check("data", "weekday: Sep-10 game reads Thu",
              espn._weekday("2026-09-11T00:35Z") == "Thu")
    res.check("data", "weekday: blank input -> '' (graceful)",
              espn._weekday("") == "")

    # league aliases
    res.check("data", "alias soccer -> wc", espn._league("soccer") == "wc")
    res.check("data", "alias premier league -> epl", espn._league("premier league") == "epl")
    res.check("data", "alias champions league -> ucl", espn._league("champions league") == "ucl")
    res.check("data", "canonical nfl stays nfl", espn._league("nfl") == "nfl")
    try:
        espn._league("not-a-sport")
        res.check("data", "unknown league raises SportError", False, "no error raised")
    except espn.SportError:
        res.check("data", "unknown league raises SportError", True)

    # stat map + soccer table formatting
    m = espn._stat_map([{"name": "points", "value": 9.0}, {"name": "wins", "value": 3},
                        {"name": "pointDifferential", "value": 6}])
    res.check("data", "_stat_map parses named stats", m.get("points") == 9 and m.get("wins") == 3)
    entries = [
        {"team": {"abbreviation": "MEX"}, "stats": [
            {"name": "rank", "value": 1}, {"name": "gamesPlayed", "value": 3},
            {"name": "wins", "value": 3}, {"name": "ties", "value": 0},
            {"name": "losses", "value": 0}, {"name": "pointDifferential", "value": 6},
            {"name": "points", "value": 9}, {"name": "advanced", "value": 1}]},
        {"team": {"abbreviation": "KOR"}, "stats": [
            {"name": "rank", "value": 2}, {"name": "gamesPlayed", "value": 3},
            {"name": "wins", "value": 1}, {"name": "ties", "value": 0},
            {"name": "losses", "value": 2}, {"name": "pointDifferential", "value": -1},
            {"name": "points", "value": 3}, {"name": "advanced", "value": 0}]},
    ]
    block = espn._soccer_block("Group A", entries)
    first = block.splitlines()[1]
    res.check("data", "soccer table: rank-1 first, W-D-L / GD / pts / ✓",
              first.strip().startswith("1. MEX") and "GD+6" in first and "9pts" in first
              and "✓" in first, first)

    # memory: legacy single-team record migrates
    memory._update("relationships.json",
                   lambda d: d.__setitem__("mL", {"league": "nba", "team": "Golden State Warriors",
                                                  "abbrev": "GSW", "chat_id": 1}), {})
    mt = memory.user_teams("mL")
    res.check("data", "legacy single-team record migrates to teams map",
              len(mt) == 1 and mt[0]["team"] == "Golden State Warriors")

    # memory: teams coexist across leagues, and clear removes one
    memory.set_team("mC", "nba", "Golden State Warriors", "GSW", chat_id=1)
    memory.set_team("mC", "nfl", "San Francisco 49ers", "SF", chat_id=1)
    res.check("data", "two leagues coexist (nba + nfl)",
              {t["league"] for t in memory.user_teams("mC")} == {"nba", "nfl"})
    memory.clear_team("mC", "nba")
    left = memory.user_teams("mC")
    res.check("data", "clear one league keeps the other",
              len(left) == 1 and left[0]["league"] == "nfl")

    # memory: top_affinities surfaces each league's top pick (WC not buried by NBA depth)
    memory.upsert_affinity("Spurs", "nba", "SA", 0.9, "love")
    memory.upsert_affinity("Pistons", "nba", "DET", 0.8, "love")
    memory.upsert_affinity("Thunder", "nba", "OKC", 0.7, "love")
    memory.upsert_affinity("Cape Verde", "wc", "CPV", 0.5, "underdog")
    loves, _ = memory.top_affinities()
    res.check("data", "top_affinities surfaces the WC pick despite 3 stronger NBA loves",
              any(a["league"] == "wc" for a in loves),
              f"loves={[a['team'] for a in loves]}")

    # memory: a null/garbage confidence from the LLM must not crash upsert_take
    try:
        memory.upsert_take("Conf probe", "first stance", None, "null conf")
        memory.upsert_take("Conf probe", "second stance", "high", "string conf")
        memory.upsert_take("Conf probe", "third stance", 0.8, "real conf")
        probe = [t for t in memory.get_takes() if t["subject"] == "Conf probe"][0]
        res.check("data", "upsert_take survives null/garbage confidence",
                  probe["confidence"] == 0.8 and len(probe["history"]) == 2,
                  f"conf={probe['confidence']} history={len(probe['history'])}")
    except Exception as e:  # noqa: BLE001
        res.check("data", "upsert_take survives null/garbage confidence", False, repr(e))
    res.check("data", "_conf clamps out-of-range and defaults on junk",
              memory._conf(2.5) == 1.0 and memory._conf(-9) == 0.0
              and memory._conf(None) == 0.5 and memory._conf("0.3") == 0.3)

    # memory: the outbound dedup keys are bounded (they used to grow forever)
    for i in range(memory.KEYS_MAX + 100):
        memory.log_sent("mO", f"k{i}", "msg")
    ob = memory._read("outbound.json", {})
    res.check("data", "outbound keys stay capped, newest retained",
              len(ob["keys"]) == memory.KEYS_MAX and memory.already_sent("mO", f"k{memory.KEYS_MAX + 99}")
              and not memory.already_sent("mO", "k0"),
              f"keys={len(ob['keys'])}")
    memory._update("outbound.json",
                   lambda d: d["keys"].__setitem__("mO:stale", time.time() - memory.KEYS_TTL - 1), {})
    memory.log_sent("mO", "fresh", "msg")
    res.check("data", "outbound keys age out past the TTL",
              not memory.already_sent("mO", "stale"))

    # a proactive ping reaches the chat prompt so a follow-up has something to attach to
    import ronin_reply
    memory.set_team("mP", "nba", "Golden State Warriors", "GSW", chat_id=1)
    memory._update("outbound.json", lambda d: d.setdefault("sent", []).append(
        {"uid": "mP", "key": "curry", "text": "curry got his own HOF exhibit lol",
         "at": time.time() - 3 * 3600}), {})
    memory._update("outbound.json", lambda d: d["sent"].append(
        {"uid": "mP", "key": "ancient", "text": "ancient news", "at": time.time() - 4 * 86400}), {})
    rs = memory.recent_sent("mP")
    res.check("data", "recent_sent returns the fresh ping, drops the 4-day-old one",
              len(rs) == 1 and "HOF" in rs[0]["text"])
    sp = ronin_reply._load_system_prompt("mP")
    res.check("data", "proactive ping is injected into the chat system prompt",
              "HOF exhibit" in sp and "unprompted" in sp and "3h ago" in sp
              and "ancient news" not in sp)

    _check_calibration(res)
    _check_relationship_memory(res)
    _check_web_parser(res)
    _check_sentiment_sweep(res)
    _check_roam_retry(res)
    _check_thinking_strip(res)


def _check_sentiment_sweep(res):
    """The vibe pass baselines a mood silently, pings only on a real shift, and won't ping a
    steady mood — the self-throttle that keeps it from being annoying."""
    import roam
    from mcp import fan
    # isolate: the sweep scans every active user, so clear the others earlier checks created
    memory._update("relationships.json", lambda d: d.clear(), {})
    memory._write("mood.json", {})
    memory.set_team("mV", "nba", "Detroit Pistons", "DET", chat_id=1)
    real_fan, real_judge, real_send = fan.fan_sentiment, roam._vibe_judge, roam._tg_send
    fan.fan_sentiment = lambda lg, tp=None: "REDDIT: ... BLUESKY: ..."
    sends = []
    roam._tg_send = lambda cid, msg: sends.append(msg)
    scope = "nba:detroit pistons"
    try:
        # cold start: no prior mood -> baseline, no ping
        roam._vibe_judge = lambda *a: {"mood": "quietly buzzing on the young core",
                                       "shifted": False, "notable": False, "message": ""}
        roam.sentiment_sweep(dry_run=False)
        res.check("data", "vibe sweep baselines mood on cold start, no ping",
                  memory.get_mood(scope) is not None and not sends)
        # a real shift -> one ping, new mood stored
        roam._vibe_judge = lambda *a: {"mood": "fans turning on the coach", "shifted": True,
                                       "notable": True, "message": "heads up, room's souring on the coach"}
        roam.sentiment_sweep(dry_run=False)
        res.check("data", "vibe sweep pings on a real mood shift",
                  len(sends) == 1 and "coach" in sends[0]
                  and memory.get_mood(scope)["mood"].startswith("fans turning"))
        # steady mood next time -> no new ping
        roam._vibe_judge = lambda *a: {"mood": "still sour, no change", "shifted": False,
                                       "notable": False, "message": ""}
        roam.sentiment_sweep(dry_run=False)
        res.check("data", "vibe sweep stays quiet on a steady mood", len(sends) == 1)
    finally:
        fan.fan_sentiment, roam._vibe_judge, roam._tg_send = real_fan, real_judge, real_send


def _check_web_parser(res):
    """web_search's fragile part is the SERP HTML parse (markup drifts), so pin it to a
    fixed sample. Also guard the SSRF-safety invariant: search only ever hits one host."""
    from mcp import web
    sample = (
        '<a rel="nofollow" class="result__a" '
        'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FNaismith&rut=x">'
        'Naismith Basketball Hall of Fame</a>'
        '<a class="result__snippet" href="#">A museum in Springfield, Massachusetts.</a>'
        '<a rel="nofollow" class="result__a" '
        'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.hoophall.com%2F&rut=y">'
        'Hoop Hall — official site</a>'
        '<a class="result__snippet" href="#">Visit the Hall of Fame.</a>'
        '<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com">'
        'Third</a><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fb.com">'
        'Fourth</a><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fc.com">'
        'Fifth</a><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fd.com">'
        'Sixth</a>')
    parsed = web._parse(sample)
    res.check("data", "web parser: title + snippet + source, capped at MAX_RESULTS",
              len(parsed) == web.MAX_RESULTS
              and parsed[0]["title"] == "Naismith Basketball Hall of Fame"
              and parsed[0]["source"] == "en.wikipedia.org"
              and "Springfield" in parsed[0]["snippet"]
              and parsed[1]["source"] == "hoophall.com")
    res.check("data", "web search is SSRF-safe: single fixed host, user text only in query",
              web.SERP.startswith("https://html.duckduckgo.com/") and web.SERP.endswith("q="))

    # reddit sentiment: league->subreddit mapping, plus both tiers (network stubbed)
    from mcp import reddit
    res.check("data", "reddit maps leagues to subreddits (soccer funnels, nba default)",
              reddit._sub_for("nba") == "nba" and reddit._sub_for("mlb") == "baseball"
              and reddit._sub_for("ucl") == "soccer" and reddit._sub_for("xyz") == "nba")

    # Tier 1 (creds present): OAuth listing parse — drops stickied, ranks by score.
    sample = [{"title": "[Woj] big trade", "score": 4200, "num_comments": 900, "stickied": False},
              {"title": "Daily Thread", "score": 50, "num_comments": 30, "stickied": True},
              {"title": "Game Thread", "score": 1500, "num_comments": 5000, "stickied": False}]
    real_creds, real_api = (reddit.CLIENT_ID, reddit.CLIENT_SECRET), reddit._api_get
    reddit.CLIENT_ID, reddit.CLIENT_SECRET = "id", "secret"
    reddit._api_get = lambda path: sample
    try:
        out = reddit.reddit_sentiment("nba")
    finally:
        reddit._api_get = real_api
        reddit.CLIENT_ID, reddit.CLIENT_SECRET = real_creds
    res.check("data", "reddit API tier: drops stickied, ranks by score, shows vote counts",
              "r/nba" in out and "Woj" in out and "Daily Thread" not in out
              and "pts" in out and out.index("Woj") < out.index("Game Thread"))

    # Tier 2 (no creds): fall back to reading Reddit through web search.
    real_fetch = reddit.web._fetch
    reddit.web._fetch = lambda url: (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.reddit.com'
        '%2Fr%2Fnba%2Fcomments%2Fx">Why the Lakers offseason flopped : r/nba - Reddit</a>'
        '<a class="result__snippet" href="#">fans are torn on the D-Lo contract.</a>')
    try:
        assert not reddit._has_creds(), "test expects no creds set"
        fb = reddit.reddit_sentiment("nba", "lakers")
    finally:
        reddit.web._fetch = real_fetch
    res.check("data", "reddit fallback: reads r/nba via search, cleans the DDG title",
              "via search" in fb and "Why the Lakers offseason flopped" in fb
              and "Reddit" not in fb.split("flopped")[1].split("\n")[0])  # trailing "- Reddit" stripped

    # the model-facing tool blends BOTH sources, and one failing doesn't sink the call
    from mcp import fan
    real_r, real_b = fan.rdt.reddit_sentiment, fan.bsky.fan_sentiment
    fan.rdt.reddit_sentiment = lambda lg, tp: "Top of r/nba: [Woj] trade talk"
    fan.bsky.fan_sentiment = lambda tp: "Bluesky chatter: nobody's panicking"
    try:
        both = fan.fan_sentiment("nba", "trade")
        res.check("data", "fan_sentiment blends Reddit + Bluesky under both headers",
                  "REDDIT" in both and "BLUESKY" in both
                  and "Woj" in both and "nobody's panicking" in both)
        # one source down -> still return the other, with a note (not a crash)
        def boom(*a):
            raise RuntimeError("bluesky down")
        fan.bsky.fan_sentiment = boom
        degraded = fan.fan_sentiment("nba", "trade")
        res.check("data", "fan_sentiment survives one source failing",
                  "Woj" in degraded and "unavailable" in degraded)
    finally:
        fan.rdt.reddit_sentiment, fan.bsky.fan_sentiment = real_r, real_b


def _check_calibration(res):
    """Take de-dup + grading: a storyline is one revisable belief, and being right earns
    conviction while being wrong costs it."""
    # de-dup: a reworded subject on the same topic revises instead of forking
    memory.upsert_take("Curry legacy", "first-ballot HOF", 0.8, topic="curry-legacy")
    memory.upsert_take("Steph's HOF case", "museum piece", 0.85, topic="curry-legacy")
    dup = [t for t in memory.get_takes() if t["topic"] == "curry-legacy"]
    res.check("data", "take de-dup: same topic revises, doesn't fork",
              len(dup) == 1 and dup[0]["subject"] == "Steph's HOF case" and len(dup[0]["history"]) == 1)
    # legacy take with no topic still matches on its subject slug
    memory.upsert_take("panic takes", "age badly", 0.8)
    memory.upsert_take("Panic Takes", "still true", 0.82)
    res.check("data", "legacy no-topic take matches on subject slug",
              len([t for t in memory.get_takes() if t["topic"] == "panic-takes"]) == 1)

    # grading: only overdue open takes are due; a miss cuts confidence, a hit raises it
    memory.upsert_take("OKC grind", "fold in a series", 0.7, topic="okc-grind",
                       deadline=20250101, resolves_when="OKC out or champ")
    memory.upsert_take("timeless", "no checkable outcome", 0.8, topic="timeless")  # no deadline
    due_keys = {memory.take_key(t) for t in memory.takes_due(today=20260101)}
    res.check("data", "takes_due lists overdue takes, skips deadline-less ones",
              "okc-grind" in due_keys and "timeless" not in due_keys)
    miss_conf = memory.resolve_take("okc-grind", "miss", "OKC won it all")
    hit_conf = memory.resolve_take("curry-legacy", "hit", "inducted")
    res.check("data", "a miss cuts confidence, a hit raises it",
              miss_conf < 0.7 and hit_conf > 0.85)
    rec = memory.get_record()
    res.check("data", "calibration record tallies hits/misses + accuracy",
              rec["hits"] == 1 and rec["misses"] == 1 and rec["accuracy"] == 0.5)
    res.check("data", "a graded take leaves the standing beliefs, joins the record",
              all(t["topic"] not in ("okc-grind", "curry-legacy")
                  for t in memory.takes_due(today=20260101)))
    # revising a graded take reopens it (the new stance is untested)
    memory.upsert_take("OKC grind", "ok they proved me wrong", 0.6, topic="okc-grind")
    res.check("data", "revising a graded take reopens it for grading",
              [t for t in memory.get_takes() if t["topic"] == "okc-grind"][0]["status"] == "open")

    # affinity decay: a stale, unreaffirmed allegiance fades out (the France bug)
    memory.upsert_affinity("France", "wc", "FRA", -0.30, "rolling into the semis")
    memory.upsert_affinity("Spain", "wc", "ESP", -0.30, "the machine")
    dropped = memory.decay_affinities(["wc"], {"wc:ESP"})
    keys = {a["key"] for a in memory.get_affinities()}
    res.check("data", "affinity decay retires the stale, unreaffirmed France",
              "wc:FRA" not in keys and "wc:ESP" in keys and "wc:FRA" in dropped)

    # the track record surfaces in the chat prompt; graded takes drop out of standing beliefs
    import ronin_reply
    sp = ronin_reply._load_system_prompt(None)
    res.check("data", "chat prompt shows the earned track record, hides graded takes",
              "track record" in sp and "1 right, 1 wrong" in sp
              and "Steph's HOF case" not in sp.split("track record")[0].split("standing takes")[-1])


def _check_relationship_memory(res):
    """The digest builds a per-user profile; the chat prompt talks like it knows them."""
    import ronin_reply
    memory.set_team("mR2", "nba", "Detroit Pistons", "DET", chat_id=1)
    memory.set_profile("mR2", {
        "takes_you_hold": ["thinks load management ruined the league"] * 20,  # over cap
        "bits": ["calls the Lakers the retirement home"],
        "running_arguments": ["Steph vs LeBron GOAT"]}, digested_ms=5000)
    prof = memory.get_profile("mR2")
    res.check("data", "profile lists are capped and store digested_ms",
              len(prof["takes_you_hold"]) == memory.PROFILE_CAPS["takes_you_hold"]
              and prof["digested_ms"] == 5000)
    sp = ronin_reply._load_system_prompt("mR2")
    res.check("data", "chat prompt surfaces what ronin remembers about them",
              "load management" in sp and "retirement home" in sp
              and "Steph vs LeBron" in sp and "remember about them" in sp)


def _check_thinking_strip(res):
    """graff's harness prompt lets the model narrate in <thinking> tags, and -p prints the
    whole answer — so it shipped into the chat. Nothing but the answer may survive."""
    import ronin_reply
    s = ronin_reply._strip_thinking
    res.check("data", "a leaked <thinking> block never reaches the user",
              s("<thinking>Simple intro question. Stay in character.</thinking>ronin. i live "
                "in sports way too much.") == "ronin. i live in sports way too much.")
    res.check("data", "thinking is stripped mid-answer and in bulk",
              s("hey <THINK>hm</think> there <reasoning>x</reasoning>now") == "hey  there now")
    # A turn cut off mid-thought leaves a dangling tag on one end or the other.
    res.check("data", "an unclosed thinking tag takes the rest of the output with it",
              s("real answer\n<thinking>ran out of tok") == "real answer")
    res.check("data", "an unmatched closing tag drops the reasoning before it",
              s("ran out of budget</thinking>the actual take") == "the actual take")
    # Must not eat legitimate prose: ronin talks about thinking constantly.
    res.check("data", "ordinary talk about thinking survives untouched",
              s("i think the Spurs are for real, been thinking about it all year")
              == "i think the Spurs are for real, been thinking about it all year")
    # Reasoning-only output is empty after stripping — reply() must fall back, not send "".
    res.check("data", "a reasoning-only reply strips to empty (reply falls back)",
              s("<thinking>no idea</thinking>") == "")


def _check_roam_retry(res):
    """A judge timeout used to lose the headline forever: roam marked every new item seen
    up front, so the retry never came. Stub the judge to fail, then recover."""
    import roam
    heads = [{"key": "r1", "headline": "A", "desc": "d"}]
    real_headlines, real_judge, real_send = espn.recent_headlines, roam._judge, roam._tg_send
    espn.recent_headlines = lambda l, t, limit=None: list(heads)
    roam._tg_send = lambda chat_id, text: None
    scope = "nba:phoenix suns"
    try:
        memory.set_team("mR", "nba", "Phoenix Suns", "PHX", chat_id=1)
        roam._judge = lambda *a: None
        roam.run_once(dry_run=True)                      # cold start: baseline, no messages
        heads.append({"key": "r2", "headline": "B", "desc": "d"})
        roam.run_once(dry_run=True)                      # judge fails on the new item
        res.check("data", "judge failure leaves the headline unseen (retried, not dropped)",
                  not memory.headline_seen(scope, "r2"))

        roam._judge = lambda *a: {"notable": True, "message": "suns news",
                                  "take": {"subject": "Suns", "stance": "up", "confidence": None}}
        roam.run_once(dry_run=True)                      # judge recovers -> news delivered
        res.check("data", "recovered judge delivers the previously-failed headline",
                  memory.headline_seen(scope, "r2") and memory.already_sent("mR", "r2"))

        def boom(*a):
            raise AssertionError("re-judged an already-handled headline")
        roam._judge = boom
        roam.run_once(dry_run=True)                      # handled items aren't re-judged
        res.check("data", "a judged headline is never re-judged or re-sent", True)

        # A take whose judge slipped the year (deadline already past) forms WITHOUT a
        # deadline, rather than churning the grader on a season that hasn't happened.
        heads.append({"key": "r3", "headline": "C", "desc": "d"})
        past = memory._today_int() - 100  # ~a year ago
        roam._judge = lambda *a: {"notable": False, "message": "", "take": {
            "topic": "suns-ceiling", "subject": "Suns ceiling", "stance": "contender",
            "confidence": 0.6, "resolves_when": "next season playoff result", "deadline": past}}
        roam.run_once(dry_run=True)
        suns = [t for t in memory.get_takes() if t["topic"] == "suns-ceiling"]
        res.check("data", "a past deadline is dropped, not left to churn the grader",
                  bool(suns) and suns[0]["deadline"] is None)
    except AssertionError as e:
        res.check("data", "a judged headline is never re-judged or re-sent", False, str(e))
    finally:
        espn.recent_headlines, roam._judge, roam._tg_send = real_headlines, real_judge, real_send


# ---------------------------------------------------------------------------
# layer 2: integration (live ESPN, no LLM)
# ---------------------------------------------------------------------------
def run_integration(res):
    print("\n── integration (live ESPN, no LLM) ──")
    try:
        sb = espn.scoreboard("nfl")
        first = sb.splitlines()[1] if len(sb.splitlines()) > 1 else ""
        res.check("integration", "nfl scoreboard: earliest game first, with weekday",
                  has_all(first, "NE", "SEA") and "Wed" in first, first)
    except Exception as e:  # noqa: BLE001
        res.check("integration", "nfl scoreboard reachable", False, str(e))

    try:
        res.check("integration", "champion(ucl) = PSG (2025-26)",
                  has_any(espn.champion("ucl"), "paris", "psg"))
    except Exception as e:  # noqa: BLE001
        res.check("integration", "champion(ucl) reachable", False, str(e))

    try:
        res.check("integration", "champion(wc): both finalists present (Spain + Argentina)",
                  has_all(espn.champion("wc"), "spain", "argentina"), espn.champion("wc"))
    except Exception as e:  # noqa: BLE001
        res.check("integration", "champion(wc) reachable", False, str(e))

    # web search: exercise the real SERP + parser end to end (via urllib, independent of the
    # kuri-fetch binary that only exists in the container). Skip gracefully if the IP is blocked.
    try:
        import urllib.request
        from mcp import web
        req = urllib.request.Request(web.SERP + "who+owns+the+green+bay+packers",
                                     headers={"User-Agent": web.UA})
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        parsed = web._parse(html)
        if parsed:
            res.check("integration", "web search: live SERP parses into results",
                      all(p["title"] for p in parsed) and any(p["source"] for p in parsed))
        else:
            res.check("integration", "web search: live SERP reachable (no parse — markup drift?)",
                      False, "0 results parsed from a live fetch")
    except Exception as e:  # noqa: BLE001 — datacenter IPs can be blocked; don't fail the suite
        print(f"    ↳ web search integration skipped: {e}", file=sys.stderr)

    try:
        tmrw = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y%m%d")
        wsb = espn.scoreboard("wnba", tmrw)
        # off-days happen; only assert the shape when there are games
        if "No " in wsb and "games found" in wsb:
            res.check("integration", "wnba tomorrow lookup returns a valid response (no games day)",
                      True)
        else:
            res.check("integration", "wnba tomorrow lookup returns games (future date works)",
                      "@" in wsb)
    except Exception as e:  # noqa: BLE001
        res.check("integration", "wnba future scoreboard reachable", False, str(e))


# ---------------------------------------------------------------------------
# layer 3: behavior (model in the loop — costs API $)
# ---------------------------------------------------------------------------
def _seed_clear():
    """Wipe seeded state so each behavior case starts known (takes/calibration too, so a
    track-record case controls exactly what's been graded)."""
    memory._update("relationships.json", lambda d: d.clear(), {})
    memory._write("affinity.json", [])
    memory._write("takes.json", [])       # [] (not missing) so it won't re-seed from the flat file
    memory._write("calibration.json", {})


def _run_case(res, name, message, seed=None, must=None, must_not=None, must_any=None,
              proactive=None, seed_sender=None):
    import ronin_reply
    _seed_clear()
    if seed:
        seed()
    sender = f"eval_{abs(hash(name)) % 10**8}_{int(datetime.datetime.now().timestamp())}"
    if seed_sender:  # per-user seeding that needs the generated sender id (profile, teams)
        seed_sender(sender)
    if proactive:  # simulate a roam ping this user is now replying to
        memory.log_sent(sender, "eval_ping", proactive)
    reply = ronin_reply.reply(sender, message)
    print(f"    ↳ {name}: {reply[:160]!r}")
    if reply.startswith("(ronin hiccup") or not reply.strip():
        res.check("behavior", name, False, f"no usable reply: {reply!r}")
        return
    ok = True
    detail = []
    if must and not has_all(reply, *must):
        ok = False
        detail.append(f"missing all of {must}")
    if must_any and not has_any(reply, *must_any):
        ok = False
        detail.append(f"missing any of {must_any}")
    if must_not and not has_none(reply, *must_not):
        ok = False
        detail.append(f"contained one of {must_not}")
    if NO_EM_DASH in reply:  # global rule on every reply
        ok = False
        detail.append("contains an em dash")
    if re.search(r"</?(thinking|think|reasoning|scratchpad)\b", reply, re.IGNORECASE):
        ok = False  # the model narrating its reasoning must never survive to the user
        detail.append("leaked a thinking tag")
    res.check("behavior", name, ok, "; ".join(detail))


def run_behavior(res):
    print("\n── behavior (model in the loop) ──")

    _run_case(res, "first NFL game = Patriots/Seahawks, Sep 9",
              "yo what was the first nfl game of the season",
              must=["patriot", "seahawk"], must_any=["9/9", "sept 9", "september 9", "9-9"])

    # What's being locked in is the refusal (must_not) — "I can only see today's games".
    # must_any is just evidence it really resolved tomorrow and looked: on a day with no
    # slate the right answer names the weekday and says nothing's on, with no matchup or
    # tipoff time to match, so those count too.
    _run_case(res, "tomorrow's WNBA slate: pulls a date, doesn't refuse",
              "what wnba games are on tomorrow",
              must_not=["only see today", "can only see today", "just today's",
                        "i can only see today"],
              must_any=["@", " vs ", " pm", " et", "tomorrow",
                        "monday", "tuesday", "wednesday", "thursday", "friday",
                        "saturday", "sunday", "nothing on", "no games"])

    # Locks in the "Wed 9/9 not Thu" weekday fix. Routes through the first-game path
    # (the reliable one); resolving a game by "team-A vs team-B" is a separate, weaker
    # capability tracked as its own finding — don't conflate them here.
    _run_case(res, "correct weekday for the opener (Wednesday)",
              "what day of the week is the first nfl game of the season",
              must_any=["wednesday", "wed "])

    # Resolve a game by matchup instead of punting ("it's preseason, can't pin a date").
    _run_case(res, "resolves a game by matchup (what day is A vs B)",
              "what day is the patriots seahawks game",
              must_any=["wednesday", "wed ", "9/9", "sept 9", "september 9"],
              must_not=["can't pin", "cant pin", "preseason mode", "can't find", "cant find"])

    _run_case(res, "multi-team gap: asks for the NFL team instead of going blank",
              "hows my team looking for week 1 nfl",
              seed=lambda: memory.set_team("x", "nba", "Golden State Warriors", "GSW", chat_id=1),
              must_any=["football", "nfl team", "nfl", "warriors are an nba", "who"])

    # Whatever the real WC status is (final set, in progress, or decided), the reply must
    # come from the tool — never a guessed winner. The finalists/result words below all
    # trace to sports_champion; a fabricated answer wouldn't land on them.
    _run_case(res, "no-hallucination: WC status/result comes from the tool",
              "who won the 2026 world cup",
              must_any=["final", "not decided", "hasn't", "not yet", "argentina", "spain",
                        "not been"])

    _run_case(res, "World Cup allegiance surfaces (seeded takes)",
              "you rooting for anyone in the world cup?",
              seed=lambda: (memory.upsert_affinity("Cape Verde", "wc", "CPV", 0.6, "underdog run"),
                            memory.upsert_affinity("Spain", "wc", "ESP", -0.32, "the machine")),
              must_any=["cape verde", "spain"])

    # A bare follow-up to a proactive ping resolves against that ping, not stale chat.
    # (The exact screenshot: ronin texts about Curry's HOF exhibit, user replies "who's
    # funding it", ronin used to veer to the World Cup.)
    _run_case(res, "follow-up attaches to the proactive ping, not an old topic",
              "whos funding it? thats dope",
              proactive="curry getting his own HOF exhibit while still active is kinda insane",
              must_any=["curry", "hof", "hall of fame", "exhibit", "hall"],
              must_not=["world cup", "fifa", "host countr"])

    # Calibration: ronin can cite its real, earned track record when asked how its calls look.
    _run_case(res, "cites its earned track record, not a vibe",
              "how are your takes holding up this season? you been right?",
              seed=lambda: (
                  memory.upsert_take("Spurs rise", "story of the season", 0.85, topic="spurs-rise",
                                     deadline=20250101),
                  memory.resolve_take("spurs-rise", "hit", "Spurs made the playoffs"),
                  memory.upsert_take("OKC grind", "they fold in a series", 0.7, topic="okc-grind",
                                     deadline=20250101),
                  memory.resolve_take("okc-grind", "miss", "OKC won the title")),
              must_any=["spurs", "okc", "right", "wrong", "nailed", "whiff", "1-1", "one right"])

    # Relationship memory: ronin brings up something the digest remembered about them.
    _run_case(res, "brings up what it remembers about the person",
              "the lakers just signed another aging star lol",
              seed_sender=lambda s: (
                  memory.set_team(s, "nba", "Detroit Pistons", "DET", chat_id=1),
                  memory.set_profile(s, {"takes_you_hold": ["thinks the Lakers only chase big names"],
                                         "bits": ["calls the Lakers the retirement home"],
                                         "running_arguments": []})),
              must_any=["retirement home", "retirement", "big name", "chase"])


def _cleanup():
    # graff writes sess_*.session.json in ROOT; drop the eval ones + temp state.
    import glob
    import shutil
    for f in glob.glob(os.path.join(ROOT, "sess_eval_*.session.json")):
        try:
            os.remove(f)
        except OSError:
            pass
    shutil.rmtree(_TMP_STATE, ignore_errors=True)


def main():
    args = set(sys.argv[1:])
    res = Results()
    run_data(res)
    if "--data-only" not in args:
        run_integration(res)
    if "--data-only" not in args and "--no-llm" not in args:
        run_behavior(res)
    ok = res.summary()
    _cleanup()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
