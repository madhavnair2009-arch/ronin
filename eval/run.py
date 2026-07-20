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
import sys
import tempfile

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
    """Wipe seeded relationships/affinity so each behavior case starts known."""
    memory._update("relationships.json", lambda d: d.clear(), {})
    memory._write("affinity.json", [])


def _run_case(res, name, message, seed=None, must=None, must_not=None, must_any=None):
    import ronin_reply
    _seed_clear()
    if seed:
        seed()
    sender = f"eval_{abs(hash(name)) % 10**8}_{int(datetime.datetime.now().timestamp())}"
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
    res.check("behavior", name, ok, "; ".join(detail))


def run_behavior(res):
    print("\n── behavior (model in the loop) ──")

    _run_case(res, "first NFL game = Patriots/Seahawks, Sep 9",
              "yo what was the first nfl game of the season",
              must=["patriot", "seahawk"], must_any=["9/9", "sept 9", "september 9", "9-9"])

    _run_case(res, "tomorrow's WNBA slate: pulls a date, doesn't refuse",
              "what wnba games are on tomorrow",
              must_not=["only see today", "can only see today", "just today's",
                        "i can only see today"],
              must_any=["@", " vs ", " pm", " et", "tomorrow"])

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
