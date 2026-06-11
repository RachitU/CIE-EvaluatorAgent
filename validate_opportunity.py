"""
Startup Idea Validation System
================================

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STAGE 1 — Problem Definition Agent
  Coaches students to define:
    · Problem Statement
    · Customer
    · Consequence
    · Assumptions
  Hard cap: 4 turns, then outputs whatever it has.

STAGE 2 — TIPS Evaluation Agent
  Traffic-light scoring on:
    · T — Timely
    · I — Important
    · P — Profitable
    · S — Solvable
  (C — Contextual excluded at this stage)

STAGE 3 — Idea Check Agent
  Quick sanity check on proposed solution.
  Flags obvious red flags; approves sensible ideas.

OUTPUT — Structured JSON ready for DFV analysis
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import sys
import json
import re
import requests
import yaml
from datetime import datetime
from pathlib import Path
from typing import List

from pydantic import BaseModel
from crewai import Agent, Task, Crew, Process, LLM
from crewai.flow.flow import Flow, listen, start


# ══════════════════════════════════════════════════════════════
# LOAD CONFIGURATION & PROMPTS
# ══════════════════════════════════════════════════════════════

os.environ["OPENAI_API_KEY"] = "lm-studio"
os.environ["OPENAI_BASE_URL"] = "http://localhost:1234/v1"
os.environ["SERPER_API_KEY"] = "8b1f23fdd635ed883fc9c7a2e9a0b3ff48cb2650"

BASE_DIR = Path(__file__).parent


def _load_yaml(rel_path: str) -> dict:
    with open(BASE_DIR / rel_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


cfg      = _load_yaml("config/settings.yaml")
prob_p   = _load_yaml("prompts/problem_agent.yaml")
opp_p    = _load_yaml("prompts/opportunity_agent.yaml")
idea_p   = _load_yaml("prompts/idea_agent.yaml")
ui       = _load_yaml("prompts/ui_strings.yaml")

_val     = cfg["validation"]
_search  = cfg["search"]
_display = cfg["display"]

MAX_TURNS_PER_CRITERION = _val["max_turns_per_criterion"]
MAX_PROBLEM_TURNS       = 4   # Hard cap for Stage 1
W                       = _display["console_width"]


# ══════════════════════════════════════════════════════════════
# LLM CONFIGURATION
# ══════════════════════════════════════════════════════════════

llm_cfg = cfg["llm"]
llm = LLM(
    model=llm_cfg["model"],
    base_url=llm_cfg["base_url"],
    api_key=llm_cfg["api_key"],
)

_serper_key = (
    os.environ.get("SERPER_API_KEY")
    or _search.get("serper_api_key", "")
)
_search_on = bool(_serper_key)

if _search_on:
    print(ui["startup"]["search_enabled"])
else:
    print(ui["startup"]["search_disabled"])


# ══════════════════════════════════════════════════════════════
# STATE MODEL
# ══════════════════════════════════════════════════════════════

class ValidationState(BaseModel):
    # Stage 1 — Problem Definition
    raw_input:         str  = ""
    problem_statement: str  = ""
    customer:          str  = ""
    consequence:       str  = ""
    assumptions:       str  = ""
    prob_history:      List[dict] = []
    prob_status:       str  = "PENDING"   # PENDING → IN_PROGRESS → COMPLETE
    prob_turn:         int  = 0

    # Stage 2 — TIPS
    tips_history:      List[dict] = []
    tips_status:       str  = "PENDING"   # PENDING → IN_PROGRESS → COMPLETE
    tips_report:       str  = ""
    tips_last_q:       str  = ""
    tips_scores:       dict = {}          # {"T": "GREEN", "I": "YELLOW", ...}
    criterion_turns:   dict = {}
    current_criterion: str  = ""

    # Stage 3 — Idea Check
    solution:          str  = ""
    idea_history:      List[dict] = []
    idea_status:       str  = "PENDING"   # PENDING → IN_PROGRESS → APPROVED
    idea_report:       str  = ""
    idea_last_q:       str  = ""
    tips_explanations: dict = {}
    idea_verdict: str = ""
    idea_notes: str = ""

# ══════════════════════════════════════════════════════════════
# CLI UTILITIES
# ══════════════════════════════════════════
def parse_tips_explanations(text: str):
    explanations = {}

    patterns = {
    "T": r"T\s*[—\-]\s*Timely\s*:\s*.*?(GREEN|YELLOW|RED)",
    "I": r"I\s*[—\-]\s*Important\s*:\s*.*?(GREEN|YELLOW|RED)",
    "P": r"P\s*[—\-]\s*Profitable\s*:\s*.*?(GREEN|YELLOW|RED)",
    "S": r"S\s*[—\-]\s*Solvable\s*:\s*.*?(GREEN|YELLOW|RED)",
}

    for k, pattern in patterns.items():
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            explanations[k] = m.group(1).strip()

    return explanations

def hr(char: str = "=") -> None:
    print(char * W)


def section(title: str) -> None:
    print()
    hr()
    pad = max(0, (W - len(title) - 2) // 2)
    print(" " * pad + title)
    hr()
    print()


def _wrap(text: str, width: int = W - 4) -> List[str]:
    words, lines, cur = text.split(), [], ""
    for word in words:
        if len(cur) + len(word) + 1 <= width:
            cur = (cur + " " + word).strip()
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def agent_says(text: str) -> None:
    question = extract_displayed_question(text)
    label    = ui["agent_display"]["asking_label"]
    print()
    hr("-")
    print(text.strip())
    hr("-")
    print()
    print("=" * W)
    print(f"  {label}")
    print("=" * W)
    for line in _wrap(question):
        print(f"  {line}")
    print("=" * W)
    print()


def ask(prompt: str = "") -> str:
    if prompt:
        print(f"\n  {prompt}")
    response = input("\n  > ").strip()
    print()
    return response


def extract_displayed_question(text: str) -> str:
    if "QUESTION:" in text.upper():
        idx   = text.upper().rfind("QUESTION:")
        chunk = text[idx + len("QUESTION:"):].strip()
        stop_markers = [
            "GOOD ANSWER EXAMPLE:", "COACHING TIP:", "WHAT HELPS:",
            "VERDICT:", "STATUS:", "SUGGESTED FIX:",
        ]
        end = len(chunk)
        for marker in stop_markers:
            pos = chunk.upper().find(marker)
            if pos != -1:
                end = min(end, pos)
        q = chunk[:end].strip()
        if q:
            return q
    for line in reversed(text.split("\n")):
        line = line.strip()
        if line.endswith("?") and len(line) > 5:
            return line
    idx = text.rfind("?")
    if idx != -1:
        start   = max(0, idx - 120)
        snippet = text[start:idx + 1].strip()
        for sep in (".", "\n"):
            parts = snippet.rsplit(sep, 1)
            if len(parts) == 2 and len(parts[1].strip()) > 5:
                snippet = parts[1].strip()
                break
        if len(snippet) > 5:
            return snippet
    return ui["agent_display"]["fallback_question"]


# ══════════════════════════════════════════════════════════════
# AGENT OUTPUT HELPERS
# ══════════════════════════════════════════════════════════════

_RESPONSE_MARKERS = [
    "FIELDS IDENTIFIED:",
    "PROBLEM DEFINITION:",
    "STATUS: DEFINITION_COMPLETE",
    "STATUS: NEEDS_MORE_INFO",
    "TIPS EVALUATION:",
    "SEARCH FINDINGS:",
    "FEEDBACK:",
    "IDEA CHECK:",
    "VERDICT: APPROVED",
    "VERDICT: NEEDS_CLARITY",
    "VERDICT: FLAG",
    "STATUS: TIPS_COMPLETE",
]


def strip_markdown(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*',     r'\1', text)
    text = re.sub(r'`(.+?)`',       r'\1', text)
    return text


def clean_agent_output(text: str) -> str:
    text       = strip_markdown(text)
    text_upper = text.upper()
    earliest   = len(text)
    for marker in _RESPONSE_MARKERS:
        idx = text_upper.find(marker.upper())
        if 0 <= idx < earliest:
            earliest = idx
    if earliest < len(text):
        return text[earliest:].strip()
    if "FINAL ANSWER:" in text_upper:
        idx = text_upper.find("FINAL ANSWER:")
        return text[idx + len("FINAL ANSWER:"):].strip()
    return text.strip()


def run_agent(task: Task, agent: Agent) -> str:
    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
        memory=False,
    )
    raw = str(crew.kickoff())
    return clean_agent_output(raw)


def extract_question(text: str) -> str:
    if "QUESTION:" in text.upper():
        idx = text.upper().find("QUESTION:")
        return text[idx + len("QUESTION:"):].strip().split("\n")[0].strip()
    return text.strip()[:200]


def repetition_warning(last_q: str, current_q: str) -> str:
    if not last_q or last_q == "(none)":
        return ""
    lq_words = set(last_q.lower().split())
    cq_words = set(current_q.lower().split())
    if not lq_words:
        return ""
    overlap = len(lq_words & cq_words) / len(lq_words)
    if overlap > 0.7:
        return ui["repetition_warning"]
    return ""


def force_close_instruction(criterion: str, turns: int) -> str:
    if turns < MAX_TURNS_PER_CRITERION:
        return ""
    _names = {"T": "Timely", "I": "Important", "P": "Profitable", "S": "Solvable"}
    name = _names.get(criterion, criterion)
    return ui["force_close_template"].format(
        turns=turns, name=name, criterion=criterion
    )


def last_exchange(history: List[dict]) -> tuple:
    last_q, last_a = "(none)", "(none)"
    for turn in reversed(history):
        if turn["role"] == "user" and last_a == "(none)":
            last_a = turn["content"]
        elif turn["role"] == "agent" and last_q == "(none)" and last_a != "(none)":
            content = turn["content"]
            if "QUESTION:" in content.upper():
                q_start = content.upper().find("QUESTION:")
                last_q  = content[q_start + len("QUESTION:"):].strip().split("\n")[0].strip()
            else:
                last_q = content
            break
    return last_q, last_a


def format_history(history: List[dict], max_pairs: int = 2) -> str:
    if not history:
        return "(No prior conversation.)"
    qa_pairs = []
    i = len(history) - 1
    while i >= 0 and len(qa_pairs) < max_pairs:
        if history[i]["role"] == "user":
            answer = history[i]["content"]
            for j in range(i - 1, -1, -1):
                if history[j]["role"] == "agent":
                    q_text = history[j]["content"]
                    if "QUESTION:" in q_text.upper():
                        q_start  = q_text.upper().find("QUESTION:")
                        question = q_text[q_start + len("QUESTION:"):].strip().split("\n")[0].strip()
                    else:
                        question = q_text.strip()[:200]
                    qa_pairs.insert(0, f"Q: {question}\nA: {answer}")
                    i = j - 1
                    break
            else:
                i -= 1
        else:
            i -= 1
    return "\n\n".join(qa_pairs) if qa_pairs else "(No prior conversation.)"


# ══════════════════════════════════════════════════════════════
# STATUS CHECKERS
# ══════════════════════════════════════════════════════════════

def prob_complete(text: str) -> bool:
    return "STATUS: DEFINITION_COMPLETE" in text.upper()


def tips_complete(text: str) -> bool:
    return "STATUS: TIPS_COMPLETE" in text.upper()


def idea_approved(text: str) -> bool:
    return "VERDICT: APPROVED" in text.upper()


def idea_needs_clarity(text: str) -> bool:
    return "VERDICT: NEEDS_CLARITY" in text.upper()


def parse_criterion(text: str) -> str:
    marker = "CRITERION IN FOCUS:"
    if marker not in text.upper():
        return ""
    idx  = text.upper().find(marker)
    line = text[idx + len(marker):].strip().split("\n")[0].upper()
    for ch in line:
        if ch in "TIPS":
            return ch
    return ""

def parse_tips_scores(text: str) -> dict:
    scores = {}

    patterns = {
        "T": r"T\s*[—\-]\s*Timely\s*:\s*.*?(GREEN|YELLOW|RED)",
        "I": r"I\s*[—\-]\s*Important\s*:\s*.*?(GREEN|YELLOW|RED)",
        "P": r"P\s*[—\-]\s*Profitable\s*:\s*.*?(GREEN|YELLOW|RED)",
        "S": r"S\s*[—\-]\s*Solvable\s*:\s*.*?(GREEN|YELLOW|RED)",
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            scores[key] = match.group(1).upper()

    return scores
def format_tips_scores(scores: dict) -> str:
    if not scores:
        return "(not yet evaluated)"
    lights = ui["traffic_lights"]
    lines  = []
    names  = {"T": "Timely", "I": "Important", "P": "Profitable", "S": "Solvable"}
    for key in ["T", "I", "P", "S"]:
        if key in scores:
            label = lights.get(scores[key], scores[key])
            lines.append(f"  {key} — {names[key]}: {label}")
    return "\n".join(lines)


def extract_problem_fields(text: str) -> dict:
    """
    Parse the structured PROBLEM DEFINITION block from Stage 1 output.
    Returns a dict with keys: problem_statement, customer, consequence, assumptions.
    """
    fields = {
        "problem_statement": "",
        "customer":          "",
        "consequence":       "",
        "assumptions":       "",
    }
    patterns = {
        "problem_statement": r"Problem Statement\s*:\s*(.+)",
        "customer":          r"Customer\s*:\s*(.+)",
        "consequence":       r"Consequence\s*:\s*(.+)",
        "assumptions":       r"Assumptions\s*:\s*([\s\S]+?)(?=\n[A-Z]|\Z)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            fields[key] = match.group(1).strip()
    return fields


# ══════════════════════════════════════════════════════════════
# WEB SEARCH HELPERS
# ══════════════════════════════════════════════════════════════

def _serper_search(query: str) -> str:
    web_cfg = ui["web_context"]
    if not _search_on:
        return web_cfg["search_off"]
    try:
        r = requests.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY":    _serper_key,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": _search["results_per_query"]},
            timeout=_search["timeout_seconds"],
        )
        data  = r.json()
        lines = []
        ab      = data.get("answerBox", {})
        ab_text = ab.get("answer") or ab.get("snippet", "")
        if ab_text:
            lines.append(f"  [Direct answer] {ab_text.strip()}")
        for item in data.get("organic", [])[:_search["results_per_query"]]:
            title   = item.get("title", "").strip()
            snippet = item.get("snippet", "").strip()
            if title and snippet:
                lines.append(f"  • {title}: {snippet}")
        return "\n".join(lines) if lines else web_cfg["no_results"]
    except Exception as e:
        return web_cfg["error"].format(error=e)


def _fetch_web_context(queries: List[str], label: str) -> str:
    if not _search_on or not queries:
        return ""
    web_cfg  = ui["web_context"]
    date_str = datetime.now().strftime("%B %d, %Y")
    sep      = web_cfg["separator"]
    lines    = ["", sep, web_cfg["header"].format(label=label, date=date_str), sep]
    for q in queries:
        lines.append(f'\nQuery: "{q}"')
        lines.append(_serper_search(q))
    lines += ["", sep, web_cfg["footer"].strip(), sep, ""]
    return "\n".join(lines)


def tips_search_context(problem_statement: str) -> str:
    short = problem_statement[:60].rstrip()
    return _fetch_web_context(
        [f"{short} existing solutions market"],
        label="TIPS Evaluation",
    )


def criterion_search_context(criterion: str, problem_statement: str) -> str:
    short = problem_statement[:55].rstrip()
    queries_map = {
        "T": [f"{short} market trends urgency"],
        "I": [f"{short} user pain points importance"],
        "P": [
    f"{short} financial impact",
    f"{short} business value created"
],
        "S": [f"{short} technical feasibility student project"],
    }
    queries = queries_map.get(criterion, [f"{short} market opportunity"])
    _names  = {"T": "Timely", "I": "Important", "P": "Profitable", "S": "Solvable"}
    return _fetch_web_context(queries, label=f"Criterion: {_names.get(criterion, criterion)}")


def idea_search_context(problem_statement: str, solution: str) -> str:
    short_p = problem_statement[:50].rstrip()
    short_s = solution[:50].rstrip()
    return _fetch_web_context(
        [f"{short_p} {short_s} existing apps alternatives"],
        label="Idea Check",
    )


# ══════════════════════════════════════════════════════════════
# AGENTS
# ══════════════════════════════════════════════════════════════

problem_agent = Agent(
    role=prob_p["agent"]["role"],
    goal=prob_p["agent"]["goal"],
    backstory=prob_p["agent"]["backstory"],
    verbose=False,
    llm=llm,
)

tips_agent = Agent(
    role=opp_p["agent"]["role"],
    goal=opp_p["agent"]["goal"],
    backstory=opp_p["agent"]["backstory"],
    verbose=False,
    llm=llm,
)

idea_agent = Agent(
    role=idea_p["agent"]["role"],
    goal=idea_p["agent"]["goal"],
    backstory=idea_p["agent"]["backstory"],
    verbose=False,
    llm=llm,
)


# ══════════════════════════════════════════════════════════════
# JSON OUTPUT BUILDER
# ══════════════════════════════════════════════════════════════

def build_output_json(state: ValidationState, idea_notes: str) -> dict:
    """
    Assemble the final structured JSON that passes to DFV.
    Mirrors the structure defined by the course instructor.
    """
    _names  = {"T": "Timely", "I": "Important", "P": "Profitable", "S": "Solvable"}
    lights  = ui["traffic_lights"]

    tips_metrics = {}
    for key in ["T", "I", "P", "S"]:
        score = state.tips_scores.get(key, "UNKNOWN")
        label = lights.get(score, score)
        tips_metrics[_names[key].lower() + "_score"] = label

    # Extract TIPS narrative lines from the final TIPS report
    tips_narrative = {}
    narrative_keys = {
        "T": "timely_factor",
        "I": "importance_metric",
        "P": "profitability_pivot",
        "S": "solvability_constraint",
    }
    patterns = {
        "T": r"T\s*[—\-]\s*Timely\s*:\s*(?:GREEN|YELLOW|RED)\s*[—\-]\s*(.+)",
        "I": r"I\s*[—\-]\s*Important\s*:\s*(?:GREEN|YELLOW|RED)\s*[—\-]\s*(.+)",
        "P": r"P\s*[—\-]\s*Profitable\s*:\s*(?:GREEN|YELLOW|RED)\s*[—\-]\s*(.+)",
        "S": r"S\s*[—\-]\s*Solvable\s*:\s*(?:GREEN|YELLOW|RED)\s*[—\-]\s*(.+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, state.tips_report, re.IGNORECASE)
        tips_narrative[narrative_keys[key]] = match.group(1).strip() if match else ""

    output = {
        "refined_idea": {
            "customer_segment":    state.customer,
            "qualified_problem":   state.problem_statement,
            "consequence":         state.consequence,
            "assumptions":         state.assumptions,
            "proposed_solution":   state.solution,
        },
       "tips_validated_metrics" : {
    "timely_score": state.tips_scores.get("T","UNKNOWN"),
    "importance_score": state.tips_scores.get("I","UNKNOWN"),
    "profitability_score": state.tips_scores.get("P","UNKNOWN"),
    "solvability_score": state.tips_scores.get("S","UNKNOWN"),
},
        "idea_check": {
    "verdict": state.idea_verdict,
    "notes": state.idea_notes
}
    }
    return output


# ══════════════════════════════════════════════════════════════
# VALIDATION FLOW
# ══════════════════════════════════════════════════════════════

class ValidationFlow(Flow[ValidationState]):

    # ──────────────────────────────────────────────────────────
    # STAGE 1a — Collect initial input
    # ──────────────────────────────────────────────────────────

    @start()
    def collect_input(self):
        section(ui["section_titles"]["main"])
        print(ui["startup"]["intro"])
        hr()
        self.state.raw_input = ask(ui["prompts"]["problem_input"])
        return self.state.raw_input

    # ──────────────────────────────────────────────────────────
    # STAGE 1b — Problem Definition Loop (max 4 turns)
    # ──────────────────────────────────────────────────────────

    @listen(collect_input)
    def problem_definition_loop(self, raw_input):
        section(ui["phases"]["problem_header"])
        print(ui["phases"]["problem_running"])

        self.state.prob_turn = 1

        # First pass — agent analyses the initial input
        description = prob_p["tasks"]["initial"].format(
            student_input=raw_input,
            turn=self.state.prob_turn,
        )
        task = Task(
            description=description,
            expected_output=(
                "Plain text with these sections:\n"
                "FIELDS IDENTIFIED: (4 fields with current values)\n"
                "STATUS: NEEDS_MORE_INFO or DEFINITION_COMPLETE\n"
                "If NEEDS_MORE_INFO: QUESTION: (one question)\n"
                "If DEFINITION_COMPLETE: PROBLEM DEFINITION: (all 4 fields)\n"
                "No JSON. No markdown. No preamble."
            ),
            agent=problem_agent,
        )
        report = run_agent(task, problem_agent)
        self.state.prob_history.append({"role": "agent", "content": report})

        # Loop until complete or turn cap reached
        while not prob_complete(report) and self.state.prob_turn < MAX_PROBLEM_TURNS:

            agent_says(report)
            answer = ask(ui["prompts"]["response_input"])
            self.state.prob_history.append({"role": "user", "content": answer})
            self.state.prob_turn += 1

            last_q, last_a = last_exchange(self.state.prob_history)

            # Extract current field values from latest agent output
            current_fields = extract_problem_fields(report)

            description = prob_p["tasks"]["followup"].format(
                problem_statement = current_fields["problem_statement"] or "Not yet stated",
                customer          = current_fields["customer"]          or "Not yet stated",
                consequence       = current_fields["consequence"]       or "Not yet stated",
                assumptions       = current_fields["assumptions"]       or "Not yet stated",
                last_q            = last_q,
                last_a            = last_a,
                turn              = self.state.prob_turn,
            )
            task = Task(
                description=description,
                expected_output=(
                    "Plain text with these sections:\n"
                    "FIELDS IDENTIFIED: (4 fields updated)\n"
                    "STATUS: NEEDS_MORE_INFO or DEFINITION_COMPLETE\n"
                    "If NEEDS_MORE_INFO: QUESTION: (one question)\n"
                    "If DEFINITION_COMPLETE: PROBLEM DEFINITION: (all 4 fields)\n"
                    "No JSON. No markdown. No preamble."
                ),
                agent=problem_agent,
            )
            report = run_agent(task, problem_agent)
            self.state.prob_history.append({"role": "agent", "content": report})

        # If we hit the cap without completion, force the final output
        if not prob_complete(report):
            # Inject the turn-4 instruction into a final call
            final_input = (
                f"MANDATORY FINAL TURN — Turn 4 of 4.\n"
                f"Output DEFINITION_COMPLETE now using everything gathered.\n"
                f"Write 'Not specified' for any missing field.\n\n"
            ) + prob_p["tasks"]["followup"].format(
                problem_statement = extract_problem_fields(report)["problem_statement"] or "Not specified",
                customer          = extract_problem_fields(report)["customer"]          or "Not specified",
                consequence       = extract_problem_fields(report)["consequence"]       or "Not specified",
                assumptions       = extract_problem_fields(report)["assumptions"]       or "Not specified",
                last_q            = "(final turn — output the definition now)",
                last_a            = "(no more input from student)",
                turn              = 4,
            )
            task = Task(
                description=final_input,
                expected_output=(
                    "STATUS: DEFINITION_COMPLETE\n"
                    "PROBLEM DEFINITION: with all 4 fields.\n"
                    "No JSON. No markdown."
                ),
                agent=problem_agent,
            )
            report = run_agent(task, problem_agent)
            self.state.prob_history.append({"role": "agent", "content": report})

        # Parse and store the structured fields
        fields = extract_problem_fields(report)
        self.state.problem_statement = fields["problem_statement"]
        self.state.customer          = fields["customer"]
        self.state.consequence       = fields["consequence"]
        self.state.assumptions       = fields["assumptions"]
        self.state.prob_status       = "COMPLETE"

        # Show the completed definition
        section(ui["section_titles"]["prob_complete"])
        print(f"  Problem Statement : {self.state.problem_statement}")
        print(f"  Customer          : {self.state.customer}")
        print(f"  Consequence       : {self.state.consequence}")
        print(f"  Assumptions       : {self.state.assumptions}")
        print()
        hr("-")
        print(f"\n  {ui['transitions']['problem_complete']}\n")
        hr("-")

        return self.state.problem_statement

    # ──────────────────────────────────────────────────────────
    # STAGE 2a — TIPS Triage (first pass)
    # ──────────────────────────────────────────────────────────

    @listen(problem_definition_loop)
    def tips_triage(self, problem_statement):
        section(ui["phases"]["tips_header"])
        print(ui["phases"]["tips_running"])

        description = opp_p["tasks"]["triage"].format(
            problem_statement = self.state.problem_statement,
            customer          = self.state.customer,
            consequence       = self.state.consequence,
            assumptions       = self.state.assumptions,
            search_context    = tips_search_context(self.state.problem_statement),
        )
        task = Task(
            description=description,
            expected_output=(
                "Plain text with these sections:\n"
                "SEARCH FINDINGS:\n"
                "TIPS EVALUATION: (T/I/P/S each rated GREEN/YELLOW/RED)\n"
                "AGENDA:\n"
                "STATUS: NEEDS_MORE_INFO or TIPS_COMPLETE\n"
                "If NEEDS_MORE_INFO: CRITERION IN FOCUS: then QUESTION:\n"
                "No JSON. No markdown. No preamble."
            ),
            agent=tips_agent,
        )
        result = run_agent(task, tips_agent)

        # Guard: retry if TIPS EVALUATION section is missing
        if "TIPS EVALUATION:" not in result.upper():
            correction = (
                "ERROR: Your response must include a TIPS EVALUATION section.\n"
                "Output TIPS EVALUATION: with all four criteria rated GREEN/YELLOW/RED.\n"
                "Repeat the full task now.\n\n"
            )
            task_retry = Task(
                description=correction + description,
                expected_output=task.expected_output,
                agent=tips_agent,
            )
            result = run_agent(task_retry, tips_agent)

        # Parse and store initial scores
        scores = parse_tips_scores(result)
        self.state.tips_scores.update(scores)

        print()
        hr("-")
        print(result.strip())
        hr("-")
        print()

        self.state.tips_history.append({"role": "agent", "content": result})
        self.state.tips_report = result
        self.state.tips_status = "IN_PROGRESS"

        return result

    # ──────────────────────────────────────────────────────────
    # STAGE 2b — TIPS Deep-Dive Loop
    # ──────────────────────────────────────────────────────────

    @listen(tips_triage)
    def tips_loop(self, triage_result):
        report = triage_result

        while not tips_complete(report):

            agent_says(report)

            # Track criterion turns for force-close
            criterion = parse_criterion(report)
            if criterion:
                self.state.current_criterion = criterion
                self.state.criterion_turns[criterion] = (
                    self.state.criterion_turns.get(criterion, 0) + 1
                )
            turns_on_current = self.state.criterion_turns.get(
                self.state.current_criterion, 0
            )

            answer = ask(ui["prompts"]["response_input"])
            self.state.tips_history.append({"role": "user", "content": answer})

            last_q, last_a = last_exchange(self.state.tips_history)
            rep_warning    = repetition_warning(self.state.tips_last_q, last_q)
            force_close    = force_close_instruction(self.state.current_criterion, turns_on_current)
            srch_context   = criterion_search_context(
                self.state.current_criterion, self.state.problem_statement
            )
            history_text   = format_history(self.state.tips_history)

            description = opp_p["tasks"]["followup"].format(
                rep_warning       = rep_warning,
                force_close       = force_close,
                search_context    = srch_context,
                last_q            = last_q,
                last_a            = last_a,
                current_scores    = format_tips_scores(self.state.tips_scores),
                problem_statement = self.state.problem_statement,
                customer          = self.state.customer,
                consequence       = self.state.consequence,
                assumptions       = self.state.assumptions,
                history           = history_text,
            )
            task = Task(
                description=description,
                expected_output=(
                    "Plain text with these sections:\n"
                    "SEARCH FINDINGS:\n"
                    "FEEDBACK:\n"
                    "TIPS EVALUATION: (all four criteria updated)\n"
                    "STATUS: NEEDS_MORE_INFO or TIPS_COMPLETE\n"
                    "If NEEDS_MORE_INFO: CRITERION IN FOCUS: then QUESTION:\n"
                    "No JSON. No markdown. No preamble."
                ),
                agent=tips_agent,
            )
            report = run_agent(task, tips_agent)

            # Update scores from latest response
            new_scores = parse_tips_scores(report)
            self.state.tips_explanations.update(
    parse_tips_explanations(report)
)
            self.state.tips_scores.update(new_scores)
            self.state.tips_last_q = extract_question(report)
            self.state.tips_history.append({"role": "agent", "content": report})
            self.state.tips_report = report

        self.state.tips_status = "COMPLETE"

        section(ui["section_titles"]["tips_complete"])
        print(report.strip())
        print()
        print("\nDEBUG TIPS STATE:")
        print(self.state.tips_scores)
        print(f"  SCORES:\n{format_tips_scores(self.state.tips_scores)}")
        print()
        hr("-")
        print(f"\n  {ui['transitions']['tips_complete']}\n")
        hr("-")

        self.state.solution = ask(ui["prompts"]["solution_input"])
        return self.state.solution

    # ──────────────────────────────────────────────────────────
    # STAGE 3 — Idea Check
    # ──────────────────────────────────────────────────────────

    @listen(tips_loop)
    def idea_check(self, solution):
        section(ui["phases"]["idea_header"])
        print(ui["phases"]["idea_running"])

        description = idea_p["tasks"]["evaluate"].format(
            problem_statement = self.state.problem_statement,
            customer          = self.state.customer,
            consequence       = self.state.consequence,
            assumptions       = self.state.assumptions,
            tips_scores       = format_tips_scores(self.state.tips_scores),
            solution          = solution,
            search_context    = idea_search_context(
                self.state.problem_statement, solution
            ),
        )
        task = Task(
            description=description,
            expected_output=(
                "Plain text with these sections:\n"
                "IDEA CHECK: (Addresses Problem / Obviously Broken / Substance)\n"
                "VERDICT: APPROVED or NEEDS_CLARITY or FLAG\n"
                "If APPROVED: IDEA NOTES:\n"
                "If NEEDS_CLARITY: QUESTION:\n"
                "If FLAG: ISSUE: then SUGGESTED FIX:\n"
                "No JSON. No markdown. No preamble."
            ),
            agent=idea_agent,
        )
        result = run_agent(task, idea_agent)
        self.state.idea_history.append({"role": "agent", "content": result})
        self.state.idea_report = result

        # One clarification allowed if too vague
        if idea_needs_clarity(result):
            agent_says(result)
            answer = ask(ui["prompts"]["response_input"])
            self.state.idea_history.append({"role": "user", "content": answer})

            last_q, last_a = last_exchange(self.state.idea_history)
            description = idea_p["tasks"]["clarify"].format(
                problem_statement = self.state.problem_statement,
                customer          = self.state.customer,
                consequence       = self.state.consequence,
                solution          = solution,
                last_q            = last_q,
                last_a            = last_a,
            )
            task = Task(
                description=description,
                expected_output=(
                    "VERDICT: APPROVED or FLAG\n"
                    "If APPROVED: IDEA NOTES:\n"
                    "If FLAG: ISSUE: then SUGGESTED FIX:\n"
                    "No JSON. No markdown. No preamble."
                ),
                agent=idea_agent,
            )
            result = run_agent(task, idea_agent)
            self.state.idea_history.append({"role": "agent", "content": result})
            self.state.idea_report = result

        self.state.idea_status = "APPROVED" if idea_approved(result) else "FLAGGED"
        self.state.idea_verdict = (
    "APPROVED"
    if idea_approved(result)
    else "FLAG"
)

        self.state.idea_notes = result

        return result

    # ──────────────────────────────────────────────────────────
    # FINAL REPORT — Assemble and print structured JSON output
    # ──────────────────────────────────────────────────────────

    @listen(idea_check)
    def final_report(self, idea_result):
        labels = ui["report_labels"]

        # Extract idea notes from result
        idea_notes = ""
        if "IDEA NOTES:" in idea_result.upper():
            idx        = idea_result.upper().find("IDEA NOTES:")
            idea_notes = idea_result[idx + len("IDEA NOTES:"):].strip().split("\n\n")[0].strip()

        # Build the final JSON
        output_json = build_output_json(self.state, idea_notes)

        section(ui["section_titles"]["final"])

        hr("-")
        print(f"  {labels['problem']}")
        hr("-")
        print(f"  Statement  : {self.state.problem_statement}")
        print(f"  Customer   : {self.state.customer}")
        print(f"  Consequence: {self.state.consequence}")
        print(f"  Assumptions: {self.state.assumptions}")
        print()

        hr("-")
        print(f"  {labels['tips']}")
        hr("-")
        print(format_tips_scores(self.state.tips_scores))
        print()

        hr("-")
        print(f"  {labels['solution']}")
        hr("-")
        print(f"  {self.state.solution}")
        print()

        hr("-")
        print(f"  {labels['idea']}")
        hr("-")
        print(idea_result.strip())
        print()

        hr("-")
        print(f"  {labels['json_output']}")
        hr("-")
        print(json.dumps(output_json, indent=2))
        print()

        section(ui["section_titles"]["final"])
        print(f"  {ui['transitions']['final_status']}\n")
        hr()

        return output_json


# ══════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        flow = ValidationFlow()
        flow.kickoff()
    except KeyboardInterrupt:
        print("\n\n  Session interrupted. Exiting.\n")
        sys.exit(0)