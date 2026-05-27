FROM python:3.11-slim

# System deps: git, Node.js 20 (for claude-code + codex CLI), ssh
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git openssh-client ca-certificates gnupg tmux \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI and Codex CLI globally
RUN npm install -g @anthropic-ai/claude-code@latest 2>/dev/null; \
    npm install -g @openai/codex@latest 2>/dev/null; \
    npm cache clean --force

# Create clauded wrapper: claude --dangerously-skip-permissions
RUN printf '#!/bin/sh\nexec claude --dangerously-skip-permissions "$@"\n' > /usr/local/bin/clauded \
    && chmod +x /usr/local/bin/clauded

# Create non-root user (claude CLI refuses --dangerously-skip-permissions as root)
RUN groupadd -g 1002 ceouser && useradd -u 1002 -g 1002 -m -s /bin/bash ceouser

# Python dependencies
WORKDIR /app
COPY requirements.txt .
COPY agents/ceo/requirements.txt agents/ceo/requirements.txt
COPY agents/dev/requirements.txt agents/dev/requirements.txt
COPY agents/finance/requirements.txt agents/finance/requirements.txt
COPY agents/marketing/requirements.txt agents/marketing/requirements.txt
COPY agents/media/requirements.txt agents/media/requirements.txt
COPY agents/sales/requirements.txt agents/sales/requirements.txt
COPY agents/secretary/requirements.txt agents/secretary/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && find /app/agents -mindepth 2 -maxdepth 2 -type f -name requirements.txt | sort | while read -r req_file; do pip install --no-cache-dir -r "$req_file"; done

COPY . .

# Default projects directory (mounted as volume in production)
RUN mkdir -p /app/projects && chown -R ceouser:ceouser /app

# Ensure ceouser home dirs exist for CLI tools (OpenCode, etc)
RUN mkdir -p /home/ceouser/.local/share /home/ceouser/.local/state /home/ceouser/.config \
    && chown -R ceouser:ceouser /home/ceouser/.local /home/ceouser/.config

USER ceouser

EXPOSE 8000

CMD ["python", "main.py"]
