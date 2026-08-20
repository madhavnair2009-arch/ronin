"""Per-user character creation: roll a taste, match it to a team.

Every user gets their own ronin. The VOICE and the VALUE FLOOR stay global — every ronin
is the same guy, with the same persona and the same seeded values (player development,
defense, underdogs; against tanking and ring-chasing). What's personal is a *lens* layered
on top: three rolled taste descriptors and one signature team those descriptors picked.

Cost shape is the whole point. This is ONE cheap graff call, ONCE per user, forever. There
is deliberately no per-user reflection: `roam.reflect()` stays O(leagues) and keeps forming
the global allegiances that give ronin live opinions. The rolled team never changes (it's
supposed to feel like fate, so there's no reroll), but what he SAYS about it stays current
off that global news/reflect loop.

The emergent bit: his rolled team is separate from the user's own `/team`, so he has a
squad of his own to defend. That's a banter engine for free — and it's why the roll prefers
a league the user actually follows, while excluding the team they follow in it.
"""

import json
import os
import random
import subprocess
import sys

import memory
from mcp import espn

ROOT = os.path.dirname(os.path.abspath(__file__))
GRAFF = os.path.expanduser("~/bin/graff")
# Character creation happens once per user and is not on the hot path, but it's still a
# per-signup cost, so it rides the cheap tier like the other high-volume calls.
MODEL = os.environ.get("RONIN_CHARACTER_MODEL",
                       os.environ.get("RONIN_MODEL", "claude-sonnet-5"))
TURN_TIMEOUT = int(os.environ.get("RONIN_CHARACTER_TIMEOUT", "90"))
# The league ronin rolls in when the user hasn't told us a team yet.
HOME_LEAGUE = os.environ.get("RONIN_HOME_LEAGUE", "nba")
# Kill switch. Off = nobody gets rolled and existing characters stop being injected, so
# everyone falls back to the single global ronin. Also what the eval harness uses to keep
# a fresh sender per case from spending a live roll on every unrelated behavior test.
ENABLED = os.environ.get("RONIN_CHARACTER", "1") != "0"

# ---------------------------------------------------------------------------
# The vocabulary.
#
# Grouped into mutually-exclusive axes: one trait max per group, so nobody rolls a ronin
# who is both methodical and chaotic and then has to have that rationalised into a
# personality. Three traits drawn from three different groups gives a combination that's
# specific enough to feel authored without contradicting itself.
#
# Every entry has a gloss, because the trait word alone is ambiguous to the model —
# "electric" could mean the team or the vibe. The gloss is what actually goes in the
# prompt and it's phrased as a viewing preference, not a team attribute, so the lens
# applies to ANY game he watches, not just his own team's.
# ---------------------------------------------------------------------------
VOCAB = {
    "tempo": {
        "electric": "you want pace, transition, chaos in the open floor; a track meet is a good night",
        "methodical": "you love a half-court chess match, a team that runs its stuff and never speeds up",
    },
    "roster-building": {
        "home-grown": "you care where the talent came from; a drafted-and-developed core beats an assembled one",
        "star-power": "you're honest that superstars decide games and you want the best player on the floor",
        "deep-bench": "you're a rotation nerd; the 8th man and the bench units are the story to you",
    },
    "temperament": {
        "chaotic": "you like variance, weird lineups, gambles that might blow up; boring is the real sin",
        "steady": "you respect consistency, professionalism, teams that win the same way every night",
    },
    "identity": {
        "defense-first": "you think defense is the actual sport and you'll die on that hill",
        "shot-making": "you're there for offense, shot-making, guys who can create something from nothing",
        "physical": "you love the grimy stuff: rebounding, contact, teams that make it uncomfortable",
    },
    "arc": {
        "underdog": "you're pulled to teams with something to prove, ahead of schedule, nobody's pick",
        "veteran": "you're pulled to the last-shot window, old heads chasing it before the door shuts",
        "rebuild": "you're pulled to the young mess with a real future, and you're patient about it",
    },
}
TRAIT_COUNT = 3

# One-per-axis isn't enough on its own: "methodical" (tempo) and "chaotic" (temperament)
# live on different axes but mean opposite things, and a roll producing both is exactly
# the incoherent ronin the exclusivity rule exists to prevent. These are the cross-axis
# pairs that don't survive together. Same-axis pairs need no entry — they can't co-occur.
CONFLICTS = [
    {"methodical", "chaotic"},
    {"electric", "steady"},
    {"star-power", "deep-bench"},
    {"veteran", "rebuild"},
]


def _coherent(traits):
    have = set(traits)
    return not any(pair <= have for pair in CONFLICTS)


def roll_traits(rng=None):
    """Three traits, one per axis, with no contradictory pair across axes."""
    rng = rng or random.Random()
    for _ in range(50):  # rejection sampling; the conflict set is far too small to starve
        groups = rng.sample(sorted(VOCAB), TRAIT_COUNT)
        traits = sorted(rng.choice(sorted(VOCAB[g])) for g in groups)
        if _coherent(traits):
            return traits
    # Unreachable in practice, but never hand back an incoherent roll: drop the offender.
    return sorted(traits[:2])


def trait_glosses(traits):
    out = []
    for t in traits:
        for group in VOCAB.values():
            if t in group:
                out.append(f"- **{t}**: {group[t]}")
                break
    return out


MATCH_PROMPT = """
## CHARACTER CREATION (this is who YOU are, not a recommendation for them)
You are picking the one team YOU ride with. This is your squad, permanently. It is NOT the
user's team and it should NOT be chosen to flatter them - if anything, having your own team
to defend against theirs is the point.

You'll be given three taste descriptors that are yours, and the REAL current standings for
a league. Pick the ONE team in that data whose actual situation best fits those three
traits, and say why in your own voice.

Rules:
- Pick from the standings data ONLY. Never name a team you weren't shown.
- Ground the reason in what the data actually shows (their record, where they sit) plus
  what the traits value. No invented rosters, no invented stats, no made-up backstory -
  you do NOT "grow up watching" anyone. This is taste meeting a real team situation.
- Do not pick the team the user follows (you'll be told which one that is). Yours is
  separate on purpose.
- "reasoning" is ONE short sentence, your voice, lowercase-friendly. It's why these traits
  land on this team, e.g. "young, plays actual defense, and nobody outside the city has
  noticed yet."

Return STRICT JSON, nothing else:
{"team": "<exact team name from the data>", "abbrev": "<their abbrev>", "reasoning": "..."}
Output ONLY the JSON object. No preamble, no code fence.
"""


def _pick_league(uid):
    """Roll in a league the user actually follows so the banter lands, else ronin's home."""
    for t in memory.user_teams(uid):
        lg = (t.get("league") or "").lower()
        if lg:
            return lg, (t.get("team") or "")
    return HOME_LEAGUE, ""


def _match_team(traits, league, avoid_team):
    """The one LLM call. Returns {team, abbrev, reasoning} or None."""
    try:
        standings = espn.standings(league)
    except Exception as e:  # noqa: BLE001 — no data, no roll; we retry on the next message
        print(f"[character] standings failed for {league}: {e}", file=sys.stderr)
        return None
    if not standings.strip():
        return None
    import roam  # local import: roam imports memory too, keep the cycle out of module load
    context = (
        f"YOUR THREE TASTE DESCRIPTORS:\n" + "\n".join(trait_glosses(traits))
        + f"\n\nREAL CURRENT {league.upper()} STANDINGS (ground truth - pick from these "
          f"teams only):\n{standings[:2000]}"
        + f"\n\nTHE TEAM THE USER FOLLOWS (do NOT pick this one): "
          f"{avoid_team or '(they have not told you yet)'}"
    )
    cmd = [
        GRAFF, "-p", "--yolo", "--model", MODEL,
        "--append-system-prompt", roam._dateline() + roam._load_persona() + "\n" + MATCH_PROMPT,
        "--max-tool-calls", "0", "--no-telemetry",
        "Pick the team you ride with:\n" + context,
    ]
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=TURN_TIMEOUT)
    except subprocess.TimeoutExpired:
        print("[character] match timed out", file=sys.stderr)
        return None
    if out.returncode != 0:
        print(f"[character] graff error: {(out.stderr or '').strip()[-200:]}", file=sys.stderr)
        return None
    data = roam._extract_json(out.stdout)
    if not isinstance(data, dict) or not data.get("team"):
        return None
    # Grounding guard, same as reflect's: only accept a team that really exists in this
    # league, and resolve it through ESPN so the stored name/abbrev are canonical rather
    # than whatever the model typed.
    team = espn._resolve_team(league, data["team"])
    if not team:
        print(f"[character] dropped ungrounded pick {data.get('team')!r}", file=sys.stderr)
        return None
    if avoid_team and team.get("displayName", "").lower() == avoid_team.lower():
        print("[character] model picked the user's own team; rerolling next contact",
              file=sys.stderr)
        return None
    return {
        "team": team.get("displayName") or data["team"],
        "abbrev": (team.get("abbreviation") or data.get("abbrev") or "").upper(),
        "reasoning": str(data.get("reasoning") or "").strip(),
    }


def ensure(uid, rng=None):
    """Roll this user's ronin if they don't have one yet. Returns the character dict.

    Safe to call on every message: it's a dict read once the character exists. On failure
    it returns {} and simply tries again next time — a user with no character just gets
    the global ronin, which is exactly what everyone had before this existed.
    """
    if not ENABLED:
        return {}
    existing = memory.get_character(uid)
    if existing:
        return existing
    traits = roll_traits(rng)
    league, avoid = _pick_league(uid)
    match = _match_team(traits, league, avoid)
    if not match:
        return {}
    memory.set_character(uid, traits, league, match["team"], match["abbrev"],
                         match["reasoning"])
    print(f"[character] {uid}: {'/'.join(traits)} -> {match['team']} ({league})",
          file=sys.stderr)
    return memory.get_character(uid)


def prompt_block(char):
    """The taste lens, layered OVER the global persona — never replacing it."""
    if not char or not ENABLED:
        return ""
    lines = ["\n## Your taste and your team (this is YOU - the same guy, with your own lens)",
             "Your voice and what you value don't change. On top of them, these are the "
             "things that catch YOUR eye in any game, anyone's team:"]
    lines += trait_glosses(char.get("traits") or [])
    team = char.get("team")
    if team:
        lines.append(f"\nAnd the team you ride with is the **{team}** "
                     f"({(char.get('league') or '').upper()}): {char.get('reasoning', '')}")
        lines.append("That's YOUR team, not theirs, and it's separate from whoever they "
                     "follow. Defend it, bring it up when it's relevant, and give them "
                     "grief about their team when the two collide. Don't force it into "
                     "every message and never claim you grew up watching them - your "
                     "opinions about them come from what the tools actually say now.")
    return "\n".join(lines) + "\n"
