# ronin — Telegram bot container.
# Long-polling bot: only outbound calls to Telegram, so no exposed ports needed.
FROM python:3.12-slim

# graff is a static Zig binary; grab the linux build matching the container arch.
# Fly's default builders are amd64 -> x86_64.
ARG GRAFF_VERSION=v0.0.187
ARG GRAFF_ARCH=x86_64
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://github.com/justrach/codegraff/releases/download/${GRAFF_VERSION}/graff-${GRAFF_ARCH}-linux.tar.gz" \
       -o /tmp/graff.tgz \
    && tar -xzf /tmp/graff.tgz -C /usr/local/bin --strip-components=1 "graff-${GRAFF_ARCH}-linux/graff" \
    && chmod +x /usr/local/bin/graff \
    && rm /tmp/graff.tgz \
    && graff --version

# kuri-fetch (justrach/kuri): the standalone HTTP fetcher used by the reddit sentiment
# tool to read old.reddit (Reddit's API/JSON 403s us). Linux tarball is flat binaries.
ARG KURI_VERSION=v0.4.4
RUN curl -fsSL "https://github.com/justrach/kuri/releases/download/${KURI_VERSION}/kuri-${KURI_VERSION}-${GRAFF_ARCH}-linux.tar.gz" \
       -o /tmp/kuri.tgz \
    && tar -xzf /tmp/kuri.tgz -C /usr/local/bin kuri-fetch \
    && chmod +x /usr/local/bin/kuri-fetch \
    && rm /tmp/kuri.tgz \
    && kuri-fetch --version

WORKDIR /app
COPY mcp/ ./mcp/
# .harness/ holds the graff tool firewall (pre_tool hook) — allows only the MCP
# servers, blocks bash/file/webfetch/subagent so an untrusted DM can't get a shell.
COPY .harness/ ./.harness/
COPY persona.md takes.json ronin_reply.py telegram_bot.py memory.py roam.py .mcp.json ./

# graff resolves ~/bin/graff or PATH; ronin_reply calls ~/bin/graff, so symlink it.
RUN mkdir -p /root/bin && ln -sf /usr/local/bin/graff /root/bin/graff

# graff reads ANTHROPIC_API_KEY from env on Linux; Telegram token likewise.
# Both are injected as secrets at deploy time (never baked into the image).
ENV HOME=/root
CMD ["python3", "telegram_bot.py"]
