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
import random
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


    # Season-opener labelling. The no-date scoreboard used to hand back a page of
    # FINISHED preseason games, so "what's the opener" had nothing to read and the
    # model fell back to its weights (it kept naming the Australia game). The answer
    # is now labelled in the tool output. These pin the two ways that can go wrong.
    def _ev(iso, stype, state="pre", rec="0-0"):
        # Real ESPN events always carry records, and _season_started reads them, so the
        # fixtures have to as well or they test a shape that never occurs.
        return {"date": iso, "season": {"type": stype},
                "status": {"type": {"state": state}},
                "competitions": [{"competitors": [
                    {"records": [{"type": "total", "summary": rec}]}]}]}

    pre_then_reg = [_ev("2026-08-20T00:00Z", 1), _ev("2026-08-27T00:00Z", 1),
                    _ev("2026-09-10T00:20Z", 2), _ev("2026-09-11T00:35Z", 2)]
    op = espn._find_opener("nfl", pre_then_reg)
    res.check("data", "opener: first REGULAR-season game, not the first game",
              op is not None and op["date"] == "2026-09-10T00:20Z",
              f"got {op and op.get('date')!r}")
    # The regression that shipped in the first draft: mid-season, every upcoming game
    # is season type 2, so a naive "first type-2 game" call labels an ordinary
    # Wednesday game as the season opener.
    mid = [_ev("2026-08-19T23:30Z", 2, rec="14-9"), _ev("2026-08-20T23:30Z", 2, rec="8-15")]
    res.check("data", "opener: None once the regular season is under way",
              espn._find_opener("wnba", mid) is None,
              f"got {espn._find_opener('wnba', mid)!r}")
    res.check("data", "opener: None on an empty slate", espn._find_opener("nfl", []) is None)
    b = espn._banner("nfl", pre_then_reg)
    res.check("data", "banner: names NEXT GAME and SEASON OPENER separately",
              len(b) == 2 and b[0].startswith("NEXT GAME:")
              and b[1].startswith("SEASON OPENER"), str(b))
    res.check("data", "banner: mid-season gets NEXT GAME only",
              [x.split(":")[0] for x in espn._banner("wnba", mid)] == ["NEXT GAME"],
              str(espn._banner("wnba", mid)))
    # Tool output feeds straight into replies, and the persona bans em dashes.
    res.check("data", "banner: no em dash in tool output",
              all("\u2014" not in x for x in b), str(b))


    # One team, not a list. Ronin's rolled team is per-user; the affinities from reflect()
    # are GLOBAL, so every user saw the same top-scored teams. With the affinity block first
    # and framed "root for these", the model led with the global teams and demoted its own
    # rolled team to an afterthought ("spurs and pistons, no debate ... and lowkey i'm riding
    # with the hornets too" - real screenshot, 2026-08-20). These pin the fix.
    import ronin_reply as _rr
    _saved = (memory.top_affinities, memory.get_takes, memory.get_record,
              memory.user_teams, memory.get_profile, memory.recent_sent)
    try:
        memory.top_affinities = lambda *a, **k: (
            [{"team": "San Antonio Spurs", "league": "nba", "stance": "62-20"},
             {"team": "Minnesota Lynx", "league": "wnba", "stance": "19-6"}],
            [{"team": "New York Knicks", "league": "nba", "stance": "ring-chasing"}])
        memory.get_takes = lambda: []
        memory.get_record = lambda: {"accuracy": None, "hits": 0, "misses": 0}
        memory.user_teams = lambda u: []
        memory.get_profile = lambda u: {}
        memory.recent_sent = lambda *a, **k: []
        import character as _ch
        _ens = _ch.ensure
        try:
            _ch.ensure = lambda uid: {"traits": ["methodical"], "team": "Charlotte Hornets",
                                      "league": "nba", "reasoning": "grimy young rebuild"}
            pr = _rr._load_system_prompt("u_one_team")
            i_team = pr.find("## Your taste and YOUR team")
            i_read = pr.find("## Your read on other teams")
            res.check("data", "prompt: rolled team block comes BEFORE the global read",
                      0 <= i_team < i_read, f"team@{i_team} read@{i_read}")
            res.check("data", "prompt: global block reframed as opinion, not allegiance",
                      "NOT allegiance" in pr and "root for these" not in pr)
            res.check("data", "prompt: same-league LOVE is dropped (nothing rivals your team)",
                      "San Antonio Spurs" not in pr[i_read:], pr[i_read:i_read + 200])
            res.check("data", "prompt: same-league DISLIKE is kept (a rival is good for a fan)",
                      "New York Knicks" in pr[i_read:])
            res.check("data", "prompt: other-league read survives the filter",
                      "Minnesota Lynx" in pr[i_read:])
            # No character (roll failed, or RONIN_CHARACTER=0): the old global framing stands,
            # otherwise a user with no rolled team is told to name a team that doesn't exist.
            _ch.ensure = lambda uid: None
            pr2 = _rr._load_system_prompt("u_no_char")
            res.check("data", "prompt: no character falls back to the old allegiance framing",
                      "root for these" in pr2 and "San Antonio Spurs" in pr2)
        finally:
            _ch.ensure = _ens
    finally:
        (memory.top_affinities, memory.get_takes, memory.get_record,
         memory.user_teams, memory.get_profile, memory.recent_sent) = _saved


    # Affinity stances are GLOBAL (every user reads the same ones), so "my Spurs" makes ronin
    # claim a team that isn't the one he rides with. That's how "closed my spurs out 4-1"
    # reached a user whose ronin rides with the Pistons. Prompt rules hold only
    # probabilistically, so the write path strips it deterministically.
    import roam as _roam
    _T = {"San Antonio Spurs", "New York Knicks", "Oklahoma City Thunder"}
    res.check("data", "stance: 'my Spurs' -> 'the Spurs'",
              _roam._depossess("closed my Spurs out 4-1", _T) == "closed the Spurs out 4-1")
    res.check("data", "stance: lowercase 'my spurs' too (ronin types lowercase)",
              _roam._depossess("my spurs got run off", _T) == "the spurs got run off")
    res.check("data", "stance: full team name, longest match wins",
              _roam._depossess("beat my San Antonio Spurs", _T) == "beat the San Antonio Spurs")
    res.check("data", "stance: 'my take' / 'my guy' are NOT team claims, left alone",
              _roam._depossess("that's my take and my guy is fine", _T)
              == "that's my take and my guy is fine")
    res.check("data", "stance: no team named -> unchanged",
              _roam._depossess("64-18 is the real best record", _T)
              == "64-18 is the real best record")


    # The window between the last preseason game and the opener: every upcoming game is
    # season type 2, but the season HASN'T started. The first version returned None here and
    # silently stopped labelling the opener during the exact week it matters most. Records are
    # the signal - before kickoff every team is 0-0.
    res.check("data", "record '0-0' / '0-0-0' reads as no games played",
              espn._record_is_blank("0-0") and espn._record_is_blank("0-0-0")
              and espn._record_is_blank(""))
    res.check("data", "record '1-0' / '12-4' reads as season under way",
              not espn._record_is_blank("1-0") and not espn._record_is_blank("12-4"))

    def _ev2(iso, rec):
        return {"date": iso, "season": {"type": 2},
                "status": {"type": {"state": "pre"}},
                "competitions": [{"competitors": [
                    {"records": [{"type": "total", "summary": rec}]}]}]}

    unplayed = [_ev2("2026-09-10T00:20Z", "0-0"), _ev2("2026-09-11T00:35Z", "0-0")]
    played = [_ev2("2026-10-10T00:20Z", "3-1"), _ev2("2026-10-11T00:35Z", "0-0")]
    res.check("data", "opener still labelled after preseason ends, before kickoff",
              espn._find_opener("nfl", unplayed) is unplayed[0])
    res.check("data", "no opener once a regular-season game has been played",
              espn._find_opener("nfl", played) is None)

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
    _check_grade_pass(res)
    _check_relationship_memory(res)
    _check_web_parser(res)
    _check_sentiment_sweep(res)
    _check_roam_retry(res)
    _check_shared_cursor(res)
    _check_thinking_strip(res)
    _check_judge_recall(res)
    _check_player_card(res)
    _check_character(res)


def _check_character(res):
    """Per-user personality: three rolled traits + one signature team, layered over the
    global persona. The roll must never contradict itself, must never be re-rolled (it's
    meant to feel like fate), and must survive a digest pass."""
    import character as C
    import ronin_reply

    # Coherence: one trait per axis AND no cross-axis contradiction. Same-axis pairs can't
    # co-occur by construction; the cross-axis ones (methodical+chaotic) needed a rule.
    bad = []
    for i in range(400):
        tr = C.roll_traits(random.Random(i))
        axes = [g for t in tr for g, d in C.VOCAB.items() if t in d]
        if len(tr) != C.TRAIT_COUNT or len(set(axes)) != len(axes) or not C._coherent(tr):
            bad.append(tr)
    res.check("data", "rolled traits never contradict (one per axis, no conflicting pair)",
              not bad, f"{len(bad)} bad rolls e.g. {bad[:2]}")
    res.check("data", "every conflicting pair is actually rejected",
              all(not C._coherent(list(p)) for p in C.CONFLICTS))
    res.check("data", "the vocab is big enough to not clone everyone",
              len({tuple(C.roll_traits(random.Random(i))) for i in range(400)}) > 50)

    # Write-once: the roll is fate, not a preference. A second call must not change it.
    memory._write("relationships.json", {})
    memory.set_character("c1", ["electric"], "nba", "Miami Heat", "MIA", "why")
    res.check("data", "a character round-trips",
              memory.get_character("c1").get("team") == "Miami Heat")
    res.check("data", "set_character refuses to re-roll an existing character",
              memory.set_character("c1", ["steady"], "nba", "Utah Jazz", "UTA") is False
              and memory.get_character("c1")["team"] == "Miami Heat")
    res.check("data", "an incomplete character is rejected outright",
              memory.set_character("c2", [], "nba", "", "") is False)

    # The digest REPLACES the whole profile dict every ~4h. A character stored inside it
    # would be silently wiped; this is why it's a sibling key, and this pins that.
    memory.set_profile("c1", {"bits": ["always says 'ship it'"]})
    res.check("data", "a digest pass can't wipe the character",
              memory.get_character("c1").get("team") == "Miami Heat"
              and memory.get_profile("c1").get("bits"))

    # The lens is LAYERED: the global persona and its values must still be present.
    memory.set_team("c1", "nba", "Golden State Warriors", "GSW", chat_id=1)
    sp = ronin_reply._load_system_prompt("c1")
    res.check("data", "the taste lens reaches the chat prompt",
              "Miami Heat" in sp and "electric" in sp)
    res.check("data", "the lens layers OVER the global persona, never replacing it",
              "never blur facts and opinions" in sp and "Golden State Warriors" in sp)
    res.check("data", "ronin's team is marked as HIS, distinct from theirs",
              "separate from whoever they follow" in sp)
    # Same intent, stronger: it must also be the ONLY team, so "who you riding with" can't
    # come back as a ranked list of the global affinities.
    res.check("data", "ronin rides with exactly ONE team, not a list",
              "ONLY team you ride with" in sp and "name this ONE team" in sp)

    # The Dockerfile COPY is an explicit allowlist, so a NEW top-level module is simply
    # absent from the image and the feature silently doesn't exist in production. That's
    # exactly what happened to character.py on its first deploy — caught only because the
    # in-container check ran. Assert every runtime module is actually shipped.
    with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as f:
        dockerfile = f.read()
    runtime_mods = [n for n in os.listdir(ROOT)
                    if n.endswith(".py") and not n.startswith(("test_", "_"))]
    missing = [n for n in runtime_mods if n not in dockerfile]
    res.check("data", "every top-level module is COPYed into the image",
              not missing, f"not in Dockerfile: {missing}")

    # Kill switch: flipping it off returns everyone to the single global ronin.
    real = C.ENABLED
    try:
        C.ENABLED = False
        res.check("data", "kill switch removes the lens and blocks new rolls",
                  C.prompt_block(memory.get_character("c1")) == "" and C.ensure("c9") == {})
    finally:
        C.ENABLED = real


def _check_judge_recall(res):
    """A developing story gets pinged more than once (right), but the judge composed each
    ping against memory.recent_sent's 48h default (wrong). The Don Nelson memorial ping on
    2026-08-14 could not see the death ping from 2026-08-09 (~114h earlier), so it
    re-explained nellieball to someone who'd already been told. The judge needs a window
    measured in the life of a STORY, not the life of a chat follow-up."""
    import roam
    memory._write("outbound.json", {})
    now = time.time()
    memory._update("outbound.json", lambda d: d.setdefault("sent", []).extend([
        {"uid": "mJ", "key": "nelson-dies", "text": "don nelson passed away at 86, nellieball",
         "at": now - 114 * 3600},                      # the Aug 9 ping: ~4.75 days back
        {"uid": "mJ", "key": "cup", "text": "nba cup schedule's out", "at": now - 43 * 3600},
    ]), {})

    # The chat path's window is deliberately unchanged — 48h is right for resolving a
    # follow-up. This asserts the old behavior still holds there, i.e. the bug was real.
    chat_texts = [p["text"] for p in memory.recent_sent("mJ")]
    res.check("data", "chat's 48h window still drops the 5-day-old ping (unchanged)",
              len(chat_texts) == 1 and "cup" in chat_texts[0])

    recalled = roam._recent_texts("mJ", n=roam.JUDGE_RECALL_N,
                                 within_secs=roam.JUDGE_RECALL_SECS)
    res.check("data", "judge recall reaches back past 48h to the same storyline",
              any("nellieball" in t for t in recalled), f"recalled {recalled}")

    # Drive the real _judge body, stubbing only the graff call, and assert the old ping
    # actually reaches the prompt. This is what fails against the pre-fix code.
    real_run = roam.subprocess.run
    seen = {}

    class _Out:
        returncode = 0
        stdout = '{"notable": false, "message": "", "take": null}'
        stderr = ""

    try:
        def fake_run(cmd, **kw):
            seen["prompt"] = cmd[-1]
            return _Out()
        roam.subprocess.run = fake_run
        roam._judge("mJ", "Golden State Warriors", "nba",
                    {"key": "memorial", "headline": "Don Nelson memorialized in Dallas",
                     "desc": "Dirk and Carlisle attended."})
    finally:
        roam.subprocess.run = real_run
    res.check("data", "the judge prompt carries what it already said about the storyline",
              "nellieball" in seen.get("prompt", ""))


def _check_player_card(res):
    """Player facts had NO tool behind them, so ronin called RJ Harvey a rookie in his
    second season. ESPN's experience.years is the season being ENTERED, 1-indexed
    (verified: Harvey/2025 draft = 2, Nix/2024 = 3, Sutton/2018 = 9), so <= 1 is a real
    rookie. Getting this backwards would just invert the bug, hence the pinned check."""
    line = espn._experience_line
    res.check("data", "experience 1 reads as a rookie",
              "ROOKIE" in line(1) and "NOT a rookie" not in line(1))
    res.check("data", "experience 0 (undrafted, no accrued season) reads as a rookie",
              "ROOKIE" in line(0))
    res.check("data", "experience 2 reads as a 2nd-season non-rookie (the RJ Harvey case)",
              "2nd season" in line(2) and "NOT a rookie" in line(2))
    res.check("data", "experience 9 ordinal is right", "9th season" in line(9))
    res.check("data", "experience 11 ordinal is right (teen suffix)", "11th season" in line(11))
    res.check("data", "missing experience refuses instead of guessing",
              "not listed" in line(None) and "do NOT guess" in line(None))
    res.check("data", "sports_player is registered and reachable through the MCP registry",
              "sports_player" in espn.TOOLS
              and callable(espn.TOOLS["sports_player"]["fn"]))
    res.check("data", "sports_roster is registered and reachable through the MCP registry",
              "sports_roster" in espn.TOOLS
              and callable(espn.TOOLS["sports_roster"]["fn"]))
    res.check("data", "roster rows use the same 1-indexed rookie convention",
              espn._exp_short(0) == "ROOKIE" and espn._exp_short(1) == "ROOKIE"
              and espn._exp_short(2) == "2nd yr" and espn._exp_short(None) == "exp n/a")
    res.check("data", "ordinals are right across the teen suffixes",
              [espn._ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21)]
              == ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st"])
    # The firewall allowlists mcp__espn__*, so a new espn tool needs no firewall change —
    # but assert it, because a rule change there would silently disarm this fix.
    fw = open(os.path.join(ROOT, ".harness", "tool-firewall.sh"), encoding="utf-8").read()
    res.check("data", "tool firewall still allows the espn server wholesale",
              "mcp__espn__*" in fw)


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
    scope = "mV:nba:detroit pistons"  # mood is scoped per user, not per team
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


def _check_grade_pass(res):
    """Drive the whole grade() path, not just the memory helpers, with only the graff call
    stubbed — so _grade_one runs its real body. That body once referenced time.strftime with
    no `import time` in roam.py, which crashed the grader on the first due take every time;
    the memory-only calibration test never exercised it, so nothing caught it."""
    import types
    import roam
    real_sub = roam.subprocess

    def fake_run(verdict):
        resolved = "true" if verdict else "false"
        payload = ('{"resolved": %s, "verdict": "%s", "note": "self-test"}'
                   % (resolved, verdict or "unclear"))
        return types.SimpleNamespace(returncode=0, stdout=payload, stderr="")

    try:
        # A real resolve: the grader reaches _grade_one's body (the crashing line) and settles.
        # grade() drains every due take in one pass, so seed the defer case only afterward.
        memory.upsert_take("Spain WC", "Spain wins it", 0.6, topic="grade-hit-test",
                           deadline=20250101, resolves_when="WC final")
        roam.subprocess = types.SimpleNamespace(
            run=lambda *a, **k: fake_run("hit"), TimeoutExpired=real_sub.TimeoutExpired)
        roam.grade(dry_run=False)
        hit = [t for t in memory.get_takes() if t["topic"] == "grade-hit-test"][0]
        res.check("data", "grade() runs _grade_one end to end and settles a due take",
                  hit["status"] == "hit" and memory.get_record()["hits"] >= 1)
        # An unclear verdict defers rather than settling, and doesn't touch the record.
        memory.upsert_take("Knicks ceiling", "conf finals", 0.6, topic="grade-defer-test",
                           deadline=20250101, resolves_when="playoff result")
        before = memory.get_record()["hits"] + memory.get_record()["misses"]
        roam.subprocess = types.SimpleNamespace(
            run=lambda *a, **k: fake_run(None), TimeoutExpired=real_sub.TimeoutExpired)
        roam.grade(dry_run=False)
        defer = [t for t in memory.get_takes() if t["topic"] == "grade-defer-test"][0]
        after = memory.get_record()["hits"] + memory.get_record()["misses"]
        res.check("data", "an unclear grade defers the take and leaves the record untouched",
                  defer["status"] == "open" and defer["deadline"] > 20250101 and after == before)
    finally:
        roam.subprocess = real_sub


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


def _check_shared_cursor(res):
    """Two people following the SAME team must both hear its news. The cursor and the mood
    were keyed by team alone but read/written inside the per-user loop, so the first user's
    pass consumed the news and the second was silently starved."""
    import roam
    from mcp import fan
    memory._update("relationships.json", lambda d: d.clear(), {})  # isolate: run_once scans all
    memory._write("cursor.json", {})
    memory._write("mood.json", {})
    heads = [{"key": "s1", "headline": "A", "desc": "d"}]
    real_heads, real_judge = espn.recent_headlines, roam._judge
    real_fan, real_vibe, real_send = fan.fan_sentiment, roam._vibe_judge, roam._tg_send
    sends = []
    try:
        espn.recent_headlines = lambda l, t, limit=None: list(heads)
        roam._tg_send = lambda chat_id, text: sends.append(chat_id)
        memory.set_team("uA", "nba", "Boston Celtics", "BOS", chat_id=101)
        memory.set_team("uB", "nba", "Boston Celtics", "BOS", chat_id=202)
        roam._judge = lambda *a: {"notable": True, "message": "celtics news",
                                  "take": {"subject": "Celtics", "stance": "up",
                                           "confidence": 0.6}}
        roam.run_once(dry_run=False)                     # cold start: both baseline, silent
        res.check("data", "both followers of a team baseline silently on cold start", not sends)

        heads.append({"key": "s2", "headline": "B", "desc": "d"})
        roam.run_once(dry_run=False)
        res.check("data", "two users on the same team BOTH get its news (shared cursor)",
                  sorted(sends) == [101, 202], f"reached {sorted(sends)}")

        # Same defect on the mood axis: a judge only calls a shift when the vibe differs
        # from the mood it last saw, so a shared prior let the first user's write mask it.
        sends.clear()
        fan.fan_sentiment = lambda lg, tp=None: "REDDIT: ... BLUESKY: ..."
        memory._update("relationships.json", lambda d: [  # reset the news pass's min-gap throttle
            d[u].pop("last_proactive", None) for u in d], {})
        roam._vibe_judge = lambda uid, team, league, vibe, prior: {
            "mood": "steady", "shifted": False, "notable": False, "message": ""}
        roam.sentiment_sweep(dry_run=False)              # baseline each user's mood
        SOUR = "fans turning on the coach"
        roam._vibe_judge = lambda uid, team, league, vibe, prior: {
            "mood": SOUR, "shifted": prior != SOUR, "notable": prior != SOUR,
            "message": "room's souring"}
        roam.sentiment_sweep(dry_run=False)
        res.check("data", "two users on the same team BOTH get the vibe shift (shared mood)",
                  sorted(sends) == [101, 202], f"reached {sorted(sends)}")
    finally:
        espn.recent_headlines, roam._judge = real_heads, real_judge
        fan.fan_sentiment, roam._vibe_judge, roam._tg_send = real_fan, real_vibe, real_send


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

    # Em/en dashes are banned by the persona, but a prompt rule is probabilistic — cheaper
    # models leak them. _normalize_voice enforces it deterministically at the boundary.
    n = ronin_reply._normalize_voice
    res.check("data", "a spaced em dash becomes a comma",
              n("Curry's the pick — dude bent the sport") == "Curry's the pick, dude bent the sport")
    res.check("data", "a tight em dash and an en dash both go",
              n("first-ballot—no debate, 2020–2021 run") == "first-ballot, no debate, 2020, 2021 run"
              and NO_EM_DASH not in n("a—b–c"))
    res.check("data", "a trailing dash leaves no dangling comma",
              n("wait —") == "wait")
    res.check("data", "a dash before punctuation doesn't double it up",
              n("he's done — .") == "he's done.")
    res.check("data", "ordinary hyphens and prose are untouched",
              n("a well-coached, run-first team") == "a well-coached, run-first team")


def _check_roam_retry(res):
    """A judge timeout used to lose the headline forever: roam marked every new item seen
    up front, so the retry never came. Stub the judge to fail, then recover."""
    import roam
    heads = [{"key": "r1", "headline": "A", "desc": "d"}]
    real_headlines, real_judge, real_send = espn.recent_headlines, roam._judge, roam._tg_send
    espn.recent_headlines = lambda l, t, limit=None: list(heads)
    roam._tg_send = lambda chat_id, text: None
    scope = "mR:nba:phoenix suns"  # news cursor is scoped per user, not per team
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
def _is_pre(ev):
    return ((ev.get('status') or {}).get('type', {})).get('state') == 'pre'


def run_integration(res):
    print("\n── integration (live ESPN, no LLM) ──")
    try:
        # Assert the STRUCTURE (earliest-first ordering + a weekday), not a specific matchup:
        # the earliest game changes with the calendar (preseason enters the window in late
        # July), so a hardcoded "NE @ SEA Wed" goes stale. Season runs Aug->Feb, so map
        # months to a season ordinal to compare dates across the Dec->Jan rollover.
        def _season_md(line):
            m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", line)
            if not m:
                return None
            mo, d = int(m.group(1)), int(m.group(2))
            return (mo if mo >= 8 else mo + 12, d)
        # Two shapes come back and both are correct. When games are on TODAY the header
        # says "(today)" and the lines carry no date or weekday (they're live/final right
        # now, so there's nothing to disambiguate). Only when the window is future-dated
        # does each line get a weekday + M/D. The old check assumed the future-dated shape
        # unconditionally, so it went red on any day with a live slate — a test bug, not a
        # product one. Assert whichever contract actually applies.
        sb = espn.scoreboard("nfl")
        # Skip the labelled banner: it answers "next game" and "season opener" up top,
        # so it deliberately is NOT in kickoff order. Order applies to the slate below it.
        games = [ln for ln in sb.splitlines()
                 if "@" in ln and not ln.startswith(("NEXT GAME", "SEASON OPENER"))]
        # THREE slate shapes are all correct, and the assertion has to survive all of them:
        # all-future (every line dated), all-today (live/final, no dates to disambiguate),
        # and MIXED - last night's finals sitting above tomorrow's dated games, which is
        # what a preseason morning looks like. Asserting on the first line assumes the
        # all-future shape and goes red on the other two. The real invariant is narrower:
        # whichever lines DO carry a date must be earliest-first and carry a weekday.
        dated = [(ln, _season_md(ln)) for ln in games]
        dated = [(ln, md) for ln, md in dated if md]
        if dated:
            keys = [md for _, md in dated]
            earliest_first = all(keys[i] <= keys[i + 1] for i in range(len(keys) - 1))
            has_weekday = any(w in dated[0][0] for w in
                              ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))
            res.check("integration", "nfl scoreboard: dated games earliest-first, with weekday",
                      has_weekday and earliest_first, dated[0][0])
        else:
            res.check("integration", "nfl scoreboard: today's slate returns games",
                      bool(games) and "today" in sb.splitlines()[0].lower(),
                      games[0] if games else "")
    except Exception as e:  # noqa: BLE001
        res.check("integration", "nfl scoreboard reachable", False, str(e))


    # The opener, verified against a different ESPN endpoint than the one scoreboard()
    # uses (regular-season week 1) so this can't pass by agreeing with itself.
    try:
        wk1 = espn._get("https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
                        "scoreboard?seasontype=2&week=1")
        evs = sorted(wk1.get("events", []), key=lambda e: e.get("date", ""))
        truth = evs[0] if evs else None
        abbrs = []
        if truth:
            for c in (truth.get("competitions") or [{}])[0].get("competitors", []):
                abbrs.append(((c.get("team") or {}).get("abbreviation") or "").upper())
        sb = espn.scoreboard("nfl")
        line = next((l for l in sb.splitlines() if l.startswith("SEASON OPENER")), "")
        if truth and any(_is_pre(e) for e in [truth]):
            res.check("integration", "nfl opener line names ESPN's week-1 opener",
                      bool(line) and all(a and a in line.upper() for a in abbrs),
                      f"want {abbrs}, line={line!r}")
        else:
            res.check("integration", "nfl opener: regular season started, no opener claimed",
                      not line, line)
    except Exception as e:  # noqa: BLE001
        res.check("integration", "nfl opener check reachable", False, str(e))

    # Player grounding against the live roster. Assert the SHAPE plus one fact that can't
    # drift the wrong way: a player's season count only ever goes up, so someone already
    # past their rookie year can never read as a rookie again.
    try:
        card = espn.player("RJ Harvey")
        res.check("integration", "sports_player: RJ Harvey resolves with real roster fields",
                  has_all(low(card), "rj harvey", "broncos", "running back"), card[:90])
        res.check("integration", "sports_player: a 2nd-season back is NOT called a rookie",
                  "NOT a rookie" in card and "ROOKIE," not in card, card)
    except Exception as e:  # noqa: BLE001
        res.check("integration", "sports_player reachable", False, str(e))

    try:
        # Non-NFL rosters come back in a different shape (flat list vs position groups);
        # this is what caught every non-NFL lookup silently returning "unverified".
        curry = espn.player("Stephen Curry", "nba")
        res.check("integration", "sports_player: nba roster shape parses (flat list)",
                  has_all(low(curry), "curry", "warriors") and "season" in curry, curry[:90])
    except Exception as e:  # noqa: BLE001
        res.check("integration", "sports_player nba reachable", False, str(e))

    # The roster tool — the grounded answer to "who else is in that backfield", which is
    # where the volunteered-teammate hallucination came from.
    try:
        rb = espn.roster("nfl", "denver broncos", "RB")
        groups = [ln for ln in rb.splitlines() if ln and not ln.startswith(("  ", "("))]
        # Position filtering must be exact: "rb" is a substring of both "quarterback" and
        # "cornerback", so a naive `in` test returns the QBs and CBs too. It did.
        res.check("integration", "roster position filter is exact, not substring",
                  len(groups) == 2 and "Running Back" in groups[1]
                  and not any("back" in g.lower() and "running" not in g.lower()
                              for g in groups[1:]), str(groups))
        res.check("integration", "roster rows carry age + experience for grounding",
                  "RJ Harvey" in rb and "2nd yr" in rb, rb[:120])
    except Exception as e:  # noqa: BLE001
        res.check("integration", "sports_roster reachable", False, str(e))

    try:
        full = espn.roster("nfl", "denver broncos")
        # A truncated roster must SAY it's truncated — a silent cut reads as the whole team.
        res.check("integration", "an over-cap roster announces what it dropped",
                  ("shown" in full and "of" in full.split("shown")[0][-12:])
                  or full.count("\n") < espn.ROSTER_CAP, full.splitlines()[-1])
        res.check("integration", "an unknown position lists the real ones instead of empty",
                  "nobody listed at" in espn.roster("nba", "detroit pistons", "goalie"))
    except Exception as e:  # noqa: BLE001
        res.check("integration", "sports_roster edge cases", False, str(e))

    try:
        miss = espn.player("Zzzq Notarealplayer")
        res.check("integration", "sports_player refuses an unknown name instead of inventing",
                  has_all(low(miss), "no player matching"), miss[:90])
    except Exception as e:  # noqa: BLE001
        res.check("integration", "sports_player unknown-name path", False, str(e))

    try:
        # Assert the CONTRACT, not a calendar state. Once the next UCL season kicks off,
        # champion() correctly reports the CURRENT season as undecided instead of returning
        # last season's winner - so hardcoding "PSG" turns red on the season rollover and
        # looks like a parser break. Either shape is right; a wrong-shaped answer is not.
        ch = espn.champion("ucl")
        res.check("integration", "champion(ucl): names a winner or says the season is open",
                  has_any(ch, "paris", "psg") or has_any(ch, "isn't decided", "not decided"),
                  ch[:100])
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
              proactive=None, seed_sender=None, character=None):
    import ronin_reply
    _seed_clear()
    if seed:
        seed()
    sender = f"eval_{abs(hash(name)) % 10**8}_{int(datetime.datetime.now().timestamp())}"
    if seed_sender:  # per-user seeding that needs the generated sender id (profile, teams)
        seed_sender(sender)
    # Every case uses a fresh sender, so without this each one would trigger a live
    # character roll — an extra graff call per case, testing nothing the case is about.
    # Seed one instead: the taste lens is still in the prompt (realistic), for free.
    # `character="roll"` opts a case into exercising the real roll.
    if character != "roll":
        memory.set_character(sender, ["defense-first", "home-grown", "steady"],
                             "nba", "San Antonio Spurs", "SA",
                             "young core that actually guards, built not bought")
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

    # "regular season" is load-bearing: once preseason enters the scoreboard window (late
    # July), the literal first game is the Hall of Fame Game, so a bare "first game" no longer
    # means the opener. The test's real intent is opener resolution without date-guessing.
    _run_case(res, "first NFL regular-season game = Patriots/Seahawks, Sep 9",
              "yo what's the first nfl regular season game of the year",
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
    # "regular season" again: the literal first game is now the preseason HOF game (a
    # Thursday), so without qualifying it the correct weekday is Thursday, not the opener's.
    _run_case(res, "correct weekday for the opener (Wednesday)",
              "what day of the week is the first nfl regular season game",
              must_any=["wednesday", "wed "])

    # The exact miss this bug always produced: picking the Australia game (Rams/Niners,
    # Thu 9/10) as the opener over the real one (Patriots/Seahawks, Wed 9/9). Asked BARE,
    # without "regular season" steering it — the no-date slate now labels the opener, so
    # the answer no longer depends on the model counting down the list correctly.
    _run_case(res, "bare 'first game' doesn't name the Australia game as the opener",
              "whats the first nfl game of the season",
              must_not=["rams", "49ers", "niners"])

    # The screenshot case: asked who it rides with, ronin named two GLOBAL teams and buried
    # its own. Every case seeds a Spurs character, so the answer must be the Spurs alone.
    _run_case(res, "names exactly ONE team when asked who it rides with",
              "who you riding with this season",
              must=["spurs"],
              must_not=["pistons", "thunder", "lowkey i'm riding", "also riding"])

    # Resolve a game by matchup instead of punting ("it's preseason, can't pin a date").
    _run_case(res, "resolves a game by matchup (what day is A vs B)",
              "what day is the patriots seahawks game",
              must_any=["wednesday", "wed ", "9/9", "sept 9", "september 9"],
              must_not=["can't pin", "cant pin", "preseason mode", "can't find", "cant find"])

    # Per-user personality, with a REAL roll (character="roll"): his own team must come
    # from the rolled character, and it must not be the user's team. must_not bans the
    # user's own squad as the answer to "who do YOU root for" — the whole point is that
    # he has a separate one to argue with them about.
    _run_case(res, "his team is his own, rolled, and not the user's",
              "who do you actually root for?",
              seed_sender=lambda s: memory.set_team(s, "nba", "Golden State Warriors",
                                                    "GSW", chat_id=1),
              character="roll",
              must_not=["i root for the warriors", "warriors, all day", "i'm a warriors",
                        "im a warriors", "i ride with the warriors"])

    # The live bug, verbatim: asked to rate a fantasy draft, ronin called RJ Harvey a
    # "rookie" in his second season (2026-08-14). There was no player tool at all then, so
    # the claim came straight from weights — and the model is confident about it, which is
    # why this needs a model-in-the-loop case and not just the data check on the formatter.
    # Assert the corrected fact appears rather than banning "rookie": the right answer
    # often contains the word ("he's not a rookie anymore").
    _run_case(res, "player experience is looked up, not recalled (the RJ Harvey case)",
              "quick take on rj harvey for my fantasy team",
              must_any=["second season", "2nd season", "second year", "2nd year",
                        "not a rookie", "no longer a rookie", "sophomore", "year two"],
              must_not=["he's a rookie", "hes a rookie", "rookie back", "rookie rb",
                        "as a rookie he", "rookie season for him",
                        # grounding must stay invisible: he's a guy who follows it, not a
                        # bot reading a readout. First pass leaked "per the tool" 2 of 3.
                        "per the tool", "the tool", "according to my data", "my data",
                        "let me check", "looked it up"])

    # The volunteered-teammate leak: ronin used to place Javonte Williams (a Cowboy) in
    # Denver's backfield as color. Expectations are derived from the LIVE roster rather
    # than hardcoded, so this doesn't rot the moment Denver signs someone.
    try:
        _rb = espn.roster("nfl", "denver broncos", "RB")
        _surnames = [ln.split("—")[0].split()[-1].lower()
                     for ln in _rb.splitlines() if ln.startswith("  ")]
        _run_case(res, "names a REAL teammate, not a remembered one",
                  "who else is in the broncos backfield with rj harvey?",
                  must_any=[s for s in _surnames if s and s != "harvey"],
                  # Pinned historical failure: he left Denver, and ronin kept him there.
                  must_not=["javonte"])
    except Exception as e:  # noqa: BLE001
        res.check("behavior", "roster-grounded teammate case set up", False, str(e))

    # It must ASK which NFL team, not answer about the NBA team it happens to have. The
    # must_any is the ask itself ("?" covers phrasings the enumerated list kept missing —
    # "which squad is yours?" used to fail this); must_not is the real failure mode, using
    # the Warriors as if they were the NFL team.
    _run_case(res, "multi-team gap: asks for the NFL team instead of going blank",
              "hows my team looking for week 1 nfl",
              seed=lambda: memory.set_team("x", "nba", "Golden State Warriors", "GSW", chat_id=1),
              must_any=["football", "nfl team", "nfl", "warriors are an nba", "who", "which", "?"],
              must_not=["warriors are looking", "warriors look good", "warriors open week"])

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
