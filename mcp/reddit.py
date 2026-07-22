#!/usr/bin/env python3
"""
ronin — fan sentiment via Reddit, exposed as an MCP stdio server.

Reddit blocks datacenter IPs on the UNAUTHENTICATED paths (old.reddit HTML and the public
JSON both 403 us from Fly — verified 2026-07-21). But the OAuth API answers from the same IP
(a bogus-cred probe there returns 401, not 403), so we go the sanctioned route, exactly like
the Bluesky sentiment server: app credentials -> a token -> read the subreddit.

App-only (client_credentials) grant — no Reddit user password, just a registered "script"
app's id + secret, the direct parallel to a Bluesky app-password.

This is SENTIMENT, not fact (design doc: social = personality fuel). ronin reads the mood
and reacts in its own voice — it does NOT mirror the crowd. r/nba etc. are where the real
fan takes live, richer than the media-skewed Bluesky feed.

Tool:
  reddit_sentiment(league?, topic?)   hot posts in the sport's subreddit, or a search

Env:
  REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET   from reddit.com/prefs/apps ("script" app)

Run:
  python3 reddit.py            MCP stdio server
  python3 reddit.py selftest   query live (needs creds in env)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
UA = "web:ronin-sports:0.1 (fan-sentiment bot)"

# Which subreddit carries a league's fan chatter. Soccer leagues all funnel to r/soccer.
LEAGUE_SUB = {
    "nba": "nba", "wnba": "wnba", "nfl": "nfl", "mlb": "baseball", "nhl": "hockey",
    "wc": "worldcup", "epl": "soccer", "laliga": "soccer", "seriea": "soccer",
    "bundesliga": "soccer", "ligue1": "soccer", "ucl": "soccer", "mls": "MLS",
}
DEFAULT_SUB = "nba"

_session = {"token": None, "exp": 0}


class SentimentError(Exception):
    pass


def _token():
    if _session["token"] and time.time() < _session["exp"] - 60:
        return _session["token"]
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SentimentError("Reddit creds not set (REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET).")
    import base64
    auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    req = urllib.request.Request(
        "https://www.reddit.com/api/v1/access_token",
        data=b"grant_type=client_credentials",
        headers={"Authorization": "Basic " + auth, "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SentimentError(f"Reddit auth failed (HTTP {e.code}) — check client id/secret.")
    tok = body.get("access_token")
    if not tok:
        raise SentimentError("Reddit auth returned no token.")
    _session["token"] = tok
    _session["exp"] = time.time() + int(body.get("expires_in", 3600))
    return tok


def _get(path):
    """GET an oauth.reddit.com path, returning the listing children (post data dicts)."""
    req = urllib.request.Request(
        "https://oauth.reddit.com" + path,
        headers={"Authorization": "Bearer " + _token(), "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SentimentError(f"Reddit read failed (HTTP {e.code}).")
    children = (data.get("data") or {}).get("children") or []
    return [c.get("data") or {} for c in children]


def _sub_for(league):
    return LEAGUE_SUB.get((league or "").strip().lower(), DEFAULT_SUB)


def reddit_sentiment(league=None, topic=None):
    sub = _sub_for(league)
    if topic:
        q = urllib.parse.urlencode({"q": topic, "restrict_sr": 1, "sort": "top", "t": "month",
                                    "limit": 15})
        posts = _get(f"/r/{sub}/search?{q}")
        header = f"What r/{sub} is saying about '{topic}' (fan sentiment, NOT fact):"
    else:
        posts = _get(f"/r/{sub}/hot?limit=15")
        header = f"Top of r/{sub} right now (fan sentiment, NOT fact):"
    # Drop pinned/stickied mod posts; keep the real chatter, ranked by score.
    posts = [p for p in posts if not p.get("stickied")]
    posts.sort(key=lambda p: p.get("score", 0), reverse=True)
    if not posts:
        where = f"about '{topic}'" if topic else ""
        return f"Not much on r/{sub} {where} right now.".replace("  ", " ")
    lines = [header]
    for p in posts[:8]:
        title = (p.get("title") or "").strip().replace("\n", " ")
        if not title:
            continue
        lines.append(f"• [{p.get('score', 0)} pts, {p.get('num_comments', 0)} comments] {title}")
    return "\n".join(lines)


TOOLS = {
    "reddit_sentiment": {
        "fn": lambda a: reddit_sentiment(a.get("league"), a.get("topic")),
        "schema": {
            "name": "reddit_sentiment",
            "description": "What fans are actually saying on Reddit — the mood, hot takes, what's "
                           "blowing up in a sport's subreddit (r/nba, r/nfl, r/soccer, etc.). "
                           "Pass the league to pick the right sub; add a topic/team/player to "
                           "search it, or omit for what's hot. Richer fan takes than the social "
                           "feed. This is SENTIMENT, not fact: read the vibe, then give YOUR "
                           "read — don't just mirror it, and verify any 'trade' with sports_news.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "league": {
                        "type": "string",
                        "description": "League, to pick the subreddit (nba, wnba, nfl, mlb, nhl, "
                                       "epl/laliga/ucl/wc -> soccer). Defaults to nba.",
                    },
                    "topic": {
                        "type": "string",
                        "description": "Team, player, or storyline to search (e.g. 'Lakers', "
                                       "'LeBron', 'Jaylen Brown trade'). Omit for what's hot.",
                    },
                },
            },
        },
    },
}


# --- MCP stdio plumbing (matches mcp/sentiment.py) ---
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
                "serverInfo": {"name": "ronin-reddit", "version": "0.0.0"},
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
                    "content": [{"type": "text", "text": f"Reddit sentiment unavailable: {e}"}],
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
        print("=== reddit_sentiment('nba') ===")
        print(reddit_sentiment("nba"))
        print("\n=== reddit_sentiment('nba', 'Lakers') ===")
        print(reddit_sentiment("nba", "Lakers"))
    else:
        serve()
