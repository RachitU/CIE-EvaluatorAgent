---
name: TIPS Evaluation
description: Framework for evaluating entrepreneurial opportunities using the manager's Green/Yellow/Red rubric.
version: "2.0"
---

# TIPS Evaluation Framework

Four primary criteria scored Green / Yellow / Red.
One soft contextual awareness check (C) — noted but not a gate.

---

## T — Timely

Is the problem worth solving **right now**?

| Rating | Criteria |
|--------|----------|
| 🟢 Green | Active daily/weekly issue. Time horizon is short or medium term (≤ 1 year). Urgency is clear. |
| 🟡 Yellow | Problem exists but urgency is approximate (~1 year horizon or somewhat hazy). |
| 🔴 Red | Problem is long-term, speculative, or time horizon is > 1 year and very vague. |

**Coaching question**: "When does this problem affect your customer — is it happening right now, every week, or only sometimes in the future?"

---

## I — Important

Is the consequence serious enough that people **must** solve it?

| Rating | Criteria |
|--------|----------|
| 🟢 Green | Problem is a **Must Have** — direct, measurable harm. Consequence is within a 1-year time horizon. |
| 🟡 Yellow | Problem is a **Should Have** — significant but not critical. Time horizon is approximately 1 year. |
| 🔴 Red | Problem is a **Nice to Have** — mild inconvenience. Time horizon is > 1 year or consequence is vague. |

**Coaching question**: "What specifically happens to your customer if this problem is NOT solved? How severe is that outcome?"

---

## P — Profitable

Are customers willing to pay for a solution?

| Rating | Criteria |
|--------|----------|
| 🟢 Green | Students have clear evidence that customers would pay (surveys, direct interviews, analogous products, or explicit willingness). |
| 🟡 Yellow | Customers may pay — team has indirect evidence or a reasonable model, but nothing confirmed. |
| 🔴 Red | No evidence of willingness to pay. Customers expect it free, or the team has not considered this. |

**Coaching question**: "Have you spoken to potential customers? Would they pay for a solution, and how much?"

---

## S — Solvable

Can **this team** build a solution with the resources they have?

| Rating | Criteria |
|--------|----------|
| 🟢 Green | Team has the required knowledge, computing resources, data access, and budget (or credible plan to get them). |
| 🟡 Yellow | Team has most requirements but has identifiable gaps that are bridgeable (can learn, partner, or acquire). |
| 🔴 Red | Team is missing critical skills, data, compute, or funding with no clear path to acquire them. |

**Coaching question**: "What technical skills, data, tools, and budget does your team have? What is still missing?"

---

## C — Contextual (Soft Awareness Check)
Are there any obvious **regulatory, cultural, or socio-economic blockers**?

| Rating | Criteria |
|--------|----------|
| 🟢 Green | No major contextual concerns identified. |
| 🟡 Yellow | Vague context, but no obvious blockers. |
| 🔴 Red | Clear regulatory or cultural blocker. |

- This is NOT a hard gate. A Yellow or Red here does not block progression.
- Ask at most ONE question about context. Do not probe deeply.

---

## Process Rules
1. First pass: assign a Green/Yellow/Red color to every criterion T, I, P, S. Note C.
2. Python controls which criterion is in focus — you only evaluate the one you are given.
3. Ask exactly ONE coaching question per turn.
4. After each student response: upgrade rating if evidence improves, or accept current rating.
5. A Red criterion after 3 turns is accepted as a known risk and moved forward — do not block.
6. C criterion: assess in 1 turn only. Move on immediately.

## Output Format for Each Verdict
Always output final verdicts in this exact format so Python can parse them:

VERDICT UPDATE: T — Timely: 🟢 Green
VERDICT UPDATE: I — Important: 🟡 Yellow
VERDICT UPDATE: P — Profitable: 🔴 Red
VERDICT UPDATE: S — Solvable: 🟢 Green
VERDICT UPDATE: C — Contextual: 🟢 Green

## Evidence Standards
- Green requires: bounded customer group + specific consequence + timeframe + some form of validation evidence (can be logical or anecdotal if formal data is unavailable). **Be lenient: if the student provides a reasonable logical argument, give them Green.**
- Yellow requires: at least the customer group + consequence, even if timeframe or validation is approximate. **If you have coached for 2 or 3 turns and the answer is still vague, accept it as Yellow and move on.**
- Red means: the student cannot provide any of the above despite coaching
- Do NOT demand investor-grade evidence — students are in early ideation
