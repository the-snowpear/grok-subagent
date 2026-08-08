# Decision and context-budget contract

本文件定义 grok-work 的“强 Main 决策 + 低 Main context”数据交换格式。

## 1. Decision authority

### Reserved to Codex/Main

- requirements interpretation and acceptance criteria;
- architecture, cross-unit interfaces, data model, state-machine semantics;
- backward-compatibility and migration policy;
- task decomposition, ownership, ordering, concurrency;
- severity adjudication and accepted residual risk;
- fix scope, integration decision, final signoff.

### Delegable to workers

- evidence gathering;
- local implementation mechanics inside an already-decided contract;
- focused tests and measurements;
- independent review findings;
- local reversible choices that do not change observable contract.

When a worker encounters a reserved decision, return:

```text
DECISION_NEEDED
Question:
Why it matters:
Evidence:
Option A:
Option B:
Recommendation (optional):
Trade-offs:
```

A recommendation is advice, not authority.

## 2. Decision Snapshot

Main should carry forward only this compressed state between phases:

```text
Goal:
Acceptance criteria:
Chosen design:
Key invariants:
Implementation units + ownership:
Protected/non-goal scope:
Required tests:
Open decisions/blockers:
Accepted risks:
```

Target: 10–25 lines. Do not embed raw explorer reports.

## 3. Explorer Work Order

```text
ROLE: Explorer (read-only; role="explore")
QUESTION: <one decision-relevant question>
WHY MAIN NEEDS THIS: <decision this evidence will inform>
SCOPE: <paths/subsystems>
DO NOT: edit, patch, commit, redesign, broaden investigation
RETURN:
- Direct answer
- Evidence (file:line / concise command output)
- Constraints
- Risks
- Unknowns
- DECISION_NEEDED options only when evidence exposes a real choice
OUTPUT BUDGET: target 600–900 tokens; verbose material -> artifact/path
```

One Explorer should answer one coherent question. Avoid “review the whole repo”.

## 4. Implementer Work Order

```text
ROLE: Implementer (role="implement"; isolated worktree default)
OBJECTIVE: <single outcome>

DECISION ALREADY MADE:
<the design/contract selected by Main; do not reopen it>

SELECTED EVIDENCE:
- <file:line fact>
- <file:line fact>

OWN:
- <allowed files/modules>

REQUIRED CHANGES:
1. <behavioral change>
2. <behavioral change>

INVARIANTS:
- <must remain true>

DO NOT TOUCH / NON-GOALS:
- <paths/features>

ACCEPTANCE TESTS:
- <command/behavior>

STOP CONDITIONS:
If code reality conflicts with the decided contract, do not redesign.
Return DECISION_NEEDED with evidence and options.

RETURN ONLY:
- changed files
- concise implementation summary
- focused tests + exact result
- unresolved risks/DECISION_NEEDED
- patch/result artifact location
OUTPUT BUDGET: target 400–700 tokens; no full diff/log in chat
```

## 5. Reviewer Work Order

```text
ROLE: Independent Reviewer (role="review"; read-only policy)
REVIEW AGAINST:
- Work Order
- Acceptance criteria
- Decision Snapshot invariants
- Actual patch/result artifact

DO NOT:
- modify code
- redesign unless required to explain a blocker
- send work instructions directly to Implementer

RETURN:
Verdict: APPROVE | CHANGES_REQUESTED
P0/P1/P2/P3 findings only when concrete
For each finding:
  Evidence: file:line
  Violated criterion/invariant
  Failure scenario
  Suggested fix shape (not code)
Residual risks
OUTPUT BUDGET: target 600–900 tokens; omit praise and long summaries
```

## 6. Main Fix Order

Main adjudicates reviewer findings before work is authorized:

```text
ROLE: Implementer follow-up
APPROVED FINDINGS TO FIX:
- F1: <precise issue>
- F3: <precise issue>

REJECTED/NON-BLOCKING FINDINGS:
- F2: <do not change; rationale>

DECISION / REQUIRED FIX SHAPE:
<Main's resolution>

SCOPE:
<same or explicitly expanded ownership>

REGRESSION TESTS:
<exact tests>

Do not address other optional ideas.
Return concise delta + test result.
```

This preserves Main decision authority while using durable follow-up context.

## 7. Context firewall

### Main should normally see

- user goal and acceptance criteria;
- Decision Snapshot;
- short Evidence Packets;
- implementation summaries + artifact pointers;
- prioritized reviewer findings;
- concise test status;
- unresolved decision requests.

### Main should normally not ingest

- full repository files;
- full diffs from every worker;
- complete test logs;
- repeated copies of requirements;
- verbose worker reasoning;
- low-value progress narration.

If raw data is needed for adjudication, retrieve only the exact range implicated by conflicting evidence.

## 8. Efficient graph policy

### S

```text
1 explorer -> Main decision -> 1 implementer -> 1 reviewer -> verify
```

### M

```text
1–2 targeted explorers -> Main decision -> 1–2 non-overlap implementers
-> 1 reviewer -> aggregate review only if multiple units interact
```

### L/high-risk

```text
2–3 orthogonal explorers -> Main decision -> bounded implementers
-> spec + code reviewers -> Main adjudication -> aggregate review
```

Do not create parallel agents whose expected information gain substantially overlaps.

## 9. Cost accounting heuristics

Optimize for information gain per Main token:

- pay worker tokens to inspect 20 files rather than loading 20 files into Main;
- pay worker tokens to parse a 5,000-line test log rather than putting it in Main context;
- spend Main tokens on choosing between well-supported options;
- spend additional agent count only when independence, parallelism, or confirmation materially reduces risk;
- reuse completed worker context for bounded follow-ups;
- compress after each phase so later prompts use the canonical snapshot, not history.
