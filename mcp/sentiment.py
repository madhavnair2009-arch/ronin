#!/usr/bin/env python3
"""
ronin — fan/media sentiment via Bluesky, exposed as an MCP stdio server.

Why Bluesky and not Reddit: cloud/datacenter IPs get 403'd by Reddit AND by Bluesky's
*unauthenticated* public API. Authenticated calls go through, and Bluesky app-passwords
(unlike Reddit's currently-broken app creation) actually work. So we log in with an
app-password and query the AppView with a Bearer token.

This is SENTIMENT, not fact (design doc: social = personality fuel). ronin reads the
mood and reacts in its own voice — it does NOT mirror the crowd.

Tool:
  nba_fan_sentiment(topic?)   search Bluesky for a topic (or broad NBA chatter)

Env:
  BSKY_HANDLE, BSKY_APP_PASSWORD   required (bsky.app → Settings → App Passwords)

Run:
  python3 sentiment.py            MCP stdio server
  python3 sentiment.py selftest   query live (needs creds in env)
"""

import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error

HANDLE = os.environ.get("BSKY_HANDLE", "")
APP_PASSWORD = os.environ.get("BSKY_APP_PASSWORD", "")
ENTRYWAY = "https://bsky.social"
UA = "ronin/0.0 sports-sentiment"

_session = {"jwt": None}


class SentimentError(Exception):
    pass


def _login():
    if _session["jwt"]:
        return _session["jwt"]
    if not HANDLE or not APP_PASSWORD:
        raise SentimentError("Bluesky creds not set (BSKY_HANDLE/BSKY_APP_PASSWORD).")
    body = json.dumps({"identifier": HANDLE, "password": APP_PASSWORD}).encode()
    req = urllib.request.Request(
        f"{ENTRYWAY}/xrpc/com.atproto.server.createSession",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            jwt = json.loads(r.read().decode()).get("accessJwt")
    except urllib.error.HTTPError as e:
        raise SentimentError(f"Bluesky login failed (HTTP {e.code}) — check handle/app-password.")
    if not jwt:
        raise SentimentError("Bluesky login returned no token.")
    _session["jwt"] = jwt
    return jwt


def _search(query, limit=20):
    jwt = _login()
    params = urllib.parse.urlencode({"q": query, "limit": limit, "sort": "top", "lang": "en"})
    req = urllib.request.Request(
        f"{ENTRYWAY}/xrpc/app.bsky.feed.searchPosts?{params}",
        headers={"Authorization": f"Bearer {jwt}", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode()).get("posts", [])


def fan_sentiment(topic=None):
    query = topic if topic else "sports"
    try:
        posts = _search(query)
    except urllib.error.HTTPError as e:
        raise SentimentError(f"Bluesky search failed (HTTP {e.code}).")
    # rank by engagement so the loudest takes surface, drop empty/near-empty posts
    scored = []
    for p in posts:
        text = ((p.get("record") or {}).get("text") or "").strip().replace("\n", " ")
        if len(text) < 15:
            continue
        likes = p.get("likeCount", 0)
        reposts = p.get("repostCount", 0)
        replies = p.get("replyCount", 0)
        handle = (p.get("author") or {}).get("handle", "?")
        scored.append((likes + reposts, likes, reposts, replies, handle, text))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        where = f"about '{topic}'" if topic else "on sports"
        return f"Not much chatter {where} on Bluesky right now."
    header = (f"What people are saying about '{topic}' on Bluesky (sentiment, NOT fact):"
              if topic else "Sports chatter on Bluesky right now (sentiment, NOT fact):")
    lines = [header]
    for _, likes, reposts, replies, handle, text in scored[:8]:
        lines.append(f"• [♥{likes} ↻{reposts} 💬{replies}] @{handle}: {text[:240]}")
    return "\n".join(lines)


TOOLS = {
    "fan_sentiment": {
        "fn": lambda a: fan_sentiment(a.get("topic")),
        "schema": {
            "name": "fan_sentiment",
            "description": "What fans and media are saying on social (Bluesky) about any "
                           "sport — the mood, hot takes, reactions. Works for any league "
                           "(NBA/NFL/MLB/NHL/etc.); pass a topic/team/player to focus it "
                           "(e.g. 'Lions', 'Aaron Judge', 'Super Bowl'), or omit for broad "
                           "sports chatter. This is SENTIMENT, not fact: gauge the vibe, then "
                           "react in your own voice — don't mirror it.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Team, player, or storyline (e.g. 'Lakers', 'LeBron', "
                                       "'Jaylen Brown trade', 'Cowboys'). Omit for broad chatter.",
                    }
                },
            },
        },
    },
}


# --- MCP stdio plumbing ---
PROTOCOL_VERSION = "2024-11-05"


def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        id_ = msg.get("id")
        if method == "initialize":
            ver = (msg.get("params") or {}).get("protocolVersion", PROTOCOL_VERSION)
            _send({"jsonrpc": "2.0", "id": id_, "result": {
                "protocolVersion": ver,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ronin-sentiment-bsky", "version": "0.0.0"},
            }})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": id_,
                   "result": {"tools": [t["schema"] for t in TOOLS.values()]}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            tool = TOOLS.get(params.get("name"))
            if not tool:
                _send({"jsonrpc": "2.0", "id": id_,
                       "error": {"code": -32602, "message": "Unknown tool"}})
                continue
            try:
                text = tool["fn"](params.get("arguments") or {})
                _send({"jsonrpc": "2.0", "id": id_,
                       "result": {"content": [{"type": "text", "text": text}]}})
            except SentimentError as e:
                _send({"jsonrpc": "2.0", "id": id_, "result": {
                    "content": [{"type": "text", "text": f"Sentiment unavailable: {e}"}],
                    "isError": True}})
            except Exception as e:  # noqa: BLE001
                _send({"jsonrpc": "2.0", "id": id_, "result": {
                    "content": [{"type": "text", "text": f"Tool error: {e}"}],
                    "isError": True}})
        elif id_ is not None:
            _send({"jsonrpc": "2.0", "id": id_,
                   "error": {"code": -32601, "message": "Method not found"}})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        print("=== fan_sentiment() ===")
        print(fan_sentiment())
        print("\n=== fan_sentiment('Lakers') ===")
        print(fan_sentiment("Lakers"))
    else:
        serve()
