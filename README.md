# ronin

**An autonomous sports agent with a personality and a memory.** Text it for scores, news, and takes across NBA, WNBA, NFL, MLB, NHL & college — and it texts *you* when your team does something worth knowing. Built on [`graff`](https://github.com/justrach/codegraff) (an agentic harness) + Claude. Live on Telegram as **@sportsronin_bot**.

> **Status: v1 — live.** Roam loop, four-axis persistent memory, belief revision, and proactive outreach are all shipped. See the roadmap below.

---

## The idea

- **Utility is the hook** — instant, accurate stats. Real numbers from a real API, never guessed.
- **Personality is the retention** — it's a *someone*: it holds opinions, remembers you, revises its own takes over time, and reaches out unprompted.
- **The one hard rule:** never blur facts and opinions. A bot that states the wrong score confidently is the worst outcome — so facts come from the API as ground truth, and personality is subjective on top.

For the full walkthrough see [`OVERVIEW.md`](OVERVIEW.md); for the exec summary, [`ONEPAGER.md`](ONEPAGER.md).

---

## How it works — two halves, one brain

- **Chat surface** (reads the mind): you message → ronin answers in voice, pulling live facts + its own takes + what it knows about you.
- **Roam loop** (builds the mind): on a schedule it scans your team's news, forms/revises opinions, and decides on its own whether something is worth texting you about.
- Both share **one persistent memory**, so a take formed at 2pm shows up in a chat reply at 8pm.

### The four memory axes

| Axis | Holds | Rule |
|---|---|---|
| **World facts** | scores, records, standings, news | API is truth — never stored, always re-fetched |
| **Its beliefs** | takes / opinions | *revised, not appended* — keeps a `history` trail so it can say "I was wrong" |
| **You** | your team, reachability, mute/throttle | per-user relationship |
| **Its temperament** | voice, disposition | stable seed, doesn't drift (`persona.md`) |

Plus an **outbound log** (dedup, so it never double-pings you) and a **news cursor** (so it only reacts to genuinely new headlines — the pre-filter that keeps an always-on loop cheap).

---

## Capabilities

**Facts — ESPN public JSON (no key), one tool set across every league** (`nba`, `wnba`, `nfl`, `mlb`, `nhl`, `ncaaf`, `ncaam`):

| Tool | Returns |
|---|---|
| `sports_scoreboard(league, date?)` | games + live/final scores |
| `sports_team(league, query)` | record, standing, next game |
| `sports_standings(league, group?)` | standings by conference/division |
| `sports_news(league, limit?)` | league-wide headlines + summaries |
| `sports_team_news(league, query)` | one team's news |
| `sports_champion(league)` | most recent decided title (Finals / Super Bowl / World Series / Stanley Cup), even in the offseason |

**Sentiment — Bluesky:** `fan_sentiment(topic?)` — the vibe/hot takes for any sport. Treated as *sentiment, not fact*: ronin reads the room but doesn't mirror it.

### Telegram commands
- `/team [league] <name>` — tell ronin your team → opt into proactive pings (e.g. `/team pistons`, `/team nhl rangers`)
- `/mute` · `/unmute` — pause/resume proactive texts
- `/start` · `/help` — intro

---

## Repo layout

```
ronin/
  telegram_bot.py    Telegram transport (long-poll) + commands + roam scheduler
  ronin_reply.py     transport-independent core; drives graff with persona + live takes + relationship
  memory.py          persistent brain (locked JSON store: takes / relationships / outbound / cursor)
  roam.py            autonomous roam loop (pre-filter → judge → revise → throttled proactive send)
  persona.md         voice + temperament + the hard fact/opinion rule
  takes.json         seed beliefs (bootstrap the living store)
  mcp/espn.py        multi-sport data MCP server (ESPN public JSON)
  mcp/sentiment.py   Bluesky fan-sentiment MCP server
  .mcp.json          graff MCP registration
  Dockerfile / fly.toml   container + Fly.io deploy config
  OVERVIEW.md / ONEPAGER.md   team walkthrough + exec summary
```

---

## Run it

Requires [`graff`](https://github.com/justrach/codegraff) on your PATH and `ANTHROPIC_API_KEY` set. Servers are Python **stdlib only** — no pip install.

```sh
# sanity-check the data layer against the live API (no MCP, no LLM):
python3 mcp/espn.py selftest

# one-shot reply through the core (per-sender memory session):
python3 ronin_reply.py me "who won the chip and how are the Pistons looking?"

# a single autonomous roam pass, without sending anything:
python3 roam.py --dry

# run the Telegram bot (needs TELEGRAM_BOT_TOKEN):
python3 telegram_bot.py
```

**Environment:** `ANTHROPIC_API_KEY` (graff), `TELEGRAM_BOT_TOKEN` (bot), `BSKY_HANDLE` + `BSKY_APP_PASSWORD` (sentiment). Keep them in a gitignored `.env`.

## Deploy

Runs on [Fly.io](https://fly.io) as one always-on machine (the Telegram long-poller is outbound-only — no inbound port), with persistent memory on a mounted volume:

```sh
fly volumes create ronin_state --size 1 -r iad   # once
fly secrets set ANTHROPIC_API_KEY=... TELEGRAM_BOT_TOKEN=... BSKY_HANDLE=... BSKY_APP_PASSWORD=...
fly deploy --remote-only
```

Exactly **one** machine on purpose — two Telegram pollers collide. See [`DEPLOY.md`](DEPLOY.md).

---

## Design notes

- **Facts never go through the LLM's memory.** Scores/records/standings/champions come straight from ESPN; the model only phrases them, and re-fetches every time. This is the "never blur facts and opinions" rule, enforced structurally.
- **Opinions are *revised*, not regenerated.** A take is a living record (`subject`, `stance`, `confidence`, `reasoning`, `evidence`, `history`). The roam loop asks "does new evidence move my stance?" — not "write a new hot take." That's what lets ronin stay coherent and say "I called this" / "I was wrong."
- **Independent, not a hive-mind mirror.** If it just echoes the timeline, it has no personality. When a fanbase melts down over one loss, ronin says "it's one game, breathe."
- **Cheap by construction.** No model call fires in the roam loop unless a watched team's news actually changed; proactive pings are throttled.
- **Zero-dep MCP servers.** Hand-rolled JSON-RPC over stdio — no SDK, stdlib only. Tool errors surface as `isError` results instead of crashing the server.

## Roadmap

- **v0** — stat lookup, one API, no memory ✅
- **+** multi-sport, news, fan sentiment, human voice ✅
- **v1** — roam loop, four-axis memory, belief revision, relationship memory, proactive outreach ✅ **live**
- **v2** — calibration engine (score resolved takes → *earned* traits like "I keep underrating this GM"), passive team inference, tiered cheap-model ingest for cost, and a dedicated recall layer for beliefs

---

*ronin is a prototype. It uses ESPN's public (unofficial) endpoints and Bluesky's API; treat data sources accordingly.*
