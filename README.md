# Entrepreneurial Opportunity Validation System

A two-agent AI system that validates entrepreneurial opportunities before a founder
commits to building a solution.

---

## How it works

```
Problem Input
  → Agent 1: Opportunity Evaluation (TIPSC → Need)
  → [APPROVED] → Solution Input
  → Agent 2: Idea Evaluation (PSEA + Feasibility)
  → [READY_FOR_DFV]
```

**Agent 1 — Opportunity Evaluation Agent**
- Phase 1a: TIPSC Triage (Timely · Important · Profitable · Solvable · Contextual)
- Phase 1b: TIPSC Deep-Dive (one criterion at a time, max 3 turns each)
- Phase 1c: Need Validation

**Agent 2 — Idea Evaluation Agent**
- Phase 2a: PSEA Evaluation (Problem-Solution Fit · Simplicity · Ethics · Assumptions)
- Phase 2b: Refinement Loop (until READY_FOR_DFV)

---

## Project structure

```
CIE-EvaluatorAgent/
├── validate_opportunity.py      # Main application
│
├── config/
│   └── settings.yaml            # LLM, search, conversation, display settings
│
├── prompts/
│   ├── opportunity_agent.yaml   # Agent 1: role, goal, backstory, task templates
│   ├── idea_agent.yaml          # Agent 2: role, goal, backstory, task templates
│   └── ui_strings.yaml          # All user-facing text (headers, prompts, labels)
│
├── skills/                      # CrewAI Skills — domain expertise packages
│   ├── tipsc-evaluation/        # TIPSC framework scoring rules
│   │   └── SKILL.md
│   ├── need-validation/         # Need validation checklist
│   │   └── SKILL.md
│   ├── psea-evaluation/         # PSEA framework & approval criteria
│   │   └── SKILL.md
│   └── question-quality/        # Shared question quality rules
│       └── SKILL.md
│
├── requirements.txt
├── .gitignore
└── README.md
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure the LLM

Edit `config/settings.yaml`:

```yaml
llm:
  model: "openai/your-model-name"
  base_url: "http://localhost:1234/v1"   # LM Studio, Ollama, OpenAI, etc.
  api_key: "your-key"
```

### 3. (Optional) Enable web search

Get a free Serper key at https://serper.dev (2,500 searches/month).

Set via environment variable (recommended):
```bash
export SERPER_API_KEY=your_key_here
```

Or set `serper_api_key` directly in `config/settings.yaml` (not recommended for shared repos).

### 4. Run

```bash
python validate_opportunity.py
```

Type `quit`, `exit`, or `q` at any prompt to end the session gracefully.

---

## Configuration reference

All settings are in `config/settings.yaml`.

| Key | Default | Description |
|---|---|---|
| `llm.model` | `openai/bonsai-8b` | Model identifier |
| `llm.base_url` | `http://localhost:1234/v1` | API endpoint |
| `llm.api_key` | `lm-studio` | API key |
| `memory.enabled` | `false` | CrewAI built-in memory (sentence-transformers) |
| `memory.embedder_model` | `all-MiniLM-L6-v2` | Local embedder when memory is on |
| `search.serper_api_key` | `""` | Serper key (prefer env var) |
| `search.results_per_query` | `4` | Organic results per search |
| `search.timeout_seconds` | `8` | HTTP timeout |
| `conversation.max_history_turns` | `8` | Q&A pairs kept in context for the LLM |
| `validation.max_turns_per_criterion` | `3` | Forced criterion/issue advancement |
| `display.console_width` | `62` | Terminal width for rules |

---

## Skills

Skills are CrewAI's filesystem-based packages that inject domain expertise into agent prompts.
Each skill is a directory containing a `SKILL.md` file with YAML frontmatter and markdown instructions.

| Skill | Used by | Purpose |
|---|---|---|
| `tipsc-evaluation` | Opportunity Agent | TIPSC scoring framework and evidence standards |
| `need-validation` | Opportunity Agent | Need validation checklist (5 required evidence items) |
| `psea-evaluation` | Idea Agent | PSEA framework, scoring, and approval rules |
| `question-quality` | Both agents | Shared rules for question quality and evidence priority |

Skills keep methodology knowledge modular and editable without touching Python code.

---

## Customising prompts

All agent prompts and task templates live in `prompts/`.
You can edit them without touching Python code.

- `opportunity_agent.yaml` — Agent 1's goal, backstory, and task descriptions
- `idea_agent.yaml` — Agent 2's goal, backstory, and task descriptions
- `ui_strings.yaml` — Section headers, prompts, transitions, and warning messages

Task descriptions use Python `.format()` placeholders: `{problem}`, `{solution}`, etc.
These are filled at runtime in `validate_opportunity.py`.

---

## Requirements

- Python 3.10+
- A running LLM endpoint (LM Studio, Ollama, OpenAI API, etc.)
- See `requirements.txt` for Python packages
