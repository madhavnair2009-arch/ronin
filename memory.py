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


def set_team(uid, league, team_display, abbrev, chat_id=None):
    uid = str(uid)

    def go(d):
        u = d.setdefault(uid, {})
        u["league"] = league
        u["team"] = team_display
        u["abbrev"] = abbrev
        u["muted"] = False
        if chat_id is not None:
            u["chat_id"] = chat_id
    _update("relationships.json", go, {})


def set_muted(uid, muted):
    uid = str(uid)
    _update("relationships.json", lambda d: d.setdefault(uid, {}).__setitem__("muted", muted), {})


def get_user(uid):
    return _read("relationships.json", {}).get(str(uid))


def active_users():
    """Users eligible for proactive outreach: team set, not muted, reachable."""
    out = []
    for uid, u in _read("relationships.json", {}).items():
        if u.get("team") and u.get("chat_id") and not u.get("muted"):
            out.append((uid, u))
    return out


def touch_proactive(uid):
    uid = str(uid)
    _update("relationships.json",
            lambda d: d.setdefault(uid, {}).__setitem__("last_proactive", time.time()), {})


def proactive_allowed(uid, min_gap_seconds):
    u = get_user(uid) or {}
    return (time.time() - u.get("last_proactive", 0)) >= min_gap_seconds


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
            "stance": t["stance"],
            "confidence": t.get("confidence", 0.5),
            "reasoning": t.get("reasoning", ""),
            "evidence": [],
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


def upsert_take(subject, stance, confidence, reasoning="", evidence=None):
    """Revise an existing take on this subject (pushing the prior stance to history)
    or form a new one. Match on case-insensitive subject."""
    subject = (subject or "").strip()
    if not subject:
        return
    now = time.time()

    def go(data):
        if not isinstance(data, list):
            data = []
        for t in data:
            if t.get("subject", "").strip().lower() == subject.lower():
                changed = (stance != t.get("stance")
                           or abs(float(confidence) - float(t.get("confidence", 0))) > 1e-9)
                if changed:
                    t.setdefault("history", []).append({
                        "stance": t.get("stance"),
                        "confidence": t.get("confidence"),
                        "at": t.get("updated_at", now),
                    })
                    t["stance"] = stance
                    t["confidence"] = confidence
                    if reasoning:
                        t["reasoning"] = reasoning
                    t["updated_at"] = now
                for ev in (evidence or []):
                    if ev not in t.setdefault("evidence", []):
                        t["evidence"].append(ev)
                return
        data.append({
            "subject": subject, "stance": stance, "confidence": confidence,
            "reasoning": reasoning, "evidence": list(evidence or []),
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
# Dedup log — what ronin has already proactively said
# ---------------------------------------------------------------------------
def already_sent(uid, key):
    return f"{uid}:{key}" in _read("outbound.json", {}).get("keys", {})


def log_sent(uid, key, text):
    now = time.time()

    def go(d):
        d.setdefault("keys", {})[f"{uid}:{key}"] = now
        d.setdefault("sent", []).append({"uid": str(uid), "key": key, "text": text, "at": now})
        d["sent"] = d["sent"][-500:]
    _update("outbound.json", go, {})


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
