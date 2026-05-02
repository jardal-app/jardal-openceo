FROM node:24-bookworm-slim

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    procps \
    python3 \
    python3-pip \
    ripgrep \
    tmux \
  && rm -rf /var/lib/apt/lists/*

COPY apps-sdk-mcp-test/package.json apps-sdk-mcp-test/package-lock.json ./
RUN npm ci --omit=dev

COPY apps-sdk-mcp-test/server.js ./server.js
COPY apps-sdk-mcp-test/public ./public
COPY apps-sdk-mcp-test/scripts ./scripts
COPY apps-sdk-mcp-test/README.md ./README.md
COPY openceo-core/requirements.txt /tmp/openceo-core-requirements.txt
COPY openceo-core/agents/dev/requirements.txt /tmp/dev-requirements.txt
COPY openceo-core/agents/finance/requirements.txt /tmp/finance-requirements.txt
COPY openceo-core/agents/secretary/requirements.txt /tmp/secretary-requirements.txt

RUN python3 -m pip install --break-system-packages --no-cache-dir \
    -r /tmp/openceo-core-requirements.txt \
    -r /tmp/dev-requirements.txt \
    -r /tmp/finance-requirements.txt \
    -r /tmp/secretary-requirements.txt

ENV PORT=8787
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8787/ >/dev/null || exit 1

CMD ["node", "server.js"]
