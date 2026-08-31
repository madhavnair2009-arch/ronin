# ronin — project brief: distillation & voice

**Purpose of this document.** Cold-start context for a *new* project exploring (a) distilling
ronin onto smaller models and (b) giving ronin voice. Written for an assistant with zero prior
knowledge of the system. Everything here is current as of **2026-08-30**.

Repo: `github.com/madhavnair2009-arch/ronin` (private). The day-to-day engineering backlog lives
in `NEXT-SESSION.md` in that repo — this document is separate and does not replace it.

**Read the epistemics markers.** Claims below are tagged where it matters:
`[VERIFIED]` = confirmed against primary source or running code. `[UNCONFIRMED]` = plausible,
widely repeated, not confirmed. `[SPECULATIVE]` = my reasoning, untested. Please preserve this
discipline; several bugs in this project came from acting on a confident-sounding wrong belief.

---

## 1. What ronin is

A sports-obsessed Telegram bot (`@sportsronin_bot`) with opinions, allegiances, and a memory.
Deployed on Fly.io as app `ronin-sports` (region `iad`, one machine). Built on **graff**, a CLI
agent that wraps Claude models.

It has two halves:

- **Chat** — user texts it, it replies. Synchronous, tool-using, personality-heavy.
- **Roam** — an autonomous background loop that forms takes, proactively pings users about
  their teams, reflects on its own allegiances, grades its past predictions, and digests
  relationships. Runs on a schedule, no human in the loop.

### The one hard rule

**Facts come from tools, never from model weights. Personality is ronin's own.**

Every score, record, standing, schedule, roster and championship comes from live ESPN APIs.
The bot's voice, taste, grudges and allegiances are its own. This separation is the entire
trustworthiness claim of the project, and most of its engineering history is about defending it.
Any proposal that lets a model supply facts is a regression, no matter how good it looks.

### Tool surface (10 tools)

Eight ESPN tools via an MCP server (`mcp/espn.py`): `sports_scoreboard`, `sports_standings`,
`sports_team`, `sports_news`, `sports_team_news`, `sports_champion`, `sports_player`,
`sports_roster`. Plus `fan_sentiment` (blended Reddit + Bluesky) and `web_search` (DuckDuckGo
SERP). Leagues: NBA, NFL, MLB, NHL, WNBA, college, plus 8 soccer competitions.

### Persistent state

`memory.py` over JSON files on the Fly volume at `/data`. Not a database, not a learning system —
deterministic reads and writes with file locking. Holds: user records and teams, per-user rolled
characters, per-user relationship profiles, ronin's open takes, its graded track record
(calibration), its team affinities, news cursors, and mood state.

### Per-user personality `[VERIFIED]`

Every user gets their own ronin: same voice, same values, plus three rolled taste traits and one
signature team, rolled once on first contact and then fixed forever. Kill switch
`RONIN_CHARACTER=0`. Example live rolls: owner → `chaotic/deep-bench/physical` → Detroit Pistons;
a second account → `methodical/physical/rebuild` → Charlotte Hornets.

**Important architectural asymmetry:** the *rolled character* is per-user, but the *affinities*
(teams ronin rates, formed by the `reflect()` pass off real standings) are **global** — every
user sees the same ones. This was a deliberate cost decision: `reflect()` is O(leagues), and
per-user affinities would make it O(users × leagues).

---

## 2. The engineering philosophy (this is the important part)

This matters more than any individual feature, and it is what makes distillation thinkable.

**Prompt rules only hold probabilistically. Enforce at the boundary instead.**

Repeatedly, this project found that telling the model "don't do X" in the persona held maybe
2/3 of the time. The fix pattern is always the same: catch it deterministically in code at the
transport boundary, where it is model-independent. Live examples:

| Guard | Location | What it enforces |
|---|---|---|
| `_strip_thinking` | `ronin_reply.py` | Model reasoning never reaches Telegram |
| `_normalize_voice` | `ronin_reply.py` | No em dashes in any output (persona bans them) |
| `_depossess` | `roam.py` | Affinity stances can't claim a team as ronin's own |
| `_extract_json` | `roam.py` | Every roam path parses structured output, never raw text |
| tool firewall | `.harness/settings.json` | Only MCP servers allowed; no bash/file/webfetch |

**Consequence for this project:** ronin is unusually well defended against a *weaker* model.
The guarantees don't come from the model being smart; they come from code the model can't reach.
That is precisely the property you need if you want to swap in something smaller.

### Other hard-won lessons that transfer

- **A green test suite proves nothing about paths it doesn't touch.** Three user-visible bugs
  once lived under a 71/71 green harness.
- **Test the model, not just the data.** A data-layer fix can pass while the bot is still wrong.
- **LLM fixes are probabilistic — run a case several times before believing it.** One fix went
  2/4, then 5/5 after a stronger prompt.
- **Verify a fix is a fix, and verify a failure is pre-existing.** Both directions cost one
  `git stash` and both have been load-bearing.
- **When a prompt rule "keeps not holding", check the tool actually does what the rule claims.**
  Two sessions of persona tuning once went into enforcing a tool contract that never existed.

### The eval harness

`python3 eval/run.py` — currently **134/134**, three layers:

- `data` (105) — offline, free, deterministic
- `integration` (14) — live ESPN, no LLM
- `behavior` (15) — model in the loop on seeded memory, with universal assertions (no em dash,
  no thinking tags) applied to every reply

`--data-only` and `--no-llm` skip the paid layers. **This harness is the single biggest asset for
a distillation project** — it means a distilled model can be measured against the Claude baseline
instead of guessed at. Most people attempting this have no such instrument.

---

## 3. Constraints that shape every decision

- **Scale is tiny: ~4 users.** Any self-hosting economics must be judged against that, not
  against a hypothetical userbase.
- **Hardware:** `shared-cpu-1x`, **512 MB RAM**, 50 MB image. A 2-4 GB model does not fit; the
  machine would need to grow or a sidecar would be needed. `[VERIFIED]` from `fly.toml`.
- **The cost cliff is the roam loop, not chat.** It fans out per user × team × pass on a
  schedule, so cost tracks *signups*, not chat volume. A whole engineering session went into
  per-path model routing to avoid this.
- **Current model routing `[VERIFIED]`:** chat + roam judge/vibe/digest/character run on
  `claude-sonnet-5`; `reflect` + `grade` run on `claude-opus-4-8`. Env kill switches:
  `RONIN_MODEL`, `RONIN_ROAM_MODEL`.
- **graff is in the path of every model call.** This is a real integration constraint, not a
  config flip. Precedent: Haiku 4.5 could not be adopted because graff forces adaptive thinking
  in one-shot (`-p`) mode and Haiku rejects it. Any non-Claude model means a second inference
  path (likely OpenAI-compatible HTTP to llama.cpp/Ollama) plus re-solving the tool-firewall
  story.

---

## 4. Research findings: small models & voice

Full memo with tables: the artifact "Distilling Ronin". Condensed here.

### Gemma 4 E2B `[VERIFIED]`

Released 2026-04-02, Apache 2.0. Smallest of four Gemma 4 variants (E2B, E4B, 26B MoE, 31B
dense). 2.3B effective params (5.1B with Per-Layer Embeddings), 128K context, multimodal in
(text/image/audio), text out only. IFEval 90.4% (vs E4B 96.7%, 12B 97.2%). MMLU Pro 60.0,
GPQA Diamond 43.4. Google claims it "roughly matches Gemma 3 27B with 10x less parameters."

- **Audio:** native ASR + speech translation. 17% better transcription than Gemma 3n at matched
  size, with the audio encoder shrinking 78% (390 MB → 87 MB quantized). Runs 265× real-time on
  LibriSpeech Clean. Independent testing: **decent on clean speech, unreliable on harder input.**
- **The decisive gap:** the Gemma 4 technical report publishes **no function-calling or tool-use
  benchmark at any model size.** Tool calling is the one capability ronin cannot compromise on.
- **Distillation:** widely stated that Gemma is distilled from Gemini, and true of earlier
  generations, but the **Gemma 4 technical report does not mention distillation anywhere**
  `[UNCONFIRMED for Gemma 4]`. Either way it is not a technique available to us — we have no
  access to Gemini's or Claude's logits. **True logit-level distillation is closed.**

### NVIDIA PersonaPlex `[VERIFIED — ICASSP 2026 preprint]`

A **full-duplex speech-to-speech** conversational model — listens and speaks simultaneously,
handles interruptions and backchannels, holds a role and a voice. 7B, built on Kyutai's **Moshi**,
fine-tuned with a "Hybrid System Prompt". Code MIT; weights NVIDIA Open Model License; Moshi base
CC-BY-4.0. **Uses no knowledge distillation** — it is a fine-tune from Moshi's weights.

**Three parallel streams:**

- *User audio* — live mic, streaming continuously
- *Agent text* — Moshi's **Inner Monologue**, time-aligned text tokens predicted as a **prefix**
  to the audio. The role prompt is injected here. This is the only human-readable surface.
- *Agent audio* — Mimi codec tokens at 12.5 Hz, 1.1 kbps. Voice prompt injected here.

A 7B **Temporal Transformer** models across time; a small **Depth Transformer** models across
codebooks within a frame. Hybrid prompt = text segment (role tokens on the text channel, audio
silent) + voice segment (speech sample on audio channel, text padded → zero-shot voice cloning).
User audio is replaced by a 440 Hz sine wave during prompting for stable conditioning. Voice
segment goes first so it can be prefilled when cloning isn't needed, reducing latency.

**Benchmarks** (vs Gemini, Qwen-2.5-Omni, Freeze-Omni, Moshi):

| Metric | PersonaPlex | Gemini |
|---|---|---|
| Dialog naturalness (DMOS) | 3.90 | 3.72 |
| Voice cloning similarity (SSIM) | **0.57** | **0.00** |
| Turn-taking latency | **0.070 s** | 1.301 s |
| Interruption latency | **0.400 s** | 1.183 s |
| Service task quality (GPT-4o) | 4.48 | **4.73** |

Gemini wins raw task quality; PersonaPlex wins everything conversational — 18× faster turn-taking
and the only model that can clone a voice at all.

**Training economics — the surprise:** 24,576 steps, batch 32, seq 2048 (163.84 s of audio),
Adam + cosine annealing, **6 hours on 8×A100** (~48 A100-hours, roughly $100-200). Dataset:
1,840 h service dialog (105,410 dialogs) + 410 h QA (39,322 dialogs), generated by Qwen-3-32B and
GPT-OSS-120B, voiced by Dia and Chatterbox TTS.

**Dataset ablation — the most actionable number in the paper:**

| Data | Voice SSIM | Full-Duplex-Bench | Service-Duplex-Bench |
|---|---|---|---|
| 100% | 0.57 | 4.21 | 4.48 |
| 50% | 0.56 | **4.52** | 4.24 |
| 25% | 0.54 | 4.44 | 4.20 |
| 0% (plain Moshi) | 0.10 | 0.77 | 1.75 |

0% → 25% captures essentially the whole gain. Past that it is noise (50% scores highest on one
benchmark, 100% lowest). **Target dataset is ~35,000 dialogs, not 145,000.**

### The key insight: PersonaPlex doesn't tool-call either

Ronin's hardest voice constraint looked fatal — a duplex model answers in 70 ms, an ESPN call
takes hundreds of ms, a Claude call takes seconds. You cannot tool-call inside a live
conversation.

But PersonaPlex never does. Facts are **pre-loaded into the role prompt**. An actual evaluation
context from the paper reads: *"You are an agent named Brody Murphy working for National Health
Coverage... Available plans include: Basic ($200/month), Premium ($450/month)..."* — prices and
account details baked into the prompt, with the benchmark probing recall under conversational
pressure (including "unfulfillable request" and "unrelated question" categories, which map
directly onto ronin's refusal discipline).

**This is a briefing architecture, and ronin is most of the way there already.**
`_load_system_prompt` in `ronin_reply.py` already assembles takes, track record, affinities,
rolled character, the user's teams and their profile. Extend it with the live slate and team news
and it becomes a voice briefing. The rule becomes **fetch before you talk, not during** — and
anything outside the briefing gets "hold on, let me pull that up," which is a natural thing to
say and a clean seam to drop out of duplex mode.

---

## 5. What the new project should figure out

### Two voice products, not one

| | Voice notes (async) | Live calls (duplex) |
|---|---|---|
| Stack | ASR → existing ronin → TTS | PersonaPlex, fine-tuned |
| Tools | All 10, unchanged | None mid-stream; briefing only |
| Text guards | All of them, unchanged | Inner monologue only, unproven |
| Latency need | Seconds — fine | ~200 ms — hard floor |
| GPU | No (API ASR/TTS) | Yes, always-on |
| Fine-tune | None | ~$100-200 + dataset build |

The async path preserves every guarantee ronin has and needs no GPU. Telegram supports voice
messages in both directions. The duplex path is what would make ronin feel alive, and is a real
programme.

### Open question 1 — can the Inner Monologue be used as a guard boundary? `[SPECULATIVE]`

**You cannot regex an audio stream.** A duplex ronin loses `_strip_thinking`,
`_normalize_voice`, `_depossess` — the whole boundary architecture. The Inner Monologue *should*
serve as the replacement, since the text token is predicted as a prefix to the audio for each
frame, meaning ronin's next words exist as text before they exist as sound.

Whether you can intervene there in real time at 12.5 Hz without wrecking audio that was
conditioned on the token you just changed is **not addressed in the paper**. This is the
load-bearing unknown. Prototype it against stock Moshi before committing to anything.

### Open question 2 — grounded synthetic data

Fine-tuning on synthetic ronin dialogue risks **baking hallucinated sports facts into the
weights**, which is the exact failure the whole system exists to prevent. Mitigation: generate
the synthetic dialogs *grounded on real tool output* — pull genuine ESPN data, have a strong model
write ronin-voiced conversations over it, so the student learns the voice and the *shape of
deference to facts*, not the facts themselves.

### Open question 3 — which workload to distill

Ronin's model calls are two different workloads:

- **Chat** — multi-turn, 10 tools, user-facing, personality-critical, seconds of latency budget.
  Bad small-model target; tool calling is the unbenchmarked capability.
- **Roam judge / vibe / digest** — single-shot, bounded input, strict JSON out, runs in the
  background. Good small-model target, and it is the documented cost cliff.

**If distilling anything, distill the judge, never the chat.**

### Do this now regardless: start logging

The one thing that is cheap today and **impossible to backfill**. Two datasets:

1. Every `_judge` call as an *(input, Claude's JSON output)* pair → teacher/student data for a
   distilled judge.
2. Full chat transcripts → voice fine-tune data.

Neither exists yet. A future project that wants them will be blocked without them.

---

## 6. Suggested order of work

1. **Ship async voice notes.** Cheap, keeps every guarantee, and tells you whether people
   actually want to talk to ronin before spending anything on GPUs.
2. **Start both logging streams** (above).
3. **Prototype the Inner Monologue hook** against stock Moshi. A weekend to answer the
   load-bearing unknown.
4. **If voice notes land:** build the briefing assembler (extend `_load_system_prompt` with live
   scores), generate the grounded dialog set (~35k), fine-tune PersonaPlex, and give each user's
   ronin **its own cloned voice** — the obvious extension of the per-user character already
   shipping, and something the closed competition structurally cannot match (Gemini: SSIM 0.00).

---

## 7. Things to know before proposing anything

- **Don't propose replacing Claude for chat.** Tool calling is unbenchmarked on Gemma 4 and is
  ronin's critical path.
- **Don't propose per-user affinities.** Rejected deliberately: O(users × leagues) cost cliff.
- **Don't propose fine-tuning as a way to give ronin knowledge.** Facts come from tools. Always.
- **Don't propose self-editing prompts.** Contradicts the boundary-enforcement philosophy, has no
  eval anchor, and destroys reproducibility.
- **At ~4 users, API calls beat self-hosting on every axis.** The economics invert somewhere in
  the hundreds of users. Model releases are not the trigger; the roam bill is.
- **Anything that accumulates per-user state must be uid-scoped and schema-versioned.** There is
  a documented bug family from state keyed without the uid, and a documented pain point from
  write-once per-user data that turned out to need changing.

## Sources

- PersonaPlex preprint (ICASSP 2026): `research.nvidia.com/labs/adlr/files/personaplex/personaplex_preprint.pdf`
- PersonaPlex project page: `research.nvidia.com/labs/adlr/personaplex/` · code: `github.com/NVIDIA/personaplex`
- Moshi: `kyutai.org/Moshi.pdf`
- Gemma 4 Technical Report: `arxiv.org/abs/2607.02770`
- Gemma 4 E2B card: `huggingface.co/google/gemma-4-E2B-it`
