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
  (last code commit `d2d5e92`). Everything below is live. Harness **71/71**.
- **No open correctness bugs.** The grader is now proven end-to-end (was silently crash-broken —
  see below). Next up: confirm real roam takes get `deadline`s set (item 1) and close out the key
  revocation (item 7).

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

**Shipped 2026-07-22 (live, `372a373`): the shared-cursor bug — the last open correctness issue.**
The news cursor was `f"{league}:{team.lower()}"` but read/written inside the per-user loop, so
two people on the same team shared it: the first user's pass consumed the headlines and the
second never heard them. Worse than "misses one" — `cursor_is_cold()` read the same key, so the
second user never *baselined* either and was starved permanently. **`mood.json` had the identical
defect, and subtler:** a vibe judge only calls a shift when the mood differs from the one it last
saw, and `set_mood()` lands mid-loop, so the first user's write became the second user's "prior"
and their judge saw a steady mood. **Both are now `f"{uid}:{league}:{team}"`.** Scoping mood per
user costs nothing — the sentiment fetch and the judge already ran per user (the judge
personalizes off `_recent_texts(uid)`). Verified live: both axes baselined silently on the new
keys, **0 pings**, no back-blast. Regression case pins both axes and was **confirmed to fail
against the old code** (`reached [101]` — only the first user). Harness **69/69**.

**This retires the "two halves don't share state" family** (judge-timeout → proactive-ping
context → shared cursor). The optional refactor that would prevent a fourth: have roam write to
the graff session transcript, so roam and chat share more than `memory.py`.

*Leftover:* the pre-fix keys (`nba:golden state warriors`, `nfl:san francisco 49ers`,
`wnba:dallas wings` in `cursor.json`; `nba:golden state warriors` in `mood.json`) are orphaned
and unreachable — harmless, but can be deleted whenever.

**Shipped 2026-07-23 (live, `d2d5e92`): the grader had NEVER worked.** `_grade_one` built its
context with `time.strftime(...)` but `roam.py` imports `datetime`, not `time`, so `grade()`
`NameError`ed on the first due take and settled nothing. It went undetected because it had never
run: no roam-formed take on the volume carries a `deadline` (all four takes are hand-authored
seeds), so the grader's worklist was always empty, and the calibration data test only ever called
`resolve_take`/`get_record` directly, never reaching `_grade_one`. Found by seeding a **verifiable**
synthetic take (Spain won the 2026 WC, checkable via `sports_champion`) on the live volume and
running `grade()` — it crashed exactly there. Fixed (`import time`), added `_check_grade_pass`
(drives `grade()` with only the graff call stubbed so `_grade_one` runs its real body; confirmed
it fails against the old code), and **proved it live end-to-end on the fixed container**: graded
the Spain take a HIT off the real tool, conf 0.6→0.7, wrote a grounded `calibration.json` entry.
Synthetic take + record restored from backup afterward (the volume must never carry a fake
"1-0, nailed the Spain call"). Harness **71/71**.

1. **Watch calibration in the wild — now the *mechanism* is proven, the open question is the
   judge's inputs.** The grader fires and grades correctly; what's still unverified is whether
   real roam-formed takes get sensible `resolves_when`/`deadline` values (the judge sets them, in
   `run_once`). No deadline = never graded, so the whole track record stays empty. Spot-check
   `/data/takes.json` after a few news-heavy days (season ramps ~Sept); if roam takes show up with
   `deadline: null`, that's the next thing to chase — in the JUDGE prompt, not the grader.
   - Grading spends tool calls (budget 6) per overdue take; cheap now, watch cost as they accrue.
   - Deferral pushes a stuck take's deadline +7d each unclear pass — make sure nothing thrashes.
2. **Relationship digest tuning.** `digest()` runs every ~4h (`ROAM_DIGEST_EVERY=8`) off the
   graff session transcript. Sessions live in `/app` (ephemeral, not `/data`) — a redeploy wipes
   them, so a fresh machine re-digests from scratch. If that matters, move sessions onto `/data`.
3. ~~**The two-halves state family.**~~ **Closed 2026-07-22** — all three fixed (judge-timeout,
   proactive-ping context, shared cursor + mood). Root cause stands, though: roam and chat share
   `memory.py` but not the graff transcript. **The pattern to watch for in new code:** any state
   read or written inside the per-user loop but keyed without the uid. A "roam writes to the
   session transcript" refactor would remove the class outright.
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
