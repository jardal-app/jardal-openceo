---
adr_id: ADR-OBJECTIVE-PLANNER-MODEL
title: Use Muse Code Headless as the Single OpenCEO Objective Planner
status: accepted-initial-baseline
date: 2026-10-05
runtime_document: false
planner_reads_this_document: false
---

# ADR: Use Muse Code Headless as the Single OpenCEO Objective Planner

## Context

OpenCEO needs a company-wide Objective Planner that can create child tickets for software development, research, finance, payments, creative work, operations, and future registered domains.

The previous Tier-S/Objectives v2 design assigned Objective planning to `claudej`/Opus. The newer architecture increases the frequency and breadth of Objective planning and separates global planning from DEV execution.

Muse capacity is currently substantially more abundant than scarce Claude/Codex capacity. Muse Code also provides a native agentic harness suitable for bounded headless planning runs and can expose restricted MCP tools when needed.

This ADR is deliberately **not** part of the runtime planner prompt.

## Decision

Use:

```text
Python / LangGraph / OpenCEO
    -> compiles durable Objective context
    -> launches a fresh Muse Code headless planning run
    -> Muse Spark 1.3 Max reasons over bounded state/tools
    -> returns PlannerResult JSON
    -> Pydantic + deterministic OpenCEO validation
    -> OpenCEO persists valid tickets
```

Do not use a persistent tmux planner session.

Do not use the direct Meta model API as the normal v1 Objective Planner path.

Do not add planner tiers.

Do not automatically ask Claude or Codex to review Muse planning.

Claude/Codex remain available to DEV according to the DEV S/A/B/C policy and Tier-S pipeline.

## Why Muse Code harness instead of direct model API

For this use case, the Muse Code harness already provides the agentic execution surface needed for bounded workspace access, tool use, MCP integration, multi-step reasoning/tool loops, and headless lifecycle management.

Using the direct model API would require OpenCEO to implement more of that agent harness itself.

OpenCEO still owns durable state, validation, permissions, scheduling, and persistence.

## Why fresh headless runs

A fresh run per planning cycle avoids:

- hidden conversational state;
- stale assumptions;
- context accumulation;
- planner sessions waiting on events/quota;
- dependence on one long-lived process.

Continuity comes from durable Objective artifacts.

## Accepted risk: Muse plans and may also build Tier C

Muse planning followed by Muse Tier-C execution can create correlated blind spots.

The routing policy mitigates this by requiring Tier C to have a qualifying independent frozen oracle plus bounded/reversible risk.

`planner_executable_spec` is explicitly tracked separately because it remains model-correlated even when planner and builder are different sessions.

## Required telemetry from day one

Persist enough information to measure at least:

### Planning quality

- child tickets created;
- child tickets cancelled before execution as unnecessary;
- wrong-agent reassignments;
- DEV tier reclassifications caused by planning;
- duplicate proposals rejected;
- dependency corrections;
- replans without progress;
- Objective cycles without progress;
- downstream rework attributable to planning where deterministically known.

### Tier-C safety

- C tickets started/closed;
- C failures detected within candidate cycle;
- C material replans;
- C late failures;
- late-failure severity;
- oracle type;
- tier reason;
- work type;
- project.

### Attribution prerequisite

Late failures must link deterministically through affected Git SHA(s) to the originating ticket and then to:

- Objective;
- coding tier;
- tier reason;
- oracle type;
- oracle refs.

If attribution is unavailable, do not pretend the late-failure metric is known.

## Future hypothesis: optional planning advisors

**Not implemented.**

A future bounded `request_planning_advice()` capability may be evaluated only if telemetry demonstrates systematic planning failures where the expected value of Claude/Codex advice exceeds added quota, latency, and complexity.

Possible evidence:

- high unnecessary-ticket rate;
- repeated wrong-agent routing;
- repeated missed dependencies;
- frequent Objective loops;
- systematic misinterpretation of research;
- systematic Tier-S overuse/underuse;
- premature completion;
- significant planning-attributable rework.

If ever implemented, Muse should remain the Objective Planner unless a later ADR explicitly replaces this decision.

Claude/Codex would provide advisory artifacts, not become silent co-planners.

## Non-goal

This ADR does not define child-ticket runtime selection.

The Objective Planner never chooses runtime/account/model for child tickets.

## Review trigger

Review this ADR after enough real Objective executions exist to compare:

- planning failure rate;
- downstream rework;
- Objective throughput;
- scarce Claude/Codex quota preservation;
- Tier-C late-failure behavior by oracle type.

Changes to planner model or advisor policy require an explicit owner-approved configuration/ADR change. There is no automatic self-modification.
