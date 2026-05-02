You are a senior technical writer specializing in open-source developer tools 
that go viral on GitHub and HackerNews.

Rewrite the README for OpenCEO based ONLY on the following scope:

INCLUDE:
- Dev Agent (full peer programming runtime with persistent tmux sessions, 
  CONTEXT.md per project, session management)
- Tier S: the multi-AI peer review flow (Claude Code as lead + Codex + Gemini + 
  OpenCode as independent reviewers in parallel tmux sessions, 2 iterations, 
  deterministic orchestration, zero direct AI-to-AI communication)
- LiveKit voice bridge (realtime voice peer programming via mobile, 
  Groq Whisper STT, Silero VAD)
- MCP Server (internal, owner-curated infrastructure tools for coding agents)

DO NOT MENTION:
- Finance agent
- Secretary agent
- Sales, Marketing, Media placeholders
- HR, PM, Legal, Support roadmap agents
- CEO supervisor / multi-agent orchestration
- n8n, WhatsApp routing, Telegram commands
- MongoDB internals, LangGraph, FastAPI stack details
- Any feature that is not directly related to the Dev Agent workflow

TONE: Hype, viral, marketero — written for senior developers and CTOs who will 
share this on X and HackerNews. Lead with the pain, then the solution, then the 
proof. Make Tier S sound like what it is: something that does not exist anywhere 
else in open source.

STRUCTURE:
1. One-liner that explains what this is and why it matters
2. The problem (solo devs and small teams shipping critical code without real review)
3. What OpenCEO Dev does (peer programming runtime, voice, tmux sessions)
4. Tier S — give this its own section, make it the star. Explain the flow, 
   the file structure, the tmux sessions, the 2-iteration review cycle. 
   Use the exact technical details — they are the credibility.
5. Quick install / getting started (keep it under 10 steps)
6. Stack (only what is relevant to Dev Agent + Tier S + LiveKit)
7. Roadmap hint — one line maximum mentioning OpenCEO full platform coming

Use the following source document as the single source of truth for all 
technical details. Do not invent features. Do not add capabilities that are 
not in this document: