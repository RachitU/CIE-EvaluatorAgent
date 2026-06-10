---
name: skills
description: Entrepreneurial opportunity and idea validation using structured frameworks. Activates TIPSC + Need Validation for opportunity evaluation and PSEA + Feasibility for idea evaluation.
license: Apache-2.0
compatibility: crewai>=0.1.0
metadata:
  author: opportunity-validator
  version: "1.0"
allowed-tools: web-search
---

# Opportunity Validator Skills

This package provides two evaluation skills that inject framework knowledge into agents:

| Skill file | Agent | Frameworks |
|---|---|---|
| [tipsc-evaluation/SKILL.md](tipsc-evaluation/SKILL.md) | Opportunity Evaluation Agent | TIPSC · Need Validation · COP |
| [psea-evaluation/SKILL.md](psea-evaluation/SKILL.md) | Idea Evaluation Agent | PSEA · Initial Feasibility |

## Skill Descriptions

### tipsc-evaluation

Teaches the Opportunity Evaluation Agent the full TIPSC framework (Timely, Important, Profitable, Solvable, Contextual), the Need Validation checklist, COP inference rules, evidence standards, and question discipline. Without this skill the agent lacks the detailed criteria for what constitutes sufficient founder evidence at each stage.

**Use when:** Running Phase 1 — Opportunity Evaluation.

### psea-evaluation

Teaches the Idea Evaluation Agent the PSEA framework (Problem-Solution Fit, Simplicity, Ethics, Assumptions) and the Initial Feasibility reality check. Defines approval rules, question discipline, and the two-question close-out rule so the agent does not over-probe.

**Use when:** Running Phase 2 — Idea Evaluation.

## Usage in Code

```python
from validate_opportunity import Skill, load_skill

tipsc_skill = load_skill("tipsc-evaluation")
psea_skill  = load_skill("psea-evaluation")

opportunity_skills = [tipsc_skill]
idea_skills        = [psea_skill]
```

Skills are passed to agents via `_goal_with_skills(base_goal, skills)`, which appends
skill instructions to the agent's goal prompt at runtime.
