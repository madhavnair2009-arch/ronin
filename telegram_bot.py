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
    "yo — I'm ronin. NBA, WNBA, NFL, MLB, NHL, college. ask me for scores, standings, "
    "news, or just argue with me about a team. I pull real numbers, the takes are my own.\n\n"
    "tell me your team with /team (e.g. `/team pistons` or `/team nhl rangers`) and I'll "
    "text you when something actually happens with them. /mute to stop, /unmute to resume."
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
    if cmd == "/team":
        args = parts[1:]
        if not args:
            send(chat_id, "tell me who — like `/team pistons` or `/team nhl rangers`.")
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
        send(chat_id, f"got it — you're a {t.get('displayName', query)} guy. "
                      f"I'll text you when something real happens with them. 🫡"[:300])
        return True
    return False


def _roam_scheduler():
    print(f"[roam] scheduler on, every {ROAM_INTERVAL}s", file=sys.stderr)
    while True:
        time.sleep(ROAM_INTERVAL)
        try:
            roam.run_once()
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


def handle(update):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    sender = (msg.get("from") or {}).get("id", chat_id)
    text = msg.get("text", "")
    # Relationship memory: remember this person exists + how to reach them (for roam).
    memory.touch_user(sender, chat_id)
    if not text:
        send(chat_id, "I only do text for now — type me something.")
        return
    if text.startswith("/"):
        if _handle_command(chat_id, sender, text):
            return
    if not _rate_ok(sender):
        send(chat_id, "easy — you've hit me a lot this hour. give it a bit and come back.")
        return
    print(f"[msg] {sender}: {text}", file=sys.stderr)
    typing(chat_id)
    answer = ronin_reply.reply(sender, text)
    send(chat_id, answer)
    print(f"[reply] {sender}: {answer[:80]}...", file=sys.stderr)


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
            # one thread per message so a slow graff turn doesn't block others
            threading.Thread(target=handle, args=(upd,), daemon=True).start()


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
