# Deploying ronin (Telegram) — free, always-on

The bot long-polls Telegram, so it needs **no public URL/port** — just a host that
stays running. Two free options below; Fly.io is fastest for Friday.

## Secrets it needs (never commit these)
- `TELEGRAM_BOT_TOKEN` — from @BotFather (already in local `.env`)
- `ANTHROPIC_API_KEY` — your Anthropic key (graff uses it per turn = your API spend)

---

## Option A — Fly.io (recommended, ~10 min)

Free allowance covers one tiny always-on machine. A card is required for signup
(verification); this workload stays within the free allowance.

```sh
# 1. install flyctl + sign in (opens a browser — YOUR action)
brew install flyctl          # or: curl -L https://fly.io/install.sh | sh
fly auth signup              # or `fly auth login` if you already have an account

# 2. create the app (pick a unique name; update app= in fly.toml to match)
cd ~/ronin
fly apps create ronin-sports

# 3. set secrets (values are read from your shell, not stored in the repo)
fly secrets set \
  TELEGRAM_BOT_TOKEN="$(grep '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2-)" \
  ANTHROPIC_API_KEY="sk-ant-…your-key…"

# 4. ship it
fly deploy

# 5. watch it come up / tail logs
fly logs
```

Redeploy after any code change: `fly deploy`.

---

## Option B — Truly free, no card: Oracle Cloud / GCP always-free VM

Spin up an always-free micro VM (Oracle Ampere or GCP e2-micro), then:

```sh
# on the VM (Ubuntu):
sudo apt update && sudo apt install -y python3 curl
curl -fsSL https://github.com/justrach/codegraff/releases/download/v0.0.187/graff-x86_64-linux.tar.gz \
  | sudo tar -xz -C /usr/local/bin && mkdir -p ~/bin && ln -sf /usr/local/bin/graff ~/bin/graff
# copy the ronin/ dir up (scp/rsync), then:
cd ~/ronin
export TELEGRAM_BOT_TOKEN=…  ANTHROPIC_API_KEY=…
python3 telegram_bot.py            # or run under systemd/tmux so it survives logout
```

Slower signup (can take a while / occasionally rejected), but $0 with no card charge.

---

## Sanity checks
- `python3 mcp/espn_nba.py selftest` — data layer against live ESPN (no LLM)
- `python3 ronin_reply.py me "who leads the east?"` — full reply engine locally
- Text **@sportsronin_bot** — end-to-end
