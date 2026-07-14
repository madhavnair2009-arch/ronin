# ronin — v0 (stat lookup)

The "utility hook" slice from `ronin-design.md`: graff driving a real sports
tool, no memory, no roaming. Ground-truth facts only — exactly the **World facts**
axis of the memory design (API is truth, never the LLM).

## What's here

```
ronin/
  mcp/espn_nba.py   zero-dep MCP stdio server wrapping ESPN's public NBA JSON
  .mcp.json         graff's MCP registration (points at the server)
  README.md
```

## Data source

ESPN's public `site.api.espn.com` JSON endpoints — **no API key, no signup**.
(balldontlie was the doc's first candidate but now returns 401 without a key, so
ESPN is the v0 pick.) Three tools:

| Tool | Args | Returns |
|---|---|---|
| `nba_scoreboard` | `date?` (YYYYMMDD) | games + live/final scores for a day |
| `nba_team` | `query` (name/abbrev) | record, conference standing, next game |
| `nba_standings` | `conference?` (East/West) | standings, sorted by win pct |

## Run it

```sh
# sanity-check the data layer against the live API (no MCP, no LLM):
python3 mcp/espn_nba.py selftest

# chat (interactive REPL) — graff picks up .mcp.json in this dir:
cd ~/ronin && graff

# one-shot:
cd ~/ronin && graff -p --yolo "today's NBA scores and where do the Lakers stand?"
```

First interactive run asks consent to start the MCP server (`--yolo` skips it).

## Design notes

- **Facts never go through the LLM.** Scores/records/standings come straight from
  ESPN; the model only phrases them. This is the "never blur facts and opinions"
  hard rule, enforced structurally by keeping data in the tool layer.
- **Zero deps on purpose.** The server is Python stdlib only (`urllib` + `json`),
  matching graff's zero-dependency ethos. The MCP layer is hand-rolled JSON-RPC
  over stdio — no SDK to install.
- **Tool errors don't crash the server.** API failures come back as `isError`
  tool results so the chat degrades gracefully instead of dropping the connection.

## Not yet (per build order)

v0 deliberately stops here. Next is **v1**: the roam loop writing takes/storylines
+ chat reading them (and the open question on how contrarian ronin is allowed to be).
See `../ronin-design.md`.
