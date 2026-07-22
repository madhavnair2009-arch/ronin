#!/usr/bin/env python3
"""
ronin — combined fan sentiment (Reddit + Bluesky), one MCP tool.

The two sources are complementary, and each covers the other's weak spot:
  * Reddit (mcp/reddit.py) — deeper, realer fan takes, but on the no-creds path it's read
    through search, so it can lag real-time by a few days.
  * Bluesky (mcp/sentiment.py) — live and fresh, but the feed skews media/journalist.

Pull both, in parallel, label each, and hand ronin the pair. When they disagree — Reddit
panicking while Bluesky shrugs, or vice versa — that gap is itself worth a line. One source
failing never kills the call; ronin still gets the other with a note.

This is SENTIMENT, not fact (design doc: social = personality fuel). ronin reads the mood
and reacts in its own voice — it does NOT mirror the crowd.

Tool:
  fan_sentiment(league?, topic?)   the mood on Reddit + Bluesky, blended

Run:
  python3 fan.py            MCP stdio server
  python3 fan.py selftest   query live
"""

import concurrent.futures
import json
import sys

# Sibling modules when run as a script (python3 mcp/fan.py); package path for the harness.
try:
    import reddit as rdt
    import sentiment as bsky
except ImportError:  # pragma: no cover
    from mcp import reddit as rdt
    from mcp import sentiment as bsky


class VibeError(Exception):
    pass


def _resolve(future):
    try:
        return future.result(), None
    except Exception as e:  # noqa: BLE001 — one source's failure must not sink the other
        return None, str(e)


def fan_sentiment(league=None, topic=None):
    # Fire both fetches at once — they're independent blocking IO, so parallel ~halves latency.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fr = ex.submit(rdt.reddit_sentiment, league, topic)
        fb = ex.submit(bsky.fan_sentiment, topic)
        r_out, r_err = _resolve(fr)
        b_out, b_err = _resolve(fb)
    if r_err and b_err:
        raise VibeError(f"both sources failed (reddit: {r_err}; bluesky: {b_err})")
    parts = [
        "Fan sentiment from TWO sources — weigh them together, and if Reddit and Bluesky "
        "clearly disagree, that gap is worth calling out. SENTIMENT, not fact; give YOUR read.",
        "\n=== REDDIT (deeper fan takes; search-based, can lag real-time) ===\n"
        + (r_out if r_out else f"(unavailable: {r_err})"),
        "\n=== BLUESKY (live/fresh; skews media) ===\n"
        + (b_out if b_out else f"(unavailable: {b_err})"),
    ]
    return "\n".join(parts)


TOOLS = {
    "fan_sentiment": {
        "fn": lambda a: fan_sentiment(a.get("league"), a.get("topic")),
        "schema": {
            "name": "fan_sentiment",
            "description": "The mood — what fans and media are saying — pulled from BOTH Reddit "
                           "(deeper fan takes) and Bluesky (fresher, media-leaning) at once. "
                           "Pass the league to aim Reddit at the right subreddit (nba/nfl/"
                           "soccer/etc.); add a topic/team/player to focus it, or omit for the "
                           "broad vibe. Use it when they ask the mood, the reaction, who's "
                           "getting cooked. This is SENTIMENT, not fact: read the room, then "
                           "give YOUR take — don't mirror it. If the two sources disagree, say "
                           "so. Verify any 'trade/signing' with sports_news before repeating it.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "league": {
                        "type": "string",
                        "description": "League, to pick Reddit's subreddit (nba, wnba, nfl, mlb, "
                                       "nhl, epl/laliga/ucl/wc -> soccer). Defaults to nba.",
                    },
                    "topic": {
                        "type": "string",
                        "description": "Team, player, or storyline (e.g. 'Lakers', 'LeBron', "
                                       "'Jaylen Brown trade'). Omit for the broad vibe.",
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
                "serverInfo": {"name": "ronin-fan-sentiment", "version": "0.0.0"},
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
            except VibeError as e:
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
        print("=== fan_sentiment('nba', 'Lakers') ===")
        print(fan_sentiment("nba", "Lakers"))
    else:
        serve()
