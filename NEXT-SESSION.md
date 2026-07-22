# ronin — next-session handoff

Cold-start brief for picking this back up. For the full architecture see `OVERVIEW.md`,
for the build log see `CHANGELOG.md`. Last worked: **2026-07-22**.

---

## What ronin is
A sports-obsessed Telegram bot (`@sportsronin_bot`) with opinions and a memory. Built on
**graff** (a CLI agent). Facts come from ESPN tools (never the LLM); the personality and
allegiances are its own. Two halves: a **chat** path (you text it, it replies) and an
autonomous **roam** loop (forms takes, proactively pings, reflects on allegiances).

## Live status
- **Deployed & healthy** on Fly.io — app **`ronin-sports`** (region `iad`, one machine).
- Repo: **github.com/madhavnair2009-arch/ronin** — local `main` == `origin/main` == deployed
  (last commit `145ac03`). Everything below is live. Harness **66/66**.

---

## What this session shipped (2026-07-15 → 19)
- **⚽ Soccer** — 8 leagues in `mcp/espn.py`: `wc` (World Cup, national) + `epl/laliga/seriea/
  bundesliga/ligue1/ucl/mls` (club). Points-based tables, cup-final title detection.
- **🔑 Key rotation** — `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `BSKY_APP_PASSWORD` all
  rotated on Fly (resolves the earlier RCE-incident exposure). **Confirm the OLD ones were
  revoked at source if not already.**
- **🗣️ Persona overhaul** (`persona.md`) — killed em dashes + the rest of the seven AI tells,
  no catchphrase parroting, occasional (not reflexive) questions/debates, a **tragedy floor**
  (drop the bit for genuine tragedy), and a **betting stance** (leans yes, locks/“bet the
  rent” no, never invents odds).
- **📅 Date correctness** — inject today/tomorrow into the system prompt; `sports_scoreboard`
  sorts earliest-first; “first/next/opening game” and “what day is A vs B” pull the no-date
  slate instead of guessing; games carry the real **weekday computed in US Eastern**
  (`tzdata` added to the image).
- **👥 Multi-team memory** — one team **per league** (49ers *and* Warriors coexist). Migrates
  the old single-team shape. New `/teams`, `/team clear <sport>`. Graceful “which sport?” when
  asked about a sport with no saved team. Fixed a `/team` read/write race (commands run
  synchronously now).
- **🌍 World Cup allegiances** — `ROAM_REFLECT_LEAGUES="nba,wc"` in `fly.toml`; reflection now
  feeds **knockout results** for cups (not just stale group tables); `top_affinities` surfaces
  each league’s top pick so a timely take isn’t buried.
- **🧪 Eval harness** (`eval/run.py`) — **25/25 green**. Three layers: `data` (offline, free),
  `integration` (live ESPN, no LLM), `behavior` (model-in-the-loop on seeded memory, with a
  no-em-dash assertion on every reply).

---

## Operational cheatsheet
```sh
# run the eval harness (do this before any deploy)
python3 eval/run.py                 # all 3 layers (behavior costs API $)
python3 eval/run.py --data-only     # free offline checks only
python3 eval/run.py --no-llm        # data + integration, no API cost

# deploy after a code change
fly deploy -a ronin-sports
fly logs -a ronin-sports            # tail
fly status -a ronin-sports

# poke the deployed container (ESPN + memory live on /data there, NOT locally)
fly ssh console -a ronin-sports -C "python3 -c 'import sys; sys.path.insert(0,\"/app\"); import ronin_reply; print(ronin_reply.reply(\"u\",\"<msg>\"))'"

# data-layer selftest (no LLM)
python3 mcp/espn.py selftest
```
- **Secrets:** on Fly (`fly secrets list -a ronin-sports`). Local `~/ronin/.env` (gitignored)
  has the rotated Anthropic key; its Telegram/Bsky values are **stale** (pre-rotation) — update
  if you run the full bot locally.
- **The user’s Telegram id:** `8532852228`. Current saved team: Golden State Warriors (NBA).
  They may still have leftover test teams; their live chat session may hold stale early turns.
- **Model:** `claude-opus-4-8` (`RONIN_MODEL`).

---

## Hard-won gotchas (don’t relearn these)
- **Test the model, not just the data.** Twice this session a data-layer fix “passed” but the
  bot was still wrong (first-game bug). Always run a behavior eval before declaring a fix done.
- **Test where the memory lives.** Chat behavior depends on `/data` (the Fly volume). A local
  `ronin_reply.reply` reads *local* state and can mislead — run it in the container, or seed a
  temp state dir (the harness does this).
- **LLM fixes are probabilistic.** The matchup fix went 2/4 before a stronger prompt made it
  5/5. Run a behavior case a few times, not once, before trusting it.
- **ESPN offseason quirks:** `sports_team`’s “next game” is blank in the offseason (use the
  no-date `sports_scoreboard`); the default scoreboard caps at ~100 events from the *start* of
  a range (scan backward for late cup finals); season `type.name` lags the real stage.
- **Em dashes leak from tool-output strings**, not just the model. Grep `mcp/espn.py` for `—`
  when touching output formatters.

---

## Open items / next up (rough priority)
**Shipped 2026-07-20/21 (all live):** reliability pass (judge-timeout retry, null-confidence
guard, bounded `outbound.json`); proactive-ping context in the chat prompt (follow-ups resolve);
**calibration + take de-dup** (topic-slug identity, `resolves_when`/`deadline`, `grade()` pass,
earned track record); **affinity decay** (stale France retired live via `reflect()`); **deeper
relationship memory** (`digest()` pass → per-user profile); **web search** (`mcp/web.py`,
search-only, kuri-fetch → DDG SERP); **blended Reddit+Bluesky sentiment** (`mcp/fan.py`, one
`fan_sentiment` tool, Reddit two-tier: OAuth-if-creds else `site:reddit.com` search);
**proactive vibe-shift pings** (`sentiment_sweep()` + `mood.json`, every ~12h).

**Shipped 2026-07-22 (live, `145ac03`):** **`<thinking>` leak fixed.** graff's built-in harness
prompt lets the model narrate its reasoning in `<thinking>` tags and `-p` prints the whole answer
to stdout, but `reply()` returned that stdout verbatim, so the narration shipped into Telegram
(`<thinking>Simple intro question…</thinking>ronin. i live in sports way too much`). Stripped at
the transport boundary (`_strip_thinking` in `ronin_reply.py`) rather than via a persona rule,
since prompt rules only hold probabilistically. Chat-only: every roam path reads its output
through `_extract_json`, so pings were never affected. Guarded by 6 data cases **plus a universal
behavior assertion** (any reply carrying a thinking tag fails the harness, mirroring the
no-em-dash rule). Harness **66/66**.

### ⭐ START HERE tomorrow — the shared-cursor bug (backlog #2)
The last open correctness issue, and the last of the "two halves don't share state" family.
`scope` in `roam.run_once` is `f"{league}:{team.lower()}"` with **no uid** (roam.py, the news
loop). It's read/written inside the per-user loop, so if two users follow the same team, the
first user's pass marks the team's headlines seen and the **second user never hears them**.
Same silent-news-loss family as the judge-timeout bug (already fixed) and the proactive-ping
context bug (already fixed). Fix: put the uid in the scope key (`f"{uid}:{league}:{team}"`).
That cold-starts every cursor once → each baselines silently (no back-blast), so it's safe.
Also check the `mood.json` scope (`sentiment_sweep`) — it's currently `league:team` too, but
mood is arguably shared across users of a team, so decide per-axis. Add a regression case
(two users, same team, both get the news) to the harness.

1. **Watch calibration in the wild.** The machinery is live but unproven on real outcomes:
   - `grade()` only fires on takes that carry a `deadline` — the judge sets it, so confirm real
     roam-formed takes actually get sensible `resolves_when`/`deadline` values (spot-check
     `/data/takes.json` after a few news-heavy days). No deadline = never graded.
   - Grading spends tool calls (budget 6) per overdue take; cheap now (few deadlined takes), but
     watch cost once they accumulate. `calibration.json` holds the record.
   - Deferral pushes a stuck take's deadline +7d each unclear pass — make sure nothing thrashes.
2. **Relationship digest tuning.** `digest()` runs every ~4h (`ROAM_DIGEST_EVERY=8`) off the
   graff session transcript. Sessions live in `/app` (ephemeral, not `/data`) — a redeploy wipes
   them, so a fresh machine re-digests from scratch. If that matters, move sessions onto `/data`.
3. **The two-halves state family (recurring).** Three bugs now share one root: roam and chat
   share `memory.py` but not the graff transcript. Fixed piecemeal (proactive-ping context) but
   the **shared-cursor bug is still open** — `scope` in `run_once` is `league:team` with no uid,
   so if two users follow the same team, the first user's pass marks headlines seen and the
   second never hears them. A "roam writes to the session transcript" refactor could retire the
   whole family.
4. ~~**Fact-grounding spot-check.**~~ **Closed 2026-07-22 — false alarm.** The "Matisse Thybulle,
   1yr $3.3M" claim is verbatim from `sports_team_news`: *"Sources: Matisse Thybulle agrees to
   1-year deal with Lakers … a one-year, $3.3 million deal"* (ESPN, 2026-07-22). No transactions
   tool is needed — **signings surface in the news feed**, so ESPN headlines already cover them.
   The persona held. *Residual, minor:* it also called him a "29 year old defensive wing", which
   is accurate but **not** in the tool output (the headline says "forward", no age) — so the real
   fact-grounding risk isn't invented numbers, it's correct-sounding **biographical** detail
   coming from model knowledge with no source behind it. Worth watching, not chasing.
5. **Web + Reddit search watch.** `mcp/web.py` (search-only, DDG HTML SERP) and `mcp/reddit.py`
   both fetch DDG from the Fly IP via kuri-fetch. Live now. Watch: (a) if DDG starts 429/403ing
   the datacenter IP like Reddit did, swap the SERP host or move to a search API; (b) the SERP
   parser is markup-fragile (pinned test + live integration check guard it). **Reddit is
   two-tier:** no creds -> reads Reddit via `site:reddit.com/r/<sub>` search (works today, but
   DDG's Reddit index is NOT real-time, so fresh topics can surface stale threads — ronin
   flags this itself). If official API access ever lands, set `REDDIT_CLIENT_ID`/`SECRET` and
   it auto-upgrades to the live OAuth API (scores/comments/search). Old scrape-based
   `reddit_nba.py` deleted. Rejected soci.ly (Tor/proxy scraper: fragile + unauthorized).
6. **Session hygiene** — graff `--resume` sessions grow unbounded and anchor to stale answers.
   Consider trimming/resetting. **Repo hygiene:** ~60 loose `sess_*.json` test files in the root.
7. **Confirm old API keys were revoked at source** (rotation was on Fly; revocation unverified).

---

## Quick-win toggles
- Add a club league to reflection: `ROAM_REFLECT_LEAGUES` (fly.toml) → e.g. `nba,wc,ucl`
  (capped at 3). Redeploy, then run the reflect one-liner below to form takes immediately.
- Clear a user’s stale test teams: `memory.clear_team("8532852228", "<league>")`.
- **Run a background pass now** (note: `fly ssh -C` has no shell, so `os.chdir` inline, no `cd`):
  `fly ssh console -a ronin-sports -C "python3 -c \"import sys;sys.path.insert(0,'/app');import os;os.chdir('/app');import roam;roam.reflect()\""`
  Swap `roam.reflect()` for `roam.grade()` (settle overdue takes) or `roam.digest()` (refresh
  people-memory). Cadences: `ROAM_DIGEST_EVERY=8` (~4h), `ROAM_GRADE_EVERY`/`ROAM_REFLECT_EVERY=48` (~daily).
- Inspect calibration: `memory.get_record()` (hits/misses/accuracy); takes carry
  `status`/`deadline`/`resolves_when`. People-memory: `memory.get_profile("<uid>")`.
