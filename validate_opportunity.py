"""
Entrepreneurial Opportunity Validation System
=============================================

A two-agent AI system that validates entrepreneurial opportunities
before the founder commits to building a solution.

Workflow:
  Problem Input
    → Opportunity Evaluation (TIPSC → Need)
    → [APPROVED] → Solution Input
    → Idea Evaluation (PSEA + Feasibility)
    → [READY_FOR_DFV]
"""

import os
import sys
import logging
import warnings
from typing import Optional, List, Dict
from pathlib import Path

# Completely disable CrewAI Telemetry and OpenTelemetry tracing
# This prevents the script from hanging on Ctrl+C trying to reach telemetry.crewai.com
os.environ["CREWAI_TRACING_ENABLED"] = "0"
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["CREWAI_TELEMETRY_OPT_OUT"] = "true"


import requests
import yaml
from datetime import datetime
from typing import List
from pydantic import BaseModel
from crewai import Agent, Task, Crew, Process, LLM
from crewai.flow.flow import Flow, listen, start, router


# ══════════════════════════════════════════════════════════════
# LOAD CONFIGURATION & PROMPTS
# ══════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent


def _load_yaml(rel_path: str) -> dict:
    """Load and return a YAML file relative to the project root."""
    with open(BASE_DIR / rel_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


cfg      = _load_yaml("config/settings.yaml")
opp_p    = _load_yaml("prompts/opportunity_agent.yaml")
idea_p   = _load_yaml("prompts/idea_agent.yaml")
ui       = _load_yaml("prompts/ui_strings.yaml")

# ── Convenience accessors ──
_val     = cfg["validation"]
_search  = cfg["search"]
_display = cfg["display"]
_memory  = cfg.get("memory", {})
_conv    = cfg.get("conversation", {})

MAX_TURNS_PER_CRITERION = _val["max_turns_per_criterion"]
MAX_HISTORY_TURNS       = _conv.get("max_history_turns", 2)
W                       = _display["console_width"]


# ══════════════════════════════════════════════════════════════
# CONFIGURATION — LLM & SEARCH
# ══════════════════════════════════════════════════════════════

llm_cfg = cfg["llm"]
llm = LLM(
    model=llm_cfg["model"],
    base_url=llm_cfg["base_url"],
    api_key=llm_cfg["api_key"],
)

# CrewAI / chromadb may read these when memory is enabled.
os.environ.setdefault("OPENAI_API_KEY", llm_cfg["api_key"])
os.environ.setdefault("OPENAI_BASE_URL", llm_cfg["base_url"])

# Web search is pre-fetched in Python — agents do not call tools.
agent_tools = []
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
    problem:  str = ""
    solution: str = ""

    # Separate conversation histories per agent
    opp_history:  List[dict] = []   # {"role": "agent"|"user", "content": str}
    idea_history: List[dict] = []

    # Opportunity evaluation state
    opp_status:        str  = "PENDING"   # PENDING → IN_PROGRESS → APPROVED
    opp_report:        str  = ""
    opp_last_q:        str  = ""          # Tracks last question to detect repetition
    criterion_turns:   dict = {}          # {"I": 3, "P": 1, ...} turns spent per criterion
    current_criterion: str  = ""          # Letter currently under investigation

    # Idea evaluation state
    idea_status:       str  = "PENDING"   # PENDING → IN_PROGRESS → READY_FOR_DFV
    idea_report:       str  = ""
    idea_last_q:       str  = ""          # Tracks last question to detect repetition
    idea_issue_turns:  dict = {}          # {"P": 2, "S": 1, ...} turns spent per PSEA issue
    current_idea_issue: str = ""          # Letter currently under investigation


# ══════════════════════════════════════════════════════════════
# CLI UTILITIES
# ══════════════════════════════════════════════════════════════

def hr(char: str = "=") -> None:
    """Print a horizontal rule."""
    print(char * W)


def section(title: str) -> None:
    """Print a bold section header."""
    print()
    hr()
    pad = max(0, (W - len(title) - 2) // 2)
    print(" " * pad + title)
    hr()
    print()


def extract_question(text: str, for_display: bool = False) -> str:
    """
    Extract the QUESTION: value from an agent response.

    When for_display=True, truncates at coaching markers and falls back
    to the last '?' line for cleaner UI display.
    """
    if "QUESTION:" in text.upper():
        idx = text.upper().rfind("QUESTION:")
        chunk = text[idx + len("QUESTION:"):].strip()

        if for_display:
            end = len(chunk)
            for marker in ("GOOD ANSWER EXAMPLE:", "WHAT HELPS:", "VERDICT:", "STATUS:"):
                pos = chunk.upper().find(marker)
                if pos != -1:
                    end = min(end, pos)
            q = chunk[:end].strip()
            if len(q) > 5:
                return q
        else:
            return chunk.split("\n")[0].strip()

    if for_display:
        for line in reversed(text.strip().split("\n")):
            line = line.strip()
            if line.endswith("?") and len(line) > 10:
                return line
        return ui["agent_display"]["fallback_question"]

    return text.strip()[:200]


def _wrap(text: str, width: int = W - 4) -> List[str]:
    """Word-wrap a string to a given width and return a list of lines."""
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
    """
    Print the agent's response then always render the question in its own
    clearly labelled block — so the user never has to hunt for what to answer.

    Works regardless of whether the model followed the structured template.
    """
    question = extract_question(text, for_display=True)
    label    = ui["agent_display"]["asking_label"]

    # Full agent response
    print()
    hr("-")
    print(text.strip())
    hr("-")

    # Question callout — always shown below
    print()
    print("=" * W)
    print(f"  {label}")
    print("=" * W)
    for line in _wrap(question):
        print(f"  {line}")
    print("=" * W)
    print()


def ask(prompt: str = "") -> str:
    """Prompt the user for input. Typing 'quit' or 'exit' ends the session."""
    if prompt:
        print(f"\n  {prompt}")
    response = input("\n  > ").strip()
    if response.lower() in ("quit", "exit", "q"):
        print("\n  Session ended by user.\n")
        sys.exit(0)
    print()
    return response


def format_history(history: List[dict]) -> str:
    """Build a minimal context block: resolved verdicts + last N Q&A pairs."""
    if not history:
        return "(No prior conversation.)"

    # 1. Scrape resolved verdicts from all agent turns
    verdict_lines = []
    for turn in history:
        if turn["role"] != "agent":
            continue
        for line in turn["content"].split("\n"):
            l = line.strip()
            if any(l.startswith(f"{c} —") or l.startswith(f"{c}:") for c in "TIPSCN"):
                if any(w in l.upper() for w in ("STRONG", "WEAK", "ACCEPTED", "UNCLEAR", "CONFIRMED", "RESOLVED")):
                    verdict_lines.append(f"  {l}")

    # 2. Keep only the last N Q&A pairs (configurable via settings.yaml)
    qa_pairs = []
    i = len(history) - 1
    while i >= 0 and len(qa_pairs) < MAX_HISTORY_TURNS:
        if history[i]["role"] == "user":
            answer = history[i]["content"]
            for j in range(i - 1, -1, -1):
                if history[j]["role"] == "agent":
                    question = extract_question(history[j]["content"])
                    qa_pairs.insert(0, f"Q: {question}\nA: {answer}")
                    i = j - 1
                    break
            else:
                i -= 1
        else:
            i -= 1

    # 3. Assemble
    parts = []
    if verdict_lines:
        seen = list(dict.fromkeys(verdict_lines))
        parts.append("RESOLVED SO FAR:\n" + "\n".join(seen))
    if qa_pairs:
        parts.append("RECENT EXCHANGES:\n" + "\n\n".join(qa_pairs))

    return "\n\n".join(parts) if parts else "(No prior conversation.)"


import re

def _strip_react_noise(text: str) -> str:
    """Remove ReAct OBSERVATION: traces and <think> tags from reasoning models."""
    if "OBSERVATION:" in text:
        text = text.split("OBSERVATION:")[0]
    
    # Strip <think>...</think> blocks from reasoning models
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    
    return text.strip()


def run_agent(task: Task, agent: Agent) -> str:
    """Run a single-agent crew and return a clean plain-text string."""
    memory_on = _memory.get("enabled", False)
    crew_kwargs: dict = {
        "agents": [agent],
        "tasks": [task],
        "process": Process.sequential,
        "verbose": False,
        "memory": memory_on,
    }
    if memory_on:
        crew_kwargs["embedder"] = {
            "provider": "sentence-transformer",
            "config": {
                "model": _memory.get("embedder_model", "all-MiniLM-L6-v2"),
            },
        }
    crew = Crew(**crew_kwargs)
    raw = str(crew.kickoff())
    return clean_agent_output(_strip_react_noise(raw))


# Section headers that always mark the START of the agent's formatted response.
# Everything in the string before the first matching header is noise
# (Serper JSON, chain-of-thought, tool call traces) and gets discarded.
_RESPONSE_MARKERS = [
    "TIPSC TRIAGE:",
    "SEARCH FINDINGS:",
    "PSEA EVALUATION:",
    "FEEDBACK:",
    "STATUS: APPROVED",
    "STATUS: NEEDS_MORE_INFO",
    "VERDICT: READY_FOR_DFV",
    "VERDICT: NEEDS_REFINEMENT",
]


def clean_agent_output(text: str) -> str:
    """
    Strip tool-call noise from a raw CrewAI agent output string.

    Strategy:
      1. Scan for the earliest occurrence of any known structured section header.
      2. Return the text from that point onward (the actual formatted response).
      3. Fallback A: if the model used ReAct format, extract after 'Final Answer:'.
      4. Fallback B: return the raw text stripped of leading/trailing whitespace.

    This is deliberately defensive — if none of the heuristics match we still
    return something rather than crashing.
    """
    text_upper = text.upper()
    earliest   = len(text)

    for marker in _RESPONSE_MARKERS:
        idx = text_upper.find(marker.upper())
        if 0 <= idx < earliest:
            earliest = idx

    if earliest < len(text):
        return text[earliest:].strip()

    # Fallback A — ReAct agents sometimes emit "Final Answer: ..."
    if "FINAL ANSWER:" in text_upper:
        idx = text_upper.find("FINAL ANSWER:")
        return text[idx + len("FINAL ANSWER:"):].strip()

    # Fallback B — nothing matched; return stripped raw text
    return text.strip()


def last_exchange(history: List[dict]) -> tuple[str, str]:
    """
    Extract the most recent (agent_question, user_answer) pair from history.
    Returns ("(none)", "(none)") if there isn't a complete pair yet.
    """
    last_q = "(none)"
    last_a = "(none)"
    for turn in reversed(history):
        if turn["role"] == "user" and last_a == "(none)":
            last_a = turn["content"]
        elif turn["role"] == "agent" and last_q == "(none)" and last_a != "(none)":
            last_q = extract_question(turn["content"])
            break
    return last_q, last_a


def repetition_warning(last_q: str, current_q: str) -> str:
    """
    If the new question is suspiciously similar to the last one, return an
    injected warning to include in the next prompt. Otherwise empty string.
    Similarity is measured by word overlap.
    """
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


def opp_approved(text: str) -> bool:
    """Case-insensitive check for opportunity approval signal."""
    return "STATUS: APPROVED" in text.upper()


def idea_ready(text: str) -> bool:
    """Case-insensitive check for idea readiness signal."""
    return "VERDICT: READY_FOR_DFV" in text.upper()


def parse_criterion(text: str) -> str:
    """
    Extract the single-letter criterion code from a CRITERION IN FOCUS: line.
    Returns one of T, I, P, S, C, or N (Need), or "" if not found.
    """
    marker = "CRITERION IN FOCUS:"
    if marker not in text.upper():
        return ""
    idx  = text.upper().find(marker)
    line = text[idx + len(marker):].strip().split("\n")[0].upper()
    for ch in line:
        if ch in "TIPSCN":
            return ch
    return ""


_CRITERION_NAMES = {
    "T": "Timely", "I": "Important", "P": "Profitable",
    "S": "Solvable", "C": "Contextual", "N": "Need Validation",
}

_PSEA_ISSUE_NAMES = {
    "P": "Problem-Solution Fit", "S": "Simplicity",
    "E": "Ethics", "A": "Assumptions",
}


def force_close_instruction(criterion: str, turns: int) -> str:
    """
    Return an injected instruction block when a criterion has been probed
    too many times without being closed.  Empty string otherwise.
    """
    if turns < MAX_TURNS_PER_CRITERION:
        return ""
    name     = _CRITERION_NAMES.get(criterion, criterion)
    template = ui["force_close_template"]
    return template.format(turns=turns, name=name, criterion=criterion)


# ══════════════════════════════════════════════════════════════
# WEB SEARCH HELPERS
# ══════════════════════════════════════════════════════════════

def _serper_search(query: str) -> str:
    """
    Execute one Serper search and return compact formatted results.
    Called only from the fetch_* wrappers below — never from agent code.
    """
    num = _search["results_per_query"]
    timeout = _search["timeout_seconds"]
    web_cfg = ui["web_context"]

    if not _search_on:
        return web_cfg["search_off"]
    try:
        r = requests.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY":   _serper_key,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": num},
            timeout=timeout,
        )
        data  = r.json()
        lines = []

        # Direct answer box (highest signal)
        ab      = data.get("answerBox", {})
        ab_text = ab.get("answer") or ab.get("snippet", "")
        if ab_text:
            lines.append(f"  [Direct answer] {ab_text.strip()}")

        # Organic snippets
        for item in data.get("organic", [])[:num]:
            title   = item.get("title", "").strip()
            snippet = item.get("snippet", "").strip()
            if title and snippet:
                lines.append(f"  • {title}: {snippet}")

        return "\n".join(lines) if lines else web_cfg["no_results"]
    except Exception as e:
        return web_cfg["error"].format(error=e)


def _fetch_web_context(queries: List[str], label: str) -> str:
    """
    Run a list of queries and return a formatted block ready to inject
    into a task description.  Returns empty string if search is off.
    """
    if not _search_on or not queries:
        return ""
    web_cfg  = ui["web_context"]
    date_str = datetime.now().strftime("%B %d, %Y")
    sep      = web_cfg["separator"]
    lines = [
        "",
        sep,
        web_cfg["header"].format(label=label, date=date_str),
        sep,
    ]
    for q in queries:
        lines.append(f'\nQuery: "{q}"')
        lines.append(_serper_search(q))
    lines += ["", sep, web_cfg["footer"].strip(), sep, ""]
    return "\n".join(lines)


def tipsc_search_context(problem: str) -> str:
    """Broad market context fetched once for the initial TIPSC triage."""
    yr    = datetime.now().year
    short = problem[:60].rstrip()
    return _fetch_web_context(
        [
            f"{short} market trends {yr}",
            f"{short} existing apps OR solutions",
            f"{short} startup OR investment {yr}",
        ],
        label="TIPSC Triage",
    )


def criterion_search_context(criterion: str, problem: str) -> str:
    """
    Targeted context for the specific TIPSC criterion currently being probed.
    Queries are built from the actual problem text so they're always relevant.
    """
    yr    = datetime.now().year
    short = problem[:55].rstrip()
    queries_map = {
    "T": [
        f"{short} market trends {yr}",
        f"{short} growing problem evidence"
    ],

    "I": [
        f"{short} user pain points",
        f"{short} consequences impact"
    ],

    "P": [
        f"{short} business opportunity",
        f"{short} willingness to pay"
    ],

    "S": [
        f"{short} technical feasibility",
        f"{short} existing technologies available"
    ],

    "C": [
        f"{short} regulations compliance",
        f"{short} industry context"
    ],

    "N": [
        f"{short} unmet need evidence",
        f"{short} existing solutions limitations"
    ],
}
    queries = queries_map.get(criterion, [f"{short} market opportunity {yr}"])
    return _fetch_web_context(queries, label=f"Criterion: {_CRITERION_NAMES.get(criterion, criterion)}")


def psea_search_context(problem: str, solution: str, issue: str = "") -> str:
    """
    Context for PSEA idea evaluation.  `issue` is one of P/S/E/A for targeted
    refinement searches; empty string triggers the broad initial-pass queries.
    """
    yr      = datetime.now().year
    short_p = problem[:50].rstrip()
    short_s = solution[:50].rstrip()

    config = {
        "P": (
            [f"{short_p} competitors OR existing solutions {yr}", f"{short_s} market differentiation"],
            "Problem-Solution Fit",
        ),
        "S": (
            [f"{short_s} minimum viable product examples", f"{short_p} simplest solution approach"],
            "Simplicity",
        ),
        "E": (
            [f"{short_s} privacy regulations data protection {yr}", f"{short_s} legal compliance requirements"],
            "Ethics & Compliance",
        ),
        "A": (
            [f"{short_p} market size statistics {yr}", f"{short_s} user adoption rate research"],
            "Assumptions",
        ),
    }
    queries, label = config.get(issue, (
        [f"{short_p} existing solutions competitors {yr}",
         f"{short_s} technical feasibility",
         f"{short_s} data privacy regulations"],
        "Solution Evaluation",
    ))
    return _fetch_web_context(queries, label=label)


# ══════════════════════════════════════════════════════════════
# AGENTS
# ══════════════════════════════════════════════════════════════

SKILLS_DIR = str(BASE_DIR / "skills")

opportunity_agent = Agent(
    role=opp_p["agent"]["role"],
    goal=opp_p["agent"]["goal"],
    backstory=opp_p["agent"]["backstory"],
    skills=[
        f"{SKILLS_DIR}/tipsc-evaluation",
        f"{SKILLS_DIR}/need-validation",
        f"{SKILLS_DIR}/question-quality",
    ],
    verbose=False,
    tools=agent_tools,
    llm=llm,
)

idea_agent = Agent(
    role=idea_p["agent"]["role"],
    goal=idea_p["agent"]["goal"],
    backstory=idea_p["agent"]["backstory"],
    skills=[
        f"{SKILLS_DIR}/psea-evaluation",
        f"{SKILLS_DIR}/question-quality",
    ],
    verbose=False,
    tools=agent_tools,
    llm=llm,
)


# ══════════════════════════════════════════════════════════════
# VALIDATION FLOW
# ══════════════════════════════════════════════════════════════

class ValidationFlow(Flow[ValidationState]):

    # ──────────────────────────────────────────────────────────
    # STEP 1 — Collect Problem
    # ──────────────────────────────────────────────────────────

    @start()
    def collect_problem(self):
        section(ui["section_titles"]["main"])
        print(ui["startup"]["intro"])
        hr()
        self.state.problem = ask(ui["prompts"]["problem_input"])
        return self.state.problem

    # ──────────────────────────────────────────────────────────
    # STEP 2 — TIPSC Triage (first pass)
    # ──────────────────────────────────────────────────────────

    @listen(collect_problem)
    def tipsc_triage(self, problem):
        section(ui["phases"]["opportunity_header"])
        print(ui["phases"]["opportunity_running"])

        description = opp_p["tasks"]["triage"].format(
            problem=problem,
            search_context=tipsc_search_context(problem),
        )

        triage_task = Task(
            description=description,
            expected_output="Search findings, TIPSC triage, and opening question.",
            agent=opportunity_agent,
        )

        result = run_agent(triage_task, opportunity_agent)

        self.state.opp_history.append({"role": "agent", "content": result})
        self.state.opp_report  = result
        self.state.opp_status  = "IN_PROGRESS"
        self.state.opp_last_q  = extract_question(result)

        return result

    # ──────────────────────────────────────────────────────────
    # STEP 3 — Opportunity Deep-Dive Loop
    #
    # Continues until the agent issues STATUS: APPROVED.
    # Code-level criterion turn tracking forces advancement so
    # the model cannot get stuck probing a single criterion.
    # ──────────────────────────────────────────────────────────

    @listen(tipsc_triage)
    def opportunity_loop(self, triage_result):
        report = triage_result

        while not opp_approved(report):

            agent_says(report)

            # Parse and track which criterion is active
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
            self.state.opp_history.append({"role": "user", "content": answer})

            last_q, last_a = last_exchange(self.state.opp_history)
            rep_warning    = repetition_warning(self.state.opp_last_q, last_q)
            force_close    = force_close_instruction(
                                 self.state.current_criterion, turns_on_current
                             )
            history_text   = format_history(self.state.opp_history)

            description = opp_p["tasks"]["followup"].format(
                force_close=force_close,
                rep_warning=rep_warning,
                last_q=last_q,
                last_a=last_a,
                problem=self.state.problem,
                history=history_text,
                search_context=criterion_search_context(
                    self.state.current_criterion, self.state.problem
                ),
            )

            followup_task = Task(
                description=description,
                expected_output="Feedback, criterion tracking, then next question or approval.",
                agent=opportunity_agent,
            )

            report = run_agent(followup_task, opportunity_agent)
            self.state.opp_last_q = extract_question(report)
            self.state.opp_history.append({"role": "agent", "content": report})
            self.state.opp_report = report

        # Opportunity approved
        self.state.opp_status = "APPROVED"

        section(ui["section_titles"]["opp_valid"])
        print(report)

        hr("-")
        print(f"\n  {ui['transitions']['opportunity_approved']}\n")
        hr("-")

        self.state.solution = ask(ui["prompts"]["solution_input"])
        return self.state.solution

    # ──────────────────────────────────────────────────────────
    # STEP 4 — Initial PSEA Evaluation (first pass)
    # ──────────────────────────────────────────────────────────

    @listen(opportunity_loop)
    def evaluate_idea(self, solution):
        section(ui["phases"]["idea_header"])
        print(ui["phases"]["idea_running"])

        description = idea_p["tasks"]["initial_eval"].format(
            problem=self.state.problem,
            solution=solution,
            search_context=psea_search_context(self.state.problem, solution, ""),
        )
        eval_task = Task(
            description=description,
            expected_output="Search findings, PSEA evaluation with verdict.",
            agent=idea_agent,
        )

        result = run_agent(eval_task, idea_agent)

        self.state.idea_history.append({"role": "agent", "content": result})
        self.state.idea_report = result
        self.state.idea_status = "IN_PROGRESS"
        self.state.idea_last_q = extract_question(result)

        return result

    # ──────────────────────────────────────────────────────────
    # STEP 5 — Idea Refinement Loop
    #
    # Continues until the agent issues VERDICT: READY_FOR_DFV.
    # ──────────────────────────────────────────────────────────

    @listen(evaluate_idea)
    def idea_loop(self, eval_result):
        report = eval_result

        while not idea_ready(report):

            agent_says(report)

            # Parse and track which PSEA issue is active
            active_issue = ""
            if "ISSUE IN FOCUS:" in report.upper():
                idx  = report.upper().find("ISSUE IN FOCUS:")
                line = report[idx + len("ISSUE IN FOCUS:"):].strip().split("\n")[0].upper()
                for ch in line:
                    if ch in "PSEA":
                        active_issue = ch
                        break
            if active_issue:
                self.state.current_idea_issue = active_issue
                self.state.idea_issue_turns[active_issue] = (
                    self.state.idea_issue_turns.get(active_issue, 0) + 1
                )
            turns_on_current = self.state.idea_issue_turns.get(
                self.state.current_idea_issue, 0
            )

            answer = ask(ui["prompts"]["response_input"])
            self.state.idea_history.append({"role": "user", "content": answer})

            last_q, last_a = last_exchange(self.state.idea_history)
            rep_warning    = repetition_warning(self.state.idea_last_q, last_q)
            force_close    = force_close_instruction(
                                 self.state.current_idea_issue, turns_on_current
                             )
            history_text   = format_history(self.state.idea_history)

            description = idea_p["tasks"]["refinement"].format(
                force_close=force_close,
                rep_warning=rep_warning,
                last_q=last_q,
                last_a=last_a,
                problem=self.state.problem,
                solution=self.state.solution,
                history=history_text,
                search_context=psea_search_context(
                    self.state.problem, self.state.solution, active_issue
                ),
            )

            refinement_task = Task(
                description=description,
                expected_output="Search findings, feedback, then next question or DFV clearance.",
                agent=idea_agent,
            )

            report = run_agent(refinement_task, idea_agent)
            self.state.idea_last_q = extract_question(report)
            self.state.idea_history.append({"role": "agent", "content": report})
            self.state.idea_report = report

        self.state.idea_status = "READY_FOR_DFV"
        return report

    # ──────────────────────────────────────────────────────────
    # STEP 6 — Final Report
    # ──────────────────────────────────────────────────────────

    @listen(idea_loop)
    def final_report(self, idea_result):
        labels = ui["report_labels"]

        section(ui["section_titles"]["final"])

        hr("-")
        print(f"  {labels['problem']}")
        hr("-")
        print(f"  {self.state.problem}")
        print()

        hr("-")
        print(f"  {labels['opportunity']}")
        hr("-")
        print(self.state.opp_report)
        print()

        hr("-")
        print(f"  {labels['solution']}")
        hr("-")
        print(f"  {self.state.solution}")
        print()

        hr("-")
        print(f"  {labels['idea']}")
        hr("-")
        print(idea_result)
        print()

        section(ui["section_titles"]["dfv_ready"])
        print(f"  {ui['transitions']['final_status']}\n")
        hr()

        self.state.idea_status = "COMPLETE"
        return idea_result


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
