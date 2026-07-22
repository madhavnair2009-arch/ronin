#!/usr/bin/env python3
"""
ronin — persistent memory (the shared brain).

Three of the design doc's four axes live here as JSON under state/ (the fourth,
temperament, stays a flat file = persona.md):

  takes.json          its beliefs — LIVING records the roam loop revises (with history)
  relationships.json  you — per-user: your team, chat_id, mute, proactive throttle
  outbound.json       dedup log — what ronin has already proactively said
  cursor.json         world-facts delta cursor — headline keys already seen per scope

Why JSON and not `memeory` yet: the design doc's own verdict is that memeory is a
later swap for the beliefs *recall* layer only; for v1 a plain locked KV store is the
right call (no premature vector DB). Facts are never stored here — always re-fetched.

Both the bot's reply threads and the roam process write this, so every read-modify-write
goes through an exclusive file lock (fcntl) + atomic replace.
"""

import json
import os
import re
import tempfile
import threading
import time

try:
    import fcntl  # POSIX only; we run on macOS + Linux
except ImportError:  # pragma: no cover
    fcntl = None

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("RONIN_STATE_DIR", os.path.join(ROOT, "state"))
_LOCK_PATH = os.path.join(STATE_DIR, ".lock")
_proc_lock = threading.Lock()  # guards against threads within one process


def _ensure_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


class _FileLock:
    """Coarse cross-process lock: one exclusive flock for all state mutations.
    Contention is trivial (roam runs every ~30 min), so a single lock is fine."""

    def __enter__(self):
        _proc_lock.acquire()
        _ensure_dir()
        self._fh = open(_LOCK_PATH, "w")
        if fcntl:
            fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            if fcntl:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
        finally:
            _proc_lock.release()


def _path(name):
    return os.path.join(STATE_DIR, name)


def _read(name, default):
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write(name, data):
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _path(name))  # atomic on POSIX
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _update(name, fn, default):
    """Locked read-modify-write. fn(data) mutates in place and may return a value."""
    with _FileLock():
        data = _read(name, default)
        result = fn(data)
        _write(name, data)
        return result


# ---------------------------------------------------------------------------
# Axis: YOU (relationships)
# ---------------------------------------------------------------------------
def touch_user(uid, chat_id):
    """Record that a user exists / just messaged (so roam can reach them)."""
    uid = str(uid)
    now = time.time()

    def go(d):
        u = d.setdefault(uid, {})
        u.setdefault("first_seen", now)
        u["last_seen"] = now
        if chat_id is not None:
            u["chat_id"] = chat_id
    _update("relationships.json", go, {})


def _normalize_teams(u):
    """A user's teams as {league: {team, abbrev}}, migrating the old single-team
    shape (top-level league/team/abbrev) if that's all a record carries."""
    teams = dict(u.get("teams") or {})
    legacy_lg = u.get("league")
    if legacy_lg and legacy_lg not in teams and u.get("team"):
        teams[legacy_lg] = {"team": u["team"], "abbrev": u.get("abbrev", "")}
    return teams


def set_team(uid, league, team_display, abbrev, chat_id=None):
    """Add/replace this user's team for one league. Teams in other leagues are kept,
    so a person can follow the 49ers (NFL) and the Warriors (NBA) at once."""
    uid = str(uid)

    def go(d):
        u = d.setdefault(uid, {})
        teams = _normalize_teams(u)
        teams[league] = {"team": team_display, "abbrev": abbrev}
        u["teams"] = teams
        for k in ("league", "team", "abbrev"):  # fold away legacy single-team fields
            u.pop(k, None)
        u["muted"] = False
        if chat_id is not None:
            u["chat_id"] = chat_id
    _update("relationships.json", go, {})


def clear_team(uid, league=None):
    """Drop one league's team, or all teams if league is None. Returns how many remain."""
    uid = str(uid)

    def go(d):
        u = d.get(uid)
        if not u:
            return 0
        teams = _normalize_teams(u)
        if league is None:
            teams = {}
        else:
            teams.pop(league, None)
        u["teams"] = teams
        for k in ("league", "team", "abbrev"):
            u.pop(k, None)
        return len(teams)
    return _update("relationships.json", go, {})


def user_teams(uid_or_record):
    """List of {league, team, abbrev} for a user. Accepts a uid or a record dict."""
    u = uid_or_record if isinstance(uid_or_record, dict) else (get_user(uid_or_record) or {})
    return [{"league": lg, "team": info.get("team", ""), "abbrev": info.get("abbrev", "")}
            for lg, info in _normalize_teams(u).items()]


def set_muted(uid, muted):
    uid = str(uid)
    _update("relationships.json", lambda d: d.setdefault(uid, {}).__setitem__("muted", muted), {})


def get_user(uid):
    return _read("relationships.json", {}).get(str(uid))


def active_users():
    """Users eligible for proactive outreach: team set, not muted, reachable."""
    out = []
    for uid, u in _read("relationships.json", {}).items():
        if _normalize_teams(u) and u.get("chat_id") and not u.get("muted"):
            out.append((uid, u))
    return out


def touch_proactive(uid):
    uid = str(uid)
    _update("relationships.json",
            lambda d: d.setdefault(uid, {}).__setitem__("last_proactive", time.time()), {})


def proactive_allowed(uid, min_gap_seconds):
    u = get_user(uid) or {}
    return (time.time() - u.get("last_proactive", 0)) >= min_gap_seconds


# What ronin knows about YOU beyond your team: the takes you hold, running bits, and the
# arguments you two keep having. The roam digest pass distills these from your recent chats;
# the chat path feeds them back so ronin talks like it actually remembers you.
PROFILE_CAPS = {"takes_you_hold": 8, "bits": 6, "running_arguments": 5}


def get_profile(uid):
    return (get_user(uid) or {}).get("profile") or {}


def set_profile(uid, profile, digested_ms=None):
    """Replace a user's profile with the digest's output, capping each list so it can't
    grow without bound. digested_ms marks how current the transcript was, so the next pass
    can skip a conversation it has already read."""
    uid = str(uid)
    clean = {}
    for field, cap in PROFILE_CAPS.items():
        vals = profile.get(field) or []
        if isinstance(vals, list):
            clean[field] = [str(v).strip() for v in vals if str(v).strip()][:cap]
    clean["updated_at"] = time.time()
    if digested_ms is not None:
        clean["digested_ms"] = digested_ms

    def go(d):
        d.setdefault(uid, {})["profile"] = clean
    _update("relationships.json", go, {})


# ---------------------------------------------------------------------------
# Axis: ITS BELIEFS (takes) — revision, not append; keep history
# ---------------------------------------------------------------------------
def _seed_takes_from_file():
    """One-time seed of the living store from the hand-authored takes.json."""
    seed = _read("../takes.json", None)  # relative to state/ -> ronin/takes.json
    if seed is None:
        try:
            with open(os.path.join(ROOT, "takes.json"), encoding="utf-8") as f:
                seed = json.load(f)
        except (OSError, json.JSONDecodeError):
            seed = {}
    now = time.time()
    out = []
    for t in seed.get("takes", []):
        out.append({
            "subject": t["subject"],
            "topic": _slug(t.get("topic") or t["subject"]),
            "stance": t["stance"],
            "confidence": _conf(t.get("confidence")),
            "reasoning": t.get("reasoning", ""),
            "evidence": [],
            "resolves_when": t.get("resolves_when", ""),
            "deadline": t.get("deadline"),
            "status": "open",
            "formed_at": now,
            "updated_at": now,
            "history": [],
        })
    return out


def get_takes():
    with _FileLock():
        data = _read("takes.json", None)
        if data is None:  # cold start: seed from the flat file
            data = _seed_takes_from_file()
            _write("takes.json", data)
        return data


def _conf(value, default=0.5):
    """Confidence as a clamped float. The LLM can emit null/"high"/nonsense, and older
    records may already hold one, so every read of a confidence goes through here."""
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _slug(text):
    """Stable identity for a take's storyline. The roam judge authors the subject fresh
    every pass ('Curry legacy' one week, 'Steph's HOF case' the next), so matching on the
    raw subject forks one belief into many. We match on a normalized slug instead — of an
    explicit topic when given, else the subject. This is the de-dup key."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def take_key(t):
    """The de-dup identity of a stored take: its topic slug, or the subject slug for
    legacy takes that predate topics."""
    return _slug(t.get("topic") or t.get("subject"))


def upsert_take(subject, stance, confidence, reasoning="", evidence=None,
                topic=None, resolves_when=None, deadline=None):
    """Revise the take for this storyline (pushing the prior stance to history) or form a
    new one. Identity is the topic slug (falling back to the subject slug), NOT the raw
    subject string, so a reworded subject revises the belief instead of duplicating it.

    resolves_when / deadline let a take be graded later: what real-world outcome would
    settle it, and the YYYYMMDD by which to check. A freshly formed take is status 'open'."""
    subject = (subject or "").strip()
    if not subject:
        return
    confidence = _conf(confidence)
    key = _slug(topic or subject)
    now = time.time()

    def go(data):
        if not isinstance(data, list):
            data = []
        for t in data:
            if take_key(t) == key:
                changed = (stance != t.get("stance")
                           or abs(confidence - _conf(t.get("confidence"), 0.0)) > 1e-9)
                if changed:
                    t.setdefault("history", []).append({
                        "stance": t.get("stance"),
                        "confidence": t.get("confidence"),
                        "at": t.get("updated_at", now),
                    })
                    t["stance"] = stance
                    t["confidence"] = confidence
                    t["subject"] = subject  # keep the freshest phrasing
                    if reasoning:
                        t["reasoning"] = reasoning
                    t["updated_at"] = now
                t.setdefault("topic", key)
                if resolves_when:
                    t["resolves_when"] = resolves_when
                if deadline:
                    t["deadline"] = deadline
                # A revised take reopens for grading — the new stance hasn't been tested.
                if changed and t.get("status") in ("hit", "miss"):
                    t["status"] = "open"
                    t.pop("graded_at", None)
                    t.pop("outcome", None)
                t.setdefault("status", "open")
                for ev in (evidence or []):
                    if ev not in t.setdefault("evidence", []):
                        t["evidence"].append(ev)
                return
        data.append({
            "subject": subject, "topic": key, "stance": stance, "confidence": confidence,
            "reasoning": reasoning, "evidence": list(evidence or []),
            "resolves_when": resolves_when or "", "deadline": deadline, "status": "open",
            "formed_at": now, "updated_at": now, "history": [],
        })
        return data

    with _FileLock():
        cur = _read("takes.json", None)
        if cur is None:
            cur = _seed_takes_from_file()
        if not isinstance(cur, list):
            cur = []
        go(cur)
        _write("takes.json", cur)


# ---------------------------------------------------------------------------
# Calibration: grade resolved takes against reality so being RIGHT earns conviction.
# A take carries resolves_when + a deadline; the roam grade() pass checks overdue ones
# against live data and calls resolve_take(). Confidence is then earned, not just asserted.
# ---------------------------------------------------------------------------
CONF_GAIN = 0.25   # a hit moves confidence this fraction of the way toward 1.0
CONF_LOSS = 0.40   # a miss cuts it this fraction toward 0.0 (being wrong stings more)


def _today_int():
    return int(time.strftime("%Y%m%d", time.localtime()))


def takes_due(today=None):
    """Open takes whose deadline has arrived — the grader's worklist. Takes with no
    deadline are never auto-graded (nothing to check them against)."""
    today = today or _today_int()
    out = []
    for t in get_takes():
        if t.get("status", "open") != "open":
            continue
        dl = t.get("deadline")
        try:
            if dl and int(dl) <= today:
                out.append(t)
        except (TypeError, ValueError):
            continue
    return out


def resolve_take(key, verdict, note=""):
    """Record a graded outcome for a take (verdict 'hit' or 'miss'), move its confidence
    (up on a hit, down on a miss), and roll it into the running record. Returns the new
    confidence, or None if the take/verdict was invalid."""
    key = _slug(key)
    if verdict not in ("hit", "miss"):
        return None
    now = time.time()
    result = {}

    def go(data):
        for t in data if isinstance(data, list) else []:
            if take_key(t) == key:
                old = _conf(t.get("confidence"))
                new = old + (1 - old) * CONF_GAIN if verdict == "hit" else old * (1 - CONF_LOSS)
                t.setdefault("history", []).append({
                    "stance": t.get("stance"), "confidence": old,
                    "at": t.get("updated_at", now), "graded": verdict,
                })
                t["confidence"] = round(_conf(new), 3)
                t["status"] = verdict
                t["graded_at"] = now
                t["outcome"] = note
                t["updated_at"] = now
                result["conf"] = t["confidence"]
                result["subject"] = t.get("subject", "")
                return
    with _FileLock():
        cur = _read("takes.json", None)
        if cur is None:
            cur = _seed_takes_from_file()
        go(cur)
        _write("takes.json", cur)
    if "conf" not in result:
        return None

    def rec(d):
        d.setdefault("hits", 0)
        d.setdefault("misses", 0)
        d["hits" if verdict == "hit" else "misses"] += 1
        d.setdefault("log", []).append(
            {"key": key, "subject": result["subject"], "verdict": verdict,
             "note": note, "at": now})
        d["log"] = d["log"][-100:]
    _update("calibration.json", rec, {})
    return result["conf"]


def defer_take(key, days=7):
    """Push a take's deadline out — for when the grader can't yet tell if it resolved."""
    key = _slug(key)
    new_dl = int(time.strftime("%Y%m%d", time.localtime(time.time() + days * 86400)))

    def go(data):
        for t in data if isinstance(data, list) else []:
            if take_key(t) == key:
                t["deadline"] = new_dl
                return
    _update("takes.json", go, [])


def get_record():
    """Running calibration record: {hits, misses, accuracy, log}. accuracy is None until
    at least one take has been graded."""
    d = _read("calibration.json", {})
    h, m = d.get("hits", 0), d.get("misses", 0)
    total = h + m
    return {"hits": h, "misses": m, "log": d.get("log", []),
            "accuracy": (h / total) if total else None}


# ---------------------------------------------------------------------------
# Axis: ITS ALLEGIANCES (affinity) — teams ronin roots for / against.
# Earned, not declared: the roam reflection pass derives these from standings +
# ronin's own takes + its value-seed (persona), and revises them with history.
# score in [-1, 1]: +1 = loves/roots-for, -1 = roots-against. Keyed by league+abbrev
# (stable, unlike LLM-authored take subjects — no duplication bug here).
# ---------------------------------------------------------------------------
def get_affinities():
    return _read("affinity.json", [])


def upsert_affinity(team, league, abbrev, score, stance, reasons=None, evidence=None):
    team = (team or "").strip()
    league = (league or "").strip().lower()
    abbrev = (abbrev or "").strip().upper()
    key = f"{league}:{abbrev}"
    if not team or not abbrev:
        return
    try:
        score = max(-1.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        score = 0.0
    now = time.time()

    def go(data):
        if not isinstance(data, list):
            data = []
        for a in data:
            if a.get("key") == key:
                changed = (stance != a.get("stance")
                           or abs(score - float(a.get("score", 0))) > 1e-9)
                if changed:
                    a.setdefault("history", []).append({
                        "score": a.get("score"), "stance": a.get("stance"),
                        "at": a.get("updated_at", now),
                    })
                    a["score"] = score
                    a["stance"] = stance
                    a["updated_at"] = now
                if reasons:
                    a["reasons"] = reasons
                for ev in (evidence or []):
                    if ev not in a.setdefault("evidence", []):
                        a["evidence"].append(ev)
                return
        data.append({
            "key": key, "team": team, "league": league, "abbrev": abbrev,
            "score": score, "stance": stance, "reasons": list(reasons or []),
            "evidence": list(evidence or []), "formed_at": now, "updated_at": now,
            "history": [],
        })

    with _FileLock():
        cur = _read("affinity.json", [])
        if not isinstance(cur, list):
            cur = []
        go(cur)
        _write("affinity.json", cur)


def decay_affinities(leagues, reaffirmed_keys, factor=0.5, floor=0.2):
    """After a reflection pass, fade allegiances the pass DIDN'T reaffirm — but only within
    the leagues it actually looked at (don't touch a league it never fetched). This is what
    retires a stale allegiance: France sat at -0.30 'rolling into the semis' long after it
    was knocked out, because reflection simply stopped mentioning it. Now an unmentioned
    team decays toward zero each pass and drops out once it falls below the floor."""
    leagues = {(lg or "").lower() for lg in leagues}
    reaffirmed = {k for k in reaffirmed_keys}
    dropped = []

    def go(data):
        if not isinstance(data, list):
            return
        kept = []
        for a in data:
            if a.get("league") in leagues and a.get("key") not in reaffirmed:
                faded = round(float(a.get("score", 0)) * factor, 3)
                if abs(faded) < floor:
                    dropped.append(a.get("key"))
                    continue  # retired: too weak to hold onto
                a.setdefault("history", []).append({
                    "score": a.get("score"), "stance": a.get("stance"),
                    "at": a.get("updated_at", time.time()), "decayed": True,
                })
                a["score"] = faded
                a["updated_at"] = time.time()
            kept.append(a)
        data[:] = kept
    _update("affinity.json", go, [])
    return dropped


def top_affinities(n=3, threshold=0.15, max_out=4):
    """Return (loves, dislikes): the strongest positive and negative allegiances, with
    each league's top pick guaranteed a slot so a timely allegiance (e.g. a World Cup
    team) isn't buried behind a deeper-stocked league. Bounded by max_out."""
    aff = [a for a in get_affinities() if isinstance(a, dict)]

    def select(cands, reverse):
        cands = sorted(cands, key=lambda a: a["score"], reverse=reverse)
        out = list(cands[:n])                          # strongest overall
        seen = {a.get("league") for a in out}
        for a in cands:                                # then each league's top pick
            if a.get("league") not in seen:
                out.append(a)
                seen.add(a.get("league"))
        return sorted(out, key=lambda a: a["score"], reverse=reverse)[:max_out]

    loves = select([a for a in aff if a.get("score", 0) >= threshold], True)
    dislikes = select([a for a in aff if a.get("score", 0) <= -threshold], False)
    return loves, dislikes


# ---------------------------------------------------------------------------
# Axis: WORLD FACTS delta cursor (which headlines we've already seen per scope)
# ---------------------------------------------------------------------------
def cursor_is_cold(scope):
    return scope not in _read("cursor.json", {})


def headline_seen(scope, key):
    return key in set(_read("cursor.json", {}).get(scope, []))


def mark_seen(scope, keys):
    def go(d):
        cur = d.setdefault(scope, [])
        for k in keys:
            if k not in cur:
                cur.append(k)
        d[scope] = cur[-200:]  # cap
    _update("cursor.json", go, {})


# ---------------------------------------------------------------------------
# Axis: FAN MOOD per team scope — the last read of the vibe, so the sentiment sweep can
# spot a SHIFT (fans turning on a coach, hype building) rather than re-announce a steady
# mood. Storing the new mood after a ping is what stops it re-pinging until things move.
# ---------------------------------------------------------------------------
def get_mood(scope):
    return _read("mood.json", {}).get(scope)


def set_mood(scope, mood):
    _update("mood.json",
            lambda d: d.__setitem__(scope, {"mood": mood, "at": time.time()}), {})


# ---------------------------------------------------------------------------
# Dedup log — what ronin has already proactively said
# ---------------------------------------------------------------------------
def already_sent(uid, key):
    return f"{uid}:{key}" in _read("outbound.json", {}).get("keys", {})


KEYS_TTL = 90 * 86400   # a headline older than this will never resurface upstream
KEYS_MAX = 2000         # ...and a hard ceiling in case timestamps are missing/skewed


def log_sent(uid, key, text):
    now = time.time()

    def go(d):
        keys = d.setdefault("keys", {})
        keys[f"{uid}:{key}"] = now
        # Prune: `sent` was already capped, but `keys` grew forever. Age out first,
        # then cap by recency. Both are far wider than the cursor's 200/scope window,
        # so a pruned key can't come back around and re-blast.
        cutoff = now - KEYS_TTL
        keys = {k: ts for k, ts in keys.items() if isinstance(ts, (int, float)) and ts >= cutoff}
        if len(keys) > KEYS_MAX:
            newest = sorted(keys.items(), key=lambda kv: kv[1], reverse=True)[:KEYS_MAX]
            keys = dict(newest)
        d["keys"] = keys
        d.setdefault("sent", []).append({"uid": str(uid), "key": key, "text": text, "at": now})
        d["sent"] = d["sent"][-500:]
    _update("outbound.json", go, {})


def recent_sent(uid, n=5, within_secs=48 * 3600):
    """The last few things ronin proactively texted this user, newest last, each
    {text, at}. The roam loop sends these out-of-band and they never land in the graff
    chat session, so the chat path feeds them back in as context — otherwise a follow-up
    like 'who's funding it?' has nothing to attach to and anchors on stale chat history."""
    now = time.time()
    mine = [s for s in _read("outbound.json", {}).get("sent", [])
            if s.get("uid") == str(uid) and isinstance(s.get("at"), (int, float))
            and now - s["at"] <= within_secs]
    return [{"text": s.get("text", ""), "at": s["at"]} for s in mine[-n:]]


if __name__ == "__main__":
    # tiny smoke test
    print("state dir:", STATE_DIR)
    touch_user("u1", 111)
    set_team("u1", "nba", "Detroit Pistons", "DET", chat_id=111)
    upsert_take("Pistons ceiling", "sneaky good, I buy the young core", 0.6, "roam test")
    upsert_take("Pistons ceiling", "even higher after that signing", 0.7, "revised")
    print("user:", get_user("u1"))
    print("active:", active_users())
    print("takes:", json.dumps(get_takes(), indent=2)[:400])
