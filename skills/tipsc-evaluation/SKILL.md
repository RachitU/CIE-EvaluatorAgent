---
name: tipsc-evaluation
description: >
  TIPS opportunity evaluation framework with GREEN/YELLOW/RED scoring.
  Reference document — not loaded at runtime in v2.
  Used by the TIPS Evaluation Agent (Phase 2 of the validation pipeline).
license: Apache-2.0
compatibility: crewai>=0.1.0
metadata:
  author: opportunity-validator
  version: "2.0"
---

# TIPS Evaluation Framework

> **v2 note:** C (Context) is intentionally excluded from live scoring in this
> version. The framework is referred to as TIPS throughout, not TIPSC.
> Context will be reintroduced in a future version once the C assessment
> methodology is finalised.

---

## TIPS Criteria

Evaluate the problem against four criteria. Assign **GREEN / YELLOW / RED**.

**T — Timely** : Is the problem relevant and worth solving now?
- Long-standing problems can still be Timely if they are unsolved, growing,
  or newly enabled by technology.
- TIMELY DOES NOT MEAN NEW.
- Assess the time horizon explicitly stated by the student.

**I — Important** : Is the pain real, frequent, and severe?
- Do people actively want this solved?
- Is the consequence direct and measurable, or vague and indirect?
- Distinguish between Must Have, Should Have, and Nice to Have.

**P — Profitable** : Does solving this create meaningful value that someone will pay for?
- DO NOT require pricing projections, TAM/SAM/SOM, CAC, or LTV at this stage.
- A problem indicates profitability when customers lose time, money, or
  experience measurable pain that they would pay to avoid.
- Assess whether a plausible named payment model exists (B2B, subscription,
  one-time fee, B2B2C, freemium, etc.).

**S — Solvable** : Can this team realistically build it with available resources?
- Assess skills, domain knowledge, data access, and compute.
- A single clearly identified gap = YELLOW. Multiple gaps = RED.

---

## Scoring Rubric

```
T — TIMELY
  GREEN  : Active daily/weekly problem  OR  time horizon ≤ 6 months
  YELLOW : Time horizon 6 months – 1 year
  RED    : Horizon > 1 year, undefined, or hazy

I — IMPORTANT
  GREEN  : Explicitly "Must Have" + direct, measurable consequence
  YELLOW : "Should Have"  OR  consequence is indirect or vague
  RED    : "Nice to Have"  OR  consequence is trivial / not stated

P — PROFITABLE
  GREEN  : Clear YES to paying + plausible named model identified
  YELLOW : Possibly yes, but model undefined or only indirect
  RED    : No willingness to pay; no monetisation path identified

S — SOLVABLE
  GREEN  : Team has the skills, data, compute, and domain knowledge for MVP
  YELLOW : Can build a basic version but ONE clear gap exists
  RED    : Significant skill, data, or resource gaps; not feasible as stated
```

---

## Evidence Standards

This is early-stage validation — not academic research. Reasonable estimates
and real-world examples from the student ARE sufficient.

A criterion is **sufficiently answered** when the student has provided:
- A named or clearly described customer group
- A specific pain or problem statement
- Frequency or time horizon of the problem
- Consequence if unsolved (quantified where possible)

**Student evidence overrides search results.**
A criterion cannot be marked GREEN based solely on search findings.
Do not project founder-specific capabilities from generic market data.

---

## Process Rules

1. First pass: score all four criteria T, I, P, S simultaneously.
2. Ask ONE focused question targeting the weakest criterion (first RED, then YELLOW).
3. After each student response: update affected ratings, then ask the next question.
4. After `max_tips_turns` student answers (set in `config/settings.yaml`),
   emit the final verdict regardless of remaining YELLOW ratings.
5. Never ask more than two questions about the same criterion.
6. Never ask about: TAM, SAM, SOM, pricing projections, CAC, LTV — unless
   the student introduced those terms themselves.

---

## Coaching Rules

For every YELLOW or RED criterion, provide exactly ONE coaching suggestion:
- Be concrete and actionable, not general ("define a B2B model" not "think about revenue")
- Reference what the student actually said
- A good coaching note changes a YELLOW to GREEN in one follow-up answer

---

## Final Output

The final TIPS report must include:
- Updated GREEN/YELLOW/RED for all four criteria with evidence
- `VERDICT: READY_FOR_DFV`
- Structured JSON matching this schema exactly:

```json
{
  "refined_idea": {
    "customer_segment":  "...",
    "qualified_problem": "...",
    "consequence":       "...",
    "proposed_solution": "..."
  },
  "tips_validated_metrics": {
    "timely_factor":          "...",
    "importance_metric":      "...",
    "profitability_pivot":    "...",
    "solvability_constraint": "..."
  },
  "tips_scores": {
    "T": "GREEN|YELLOW|RED",
    "I": "GREEN|YELLOW|RED",
    "P": "GREEN|YELLOW|RED",
    "S": "GREEN|YELLOW|RED"
  }
}
```

The `tips_validated_metrics` fields must each be one sentence of real evidence
from the conversation — not generic descriptions of the criterion.
