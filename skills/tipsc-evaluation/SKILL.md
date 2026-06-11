---
name: tipsc-evaluation
description: TIPSC opportunity evaluation framework with Need Validation and COP inference. Use when evaluating whether a problem represents a real entrepreneurial opportunity.
license: Apache-2.0
compatibility: crewai>=0.1.0
metadata:
  author: opportunity-validator
  version: "1.0"
allowed-tools: web-search
---

## TIPSC Framework

Evaluate the problem against five criteria. Assign **Strong / Weak / Unclear** to each.

**T — Timely**: Is the problem relevant and worth solving now?
- Long-standing problems can still be Timely if unsolved, growing, or newly enabled by technology.
- TIMELY DOES NOT MEAN NEW.

**I — Important**: Is the pain real, frequent, and severe? Do people actively want this solved?

**P — Profitable**: Does solving this create meaningful value?
- DO NOT require pricing, revenue projections, TAM/SAM/SOM, CAC, or LTV.
- Mark UNCLEAR if commercial viability is unknown.
- A problem indicates profitability when customers lose time, money, or experience measurable pain.
- Do not assign Weak solely because revenue evidence is unavailable.

**S — Solvable**: Can this realistically be solved with available technology and accessible resources?

**C — Contextual**: Does this fit the regulatory, cultural, and socio-economic environment?

### TIPS Rating Guidance

Use GREEN / YELLOW / RED for the final analysis and coaching.

- GREEN means the criterion is supported by explicit founder evidence.
- YELLOW means the criterion is plausible but incomplete or time-horizon dependent.
- RED means the criterion is weak, unproven, or contradicted by the conversation.

For this project, ignore C (Context) in the live scoring and focus on T, I, P, and S.

### TIPSC Process
1. First pass: assign Strong / Weak / Unclear to every criterion.
2. Skip criteria marked Strong — do not revisit them.
3. Investigate Weak and Unclear criteria ONE AT A TIME, in priority order.
4. Ask exactly ONE focused question per turn.
5. After each response: mark resolved, accept as known risk, or continue probing.
6. Move to the next criterion only after the current one is resolved.
7. After two follow-up questions on a criterion, close it regardless.
8. After 3–4 student answers total, force a final structured handoff and stop the chat.

## Need Validation Framework

After all TIPSC criteria are resolved, validate the need. Confirm all five:

1. A specific customer group is identified.
2. Problem frequency is identified.
3. Consequence is identified.
4. Existing alternatives are identified.
5. Why alternatives fail is identified.

Once all five are present: **Need Validation = COMPLETE**. Do not ask additional Need questions.

The final handoff must be structured and must include the problem statement,
customer segment, consequence, assumptions, and per-criterion TIPS evidence,
gaps, and coaching notes.

## COP Assessment (Inferred Naturally)

DO NOT run COP as a separate interview. Infer organically from the TIPSC and Need conversation.
- **C — Capability**: Does the founder have or can acquire required skills?
- **O — Opportunity**: Does the founder have real access to this market?
- **P — Passion**: Is the founder genuinely motivated, not just trend-chasing?

Your questions about the problem should simultaneously reveal COP.

## Approval Rules

Approve **only** when:
1. All TIPSC criteria are Strong or accepted as known risks.
2. Need is confirmed with real evidence (all five points above).
3. COP has been assessed organically.

## Evidence Standards

This is early-stage validation — NOT academic research. Reasonable estimates and real-world examples ARE sufficient.

A criterion is sufficiently answered when the founder has provided:
- A reasonable estimate of who is affected and how many
- A realistic description of how often the problem occurs
- At least one concrete example of the problem
- A plausible reason why existing solutions fall short

**Minimum founder evidence before assigning Strong:**
- Specific customer group
- Specific pain/problem
- Frequency
- Consequence

If any are missing, assign Unclear and ask a follow-up.

**Founder evidence overrides search.** A criterion cannot be marked Strong based only on search findings. Do not assume founder-specific evidence from generic market statistics.

## Question Rules

- Ask at most ONE question per turn.
- Never investigate more than one criterion at a time.
- Never revisit a criterion already marked Strong.
- Never ask the same question twice (repetition is a validation error).
- Never ask about: TAM, SAM, SOM, pricing, revenue projections, CAC, LTV — unless the founder introduced them.
- Do not demand more precision than a founder can reasonably have at this stage.
- On the final turn, do not ask another question. Emit the structured handoff and terminate the conversation.
