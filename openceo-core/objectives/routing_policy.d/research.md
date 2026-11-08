---
document_id: objective-planner-routing-policy-research
title: OpenCEO Objective Planner Routing Policy — research detail
version: 1.0
status: NORMATIVE
updated: 2026-11-08
runtime_document: true
planner_reads_this_document: on_demand
language: en
parent: ../routing_policy.md
---

> Planner policy detail for the `research` agent (ticket #618).
> Normative, read on demand: the core policy requires reading this
> file before assigning research work. §45–§49 keep their numbers;
> nothing here is pasted into the prompt.

# RESEARCH ROUTING

## 45. Research trigger: internet, library and technology questions

Every question whose answer lives outside the repository routes to
`research` with the `investigar` skill: internet facts, library
behavior, versions and APIs, technology comparisons, provider
semantics, licensing, benchmarks, cost models, regulatory
constraints. The question and the decision it unlocks travel in the
ticket (core §6, §11).

Discovery the DEV agent can answer inside the repository — root
cause, architecture, compatibility, production evidence — stays a
DEV `tier_a` ticket (dev detail §21). When both are needed, the
research ticket comes first and the DEV ticket waits on it.

Never answer an external question from the plan itself: no guessed
versions, prices, limits or provider behavior. When downstream work
depends on the answer, create the research ticket first and put the
rest in `roadmap_remaining` (core §9).

## 46. Quick live by default

`quick` is the default mode: admit with mode `quick` unless the
ticket names `deep` under §47. The ticket bridge, the submit
boundary tool and the `investigar` recipe all default to quick; the
project policy only narrows (this stretch: `jardal-openceo` is
quick-only).

Canonical run (the recipe owns the steps):

```text
admit one investigation under the ticket's consumer
  (openceo_research_submit: purpose, question, mode, seed_urls)
   -> poll it (openceo_research_get) until terminal
   -> write the brief from the returned evidence only
   -> attach the brief to the requesting ticket
   -> end hecho naming the attachment
```

## 47. Deep selection: explicit criteria, live execution pending

Deep is SELECTED only when every criterion below holds, and the
ticket names each one. Selection is defined and verified with a
simulated service; live deep execution belongs to the deep stretch,
not this one: today the project denies deep and a deep submit fails
closed (`forbidden`), never silently downgraded to quick.

```text
1. coverage gap: a prior quick brief provably cannot cover the
   question within quick limits (names the gap);
2. independent verification: researcher/verifier sets would
   materially reduce the decision risk (names the conflict or
   the claim that needs a second reading);
3. decision stakes: the answer unlocks spend, a security boundary,
   or an irreversible migration (names which);
4. paid budget: the effective budget is positive — the minimum of
   the server and the project paid budgets. Zero budget means
   quick only, however strong the other criteria.
```

## 48. Brief attached, result back to the planner

The finished brief is attached PRIVATE to the requesting ticket
(`openceo_attach_ticket_file`); the run ends `hecho` naming the
attachment. A vague question with no decidable purpose ends
`bloqueado` naming what is missing — never a guessed brief.

The result returns to the Objective planner (core §10): re-plan
from first principles. Research never auto-continues to DEV, and a
new question uncovered by the brief is a new research ticket, not
scope creep on the closed one.

## 49. Anti-patterns

Never:

- route internet, library or technology questions to dev,
  operations or secretary because a browser or shell exists there
  (domain ownership is separate from the execution mechanism,
  core §7);
- call research providers from the agent: the run admits through
  the ticket bridge only; credentials stay server-side;
- present vendor claims as verified facts; mark conflicts,
  uncertainties and source references (core §11);
- select deep to dodge a thin quick brief: first quick, then deep
  with its §47 criteria named.
