# ronin — changelog / build log

A running record of what's been built. Newest first. See `OVERVIEW.md` for the
architecture and `README.md` for how to run it.

---

## 2026-07 — from stat bot to an opinionated, autonomous agent

This stretch took ronin from a multi-sport lookup bot to the design doc's "full
version": persistent memory, an autonomous roam loop, proactive outreach, and a
personality that forms its own team allegiances — plus a security incident and fix.

### Soccer: World Cup + club football (2026-07-15)
Added soccer to the ESPN server (`mcp/espn.py`) — national **and** club, since the
2026 World Cup is on now.
- **8 new leagues**, same URL shape (`sport=soccer`): `wc` (FIFA World Cup — national),
  `epl`, `laliga`, `seriea`, `bundesliga`, `ligue1`, `ucl` (Champions League), `mls`.
  Listed after the US leagues so `find_team` still resolves US teams first. Aliases:
  bare "soccer"→World Cup (it's on), "premier league"→epl, "champions league"→ucl, etc.
  ("football" stays NFL — this is a US-sports bot.)
- **Points-based tables** (`_soccer_block` / `_stat_map`): soccer needed its own standings
  path — order by ESPN `rank`, show `P  W-D-L  GD±  Pts`, with a ✓ on teams that advanced
  (World Cup group stage). US W/L pct sort untouched.
- **Cup-final title detection** (`_cup_finals` / `_cup_champion`): `sports_champion` now
  covers soccer. Cups (World Cup, UCL) resolve the title from the **Final** match
  (`season.slug == "final"` → winner + penalty/score note). ESPN caps the scoreboard at
  ~100 events from the *start* of a range, so a whole-season window misses a late final —
  fixed by scanning **backward from the end in ~monthly chunks** (finds it in 1–2 calls;
  an in-progress cup finds none → "not decided, {stage} stage"). Top-5 European leagues
  report the table leader; MLS gives an honest "decided by MLS Cup playoffs" note.
- **Verified live:** UCL → "🏆 PSG won… beat Arsenal 1-1… 4-3 on penalties"; World Cup →
  "not decided — Semifinals"; group tables, team/news lookups all work. US leagues
  regression-free.
- **Deploy note:** the autonomous `reflect()` allegiance loop reads `ROAM_REFLECT_LEAGUES`
  (defaults `nba`) — set it to include soccer (e.g. `wc,ucl`) on Fly for ronin to form
  World Cup allegiances on its own. Chat/lookup already works with no env change.

### Earned team allegiances (2026-07-15)
ronin now roots **for and against** teams — earned from data, not hardcoded.
- **`affinity` memory axis** (`memory.py`): each team gets a score in `[-1, 1]` + a
  one-line reason, revised with a `history` trail, keyed by `league:abbrev`.
- **`reflect()` pass** (`roam.py`): a slow (~daily) tool-less Opus call over the *real*
  standings + champion + ronin's own takes, deriving which teams it's drawn to / roots
  against — grounded in stats, guarded to only accept leagues it actually fetched.
- **Value-seed** in `persona.md` (likes: player development, unselfish ball, defense,
  underdogs; dislikes: bought superteams, tanking, ring-chasing) so allegiances fall out
  of *taste meeting data*. Plus arguing rules (conviction matches confidence, self-aware
  homer, **allegiance never bends a fact**) and a **"never invent experiences"** rule.
- **Chat injection** (`ronin_reply.py`): current allegiances go into the system prompt so
  ronin roots and argues.
- Verified live: Spurs (earned — doubted-then-right), Knicks (a grudge from the Finals),
  OKC (respect-but-hedge). It argued a Knicks take using real game scores *without bending
  a fact*.

### Security: prompt-injection RCE fix (2026-07-15) — incident
- **Found (via code review):** the chat path ran `graff -p --yolo`, which auto-approves
  **all** tools — including graff's built-in `bash`/`read_file`/`write_file`/`webfetch`/
  `subagent`. Any stranger DMing the public bot could inject "run: echo $ANTHROPIC_API_KEY"
  and exfiltrate secrets / read other users' data / run arbitrary commands. **Confirmed
  exploitable** with a canary.
- **Contained:** stopped the live machine immediately.
- **Fixed:** kept `--yolo` (needed so the MCP servers connect) but added a graff **pre_tool
  hook** (`.harness/settings.json` → `.harness/tool-firewall.sh`) that **allowlists only the
  espn/sentiment MCP tools** and blocks every built-in (`exit 2`). Hooks fire even under
  `--yolo`. Defense in depth: stripped `TELEGRAM_BOT_TOKEN` from the chat subprocess env.
- **Verified** end-to-end and in-container: bash + webfetch blocked, MCP still works.
- **Follow-up for the owner:** rotate `TELEGRAM_BOT_TOKEN` + the Bluesky app-password
  (exfiltratable during the ~1-day live window), plus the long-pending Anthropic key.

### Published to GitHub (2026-07-15)
- Public repo: **github.com/madhavnair2009-arch/ronin** — README (rewritten for v1),
  OVERVIEW, ONEPAGER, MIT license, description + topics. Verified secret-free across full
  history before publishing.

### v1: roam loop + four-axis memory + proactive outreach (2026-07-14, live)
The design doc's "two halves, one brain."
- **`memory.py`** — persistent, file-locked JSON store on a durable Fly volume:
  living **takes** (revised with history), **relationships** (per-user team/chat/mute),
  **outbound** dedup log, **cursor** (news-delta).
- **`roam.py`** — autonomous loop: cheap news-delta pre-filter → one Opus judgment per new
  headline (`{notable, message, take}`) → belief revision + dedup + throttled proactive
  text. Cold-start baselines silently so new users aren't blasted.
- **Chat reads the living mind** (`ronin_reply.py`) + relationship injection; commands
  `/team`, `/mute`, `/unmute`.
- **Deploy:** single Fly machine (two Telegram pollers = 409), state on volume `ronin_state`
  → `/data`.

### Personality: human voice (2026-07-03)
- Rewrote `persona.md` from a thin slice to a real voice spec — texts like a friend
  (lowercase, contractions, short, dry, asks back), with an explicit "never sound like a
  bot" banlist. Grounded in the user's own texting style + real Bluesky tone.

### Multi-sport + WNBA (2026-07-03/04)
- Generalized the NBA-only ESPN server into one multi-league server (`mcp/espn.py`): every
  tool takes a `league` (nba/wnba/nfl/mlb/nhl/ncaaf/ncaam). Tools: `sports_scoreboard`,
  `sports_team`, `sports_standings`, `sports_news`, `sports_team_news`, `sports_champion`
  (Finals/Super Bowl/World Series/Stanley Cup, with offseason walk-back).
- Made fan sentiment (`fan_sentiment`, Bluesky) sport-agnostic.
- Fixed a champion-keyword collision ("finals" also matched "Semifinals").

---

## Earlier

### v0 + hosting (2026-06-30 → 2026-07-01)
- **v0:** stat lookup — a zero-dep MCP server over ESPN's public NBA JSON, driven by graff.
- **Hosting:** Telegram bot (long-poll = outbound-only, no inbound port) on Fly.io.
- **News + sentiment:** added news tools; landed on **Bluesky** for sentiment after
  discovering datacenter IPs are blanket-blocked by social platforms for *unauthenticated*
  reads (Reddit + Bluesky public both 403 from the cloud; only authenticated access works).

---

## Known open items (next up)
- **Take de-duplication (review #2):** takes key off team display name matched against
  LLM-authored subjects, so revision sometimes degrades to accumulation. The foundation to
  fix before deeper calibration.
- **Reliability (review #3):** `float(None)` crash path on a null confidence; a headline
  dropped permanently if the judge times out (marked seen before judging); unbounded
  `outbound.json` keys; greeting shows literal backticks; `.env` parser doesn't strip quotes.
- **Calibration engine (review #5 / v2):** score resolved takes against outcomes → *earned*
  traits, and let allegiances deepen with being right over a season. Capture `resolves_when`
  at take-formation time so takes are gradeable.
- **Deeper relationship memory:** running bits, past arguments, the user's own takes to
  throw back.
- **Tests (review #6):** none yet; `upsert_take`/`_extract_json`/the ESPN formatters are
  pure-function and trivially testable.
- **Sentiment sources:** X/Reddit still gated (paid/blocked); Bluesky skews media.
- **Dead code:** `mcp/reddit_nba.py` + kuri-fetch in the Dockerfile.
