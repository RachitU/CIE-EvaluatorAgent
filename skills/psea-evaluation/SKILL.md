---
name: psea-evaluation
description: PSEA solution evaluation framework with Initial Feasibility assessment. Use when evaluating whether a proposed solution is an appropriate response to a validated problem.
license: Apache-2.0
compatibility: crewai>=0.1.0
metadata:
  author: opportunity-validator
  version: "1.0"
allowed-tools: web-search
---

## PSEA Framework

Evaluate the proposed solution against four dimensions. Focus on critical issues only.

**P — Problem-Solution Fit**
- Does the solution actually address the validated problem?
- Is it differentiated from existing alternatives?
- Does it create meaningful value?
- Is it more than a feature disguised as a company?

**S — Simplicity**
- Is the solution appropriately simple?
- Is it unnecessarily complex when a simpler solution exists?

**E — Ethics**
- Does it comply with relevant laws?
- Does it respect user privacy?
- Does it avoid harmful outcomes?
- Does it meet societal ethical expectations?

**A — Assumptions**
- What customer behaviour assumptions are embedded?
- What market, technology, adoption, and revenue assumptions exist?
- Make hidden assumptions explicit and numbered.
- Challenge them with real market data where available.

## Initial Feasibility (evaluated separately from PSEA)

A reality check — not deep analysis:
- Is the solution technically achievable?
- Are required resources attainable?
- Is it practical enough to warrant deeper investigation?

## PSEA Process

1. Evaluate all four dimensions and Initial Feasibility on the first pass.
2. Identify the highest-risk unresolved issue.
3. Probe ONE issue at a time with ONE focused question.
4. After two follow-up questions on an issue, close it as Accepted Assumption.
5. Approve only when all dimensions are resolved.

## Approval Rules

Approve **only** when:
1. Problem-Solution Fit is strong and differentiated from search-found competitors.
2. No major ethical or legal concerns (or they are clearly addressed).
3. Key assumptions are identified and acknowledged.
4. Initial feasibility is reasonable.

## Question Rules

- Ask at most ONE question per turn.
- Focus only on the highest-risk unresolved issue.
- Never investigate multiple issues at once.
- Never repeat a concern using different wording.
- Never ask more than two follow-up questions on the same issue.
- Never ask about: market size, TAM, SAM, SOM, CAC, LTV, pricing, revenue forecasts, profitability calculations — unless the founder introduced them.

This stage evaluates **solution quality**, not business model.

## Response Format

**When refinement is needed:**
```
VERDICT: NEEDS_REFINEMENT

ISSUE IN FOCUS: [P/S/E/A]

QUESTION:
[single focused question]

GOOD ANSWER EXAMPLE:
[concise example of a strong answer]

WHAT HELPS:
- concrete details
- assumptions
- constraints
```

**When solution is acceptable:**
```
VERDICT: READY_FOR_DFV

EVALUATION SUMMARY:
Problem-Solution Fit: [verdict]
Simplicity:           [verdict]
Ethics:               [verdict]
Key Assumptions:
  1. [assumption]
  2. [assumption]
Initial Feasibility:  [verdict]

NEXT STEP: DFV Evaluation
```
