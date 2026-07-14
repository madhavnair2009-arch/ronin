#!/usr/bin/env python3
"""
ronin — fan/media sentiment via r/nba, scraped through kuri-fetch.

Reddit's JSON API + OAuth got hostile (403 for us), so instead of Reddit creds we
drive `kuri-fetch` (justrach/kuri) — a tiny standalone fetcher that reads old.reddit's
HTML with a browser UA. No API key, no OAuth.

This is SENTIMENT, not fact (design doc: "Reddit = sentiment → personality fuel").
ronin reads the mood and reacts in its own voice; it does NOT mirror the hive mind.

Tool:
  nba_fan_sentiment(topic?)   hot r/nba if no topic; a search if a topic is given

Env:
  KURI_FETCH   path to the kuri-fetch binary (else found on PATH / ~/.local/bin)

Run:
  python3 reddit_nba.py            MCP stdio server
  python3 reddit_nba.py selftest   scrape live and print
"""

import html as htmllib
import json
import os
import re
import shutil
import subprocess
import sys

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _kuri_bin():
    env = os.environ.get("KURI_FETCH")
    if env and os.path.exists(env):
        return env
    found = shutil.which("kuri-fetch")
    if found:
        return found
    local = os.path.expanduser("~/.local/bin/kuri-fetch")
    return local if os.path.exists(local) else "kuri-fetch"


class SentimentError(Exception):
    pass


def _fetch(url, fmt="html", timeout=25):
    try:
        out = subprocess.run(
            [_kuri_bin(), "-q", "-d", fmt, "-U", UA, url],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise SentimentError("kuri-fetch not installed.")
    except subprocess.TimeoutExpired:
        raise SentimentError("Reddit fetch timed out.")
    if out.returncode != 0 or not out.stdout.strip():
        err = (out.stderr or "").strip().splitlines()
        raise SentimentError(err[-1] if err else "fetch failed")
    return out.stdout


# old.reddit front-page posts: each is a "thing" div carrying data-score /
# data-comments-count, followed by a `class="title"` anchor with the real title.
def _parse_listing(html_text):
    posts = []
    for chunk in html_text.split('data-fullname="t3_')[1:]:
        head = chunk[:1500]
        if 'data-promoted="true"' in head:
            continue
        sm = re.search(r'data-score="(-?\d+)"', head)
        cm = re.search(r'data-comments-count="(\d+)"', head)
        tm = re.search(r'class="title[^"]*"[^>]*>(.*?)</a>', chunk[:4000], re.S)
        if not tm:
            continue
        title = htmllib.unescape(re.sub(r"<[^>]+>", "", tm.group(1))).strip()
        if not title:
            continue
        posts.append({
            "title": title,
            "score": int(sm.group(1)) if sm else None,
            "comments": int(cm.group(1)) if cm else None,
        })
    return posts


# Search results use different markup: search-result blocks with search-title /
# search-score / search-comments. Parse those for full titles + scores.
def _parse_search(html_text):
    posts = []
    for b in re.split(r'class="search-result-header"', html_text)[1:14]:
        tm = re.search(r'class="search-title[^"]*"[^>]*>(.*?)</a>', b, re.S)
        if not tm:
            continue
        title = htmllib.unescape(re.sub(r"<[^>]+>", "", tm.group(1))).strip()
        if not title:
            continue
        sm = re.search(r'class="search-score[^"]*"[^>]*>([\d,]+)\s*points', b)
        cm = re.search(r'class="search-comments[^"]*"[^>]*>([\d,]+)\s*comments', b)
        posts.append({
            "title": title,
            "score": int(sm.group(1).replace(",", "")) if sm else None,
            "comments": int(cm.group(1).replace(",", "")) if cm else None,
        })
    return posts


# Last-resort fallback: recover titles from the permalink slugs

# /r/nba/comments/<id>/<slug>/ — lossy but reliable when the "thing" parse comes up short.
def _slug_titles(links_text):
    seen, posts = set(), []
    for m in re.finditer(r"/r/nba/comments/([a-z0-9]+)/([a-z0-9_]+)/", links_text):
        pid, slug = m.group(1), m.group(2)
        if pid in seen:
            continue
        seen.add(pid)
        title = slug.replace("_", " ").strip().capitalize()
        if title:
            posts.append({"title": title, "score": None, "comments": None})
    return posts


def fan_sentiment(topic=None):
    if topic:
        url = (f"https://old.reddit.com/r/nba/search?q={topic.replace(' ', '+')}"
               "&restrict_sr=on&sort=top&t=month")
        header = f"What r/nba is saying about '{topic}' (fan sentiment, NOT fact):"
    else:
        url = "https://old.reddit.com/r/nba/"
        header = "Top of r/nba right now (fan sentiment, NOT fact):"

    if topic:
        posts = _parse_search(_fetch(url, "html"))
    else:
        posts = _parse_listing(_fetch(url, "html"))
    if len(posts) < 3:  # markup drift — fall back to slug titles from permalinks
        posts = _slug_titles(_fetch(url, "links"))
    if not posts:
        return (f"Couldn't find r/nba chatter on '{topic}'." if topic
                else "Couldn't read r/nba right now.")

    lines = [header]
    for p in posts[:8]:
        meta = ""
        if p["score"] is not None:
            meta = f"[{p['score']} pts"
            if p["comments"] is not None:
                meta += f", {p['comments']} comments"
            meta += "] "
        lines.append(f"• {meta}{p['title']}")
    return "\n".join(lines)


TOOLS = {
    "nba_fan_sentiment": {
        "fn": lambda a: fan_sentiment(a.get("topic")),
        "schema": {
            "name": "nba_fan_sentiment",
            "description": "What NBA fans/media are actually saying on r/nba — the mood, "
                           "hot takes, reactions, what's blowing up. Pass a topic/team/"
                           "player to focus it, or omit for what's hot right now. This is "
                           "SENTIMENT, not fact: gauge the vibe, then react in your own "
                           "voice — don't just mirror it.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Team, player, or storyline (e.g. 'Lakers', 'LeBron', "
                                       "'Jaylen Brown trade'). Omit for r/nba hot.",
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
                "serverInfo": {"name": "ronin-reddit-nba", "version": "0.0.0"},
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
