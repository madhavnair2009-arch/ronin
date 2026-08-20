# ronin — next-session handoff

Cold-start brief for picking this back up. For the full architecture see `OVERVIEW.md`,
for the build log see `CHANGELOG.md`. Last worked: **2026-08-20**.

---

## What ronin is
A sports-obsessed Telegram bot (`@sportsronin_bot`) with opinions and a memory. Built on
**graff** (a CLI agent). Facts come from ESPN tools (never the LLM); the personality and
allegiances are its own. Two halves: a **chat** path (you text it, it replies) and an
autonomous **roam** loop (forms takes, proactively pings, reflects on allegiances).

## Live status
- **Deployed & healthy** on Fly.io — app **`ronin-sports`** (region `iad`, one machine).
- Repo: **github.com/madhavnair2009-arch/ronin** — local `main` == `origin/main` == deployed
  (last commit `20dc0af`). Everything below is live. Harness **121/121**
  (data 93 / integration 14 / behavior 14) — fully green for the first time; the long-standing
  opener flake is fixed, see below. Two behavior cases go red whenever DuckDuckGo is
  unreachable (that's item C, not a product bug) — re-run before believing them.
- **Tool surface is 8:** `sports_scoreboard`, `sports_standings`, `sports_team`, `sports_news`,
  `sports_team_news`, `sports_champion`, **`sports_player`**, **`sports_roster`** (last two added
  2026-08-16/17). Plus `fan_sentiment` (mcp/fan.py) and `web_search` (mcp/web.py). The firewall
  allowlists `mcp__espn__*` wholesale, so new espn tools need no firewall change — pinned by a test.
- **Model routing is LIVE (2026-07-30).** Chat + roam judge/vibe/digest run on `claude-sonnet-5`;
  reflect + grade stay on `claude-opus-4-8`. Verified in-container: chat replies in-voice with no
  em dashes, roam passes run clean. Kill switch: set `RONIN_ROAM_MODEL=claude-opus-4-8` (roam) or
  revert `fly.toml` `RONIN_MODEL` (chat) if a pass looks off. Haiku is still a one-line flip away
  (`_CHEAP` in roam.py + `RONIN_MODEL`) once graff's one-shot adaptive-thinking block is resolved.
- **API key moved to the team workspace (2026-07-30).** `ANTHROPIC_API_KEY` rotated on Fly
  (digest `31f9f82af7024d4a`); both `claude-opus-4-8` and `claude-sonnet-5` confirmed reachable on
  it. **Still TODO (user):** revoke the OLD key at console.anthropic.com, set a workspace spend
  limit, update local `~/ronin/.env`.
- **Per-user personality is LIVE (2026-08-16).** Every user gets their own ronin: same voice,
  same values, plus three rolled taste traits and one signature team. Kill switch
  `RONIN_CHARACTER=0`. First organic roll landed 2026-08-17 — the owner (uid `8532852228`,
  a Warriors fan) rolled `chaotic/deep-bench/physical` → **Detroit Pistons**.
- **No open correctness bugs.** All three reported this session are fixed, deployed and verified
  in-container. See below.

---

## What this session shipped (2026-08-16 → 17)

Three bugs and one feature. **Every bug came from the owner's own Telegram screenshots, and all
three were invisible to a harness that was green at 71/71** — the tests only looked where we'd
already been burned. Harness went 71 → 112.

**1. Repeated storyline content** (`02a9e0d`). Ronin pinged about Don Nelson's death (Aug 9) and
again about his memorial (Aug 14), re-explaining nellieball to someone already told. **Not a dedup
failure** — ESPN ran three separate Nelson stories and the memorial was real news, so suppressing
it would have been wrong. The judge composed each message against `memory.recent_sent`'s **48h**
default, so the Aug 9 ping (~114h back) was simply absent and it re-derived the background. The
anti-repeat rule was obeyed against a silently truncated context — same family as the shared-cursor
bug. Judge now has its own window (`JUDGE_RECALL_SECS` 14d, `JUDGE_RECALL_N` 12); **chat stays at
48h on purpose**, pinned by a test.

**2. Ungrounded player facts** (`02a9e0d`). Ronin called RJ Harvey a "rookie" in his second season.
There was no player-level tool at all, so every claim about a person came from model weights. **The
dateline injection cannot help here** — knowing the date tells you when it is, not what's changed
since. Added **`sports_player`**.

**3. The volunteered-teammate leak** (`20dc0af`). Fixing #2 moved the defect rather than removing
it: grounded on the player *asked about*, ronin still invented the teammate he *volunteered*
("splitting carries with Javonte Williams" — a Cowboy). Added **`sports_roster`**. 4/4 clean after.

**4. Per-user personality** (`d4c029a`, `8818faf`). See the section below.

### Gotchas earned 2026-08-16/17 (don't relearn these)
- **A green harness proves nothing about code paths it doesn't touch.** Three user-visible bugs
  lived under 71/71. When a bug is reported, find the *seam*, then ask what else shares it.
- **`dry_run=True` on `roam.run_once` is NOT safe on the live volume.** It still calls
  `mark_seen` and `log_sent`; only `_tg_send` is gated. A dry pass silently consumes real
  headlines and logs them as sent, eating a genuine ping. `_judge` *is* safe to call directly.
- **The Dockerfile `COPY` is an explicit allowlist.** `character.py` deployed green and silently
  did nothing because it was never in the image. It failed quietly (the import is wrapped in
  try/except) so nothing flagged it — only an in-container check caught it. Now guarded by a test.
- **Substring matching on sports abbreviations is a trap.** "rb" is inside "quarte*rb*ack" and
  "corne*rb*ack", so the first roster filter returned QBs and CBs. Match the abbrev exactly or a
  whole word. `C`/`G`/`F` are worse.
- **Verify a fix is a fix, and verify a failure is pre-existing.** Both directions cost one
  command (`git stash`, or a couple of re-runs) and both were load-bearing today.

---

## Earlier — what the 2026-07-15 → 19 session shipped
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
- **Model:** `claude-sonnet-5` for chat (`RONIN_MODEL`, pinned in `fly.toml`) and for roam
  judge/vibe/digest/character; `claude-opus-4-8` for reflect + grade. See the routing note above.
- **Their ronin:** `chaotic/deep-bench/physical` → Detroit Pistons (rolled 2026-08-17).
- **Env kill switches:** `RONIN_CHARACTER=0` (per-user personality off),
  `RONIN_ROAM_MODEL` (all roam on one model), `ROAM_JUDGE_RECALL` (judge memory window, secs).

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
- **Em dashes leak from tool-output strings**, not just the model. Swept 2026-08-20: 83 em
  dashes removed from every runtime string across `mcp/*.py`, `roam.py`, `ronin_reply.py`,
  `character.py`, `telegram_bot.py` (tool output, tool DESCRIPTIONS, and the roam prompt
  addenda — the addenda mattered, they were modelling the very punctuation the persona bans).
  Docstrings and comments were left alone; they never reach the model. **The one em dash that
  must stay is `_EMDASH` in `ronin_reply.py`** — that regex IS the guard, it has to match the
  character. `_normalize_voice` still backstops all of this at the boundary.

---

## Open items / next up (rough priority)
> **Start here: the ⭐ NEXT UP block further down** (updated 2026-08-20) is the current queue.
> Everything above it in this section is historical context for how the system got here.

**Shipped 2026-07-20/21 (all live):** reliability pass (judge-timeout retry, null-confidence
guard, bounded `outbound.json`); proactive-ping context in the chat prompt (follow-ups resolve);
**calibration + take de-dup** (topic-slug identity, `resolves_when`/`deadline`, `grade()` pass,
earned track record); **affinity decay** (stale France retired live via `reflect()`); **deeper
relationship memory** (`digest()` pass → per-user profile); **web search** (`mcp/web.py`,
search-only, kuri-fetch → DDG SERP); **blended Reddit+Bluesky sentiment** (`mcp/fan.py`, one
`fan_sentiment` tool, Reddit two-tier: OAuth-if-creds else `site:reddit.com` search);
**proactive vibe-shift pings** (`sentiment_sweep()` + `mood.json`, every ~12h).

**Shipped 2026-07-29/30 (live, `503c910`): per-path model routing + em-dash guard — the
sustainability groundwork for opening to many testers.** Opus-on-everything doesn't scale, and the roam loop is
the real cliff (it fans out per user × team × pass on a schedule, so cost tracks *signups*, not
chat volume). Now every graff call routes its own model: chat + news-judging + vibe + digest →
cheap tier, reflect + grade (rare) → Opus. Per-task env vars, with `RONIN_ROAM_MODEL` as an
all-roam kill switch.
- **Cheap tier is Sonnet, not Haiku, and that's a graff limitation:** graff forces adaptive
  thinking in one-shot (`-p`) mode and Haiku 4.5 rejects it. Sonnet works today and is still a big
  drop from Opus. Flip `_CHEAP = _HAIKU` in `roam.py` (and `RONIN_MODEL` for chat) once Haiku is
  unblocked — needs a graff one-shot effort flag (0.0.220 exists, untested vs our tool-firewall
  hook) or moving these calls to graff's `--json` protocol + `set_fast`.
- **Em-dash guard (`_normalize_voice`):** the evals caught Sonnet using em dashes (persona bans
  them). Enforced deterministically at the boundary for chat AND roam pings, not by prompt — same
  approach as the `<thinking>` strip, so it's model-independent. `_normalize_voice` lives in
  `ronin_reply.py`; `roam._tg_send` imports it.
- **Validated:** Sonnet held over 3 behavior runs (7/10 solid 3/3 — all voice/tool/no-hallucination
  cases; 3/10 flaky at 2/3 = normal variance, two aggravated by preseason being in the NFL window).
  Roam JSON path (judge + vibe) verified clean on Sonnet.
- **Shipped 2026-07-30:** `fly.toml` `RONIN_MODEL` → `claude-sonnet-5`, deployed. Verified live —
  chat in-voice + no em dashes, `run_once`/`sentiment_sweep` dry passes clean (JUDGE/VIBE=sonnet,
  GRADE=opus). Roam still isn't model-in-loop tested, so keep half an eye on the first real pings.

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

### ✅ LIVE 2026-08-16 — per-user personality (rolled team + taste lens)
**Deployed and verified in-container.** Vocab signed off by the owner.
Lives in `character.py`; storage is `memory.get_character`/`set_character`; injected by
`ronin_reply._load_system_prompt` via `character.prompt_block`. Kill switch `RONIN_CHARACTER=0`
returns everyone to the single global ronin.

The three open decisions were settled: **one signature team total** (not per league),
**3 traits with exclusivity**, and **global affinities stay global** with the personal team
layered on top. That last one reverses the spec bullet below: `reflect()` is O(leagues), and
per-user affinities would make it O(users x leagues) — the exact cost cliff the Sonnet routing
work was built to avoid, and it contradicted the spec's own "no per-user daily reflection".
So affinities were NOT uid-namespaced; only the rolled character is per-user.

Two things worth knowing:
- **Exclusivity needed more than one-trait-per-axis.** "methodical" (tempo) and "chaotic"
  (temperament) sit on different axes but mean opposite things, so the first implementation
  rolled both. There's now an explicit `CONFLICTS` list + rejection sampling. 153 distinct
  coherent combinations.
- **The roll prefers a league the user already follows**, excluding their own team, because
  the banter only lands in a shared sport. Falls back to `RONIN_HOME_LEAGUE` (nba) if they
  haven't set a team yet.
- **Write-once is load-bearing, and it bites vocab edits.** Anyone already rolled keeps their
  character forever; changing a trait word later won't re-roll them. A vocab change needs a
  migration or a deliberate reroll (which undercuts the "feels like fate" premise). Decide
  vocab changes BEFORE onboarding a batch of testers.
- **Inspect a roll:** `memory.get_character("<uid>")`. Count them:
  `sum(1 for u in memory._read("relationships.json",{}).values() if u.get("character"))`.

Original spec, kept for context:

### ⭐ (original spec) per-user personality (rolled team + taste lens)
Make ronin's *personality* personal, not just his memory. Today he's one global character with
one earned allegiance set (the "it knows you" lock-in is only half-built). Give each user a ronin
with his *own* team and taste, so "my ronin rides with the Thunder, yours is a star-power junkie"
becomes a thing people screenshot. Design locked in a brainstorm (2026-08-16):

- **Same voice + value floor, per-user taste on top (layered, NOT a different persona).** The
  voice and the existing value-seed (pro player-dev/defense/underdogs, anti tanking/ring-chasing)
  stay GLOBAL — every ronin shares them. The rolled taste is an extra per-user *lens* layered
  over that floor. Honors "same values" + "colors his whole worldview": same values, plus a
  personal flavor that reaches beyond just his team.
- **Character creation = one cheap call per new user.** On first real contact: roll 2–3 descriptor
  words from a curated vocab (star-power, home-grown, electric, defense-first, chaotic, methodical,
  veteran, underdog…) → match to the team that best fits via ONE LLM call grounded in real ESPN
  roster/style data → store the traits + team per user. That's the whole cost. No per-user daily
  reflection (keeps the sustainability work intact).
- **Fixed team, live takes.** The rolled team sticks forever (that's "your ronin"), but his
  opinions about it stay current off the existing GLOBAL news/reflect. **Pure random, no reroll**
  (feels like fate). The traits also color what he finds exciting in ANY game, not just his team.
- **The juicy emergent bit:** ronin's rolled team is SEPARATE from the user's `/team`. So he has
  his own squad to defend against yours → a built-in banter engine for free (you're a Warriors
  guy, his rolled ronin needles you for star-chasing while championing his young core).
- **How it lands on the code:** most per-user scaffolding exists (`get_profile`/`set_profile`,
  `_load_system_prompt(sender_id)` already injects allegiances). Main change: **affinities go from
  global to per-user-namespaced (`uid:league:team`) — literally the shared-cursor uid-scoping
  pattern.** New piece = the one-time character-creation roll+match. `_load_system_prompt` gains a
  per-user "taste lens" block layered over the global persona.
- **Decisions still open (settle at build time):** (a) one signature team total vs one rolled team
  per league (to match per-league memory); (b) who curates the descriptor vocab, 2 vs 3 traits,
  mutually-exclusive pairs (can't be both methodical + chaotic); (c) migration — freshly roll all
  existing users, or let the current global earned allegiances become his "default league read"
  that the personal team layers over.

### ✅ CLOSED 2026-08-17 — the volunteered-teammate leak
Fixed with option (b) below, the one flagged as the cheapest real win: **`sports_roster(league,
team, position?)`**, so "who's in that backfield" is one grounded call instead of a recollection.
Persona now tells him to pull the roster before describing any backfield/rotation/depth chart,
and to make the point without a name if he won't look. **4/4 clean on the exact prompt that
produced the bug** — every run named J.K. Dobbins (really on Denver), one also named McLaughlin
and Schrader (also real). Zero Javonte Williams.

Gotcha worth keeping: the position filter must match the abbrev exactly or a WHOLE WORD of the
position name. A substring test returns quarterbacks and cornerbacks when you ask for "RB"
(both words contain it) — that shipped in my first draft and is now pinned by a test.

*Residual, minor:* he still volunteers ungrounded **biographical** color ("Dobbins' injury
history"), which is the same class as the old Thybulle "29 year old defensive wing" note. Real
but low-stakes, and no tool covers it.

Original writeup below.

### 🔖 (closed) the volunteered-teammate leak — original writeup
**`sports_player` grounds the player someone ASKS about. It does not ground the player ronin
brings up himself, and that's where he's still wrong.** Fixing the RJ Harvey "rookie" bug moved
the defect rather than removing it: asked about Harvey, ronin now correctly says year 2 (6/6),
but he volunteered *"splitting carries with Javonte Williams"* — **Williams is a Dallas Cowboy.**
The color detail is the trap, because it's the part nobody thinks to look up.

- **Current mitigation is a persona rule** ("look up anyone you're about to name, or make the
  point without the name") + a ban on narrating the tool. It went 3/3 clean on re-test, then
  leaked again in the full harness run. So: improved, **not solved**, and prompt rules hold only
  probabilistically — the same lesson as the matchup fix (2/4 → 5/5).
- **Why there's no quick deterministic fix.** The `<thinking>`/em-dash guards work because they
  match a *shape* at the boundary. A wrong teammate is well-formed prose; catching it means
  validating every name in a reply against the roster it's asserted to be on, and the reply
  rarely names that roster explicitly. That's a real design problem, not a patch.
- **Directions worth weighing:** (a) boundary pass that extracts player names and verifies each
  via `sports_player`, rewriting or dropping unverified ones — expensive, needs NER; (b) a
  `sports_roster(league, team)` tool so "who's in that backfield" is one grounded call, which
  removes the *need* to recall a teammate; (c) accept it and lean harder on "make the point
  without the name." **(b) is the cheapest real win** and reuses the `_roster` helper already
  added in `mcp/espn.py`.
- **Repro:** `reply(uid, "quick take on rj harvey for my fantasy team")` a few times and watch
  whether any *other* player gets named. Verify each with `espn.player(name)`.

### ✅ CLOSED 2026-08-20 — the season-opener bug (and the diagnosis that was wrong)

**The handoff's own diagnosis of this bug was incorrect, and acting on it would have made
things worse.** Recorded here because the wrong theory was plausible and survived two sessions.

The theory was: the tool returns upcoming games earliest-first, the model just misreads the
top line, so label the first chronological game as the opener. What was actually happening:
**ESPN's no-date scoreboard returns the CURRENT slate, not upcoming games.** In the preseason
gap that slate was 16 finished preseason games, all `Final`. So `persona.md`'s central rule
("with no date it returns the next upcoming games, the FIRST line is your answer") described
behavior the tool never had. The model followed it exactly, got nothing usable, and fell back
to model weights — which is where the Australia game (Rams-Niners) came from; it's the
salient marquee opener in training data. The prompt hadn't "hit its ceiling", it was
describing a tool contract that didn't exist.

Had we labelled the first chronological line as the opener, we'd have shipped
`DET @ CIN — Final`, a completed preseason game, as the season opener.

**The fix** (all in `mcp/espn.py`):
- `_upcoming()` — when nothing on the current slate is unplayed, scan forward in widening
  windows (45/150/300d) and stop at the first with real fixtures. The ~100-event cap counts
  from the START of a range, so the earliest games are exactly the ones that survive it.
- `_find_opener()` — splits "next game" from "season opener" off ESPN's `season.type`
  (1 preseason / 2 regular). Searches wider than the display horizon, since the NBA opener
  sits ~3 weeks past its first preseason game.
- `_banner()` — states the answer outright as `NEXT GAME:` and (only while the regular season
  hasn't started) `SEASON OPENER:`. Same enforce-at-the-boundary move as `_strip_thinking`
  and `_normalize_voice`.
- Fallback slate capped at 40 lines (`_MAX_SLATE`); it was printing 80.

**Verified:** 3/3 on each opener case in isolation, then 14/14 behavior in the full run;
every run named Patriots-Seahawks Wed 9/9 and the Australia game never appeared. The live
integration check compares against a DIFFERENT ESPN endpoint (`seasontype=2&week=1`) so it
can't pass by agreeing with the code under test. Correct across NFL/NBA/NHL (all get openers)
and WNBA/MLB/EPL/NCAAF (correctly get none).

**Gotcha earned here — don't relearn it:** the first draft labelled *any* first regular-season
game as the opener, which tagged ordinary mid-season WNBA and MLB games "SEASON OPENER".
The opener line may only appear when the next game is still preseason. Pinned by a data test.

**The bigger lesson:** when a prompt rule "keeps not holding", check that the tool actually
does what the rule claims before concluding the model is at fault. Two sessions of persona
tuning went into enforcing a contract the tool never honored.

### ⭐ NEXT UP (updated 2026-08-20, roughly in priority order)

**A. ✅ Closed 2026-08-20** — the season-opener bug. Root cause was NOT what this
doc predicted; the writeup is directly above and worth reading before trusting any
other diagnosis in here.

**B. Watch the first real roam pings on the new judge.** `JUDGE_RECALL_SECS` is deployed but the
roam loop hasn't run a live pass with it. Confirm a follow-up ping leads with what's new instead
of re-explaining. Also note `JUDGE_RECALL_N=12` puts more prior messages in every judge call —
small per call, but it scales with signups on the highest-volume roam path.

**C. The DDG SERP check can't tell "blocked" from "broken".** A blocked fetch returns an anomaly
page that parses to 0 results and hard-fails, while an outright network error skips gracefully.
It went red today purely from test volume off one IP. Detect the anomaly page and skip like the
network path does, or it'll read as a dead parser to whoever hits it cold. Small fix.
**Bumped 2026-08-20:** this bit twice more. It now also costs behavior-suite runs — when DDG is
unreachable the "follow-up attaches to the proactive ping" case goes red because ronin correctly
refuses to guess, so the reply never mentions the topic. Re-run those cases before believing a
red, and consider a fixture so the suite doesn't depend on a third-party SERP at all.

**D. Key housekeeping (owner, still outstanding).** Revoke the OLD `ANTHROPIC_API_KEY` at
console.anthropic.com, set a workspace spend limit, update local `~/ronin/.env` (its Telegram/Bsky
values are still pre-rotation). Also confirm the older Telegram/Bsky keys were revoked at source.

**E. Repo hygiene.** ~114 loose `sess_*.json` files in the repo root (gitignored, but noise).

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
