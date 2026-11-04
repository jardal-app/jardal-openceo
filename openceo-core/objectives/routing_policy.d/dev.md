---
document_id: objective-planner-routing-policy-dev
title: OpenCEO Objective Planner Routing Policy — dev detail
version: 1.0
status: NORMATIVE
updated: 2026-10-12
runtime_document: true
planner_reads_this_document: on_demand
language: en
parent: ../routing_policy.md
---

> Planner policy detail for the `dev` agent (TASK-2026-00201).
> Normative, read on demand: the core policy requires reading this
> file before assigning dev work, and always to choose the tier.
> §18–§35 keep their numbers; nothing here is pasted into the prompt.

# DEV ROUTING

## 18. DEV tier semantics

DEV has exactly four execution tiers:

```text
tier_s
tier_a
tier_b
tier_c
```

Use these mental labels:

```text
C = DEFAULT SAFE EXECUTION
B = COMPLEX STRONG SOLVING
A = DISCOVERY / ARCHITECTURE / RECOVERY
S = INDEPENDENT CHALLENGE + VERIFY + CONVERGE
```

The four executors are all top-tier programming models within a few
points of each other. Letters order the work by nature, not the models
by quality: `tier_c` is the default for specified work a single
top-model session can own. S, A and B are chosen only with their
verifiable justification (§20, §21, §26); when none applies, C.

Quota is not the technical definition of a tier. Quota admission is handled separately.

## 19. Tier S — independent high assurance

Choose `tier_s` when orchestrating several independent model families
(independent plan, plan challenge, build, multi-family review,
arbitration) is expected to produce a materially more correct result than
one session can: a result as close to fault-free as possible, or
materially fewer residual errors.

The criterion is the expected value of independent challenge, never a
domain. No domain qualifies by itself and no domain is excluded: any work
qualifies when the justification of §20 holds. The examples below are
illustrative, not a closed list: critical security/auth boundaries, money
movement/payment correctness, destructive migrations, high-impact
data-integrity rules, critical multi-system incidents with unknown cause,
durable architecture with expensive reversal, conflicting evidence on
high-leverage decisions, subtle distributed invariants, cross-repository
compatibility with large blast radius, privacy or compliance boundaries,
public contracts whose breaking change is expensive, logic that fails
silently (numerics, ordering, idempotency, concurrency) where the first
plausible solution is often wrong, ambiguous specifications whose
independent readings should be compared, and algorithm or strategy
correctness where one wrong assumption compounds.

An explicit request from the owner or the ticket for maximum assurance, a
near fault-free result or multi-model orchestration is itself a valid
reason to choose S: cite it.

Tier S is not "big ticket mode".

Do not choose S solely because many files change, a ticket is P1, the implementation is long, or premium quota is available.

S is not rationed by percentage: when it is needed it is chosen and quota
admission (§28) paces it. A really complex ticket is never forced into C,
and a routine one is never promoted to S to spend capacity.

## 20. Mandatory Tier-S justification

When `coding_tool == tier_s`, the ticket must provide:

```text
why_consensus_is_needed
why_single_session_is_insufficient
```

The justification must identify the residual failure that independent model families or review stages are expected to catch, or cite the explicit request for maximum assurance or multi-model orchestration made by the owner or the ticket (§19).

If this cannot be stated concretely, do not select S.

This pair already answers why the default tier is insufficient: when
independent consensus has clear value, a single session cannot own the
work, so Tier S needs no separate `why_not_c`.

## 21. Tier A — discovery / architecture / recovery

Choose `tier_a` when the dominant difficulty is discovering what is true, what is wrong, what should be changed, what architecture is correct, or what hidden invariant exists.

Examples include unknown/systemic root cause, undocumented legacy semantics, contradictory production evidence, architecture design, broken/inconsistent build/deploy/tooling as part of the problem, cross-system invariants, or difficult synthesis of conflicting technical evidence.

> If the hard part is discovering the right thing to do, prefer A unless S is justified by the value of independent challenge.

Tier A requires `why_not_c`: the ticket must name what breaks if the
default tier executes it (a misdiagnosed root cause, a wrong
architecture locked in, a hidden invariant missed).

## 22. Oracle definition

An **oracle** is an independent mechanism that can determine whether a candidate implementation is correct enough to accept.

For any tier, the key question is not "is this ticket easy?"

The key question is:

> If the candidate implementation is wrong, will a meaningful error be detected independently before the ticket is accepted?

An oracle is stronger when its expected outcome was fixed before the candidate implementation and the builder cannot silently redefine it to make its own solution pass.

A declared oracle is evidence, not protection: only qualifying oracles
(§23) declared before the build are frozen and verified by the
orchestrator (§24), on any simple tier.

## 23. Oracle types

Authoritative enum:

```text
preexisting_tests
contract_or_schema
bug_reproduction
separate_ticket_tests
planner_executable_spec
builder_tests
```

- `preexisting_tests`: tests existed before candidate implementation and meaningfully cover behavior.
- `contract_or_schema`: fixed contract/schema/invariant/golden file not redefinable by builder.
- `bug_reproduction`: deterministic reproduction fixed before repair.
- `separate_ticket_tests`: tests/specification created in a separate prior ticket and frozen before build.
- `planner_executable_spec`: executable acceptance fixed before build by Objective Planner. May be frozen as a qualifying oracle, but is model-correlated when Muse plans and Muse builds; measure separately.
- `builder_tests`: useful additional evidence created by the same candidate builder. It does **not** independently validate the builder's interpretation and never triggers oracle protection (§24); it is recorded as evidence (§35).

## 24. Deterministic oracle protection

Oracle independence must be enforced, not merely described.

Before a simple-tier build with a declared qualifying oracle begins, OpenCEO/orchestrator must register the frozen oracle references.

For file-backed oracle references, record at least:

```text
reference/path
content hash
oracle type
ticket id
```

During/after build:

1. the orchestrator, not the builder, runs the qualifying oracle: the executable types (`preexisting_tests`, `bug_reproduction`, `separate_ticket_tests`, `planner_executable_spec`) run through the project's `dev.oracle_cmd` as a required gate after the build, with the same retries as the other gates; `contract_or_schema` is hash-protected and verified by the gates that consume it; without `dev.oracle_cmd` the oracle is hash-protected only and the run records that limit;
2. protected oracle references are checked against registered hashes;
3. if the builder modifies a protected oracle reference, the qualifying oracle is invalidated and the gate fails;
4. the ticket must not close on the basis of that modified oracle;
5. builder-created tests may still run, but remain `builder_tests`.

If implementation legitimately requires changing a contract/schema used as oracle, that artifact cannot simultaneously remain the frozen qualifying oracle. Re-plan or provide another independent oracle.

A ref that the ticket is expected to modify (a test it must fix or extend, a schema it must change) is never declared as an oracle: freezing it would fail the very work requested.

A `preexisting_tests` oracle must be green on the base branch to judge the work. If it is already red there, it cannot accept or reject anything: it stays hash-protected only and the run records that. Reproductions and specs written before the build are red at baseline by design and are never probed.

## 25. Tier C — default safe execution

Choose `tier_c` when the work is specified well enough for a single
top-model session to own, and no S, A or B justification applies.

C is the default: the ticket needs no oracle, no reversibility proof
and no blast-radius analysis to select it. Detection still matters —
a declared qualifying oracle is frozen and verified (§24), gates run,
and late failures are attributed (§31) — but the absence of an oracle
is never a reason to leave C.

Tier C routinely receives difficult or high-volume work: volume and
difficulty never select a tier by themselves (§41).

`tier_reason` for C states what the session owns, in one line.

The C executor chain (muse, dsh, claudep) provides availability, not
selection: when the primary link is unavailable or below its quota
reserve (§28), the same C ticket runs on the next link. The link used is
recorded per ticket (`executor_link`) and analyzed separately (§32); the
fallback never changes the tier and never escalates (§29). Equivalence
between links is measured through that record, not assumed.

## 26. Tier B — complex strong solving

Choose `tier_b` when the problem is bounded, A is not required for
open-ended discovery, S is not justified by independent-consensus
value, AND at least one of the following holds:

```text
the solution needs significant interpretation (fuzzy rules, semantic
judgment calls, compatibility trade-offs), OR
the blast radius is significant while staying below S stakes
```

Weak verification (builder-authored tests only, no usable oracle) is
never a prong by itself: it only aggravates one of the two above, and
the `why_not_c` must name that interpretation or blast radius.

Typical B cases include semantic refactors where errors pass local
tests, business logic whose rules need interpretation,
data/schema migrations with compatibility constraints, concurrency or
performance work, known-subsystem bugs needing interpretation, or
integration logic where correctness requires judgment.

Tier B requires `why_not_c`: one verifiable statement of what breaks
if the default tier executes this ticket. If that cannot be stated
concretely, do not select B.

B is not "the default because the task is hard" and never a fallback
for a missing oracle alone.

## 27. DEV routing algorithm

Apply in this order:

```text
1. S CHECK
   Would orchestrating independent model families materially improve
   correctness (any domain, or the owner/ticket asks for maximum
   assurance)?
   YES -> tier_s (justify per §20)

2. A CHECK
   Is discovering what is true/wrong/correct-to-do the dominant difficulty?
   YES -> tier_a (justify with why_not_c, §21)

3. B CHECK
   Is this really complex single-session work per §26?
   YES -> tier_b (justify with why_not_c)

4. OTHERWISE
   -> tier_c (the default)
```

The C/B boundary is about **provable complexity**, not perceived
difficulty and not oracle availability: B needs its `why_not_c`, C
needs nothing beyond specified work.

The review switch is orthogonal to the tier: A/B/C review on by
default and an explicit user instruction (`review: off`, e.g. "Tier C
sin reviewers") keeps the tier, builder and gates and only skips
reviewer invocations — a generic ask for speed never implies it.
Tier S always reviews and rejects off. Preserve an explicit
instruction on the decided ticket or child; omit the field otherwise.
A reviewed A/B/C ticket runs build → gates → one quorum round
(three reviewers, two independent families) → builder fix verdicts →
final gates → close; the round never recurses and never copies the S
pipeline. Pedido examples: "C sin reviewers", "A sin reviewers" and
"B sin reviewers" set off; "rápido" alone never does.

## 28. Quota is separate from technical routing

After technical classification, deterministic admission control protects scarce Tier-S capacity.

Configuration must define reservations for the scarce accounts/families used by Tier S: Claude and Codex, and Muse, which carries the Tier S builder and closer, the objective planner and the CEO review and is shared with the C primary link.

The reservations are quota for the Tier S family, with a fixed semantic:

- honored by Tier A/B admission and by the Tier C primary link (below its reserve the same C ticket runs on the next chain link, §25);
- consumed down to zero by Tier S: its cycle, its arbiter and the objective final-review arbiter. Their quota gate never applies the reservation; only real exhaustion parks them.

A Tier A or Tier B ticket that would invade its configured reservation becomes:

```text
waiting(quota_reserved)
```

Do not silently downgrade A/B to C merely to obtain throughput.

The scheduler may dispatch other ready tickets from the same Objective when dependencies permit.

Reservation policy is configuration, not LLM judgment.

Every tier consumes real, paid-for capacity. Generous default-tier
limits exist because the owner pays for them, not because the tier is
free: never describe any tier as cheap, free, economical, or weaker,
and never select a tier for its price.

## 29. Material failure of a simple DEV tier

Do not implement automatic `C -> B -> A -> S` escalation.

On a **material** failure of a simple DEV tier:

```text
1. reset candidate work according to repository policy
2. preserve failure evidence
3. update Objective state
4. invoke Muse Objective Planner once
5. Muse chooses directly among:
   - a different DEV tier
   - research/discovery
   - another registered agent
   - split/rewrite ticket
   - Objective-level restructuring
```

After material failure, reassigning the unchanged ticket to the same tier is forbidden.

Same-tier execution is allowed only if the ticket was materially split or its contract, oracle, scope, or evidence changed enough to constitute a new execution problem.

Material failure/replanning counts against Objective anti-loop limits.

An agent ends hecho, bloqueado or no_aplica with its reason
(TASK-2026-00202). A bloqueado or no_aplica objective child
returns once to this planner carrying motivo and necesidad in
words, never tool names: relaunch it with guidance in the ticket
description, reassign it, split it, or return capability_gap. A
second unresolved return parks capability_gap (code enforces the
single return); the motivo is never checked against the catalog.
A builder's unmerged work never dies with its verdict: a simple tier
keeps it on a blocked/, declined/ or failed/ branch and names it in
necesidad (TASK-2026-00478); relaunch or split from that branch
instead of redoing the work.

A loose ticket returns the same single way (TASK-2026-00204): its
first bloqueado or no_aplica replans the pedido with the return
as context, and the relaunched ticket carries the new decision.
A second unresolved return has no objective to park on, so the
ticket stays failed with its return history; the CEO reactive
review picks failed company executions up.

## 30. Tier-S ticket that is too large

Tier S still fits one bounded Tier-S cycle.

If a standalone Tier-S ticket does not fit, convert/decompose at Objective level.

If it already belongs to an Objective, replace it with smaller child tickets.

A size conversion (plan over budget or the size brake) is not a material failure (§29): split along the converted plan and give each child the tier its own §27 check gives. A child that still carries an S criterion, such as an auth boundary (§19), stays Tier S; a narrower lower-tier child never drops it silently.

Do not keep Tier-S sessions alive across packages/subplans.

## 31. Findings and late-failure attribution

To learn whether Tier C is safe, later defects must be attributable to original work.

Every promoted bug/finding that represents a possible regression must, where deterministically derivable, record:

```text
affected SHA(s)
originating ticket id(s)
originating Objective id
originating coding_tool
originating tier_reason
originating oracle_type
originating oracle refs
```

Git/ticket mapping is the attribution backbone.

Without SHA -> ticket attribution, `late_failure_rate` is undefined and must not be reported as measured.

Do not guess attribution with an LLM when deterministic Git history can resolve it.

## 32. Tier-C telemetry

Persist at least:

```text
C tickets started
C tickets closed
C failures detected during candidate cycle
C material replans
C late failures
late failure severity
oracle_type
tier_reason
work_type
project
executor_link
```

Core metric:

```text
late_failure_rate =
  attributed Tier-C tickets later proven materially incorrect
  /
  attributable Tier-C tickets closed
```

Analyze it by oracle type, tier reason, work type, executor link, and project.

The executor link (muse, dsh, claudep) is what lets the owner contrast the
primary link with the fallbacks; without it a late failure cannot be
attributed to the model that built the work.

Pay special attention to `planner_executable_spec` because Muse planner and Muse builder are model-correlated.

Tier mix (planner decisions only, TASK-2026-00204) is recorded
alongside, as information only: per objective, count planned tickets
by tier (`tier_mix`); per decided pedido, the ticket keeps its
provenance (`decided_by`, `tier_reason`, `planner_policy_sha`). The
keyword classifier and its per-assignment records are retired. No
threshold, alert or quota derives from the mix: a really complex
ticket is never forced into C, and the owner's estimate (~85 %
future C) is a reference to compare against, not an approval gate
(§33).

## 33. No automatic C/B policy drift

Telemetry informs decisions. It does not rewrite routing thresholds automatically.

If telemetry suggests the C/B boundary should change:

```text
produce a proposal
review evidence
apply change through configuration/policy as [OWNER] decision
```

No self-modifying routing policy.

## 34. Planner-generated executable oracle

The planner can name executable acceptance (tests or a spec) as the oracle of a ticket: `oracle.type = planner_executable_spec` with `oracle.refs` pointing to files that exist in the repository when the ticket starts (refs that do not resolve to a file are recorded unresolved and unprotected, §24). It declares; it does not write: nothing in OpenCEO authors the spec on the planner's behalf. A specification that has to be written first is its own prior ticket (`separate_ticket_tests`, §23), and the dependent ticket (`blocked_by` it) declares the resulting files as its oracle.

A declared `planner_executable_spec` is frozen before the build, its references/hashes are registered, the builder cannot alter it without invalidating the oracle, and the orchestrator runs it (§24).

However, `Muse planner -> Muse C builder` is model-correlated. Do not treat it statistically as equivalent to preexisting tests or an independently produced contract.

## 35. Builder tests

Builder-authored tests are encouraged and are additional evidence.

They are not a qualifying independent oracle and never trigger oracle protection (§24).

They must not be mislabeled as stronger oracle types.
