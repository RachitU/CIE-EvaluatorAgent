# Evaluator Agent

An AI-powered evaluation agent built with CrewAI for automated assessment tasks.

## Overview

This project implements an evaluator agent system using CrewAI framework with LangChain integration. The agent is designed to perform automated evaluations based on configurable criteria.

## Features

- CrewAI-based multi-agent evaluation system
- Configurable agents and tasks via YAML
- Pydantic models for structured data validation
- YAML-based configuration for prompts and settings
- Market validation via web search (Tavily) for assumption checking and competitor research
- Regulatory compliance mapping for geography/sector-specific regulations
- Ethics pre-screening with three gates (harm, legal risk, problem-solution integrity)
- TIPSC scoring framework with automated readiness assessment
- Interactive follow-up questioning to resolve evidence gaps

## Installation

```bash
pip install -e .
```

## Dependencies

- Python >= 3.10
- crewai >= 1.14.6
- langchain-openai >= 0.3.0
- pydantic >= 2.0
- pyyaml >= 6.0
- crewai-tools (for TavilySearchTool)

## Pipeline

The evaluation pipeline runs in **five sequential phases** plus an optional follow-up loop:

1. **Pre-Evaluation** — Collects problem definition (6 items: problem, customer segment, consequence, assumptions, proposed solution, geography & sector).
2. **Market Validation** — Researches founder assumptions and competitor landscape via web search, outputs a validation summary (STRONG / MIXED / WEAK).
3. **Regulatory Mapping** — Identifies applicable regulations based on geography, sector, and business model. Flags compliance burden (HIGH/MEDIUM/LOW) and specialist review requirements.
4. **Ethics Pre-Screen** — Applies three gates (harm vector, legal risk, problem-solution integrity) to flag ethical concerns. Blocks only structurally harmful ideas.
5. **TIPSC Evaluation** — Scores overall readiness (T, I, P, S, C dimensions) and determines DFV eligibility, enriched with validation, regulatory, and follow-up context.
6. **Follow-Up Loop** (optional) — Up to 3 rounds of targeted questions to resolve RED/YELLOW score gaps; TIPSC is re-evaluated after each answer.

## Project Structure

```
src/
├── config/              # Agent and task configurations
│   ├── agents.yaml      # Agent definitions (preeval, validation, ethics, tipsc, regulatory, followup)
│   └── tasks.yaml       # Task definitions
├── skills/              # Custom skills/rubrics
│   ├── preeval/         # Pre-evaluation skills
│   ├── tipsc/           # TIPS-C scoring rubric
│   └── ethics/          # Ethics pre-screen rubric
├── main.py              # Entry point (pipeline orchestrator)
├── models.py            # Pydantic models
└── __init__.py
outputs/                 # Saved JSON outputs from each phase
```

## Usage

```bash
python -m src.main
```

The program will prompt for the 6 pre-evaluation inputs interactively.

## Configuration

Edit `src/config/agents.yaml` and `src/config/tasks.yaml` to customize agent behavior and evaluation criteria.

Edit skill rubrics in `src/skills/*/SKILL.md`:
- `preeval/SKILL.md` — interview/collection guidance
- `tipsc/SKILL.md` — TIPSC scoring rubric
- `ethics/SKILL.md` — three-gate ethics rubric

## LLM Model Configuration

This project uses LM Studio for local LLM deployment by default. The `load_llm()` function in `src/main.py` (line 115) controls the LLM connection:

```python
def load_llm() -> LLM:
    base_url = os.environ.get("LM_STUDIO_URL", "http://10.14.140.79:1234/v1")
    return LLM(
        model="openai/qwen/qwen3.5-9b",
        base_url="http://10.14.140.79:1234/v1",
        api_key="lm-studio",
        temperature=0.2,
    )
```

**Current defaults:**
- **Model**: `openai/qwen/qwen3.5-9b` (served via LM Studio)
- **Base URL**: `http://10.14.140.79:1234/v1` (override with `LM_STUDIO_URL` env var)
- **API Key**: `lm-studio` (required by LM Studio's OpenAI-compatible endpoint)
- **Temperature**: 0.2

**To use a different LLM provider**, replace the `load_llm()` function with one of the following patterns:

### OpenAI
```python
def load_llm() -> LLM:
    return LLM(
        model="gpt-4o",
        api_key=os.environ.get("OPENAI_API_KEY"),
        temperature=0.3,
    )
```
Requires: `OPENAI_API_KEY` environment variable.

### Anthropic
```python
def load_llm() -> LLM:
    return LLM(
        model="claude-3-5-sonnet-20241022",
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        temperature=0.3,
    )
```
Requires: `ANTHROPIC_API_KEY` environment variable.

### Google Gemini
```python
def load_llm() -> LLM:
    return LLM(
        model="gemini-1.5-pro",
        api_key=os.environ.get("GOOGLE_API_KEY"),
        temperature=0.3,
    )
```
Requires: `GOOGLE_API_KEY` environment variable.

### Azure OpenAI
```python
def load_llm() -> LLM:
    return LLM(
        model="gpt-4o",
        api_key=os.environ.get("AZURE_OPENAI_API_KEY"),
        base_url=os.environ.get("AZURE_OPENAI_ENDPOINT"),
        temperature=0.3,
    )
```
Requires: `AZURE_OPENAI_API_KEY` and `AZURE_OPENAI_ENDPOINT` environment variables.

## Environment Variables

Set the following environment variables before running the application:

- `LM_STUDIO_URL`: LM Studio server URL (default: `http://10.14.140.79:1234/v1`)
- `OPENAI_API_KEY`: OpenAI API key (if using OpenAI)
- `ANTHROPIC_API_KEY`: Anthropic API key (if using Anthropic)
- `GOOGLE_API_KEY`: Google Gemini API key (if using Gemini)
- `AZURE_OPENAI_API_KEY`: Azure OpenAI API key (if using Azure)
- `AZURE_OPENAI_ENDPOINT`: Azure OpenAI endpoint URL (if using Azure)
- `TAVILY_API_KEY`: Tavily Search API key (used in `src/main.py:20` for market validation research)
- `VALIDATION_TIMEOUT_SECS`: Max seconds for validation phase (default: 600)
- `REGULATORY_TIMEOUT_SECS`: Max seconds for regulatory phase (default: 300)

**Note**: The Tavily API key is currently hardcoded in `src/main.py:20` for development. For production, move it to an environment variable.

## Output Files

Each phase saves its JSON output to the `outputs/` directory:
- `preeval_output.json` — Structured problem definition
- `validation_output.json` — Market validation results
- `regulatory_output.json` — Regulatory mapping results
- `ethics_output.json` — Ethics gate verdicts
- `tipsc_output.json` — TIPSC scores (after initial evaluation)
- `tipsc_output_final.json` — TIPSC scores (after follow-up loop completes)

## Key Design Decisions

- **JSON-only LLM responses**: All agents are prompted to output strict JSON, parsed with Pydantic validation and auto-correction retries.
- **No ReAct loops for formatting**: Research and formatting are separate steps — tools used only in research, then a clean formatting pass.
- **Auto-correction logic**: Pydantic model validators enforce aggregation rules (e.g., `ethics_pass` derived from gate scores, `overall_readiness` from TIPSC scores).
- **Compliance-aware S-dimension**: TIPSC's Solvable (S) score accounts for regulatory compliance capability gaps when `compliance_context` is present.
- **Follow-up budget**: Maximum 3 follow-up questions, each targeting the most impactful unresolved evidence gap.