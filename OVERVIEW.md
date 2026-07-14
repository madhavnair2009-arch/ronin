# ronin — system overview (team walkthrough)

> An autonomous, internet-roaming sports agent with a personality and a memory.
> You text it for scores/news/takes; it also texts *you* when your team does something.
> Built on `graff` (the codegraff agent harness). Live on Telegram as **@sportsronin_bot**.

---

## 1. The bet (why this exists)

- **Utility is the hook** — instant scores, standings, news, "who won the chip," across NBA/WNBA/NFL/MLB/NHL/college. Real numbers, never guessed.
- **Personality is the retention** — you stay because it's a *someone*: it has opinions, it remembers you, it revises its own takes, and it reaches out on its own.
- **The moat we're building** — the evolving memory + relationship, not the stats wrapper (anyone can wrap an API + an LLM). That layer is ours to build; no harness gives it to you.

**The one hard rule underpinning everything:** never blur facts and opinions. A fun bot that confidently states the *wrong score* is the worst possible outcome. Facts come from a real API as ground truth; personality is subjective on top.

---

## 2. How it's wired (two halves, one brain)

```
        ┌─────────────────────────  ONE SHARED BRAIN (persistent memory on /data) ──────────────────────────┐
        │   takes (beliefs)   ·   relationships (you)   ·   outbound log (dedup)   ·   news cursor (facts)    │
        └───────────────▲───────────────────────────────────────────────────────────────▲───────────────────┘
                        │ reads + writes                                                  │ writes
   ┌────────────────────┴───────────────────┐                          ┌─────────────────┴────────────────────┐
   │  CHAT SURFACE  (reads the mind)         │                          │  ROAM LOOP  (builds the mind)         │
   │  telegram_bot.py  ── long-polls         │                          │  roam.py  ── scheduler every 30 min   │
   │       └─ ronin_reply.py                 │                          │    1. cheap news-delta pre-filter     │
   │            └─ graff -p (Opus)           │                          │    2. Opus judges each new headline   │
   │                 ├─ persona + live takes │                          │    3. revise belief + dedup           │
   │                 ├─ your relationship    │                          │    4. throttled proactive text → you  │
   │                 └─ tools (ESPN/Bluesky) │                          └───────────────────────────────────────┘
   └─────────────────────────────────────────┘
```

- **Chat surface** = foreground, on-demand. You message → ronin answers in voice, pulling live facts + its own takes + what it knows about you.
- **Roam loop** = background, autonomous. It watches your team's news, forms/revises opinions, and decides — unprompted — whether something's worth texting you about.
- They **share one persistent memory**, so a take the roam loop forms at 2pm shows up in a chat reply at 8pm. That's the whole point.

---

## 3. The four memory axes (this is the real work)

| Axis | What it holds | Rule | Where it lives |
|---|---|---|---|
| **World facts** | scores, records, standings, news | API is truth, never stored — always re-fetched | ESPN live + a `cursor` for "what news have I seen" |
| **Its beliefs** | takes / opinions | **revised, not appended** — keeps a `history` trail so it can say "I was wrong" | `state/takes.json` |
| **You** | your team, how to reach you, mute/throttle | per-user relationship | `state/relationships.json` |
| **Its temperament** | voice, disposition | stable seed, doesn't drift | `persona.md` (flat file) |
| *(+ plumbing)* | what it already told you | dedup so it never double-pings | `state/outbound.json` |

**A take is a living record**, not a one-off hot take:
```
{ subject, stance, confidence, reasoning, evidence:[headline-hashes],
  formed_at, updated_at, history:[prior stances...] }
```
The roam loop's job per storyline is *"does new evidence move my stance?"* — not "write a new opinion." That buys coherence, the ability to say "I called this / I was wrong," and eventually an **earned calibration trait** ("I keep underrating this GM, grain of salt on my skepticism") — the thing a hardcoded persona can't fake.

---

## 4. Components (what's in the repo)

| File | Role |
|---|---|
| `telegram_bot.py` | Transport. Long-polls Telegram (outbound-only, no inbound port). Commands, rate limit, runs the roam scheduler thread. |
| `ronin_reply.py` | Transport-independent core. Drives `graff -p` with persona + **living** takes + your relationship. Any transport (Signal, SMS, web) could call it. |
| `memory.py` | The persistent brain. Locked JSON store (safe for the bot + roam writing at once), atomic writes. |
| `roam.py` | The autonomous loop. Pre-filter → Opus judgment → belief revision → throttled proactive send. |
| `persona.md` | Voice + temperament + the hard fact/opinion rule. The stable core. |
| `takes.json` | Seed beliefs that bootstrap the living store on first run. |
| `mcp/espn.py` | Multi-sport data server (ESPN public JSON, no key). |
| `mcp/sentiment.py` | Fan/media sentiment (Bluesky, authenticated). |
| `mcp/reddit_nba.py` | Parked Reddit scraper (works residential-only, not wired up). |
| `Dockerfile` / `fly.toml` / `.mcp.json` | Container + Fly config + which MCP servers to load. |

### Capabilities (tools ronin can call)
- **Facts (ESPN):** `sports_scoreboard`, `sports_team`, `sports_standings`, `sports_news`, `sports_team_news`, `sports_champion` — each takes a league: `nba / wnba / nfl / mlb / nhl / ncaaf / ncaam`. `sports_champion` returns the most recent decided title (Finals / Super Bowl / World Series / Stanley Cup), even in the offseason.
- **Sentiment (Bluesky):** `fan_sentiment(topic?)` — the vibe/hot takes, for any sport. Treated as *sentiment, not fact*; ronin reads the room but doesn't mirror it.

### Commands
- `/team [league] <name>` — tell ronin your team → opt into proactive pings
- `/mute` / `/unmute` — pause/resume proactive texts
- `/start` `/help` — intro

---

## 5. Hosting & cost reality

- **Fly.io**, one always-on machine (region iad), state on a durable **volume** (`/data`). Exactly one machine on purpose — two Telegram pollers collide.
- **Telegram long-poll** = outbound-only, so no public URL/webhook/inbound port. Runs anywhere.
- **Anthropic Opus** per chat turn and per roam judgment. Cost is gated: the roam loop's cheap **news-delta pre-filter** means no model call fires unless a watched team's news actually changed, and proactive pings are throttled to ≤1 / 6h per user.
- **Data sources are ToS-gray** (ESPN's unofficial endpoints, social scraping/auth). Fine for a prototype; a real product likely needs a licensed data path.

---

## 6. Build status

| Stage | State |
|---|---|
| **v0** — stat lookup, one API, chat, no memory | ✅ done |
| **+** multi-sport, news, sentiment, human voice | ✅ done |
| **v1** — roam loop, 4-axis memory, belief revision, relationship, proactive outreach | ✅ **live** |
| **v2** — passive team inference, tiered cheap-model ingest, nightly reflection/**calibration** (score past predictions → earned traits), `memeory` as the beliefs recall layer, multi-sport fan-out | ⏭ next |

### Key lessons baked in
- **Datacenter IPs are blanket-blocked** by social platforms for *unauthenticated* reads (Reddit, Bluesky public, X). Only authenticated access works from the cloud → we use Bluesky app-password auth.
- **Fact/opinion firewall** is load-bearing and gets *more* important as memory grows (never let the model trust its own stale prose over the tools).
- **Independent, not a hive-mind mirror** — the personality knob. If it just echoes the timeline, it has no personality.

---

## 7. Open questions for the team

**Top three (decide these; they steer everything):**
1. **How opinionated/contrarian is ronin allowed to be?** The spine — it rewrites the roam judgment prompt. Who decides when a take was "wrong"? Should ronin have *allegiances*?
2. **Where are the users, and what's the real data source?** Telegram vs iMessage/Discord/web; Bluesky (thin) vs fighting for Reddit/X (rich but blocked) vs paying for licensed data.
3. **What's the actual moat?** If a big player ships "sports GPT" tomorrow, is it the evolving memory/relationship, the personality, or nothing? Be honest.

**Also worth chewing on:**
- Utility-with-personality vs. a *companion* where utility is the excuse to stay?
- One-sport depth vs. multi-sport breadth (we went broad — was that right)?
- Autonomy: what earns an unsolicited message, and what's our "annoyance budget"? Do we ever start a convo with *no* news trigger?
- Unit economics at 100 / 10k users on Opus-per-turn — and monetization (subscription? free)?
- Trust/liability: how do we test the fact firewall holds at scale? Any risk in spicy takes about real athletes?

## 8. Concrete next steps (candidates)
- [ ] **Calibration engine** (v2's differentiator): score resolved takes → earned traits. The hardest-to-copy thing.
- [ ] **Passive team inference** so relationship memory grows from conversation, not just `/team`.
- [ ] **Tiered model routing** (cheap Haiku-class for ingest/filtering, Opus only for opinion-forming + chat) — the cost story.
- [ ] **Reddit/X access** decision (fight, pay, or stay on Bluesky).
- [ ] **Instrument it**: log cost/turn, proactive send rate, engagement — so the economics/annoyance questions have data.
