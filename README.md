# Entrepreneurial Opportunity Validation System — v2.1

A two-phase AI pipeline that helps student teams define their problem clearly
and validate it against the TIPS opportunity framework before building anything.

---

## How it works

```
Startup Idea Input
  → Phase 1: Problem Definition Agent   (max 3 Q&A rounds)
      Extracts: Problem Statement · Customer Segment · Consequence · Assumptions
  → Phase 1.5: Solution Collection      (one-shot input)
  → Phase 2: TIPS Evaluation Agent      (max 4 Q&A rounds)
      Scores:  T (Timely) · I (Important) · P (Profitable) · S (Solvable)
      C (Context) is skipped in this version
  → DFV-Ready JSON Output
```

**Phase 1 — Problem Definition Agent**

Guides student teams through four structured components using targeted
one-question-per-turn coaching. Terminates automatically after the iteration
cap to prevent hallucination spirals on small local LLMs.

**Phase 2 — TIPS Evaluation Agent**

Scores each criterion GREEN / YELLOW / RED with evidence drawn from the
student's own answers. Coaches on every YELLOW or RED. Produces a structured
JSON handoff to the DFV (Design-For-Validation) stage.

---

## Project structure

```
opportunity_validator/
├── main.py                        # Main application — all agents, prompts, flow
│
├── config/
│   └── settings.yaml              # LLM, search, validation, display settings
│
├── skills/
│   └── tipsc-evaluation/
│       └── SKILL.md               # TIPS framework reference (not loaded at runtime)
│
└── README.md
```

### What was removed in v2

The following files from v1 are no longer used and should be deleted:

```
prompts/opportunity_agent.yaml     ← DELETE
prompts/idea_agent.yaml            ← DELETE
prompts/ui_strings.yaml            ← DELETE
skills/SKILL.md                    ← DELETE
skills/psea-evaluation/SKILL.md    ← DELETE (PSEA phase not in v2)
```

All agent prompts, task templates, and UI strings are now embedded directly
in `main.py` as Python functions. This makes prompt engineering faster on
local LLMs — you edit one file and re-run immediately.

---

## Setup

### 1. Install dependencies

```bash
pip install crewai crewai-tools pydantic pyyaml requests
```

### 2. Configure the LLM

Edit `config/settings.yaml`:

```yaml
llm:
  model:    "openai/your-model-name"
  base_url: "http://localhost:1234/v1"   # LM Studio, Ollama, OpenAI, etc.
  api_key:  "your-key"
```

### 3. Configure web search (optional)

Get a free Serper key at https://serper.dev (2,500 searches/month).

```bash
export SERPER_API_KEY=your_key_here
```

**Important:** Set `enabled_for_local_llm: false` in `settings.yaml` when
using any model ≤ 8B parameters (bonsai-8b, Mistral-7B, LLaMA-8B, etc.).
Small models cannot reliably use injected web context and produce inconsistent
results when search is enabled. Enable only for frontier models (GPT-4o,
Claude 3, etc.).

### 4. Run

```bash
python main.py
```

---

## Configuration reference

All settings are in `config/settings.yaml`.

| Key | Default | Description |
|-----|---------|-------------|
| `llm.model` | `openai/bonsai-8b` | Model identifier for CrewAI |
| `llm.base_url` | `http://localhost:1234/v1` | API endpoint |
| `llm.api_key` | `lm-studio` | API key |
| `search.serper_api_key` | `""` | Serper key (prefer env var) |
| `search.results_per_query` | `3` | Organic results per query |
| `search.timeout_seconds` | `10` | HTTP timeout |
| `search.enabled_for_local_llm` | `false` | Set true only for frontier models |
| `validation.max_prob_turns` | `3` | Student answer cap in Phase 1 |
| `validation.max_tips_turns` | `4` | Student answer cap in Phase 2 |
| `display.console_width` | `62` | Terminal width for rules |

---

## Output format

The final output is a JSON block printed to the terminal:

```json
{
  "refined_idea": {
    "customer_segment":  "Undergraduate engineering students at a university",
    "qualified_problem": "Students miss deadlines because updates are scattered across email, WhatsApp, and college apps with no priority sorting",
    "consequence":       "Loss of 5–15% of internal marks; potential ineligibility to sit exams",
    "proposed_solution": "A local-first platform that aggregates and prioritises academic notifications from all channels"
  },
  "tips_validated_metrics": {
    "timely_factor":          "Active daily problem — students manually check 3+ platforms every day",
    "importance_metric":      "Must Have — missing a deadline directly reduces grades and can affect graduation",
    "profitability_pivot":    "One-time base fee + feature subscription; students confirmed willingness to pay",
    "solvability_constraint": "Team has Python and LLM skills for MVP; WhatsApp API integration is the one open gap"
  },
  "tips_scores": {
    "T": "GREEN",
    "I": "GREEN",
    "P": "YELLOW",
    "S": "YELLOW"
  }
}
```

Use the `tips_scores` to design your DFV experiments:
- **GREEN** → move forward; assumption is confirmed
- **YELLOW** → run a quick test to confirm before building
- **RED** → do a focused experiment before spending any resources

---

## Customising prompts

All prompts live in `main.py` as Python functions starting with `_prob_` or
`_tips_`. Edit these functions and re-run — no YAML reload required.

Key functions:

| Function | Phase | Purpose |
|----------|-------|---------|
| `_prob_first_pass()` | 1 | Initial problem assessment prompt |
| `_prob_followup()` | 1 | Per-turn follow-up prompt |
| `_prob_force_synthesis()` | 1 | Forced synthesis when cap is hit |
| `_tips_first_pass()` | 2 | Initial TIPS scoring prompt |
| `_tips_followup()` | 2 | Per-turn refinement prompt |
| `_tips_force_final()` | 2 | Forced final output when cap is hit |

---

## Known limitations

- C (Context) criterion is not scored in v2. It will be added in v3 once the
  regulatory/cultural assessment methodology is finalised.
- PSEA idea evaluation (solution quality, ethics, assumptions) from v1 is not
  included in v2. The system currently validates the problem and produces a
  TIPS-scored JSON; PSEA will return in a future phase.
- Web search is disabled by default for local LLMs. Enable cautiously.

---

## Requirements

- Python 3.10+
- A running LLM endpoint (LM Studio, Ollama, OpenAI API, etc.)
- `crewai`, `crewai-tools`, `pydantic`, `pyyaml`, `requests`
