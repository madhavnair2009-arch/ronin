# ronin — one-pager

**What it is:** An autonomous sports agent with a personality and a memory. You text it for scores/news/takes across NBA, WNBA, NFL, MLB, NHL & college — and it texts *you* when your team does something. Live on Telegram as **@sportsronin_bot**.

**The bet:** Utility is the hook (instant, accurate stats), personality is the retention (it's a *someone* — has opinions, remembers you, revises its own takes, reaches out unprompted). The moat is that evolving memory + relationship layer, not the stats wrapper. One hard rule underneath: **never blur facts and opinions** — a bot that states the wrong score confidently is the worst outcome.

---

**How it works — two halves, one shared brain:**
- **Chat (reads the mind):** you message → it answers in voice, pulling live facts + its own takes + what it knows about you.
- **Roam loop (builds the mind):** every 30 min it scans your team's news, forms/revises opinions, and decides — on its own — whether something's worth texting you about.
- Both share one **persistent memory**, so a take it forms at 2pm surfaces in a chat reply at 8pm.

**Four memory axes:**
| Axis | Rule |
|---|---|
| World facts (scores/news) | API is truth — never stored, always re-fetched |
| Its beliefs (takes) | *revised, not appended* — keeps history, can say "I was wrong" |
| You (relationship) | your team, mute/throttle |
| Its temperament (voice) | stable, doesn't drift |

---

**Status:** v0 stat lookup ✅ → multi-sport + news + fan sentiment + human voice ✅ → **v1 (roam loop, 4-axis memory, belief revision, proactive outreach) ✅ LIVE.** Next (v2): calibration engine (earned traits from scored predictions), passive team inference, cheap-model tiering for cost.

**Stack:** graff (agent harness) + Opus · ESPN public data · Bluesky sentiment · Fly.io (1 machine + durable volume) · Telegram.

---

**Three questions for the team:**
1. **How opinionated/contrarian is ronin allowed to be?** — the spine; does it have allegiances?
2. **Where are the users, and what's the real data source?** — Telegram vs elsewhere; Bluesky vs fighting for Reddit/X vs licensed data.
3. **What's the actual moat** if a big player ships "sports GPT" tomorrow?

**Highest-leverage next step:** the calibration engine — scoring resolved takes into earned traits ("I keep underrating this GM") is the hardest-to-copy thing we can build.
