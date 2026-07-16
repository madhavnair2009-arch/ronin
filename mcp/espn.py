#!/usr/bin/env python3
"""
ronin — ESPN multi-sport data, exposed as an MCP stdio server.

Zero dependencies (Python stdlib only), matching graff's ethos. This is the
"world facts" fuel for ronin: scores/standings/records/news come from a real
API as ground truth, never the LLM. (See ronin-design.md: never blur facts &
opinions.)

Every league ESPN carries lives at the same URL shape:
    https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/...
so one generic server covers NBA / NFL / MLB / NHL / college plus soccer —
national (World Cup) and club (Premier League, La Liga, Serie A, Bundesliga,
Ligue 1, Champions League, MLS). Each tool takes a `league` argument. Soccer
differs in two spots: points-based tables (W-D-L + goal difference) and titles
decided by a cup Final (World Cup, UCL) rather than a US-style series.

Tools:
  sports_scoreboard(league, date?)         games + live/final scores for a day
  sports_team(league, query)               resolve a team -> record, standing, next game
  sports_standings(league, group?)         standings (optional group/division filter)
  sports_news(league, limit?)              league-wide news headlines + summaries
  sports_team_news(league, query, limit?)  news for one team
  sports_champion(league)                  most recent decided title + series/game

Run modes:
  python3 espn.py            speak MCP JSON-RPC over stdio (how graff calls it)
  python3 espn.py selftest   hit the live API and print results (no MCP)
"""

import datetime
import hashlib
import json
import sys
import urllib.request
import urllib.error

UA = "ronin/0.0 (graff MCP; +https://github.com/)"  # ESPN blocks empty UAs

# league key -> (sport, espn-league-path, human label)
# Soccer keys share the same URL shape (sport="soccer", path=ESPN competition slug),
# but soccer needs points-based tables and cup-final title detection — see the
# SOCCER / SOCCER_CUPS sets below, which drive those branches.
LEAGUES = {
    "nba":   ("basketball", "nba", "NBA"),
    "wnba":  ("basketball", "wnba", "WNBA"),
    "nfl":   ("football", "nfl", "NFL"),
    "mlb":   ("baseball", "mlb", "MLB"),
    "nhl":   ("hockey", "nhl", "NHL"),
    "ncaaf": ("football", "college-football", "College Football"),
    "ncaam": ("basketball", "mens-college-basketball", "Men's College Basketball"),
    # soccer — national + club. Listed after the US leagues so team resolution
    # (find_team) prefers a US match before scanning soccer.
    "wc":         ("soccer", "fifa.world", "World Cup"),
    "epl":        ("soccer", "eng.1", "Premier League"),
    "laliga":     ("soccer", "esp.1", "La Liga"),
    "seriea":     ("soccer", "ita.1", "Serie A"),
    "bundesliga": ("soccer", "ger.1", "Bundesliga"),
    "ligue1":     ("soccer", "fra.1", "Ligue 1"),
    "ucl":        ("soccer", "uefa.champions", "Champions League"),
    "mls":        ("soccer", "usa.1", "MLS"),
}

# soccer keys (points tables, W-D-L, goal difference) vs the US W/L model
SOCCER = {k for k, v in LEAGUES.items() if v[0] == "soccer"}
# knockout cups — titles decided by a Final match, not a league table
SOCCER_CUPS = {"wc", "ucl"}
# single-table leagues where the title = top of the final table (top-5 Europe).
# MLS is soccer but its title is the MLS Cup playoffs, so it's excluded here and
# handled with an honest note in _soccer_champion.
SOCCER_TABLE_TITLES = SOCCER - SOCCER_CUPS - {"mls"}

# forgiving input -> canonical league key
ALIASES = {
    "basketball": "nba", "hoops": "nba",
    "football": "nfl", "nfl football": "nfl", "pro football": "nfl",
    "baseball": "mlb",
    "hockey": "nhl", "ice hockey": "nhl",
    "college football": "ncaaf", "cfb": "ncaaf",
    "college basketball": "ncaam", "cbb": "ncaam", "ncaab": "ncaam",
    "mens college basketball": "ncaam",
    "women's basketball": "wnba", "womens basketball": "wnba",
    # soccer. "football"/"soccer" are ambiguous in a US-sports bot: "football"
    # stays NFL (above); bare "soccer" defaults to the World Cup (it's on now).
    "soccer": "wc", "world cup": "wc", "fifa": "wc", "the world cup": "wc",
    "premier league": "epl", "prem": "epl", "english premier league": "epl", "epl football": "epl",
    "la liga": "laliga", "spanish league": "laliga",
    "serie a": "seriea", "italian league": "seriea",
    "bundesliga football": "bundesliga", "german league": "bundesliga",
    "ligue 1": "ligue1", "ligue un": "ligue1", "french league": "ligue1",
    "champions league": "ucl", "champions": "ucl", "uefa champions league": "ucl", "ucl football": "ucl",
    "major league soccer": "mls",
}

# championship config: (headline keyword, wins-to-clinch, start MMDD, end MMDD, year offset)
# year offset = games are played this many calendar years after ESPN's season.year
# (NFL's Super Bowl for the 2025 season is played in Feb 2026, so offset 1).
# keyword must be specific enough to NOT match earlier rounds: "finals" alone also
# matches "Semifinals"/"Conference Finals", so use the full title-round name.
CHAMP = {
    "nba":   ("nba finals", 4, "0601", "0710", 0),
    "wnba":  ("wnba finals", 4, "0915", "1110", 0),
    "nfl":   ("super bowl", 1, "0201", "0216", 1),
    "mlb":   ("world series", 4, "1015", "1110", 0),
    "nhl":   ("stanley cup", 4, "0525", "0705", 0),
}


class SportError(Exception):
    pass


def _league(league):
    key = (league or "").strip().lower()
    key = ALIASES.get(key, key)
    if key not in LEAGUES:
        known = ", ".join(sorted(LEAGUES))
        raise SportError(f"Unknown league '{league}'. Try one of: {known}.")
    return key


def _base(key):
    sport, path, _ = LEAGUES[key]
    return f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{path}"


def _standings_url(key):
    sport, path, _ = LEAGUES[key]
    return f"https://site.api.espn.com/apis/v2/sports/{sport}/{path}/standings"


def _label(key):
    return LEAGUES[key][2]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Tool implementations -> return plain strings (the text content for the LLM)
# ---------------------------------------------------------------------------
def scoreboard(league, date=None):
    key = _league(league)
    url = f"{_base(key)}/scoreboard"
    if date:
        url += f"?dates={date}"
    data = _get(url)
    # Sort by kickoff so the first line is always the earliest game — "what's the
    # first/next game" must not depend on ESPN's (usually-but-not-guaranteed) order.
    events = sorted(data.get("events", []), key=lambda e: e.get("date", ""))
    lbl = _label(key)
    if not events:
        return f"No {lbl} games found for {date or 'today'}."
    lines = []
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        status = (ev.get("status") or {}).get("type", {})
        detail = status.get("shortDetail", "")
        away = home = None
        for c in comp.get("competitors", []):
            side = c.get("homeAway")
            team = (c.get("team") or {}).get("abbreviation", "?")
            score = c.get("score", "")
            rec = ""
            for rr in c.get("records", []) or []:
                if rr.get("type") in ("total", None):
                    rec = rr.get("summary", "")
                    break
            entry = (team, score, rec)
            if side == "away":
                away = entry
            elif side == "home":
                home = entry
        if away and home:
            a = f"{away[0]} {away[2]}".strip()
            h = f"{home[0]} {home[2]}".strip()
            if status.get("state") == "pre":
                lines.append(f"{a} @ {h} — {detail}")
            else:
                lines.append(f"{a} {away[1]} @ {h} {home[1]} — {detail}")
        else:
            lines.append(ev.get("shortName", ev.get("name", "game")))
    return f"{lbl} games ({date or 'today'}):\n" + "\n".join(lines)


_TEAM_CACHE = {}


def _teams(key):
    if key not in _TEAM_CACHE:
        data = _get(f"{_base(key)}/teams")
        teams = []
        for t in data["sports"][0]["leagues"][0]["teams"]:
            teams.append(t["team"])
        _TEAM_CACHE[key] = teams
    return _TEAM_CACHE[key]


def _resolve_team(key, query):
    q = query.strip().lower()
    teams = _teams(key)
    for t in teams:  # exact-ish matches first
        fields = [
            t.get("abbreviation", ""),
            t.get("nickname", ""),
            t.get("name", ""),
            t.get("location", ""),
            t.get("displayName", ""),
        ]
        if q in [f.lower() for f in fields if f]:
            return t
    for t in teams:  # substring fallback
        if q in t.get("displayName", "").lower():
            return t
    return None


def team(league, query):
    key = _league(league)
    if not query:
        return f"Pass a team name, e.g. sports_team('{key}', 'Lakers')."
    t = _resolve_team(key, query)
    if not t:
        return f"No {_label(key)} team matched '{query}'."
    detail = _get(f"{_base(key)}/teams/{t['id']}")
    tm = detail.get("team", {})
    name = tm.get("displayName", t.get("displayName"))
    rec = ""
    for it in (tm.get("record") or {}).get("items", []):
        if it.get("type") == "total" or not rec:
            rec = it.get("summary", "")
    standing = tm.get("standingSummary", "")
    out = [name]
    if rec:
        out.append(f"Record: {rec}")
    if standing:
        out.append(f"Standing: {standing}")
    nxt = tm.get("nextEvent") or []
    if nxt:
        ev = nxt[0]
        out.append(f"Next: {ev.get('shortName', ev.get('name', ''))} — {ev.get('date', '')}")
    return "\n".join(out)


def _wlt(stats):
    w = l = t = 0
    for s in stats:
        try:
            v = int(float(s.get("value", 0) or 0))
        except (TypeError, ValueError):
            v = 0
        n = s.get("name")
        if n == "wins":
            w = v
        elif n == "losses":
            l = v
        elif n == "ties":
            t = v
    return w, l, t


def _collect_groups(node, out):
    st = node.get("standings")
    if isinstance(st, dict) and st.get("entries"):
        out.append((node.get("name", ""), st["entries"]))
    for child in node.get("children", []) or []:
        _collect_groups(child, out)


def _stat_map(stats):
    """{stat name -> int value} for one standings entry (soccer uses named stats)."""
    out = {}
    for s in stats:
        n = s.get("name")
        if not n:
            continue
        v = s.get("value")
        try:
            out[n] = int(float(v)) if v is not None else 0
        except (TypeError, ValueError):
            out[n] = 0
    return out


def _wl_block(gname, entries):
    """US-sports table: rank by win pct, show W-L(-T)."""
    parsed = []
    for e in entries:
        tm = e.get("team", {})
        tname = tm.get("abbreviation") or tm.get("displayName", "?")
        w, l, t = _wlt(e.get("stats", []))
        pct = w / (w + l) if (w + l) else 0.0
        parsed.append((pct, w, l, t, tname))
    parsed.sort(key=lambda r: r[0], reverse=True)  # ESPN entries aren't pre-sorted
    rows = []
    for i, (_, w, l, t, tname) in enumerate(parsed, 1):
        rec = f"{w}-{l}" + (f"-{t}" if t else "")
        rows.append(f"{i:>2}. {tname:<4} {rec}")
    return gname + "\n" + "\n".join(rows) if rows else ""


def _soccer_block(gname, entries):
    """Soccer table: order by ESPN rank, show P W-D-L, goal diff, points.
    A ✓ marks teams already advanced (World Cup group stage)."""
    parsed = []
    for e in entries:
        tm = e.get("team", {})
        tname = tm.get("abbreviation") or tm.get("displayName", "?")
        m = _stat_map(e.get("stats", []))
        parsed.append((
            m.get("rank", 0), m.get("gamesPlayed", 0), m.get("wins", 0),
            m.get("ties", 0), m.get("losses", 0), m.get("pointDifferential", 0),
            m.get("points", 0), m.get("advanced", 0), tname,
        ))
    # rank is ESPN's tiebroken order; fall back to points then goal diff if absent.
    if all(p[0] > 0 for p in parsed):
        parsed.sort(key=lambda r: r[0])
    else:
        parsed.sort(key=lambda r: (r[6], r[5]), reverse=True)
    rows = []
    for i, (rank, gp, w, d, l, gd, pts, adv, tname) in enumerate(parsed, 1):
        pos = rank if rank > 0 else i
        mark = " ✓" if adv else ""
        rows.append(f"{pos:>2}. {tname:<4} {gp}gp {w}-{d}-{l} GD{gd:+d} {pts}pts{mark}")
    return gname + "\n" + "\n".join(rows) if rows else ""


def standings(league, group=None):
    key = _league(league)
    data = _get(_standings_url(key))
    groups = []
    for child in data.get("children", []) or []:
        _collect_groups(child, groups)
    if not groups:  # some leagues put entries at the root
        _collect_groups(data, groups)
    filt = (group or "").strip().lower()
    build = _soccer_block if key in SOCCER else _wl_block
    blocks = []
    for gname, entries in groups:
        if filt and filt not in gname.lower():
            continue
        block = build(gname, entries)
        if block:
            blocks.append(block)
    if not blocks:
        if filt:
            return f"No {_label(key)} standings group matched '{group}'."
        return f"Could not load {_label(key)} standings."
    return "\n\n".join(blocks)


def _format_articles(arts, header):
    lines = [header]
    for a in arts:
        date = a.get("published", "")[:10]
        head = a.get("headline", "")
        desc = a.get("description", "")
        line = f"• [{date}] {head}"
        if desc:
            line += f"\n  {desc}"
        lines.append(line)
    return "\n".join(lines)


def news(league, limit=12):
    key = _league(league)
    limit = min(max(int(limit or 12), 1), 30)
    data = _get(f"{_base(key)}/news?limit={limit}")
    arts = data.get("articles", [])
    lbl = _label(key)
    if not arts:
        return f"No recent {lbl} news found."
    return _format_articles(arts, f"Latest {lbl} news (trades, signings, storylines):")


def team_news(league, query, limit=8):
    key = _league(league)
    if not query:
        return f"Pass a team, e.g. sports_team_news('{key}', 'Pistons')."
    t = _resolve_team(key, query)
    if not t:
        return f"No {_label(key)} team matched '{query}'."
    limit = min(max(int(limit or 8), 1), 20)
    data = _get(f"{_base(key)}/news?team={t['id']}&limit={limit}")
    arts = data.get("articles", [])
    name = t.get("displayName", query)
    if not arts:
        return f"No recent news found for the {name}."
    return _format_articles(arts, f"Latest {name} news:")


def _finals_games(key, year, kw, win):
    """Pull decided championship games played in `year` for this league."""
    kwd, _wins_needed, s_md, e_md, _yoff = CHAMP[key]
    url = f"{_base(key)}/scoreboard?dates={year}{s_md}-{year}{e_md}&seasontype=3"
    data = _get(url)
    games = []
    for e in data.get("events", []):
        comp = (e.get("competitions") or [{}])[0]
        notes = comp.get("notes") or []
        head = notes[0].get("headline", "") if notes else ""
        if kwd not in head.lower():
            continue
        if (e.get("status") or {}).get("type", {}).get("state") != "post":
            continue  # only completed games
        teams = [
            {
                "abbr": c.get("team", {}).get("abbreviation", "?"),
                "score": c.get("score", ""),
                "winner": c.get("winner", False),
            }
            for c in comp.get("competitors", [])
        ]
        games.append({"date": e.get("date", "")[:10], "head": head, "teams": teams})
    return games


def _iso_to_date(s):
    try:
        return datetime.date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def _cup_finals(key, start, end):
    """Find the Final match(es) of a cup. ESPN caps the scoreboard at ~100 events
    from the *start* of a date range, so a whole-season window misses a late final.
    Scan backward from the end in ~monthly chunks — a decided final is at the last
    stage, so it turns up in the first chunk or two; an in-progress cup finds none."""
    lo = _iso_to_date(start)
    hi = _iso_to_date(end)
    if not lo or not hi:
        return []
    today = datetime.date.today()
    if hi > today:  # don't scan past today (padded end dates, in-progress cups)
        hi = today
    step = datetime.timedelta(days=35)
    cur = hi
    for _ in range(18):  # safety bound; covers a ~1.5yr season in monthly hops
        if cur < lo:
            break
        w_lo = max(cur - step, lo)
        span = f"{w_lo.strftime('%Y%m%d')}-{cur.strftime('%Y%m%d')}"
        try:
            events = _get(f"{_base(key)}/scoreboard?dates={span}").get("events", [])
        except urllib.error.URLError:
            events = []
        finals = [e for e in events if (e.get("season") or {}).get("slug") == "final"]
        if finals:
            return finals
        cur = w_lo - datetime.timedelta(days=1)
    return []


def _cup_champion(key, disp, stage, start, end):
    """Knockout cup (World Cup, Champions League): the title is the Final match.
    ESPN tags the final event with season.slug == 'final'."""
    finals = _cup_finals(key, start, end)
    for e in sorted(finals, key=lambda x: x.get("date", ""), reverse=True):
        comp = (e.get("competitions") or [{}])[0]
        state = (e.get("status") or {}).get("type", {}).get("state")
        comps = comp.get("competitors", [])
        note = ""
        if comp.get("notes"):
            note = comp["notes"][0].get("headline", "")
        if state == "post":
            win = next((c for c in comps if c.get("winner")), None)
            los = next((c for c in comps if not c.get("winner")), None)
            wn = (win or {}).get("team", {}).get("displayName", "?")
            ln = (los or {}).get("team", {}).get("displayName", "?")
            score = f"{(win or {}).get('score','')}-{(los or {}).get('score','')}"
            out = f"🏆 {wn} won the {disp} — beat {ln} {score} in the final."
            return out + (f"\n{note}" if note and note.lower() not in out.lower() else "")
        names = " vs ".join(c.get("team", {}).get("displayName", "?") for c in comps)
        return f"{disp}: the final is set — {names} ({e.get('date','')[:10]}). Not played yet."
    return f"The {disp} isn't decided yet — currently the {stage} stage." if stage \
        else f"The {disp} isn't decided yet."


def _table_champion(key, disp):
    """Single-table league: the title is top of the table (champions once it ends)."""
    try:
        data = _get(_standings_url(key))
    except urllib.error.URLError as e:
        return f"Couldn't load {disp} standings ({e})."
    groups = []
    for child in data.get("children", []) or []:
        _collect_groups(child, groups)
    if not groups:
        _collect_groups(data, groups)
    best = None  # (rank, gp, pts, gd, name)
    for _, entries in groups:
        for e in entries:
            m = _stat_map(e.get("stats", []))
            tname = e.get("team", {}).get("displayName", "?")
            cand = (m.get("rank", 0) or 999, m.get("gamesPlayed", 0),
                    m.get("points", 0), m.get("pointDifferential", 0), tname)
            if best is None or cand[0] < best[0]:
                best = cand
    if best is None:
        return f"Couldn't load {disp} standings."
    rank, gp, pts, gd, tname = best
    if gp == 0:
        return f"The {disp} season hasn't kicked off yet — no standings to call a leader from."
    return f"{disp}: {tname} lead the table — {pts} pts, GD {gd:+d}, from {gp} games. " \
           f"Title's theirs if they finish top."


def _soccer_champion(key):
    sb = _get(f"{_base(key)}/scoreboard")
    season = (sb.get("leagues") or [{}])[0].get("season", {}) or {}
    disp = season.get("displayName") or _label(key)
    stage = (season.get("type") or {}).get("name", "")
    start = season.get("startDate", "")[:10].replace("-", "")
    end = season.get("endDate", "")[:10].replace("-", "")
    if key in SOCCER_CUPS:
        return _cup_champion(key, disp, stage, start, end)
    if key in SOCCER_TABLE_TITLES:
        return _table_champion(key, disp)
    # MLS: decided by the MLS Cup playoffs, not the table — don't fake a champion.
    return (f"{disp} titles are decided by the MLS Cup playoffs, not the table — "
            f"check sports_scoreboard('mls') for the latest playoff results.")


def champion(league):
    key = _league(league)
    if key in SOCCER:
        return _soccer_champion(key)
    if key not in CHAMP:
        return f"No championship lookup available for {_label(key)} yet."
    kwd, wins_needed, s_md, e_md, yoff = CHAMP[key]
    sb = _get(f"{_base(key)}/scoreboard")
    cur = (sb.get("leagues") or [{}])[0].get("season", {}).get("year")
    if not cur:
        cur = datetime.date.today().year
    # Walk back from the current season until we find a decided title (handles
    # offseason, where the current season's playoffs haven't happened yet).
    for season in range(cur, cur - 3, -1):
        play_year = season + yoff
        games = _finals_games(key, play_year, kwd, wins_needed)
        if not games:
            continue
        games.sort(key=lambda g: g["date"])
        wins, lines = {}, []
        for g in games:
            winner = next((t for t in g["teams"] if t["winner"]), None)
            loser = next((t for t in g["teams"] if not t["winner"]), None)
            if winner:
                wins[winner["abbr"]] = wins.get(winner["abbr"], 0) + 1
            label = g["head"].strip() or g["date"]
            if winner and loser:
                lines.append(f"{label}: {winner['abbr']} def. {loser['abbr']} "
                             f"{winner['score']}-{loser['score']}")
        if not wins:
            continue
        series = ", ".join(f"{t} {w}" for t, w in sorted(wins.items(), key=lambda x: -x[1]))
        champ = next((t for t, w in wins.items() if w >= wins_needed), None)
        lbl = _label(key)
        if champ:
            head = f"{season} {lbl} — 🏆 {champ} won it (series {series})."
        else:
            lead = max(wins.items(), key=lambda x: x[1])
            head = f"{season} {lbl} — in progress, {lead[0]} leads (series {series})."
        return head + "\n" + "\n".join(lines)
    return f"Couldn't find a decided {_label(key)} title in the last few seasons."


# ---------------------------------------------------------------------------
# Helpers for the roam loop (not MCP tools — imported directly by roam.py).
# ---------------------------------------------------------------------------
def recent_headlines(league, team=None, limit=10):
    """Raw news items for delta-detection: [{key, headline, desc, date}]."""
    key = _league(league)
    if team:
        t = _resolve_team(key, team)
        if not t:
            return []
        url = f"{_base(key)}/news?team={t['id']}&limit={limit}"
    else:
        url = f"{_base(key)}/news?limit={limit}"
    out = []
    for a in _get(url).get("articles", []):
        head = a.get("headline", "")
        if not head:
            continue
        k = hashlib.sha1(head.encode("utf-8")).hexdigest()[:12]
        out.append({
            "key": k,
            "headline": head,
            "desc": a.get("description", ""),
            "date": a.get("published", "")[:10],
        })
    return out


def find_team(query, league=None):
    """Resolve a team, optionally searching all leagues. Returns (league_key, team) or (None, None)."""
    if not query:
        return None, None
    order = [_league(league)] if league else list(LEAGUES)
    for key in order:
        try:
            t = _resolve_team(key, query)
        except SportError:
            continue
        if t:
            return key, t
    return None, None


# ---------------------------------------------------------------------------
# MCP tool registry
# ---------------------------------------------------------------------------
_LEAGUE_PROP = {
    "type": "string",
    "description": "Which league. US: nba, wnba, nfl, mlb, nhl, ncaaf (college "
                   "football), ncaam (men's college basketball). Soccer: wc "
                   "(FIFA World Cup — national teams), epl (Premier League), laliga, "
                   "seriea, bundesliga, ligue1, ucl (Champions League), mls.",
}

TOOLS = {
    "sports_news": {
        "fn": lambda a: news(a.get("league", ""), a.get("limit", 12)),
        "schema": {
            "name": "sports_news",
            "description": "Latest league-wide news headlines with summaries — trades, "
                           "free-agent signings, injuries, storylines — for any league "
                           "(NBA/NFL/MLB/NHL/college). Use for 'what's the latest / any "
                           "news / trade buzz' questions.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "league": _LEAGUE_PROP,
                    "limit": {"type": "integer",
                              "description": "How many articles (default 12, max 30)."},
                },
                "required": ["league"],
            },
        },
    },
    "sports_team_news": {
        "fn": lambda a: team_news(a.get("league", ""), a.get("query", ""), a.get("limit", 8)),
        "schema": {
            "name": "sports_team_news",
            "description": "Recent news for one team in a league — their signings, trades, "
                           "injuries, storylines. Use for 'what's going on with the <team>'.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "league": _LEAGUE_PROP,
                    "query": {"type": "string",
                              "description": "Team name/city/abbrev (e.g. 'Pistons', 'DET')."},
                    "limit": {"type": "integer",
                              "description": "How many articles (default 8, max 20)."},
                },
                "required": ["league", "query"],
            },
        },
    },
    "sports_champion": {
        "fn": lambda a: champion(a.get("league", "")),
        "schema": {
            "name": "sports_champion",
            "description": "Most recent decided championship for a league — the winner, "
                           "the series score, and game-by-game results. Covers NBA Finals, "
                           "Super Bowl (NFL), World Series (MLB), Stanley Cup (NHL), plus "
                           "soccer: the World Cup (wc) and Champions League (ucl) finals, and "
                           "the current table leader for epl/laliga/seriea/bundesliga/ligue1. "
                           "Use for 'who won the title/cup/world cup/super bowl' questions.",
            "inputSchema": {
                "type": "object",
                "properties": {"league": _LEAGUE_PROP},
                "required": ["league"],
            },
        },
    },
    "sports_scoreboard": {
        "fn": lambda a: scoreboard(a.get("league", ""), a.get("date")),
        "schema": {
            "name": "sports_scoreboard",
            "description": "Games and live/final scores for a day in any league, returned "
                           "earliest-kickoff first (so the first line is the first game that "
                           "day). Ground-truth from ESPN. Date optional (default today). For "
                           "'first/next/opening game' questions, read the earliest game here "
                           "rather than guessing the date.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "league": _LEAGUE_PROP,
                    "date": {"type": "string",
                             "description": "Any day as YYYYMMDD (e.g. 20260315), past or "
                                            "future, not just today. Omit for today. Use this "
                                            "for 'tomorrow', 'this weekend', a specific date."},
                },
                "required": ["league"],
            },
        },
    },
    "sports_team": {
        "fn": lambda a: team(a.get("league", ""), a.get("query", "")),
        "schema": {
            "name": "sports_team",
            "description": "Resolve a team by name/abbreviation in a league and return its "
                           "current record, standing, and next game.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "league": _LEAGUE_PROP,
                    "query": {"type": "string",
                              "description": "Team name, city, nickname, or abbrev "
                                             "(e.g. 'Lakers', 'BOS', 'Lions')."},
                },
                "required": ["league", "query"],
            },
        },
    },
    "sports_standings": {
        "fn": lambda a: standings(a.get("league", ""), a.get("group")),
        "schema": {
            "name": "sports_standings",
            "description": "Current standings for a league, grouped by conference/division. "
                           "Optional `group` filters to one conference/division by name "
                           "(e.g. 'East', 'AFC', 'NL West'); omit for all.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "league": _LEAGUE_PROP,
                    "group": {"type": "string",
                              "description": "Conference/division name filter (e.g. 'West', "
                                             "'AFC North'); omit for the whole league."},
                },
                "required": ["league"],
            },
        },
    },
}


# ---------------------------------------------------------------------------
# MCP stdio server (JSON-RPC 2.0, newline-delimited)
# ---------------------------------------------------------------------------
PROTOCOL_VERSION = "2024-11-05"


def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _result(id_, result):
    _send({"jsonrpc": "2.0", "id": id_, "result": result})


def _rpc_error(id_, code, message):
    _send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


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
        is_request = id_ is not None

        if method == "initialize":
            client_ver = (msg.get("params") or {}).get("protocolVersion", PROTOCOL_VERSION)
            _result(id_, {
                "protocolVersion": client_ver,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ronin-espn", "version": "0.0.0"},
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            _result(id_, {"tools": [t["schema"] for t in TOOLS.values()]})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            tool = TOOLS.get(name)
            if not tool:
                _rpc_error(id_, -32602, f"Unknown tool: {name}")
                continue
            try:
                text = tool["fn"](args)
                _result(id_, {"content": [{"type": "text", "text": text}]})
            except SportError as e:
                _result(id_, {"content": [{"type": "text", "text": str(e)}], "isError": True})
            except urllib.error.URLError as e:
                _result(id_, {
                    "content": [{"type": "text", "text": f"ESPN request failed: {e}"}],
                    "isError": True,
                })
            except Exception as e:  # noqa: BLE001 — surface as tool error, don't crash server
                _result(id_, {
                    "content": [{"type": "text", "text": f"Tool error: {e}"}],
                    "isError": True,
                })
        elif is_request:
            _rpc_error(id_, -32601, f"Method not found: {method}")


def selftest():
    for lg in ("nba", "nfl", "mlb", "nhl"):
        print(f"\n########## {lg.upper()} ##########")
        print("--- scoreboard ---")
        print(scoreboard(lg))
        print("\n--- standings ---")
        print(standings(lg)[:600])
        print("\n--- news ---")
        print(news(lg, 3))
        print("\n--- champion ---")
        print(champion(lg))
    print("\n--- team(nba, Lakers) ---")
    print(team("nba", "Lakers"))
    print("\n--- team(nfl, Lions) ---")
    print(team("nfl", "Lions"))
    for lg in ("wc", "ucl", "epl"):
        print(f"\n########## {lg.upper()} (soccer) ##########")
        print("--- scoreboard ---")
        print(scoreboard(lg))
        print("\n--- standings (first block) ---")
        print(standings(lg).split("\n\n")[0])
        print("\n--- champion ---")
        print(champion(lg))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
    else:
        serve()
