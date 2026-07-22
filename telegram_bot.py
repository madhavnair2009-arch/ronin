#!/usr/bin/env python3
"""
ronin — Telegram transport.

Long-polls Telegram for incoming messages, hands each to the transport-independent
core (ronin_reply.reply), and sends the answer back. Stdlib only (urllib), no deps.

Long-polling means the bot only makes OUTBOUND calls to Telegram — no inbound port,
no public URL, no webhook. So it runs on any always-on host (or your Mac).

Env:
    TELEGRAM_BOT_TOKEN   required (also read from ./.env if present)
    ANTHROPIC_API_KEY    used by graff under the hood

Run:
    python3 telegram_bot.py
"""

import json
import os
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request

import memory
import ronin_reply
import roam
from mcp import espn

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    sys.exit("TELEGRAM_BOT_TOKEN not set (put it in ./.env or the environment)")
API = f"https://api.telegram.org/bot{TOKEN}"

GREETING = (
    "yo, I'm ronin. NBA, WNBA, NFL, MLB, NHL, college, and soccer (world cup + club). ask "
    "me for scores, standings, news, or just argue with me about a team. I pull real "
    "numbers, the takes are my own.\n\n"
    "tell me your teams with /team (e.g. `/team 49ers` or `/team nba warriors`), one per "
    "sport so you can stack them. /teams to see them, `/team clear <sport>` to drop one. "
    "/mute to stop pings, /unmute to resume."
)

# Roam loop (proactive outreach) config — an in-process scheduler so the bot host also
# builds the mind. Gated so it can be turned off without a redeploy.
ROAM_ENABLED = os.environ.get("ROAM_ENABLED", "1") not in ("0", "false", "")
ROAM_INTERVAL = int(os.environ.get("ROAM_INTERVAL", "1800"))  # 30 min


def _handle_command(chat_id, sender, text):
    """Return True if this was a slash command we handled."""
    parts = text.strip().split()
    cmd = parts[0].lower()
    if cmd in ("/start", "/help"):
        send(chat_id, GREETING)
        return True
    if cmd == "/mute":
        memory.set_muted(sender, True)
        send(chat_id, "cool, I'll keep quiet. /unmute when you want me back.")
        return True
    if cmd == "/unmute":
        memory.set_muted(sender, False)
        send(chat_id, "back on — I'll ping you when your team does something.")
        return True
    if cmd == "/teams":  # show what I've got you down for
        mine = memory.user_teams(sender)
        if not mine:
            send(chat_id, "haven't got a team for you yet. `/team 49ers` or `/team nba warriors`.")
        else:
            lst = ", ".join(f"{t['team']} ({t['league'].upper()})" for t in mine)
            send(chat_id, f"i've got you down for: {lst}. add more with /team, drop one with "
                          f"`/team clear <sport>`.")
        return True
    if cmd == "/team":
        args = parts[1:]
        if not args:
            send(chat_id, "tell me who, like `/team 49ers` or `/team nba warriors`. you can "
                          "keep one team per sport. /teams to see what i've got.")
            return True
        # `/team clear` drops one sport's team (or all), so stale teams can be pruned.
        if args[0].lower() == "clear":
            rest = args[1:]
            lg = rest[0].lower() if rest else None
            if lg is not None and lg not in espn.LEAGUES and lg not in espn.ALIASES:
                send(chat_id, f"'{rest[0]}' isn't a sport i know. try like `/team clear nfl`, "
                              f"or just `/team clear` to wipe them all.")
                return True
            lg = espn.ALIASES.get(lg, lg) if lg else None
            left = memory.clear_team(sender, lg)
            send(chat_id, ("cleared them all." if lg is None else f"dropped your {lg.upper()} team.")
                          + (f" still holding {left}." if left else " no teams left."))
            return True
        league = None
        if args[0].lower() in espn.LEAGUES or args[0].lower() in espn.ALIASES:
            league = args[0].lower()
            args = args[1:]
        query = " ".join(args)
        lg, t = espn.find_team(query, league=league)
        if not t:
            send(chat_id, f"couldn't find a team called '{query}'. try the city or nickname.")
            return True
        memory.set_team(sender, lg, t.get("displayName", query),
                        t.get("abbreviation", ""), chat_id=chat_id)
        others = [x for x in memory.user_teams(sender) if x["league"] != lg]
        extra = (" (still got your " + ", ".join(x["team"] for x in others) + " too)") if others else ""
        send(chat_id, f"got it, you're a {t.get('displayName', query)} ({lg.upper()}) guy.{extra} "
                      f"I'll text you when something real happens. 🫡"[:300])
        return True
    return False


# The slow background loops, counted in roam ticks (news roaming is the fast one).
# Reflection = "who do I root for", grading = "was I right", digest = "who is this person".
# All default to ~daily at 30-min ticks; they're cheap-when-idle (each gates on new work).
REFLECT_EVERY = int(os.environ.get("ROAM_REFLECT_EVERY", "48"))
GRADE_EVERY = int(os.environ.get("ROAM_GRADE_EVERY", "48"))
DIGEST_EVERY = int(os.environ.get("ROAM_DIGEST_EVERY", "8"))  # ~4h: keep memory of people fresher
SENTIMENT_EVERY = int(os.environ.get("ROAM_SENTIMENT_EVERY", "24"))  # ~12h: catch mood shifts


def _roam_scheduler():
    print(f"[roam] scheduler on, every {ROAM_INTERVAL}s (reflect/{REFLECT_EVERY}, "
          f"grade/{GRADE_EVERY}, digest/{DIGEST_EVERY}, sentiment/{SENTIMENT_EVERY} ticks)",
          file=sys.stderr)
    # Cold start: if this fresh machine has no allegiances yet, form them once now so
    # ronin has a personality before the first daily reflection comes around.
    try:
        if not memory.get_affinities():
            roam.reflect()
    except Exception as e:  # noqa: BLE001
        print(f"[roam] initial reflect errored: {e}", file=sys.stderr)
    tick = 0
    while True:
        time.sleep(ROAM_INTERVAL)
        tick += 1
        try:
            roam.run_once()
            if tick % DIGEST_EVERY == 0:
                roam.digest()
            if tick % SENTIMENT_EVERY == 0:
                roam.sentiment_sweep()
            if tick % GRADE_EVERY == 0:
                roam.grade()
            if tick % REFLECT_EVERY == 0:
                roam.reflect()
        except Exception as e:  # noqa: BLE001 — never let roam kill the bot
            print(f"[roam] pass errored: {e}", file=sys.stderr)

# Per-user rate limit (protects the Anthropic bill if the bot gets passed around).
RATE_MAX = int(os.environ.get("RONIN_RATE_MAX", "20"))       # messages...
RATE_WINDOW = int(os.environ.get("RONIN_RATE_WINDOW", "3600"))  # ...per this many seconds
_hits = {}
_hits_lock = threading.Lock()


def _rate_ok(sender):
    now = time.time()
    with _hits_lock:
        q = [t for t in _hits.get(sender, []) if now - t < RATE_WINDOW]
        if len(q) >= RATE_MAX:
            _hits[sender] = q
            return False
        q.append(now)
        _hits[sender] = q
        return True


def _api(method, params=None, timeout=40):
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def send(chat_id, text):
    # Telegram caps messages at 4096 chars
    for i in range(0, len(text) or 1, 4000):
        try:
            _api("sendMessage", {"chat_id": chat_id, "text": text[i:i + 4000]})
        except Exception as e:  # noqa: BLE001 — never let a send failure kill the loop
            print(f"[send error] {e}", file=sys.stderr)


def typing(chat_id):
    try:
        _api("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _reply_async(chat_id, sender, text):
    """The slow part (the model turn) — runs in its own thread so one user's long reply
    doesn't block anyone else."""
    print(f"[msg] {sender}: {text}", file=sys.stderr)
    typing(chat_id)
    answer = ronin_reply.reply(sender, text)
    send(chat_id, answer)
    print(f"[reply] {sender}: {answer[:80]}...", file=sys.stderr)


def dispatch(update):
    """Fast path runs synchronously in the poll loop; only the model reply is threaded.
    Handling commands synchronously means a /team write commits before the next message's
    reply thread reads memory, so a '/team' + immediate question can't race."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    sender = (msg.get("from") or {}).get("id", chat_id)
    text = msg.get("text", "")
    # Relationship memory: remember this person exists + how to reach them (for roam).
    memory.touch_user(sender, chat_id)
    if not text:
        send(chat_id, "I only do text for now, type me something.")
        return
    if text.startswith("/") and _handle_command(chat_id, sender, text):
        return
    if not _rate_ok(sender):
        send(chat_id, "easy, you've hit me a lot this hour. give it a bit and come back.")
        return
    threading.Thread(target=_reply_async, args=(chat_id, sender, text), daemon=True).start()


def main():
    print(f"ronin telegram bot up. polling as bot… (Ctrl-C to stop)", file=sys.stderr)
    if ROAM_ENABLED:
        threading.Thread(target=_roam_scheduler, daemon=True).start()
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            resp = _api("getUpdates", params, timeout=40)
        except Exception as e:  # noqa: BLE001 — transient network; back off and retry
            print(f"[poll error] {e}", file=sys.stderr)
            time.sleep(3)
            continue
        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            # sync fast path (commands/memory) + threaded model reply — see dispatch()
            dispatch(upd)


def _shutdown(signum, _frame):
    # Fly sends SIGINT/SIGTERM on deploy; exit cleanly instead of crashing mid-poll.
    print(f"received signal {signum}, shutting down cleanly", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _shutdown)
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        print("ronin bot stopped.", file=sys.stderr)
