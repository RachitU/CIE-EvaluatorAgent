"""
Entrepreneurial Opportunity Validation System  v2
=================================================

Three-agent pipeline:

  Raw Problem Input
    → PHASE 1: Problem Structuring Agent
        Coaches team to produce 4-field structured problem definition
        (Customer Segment · Qualified Problem · Consequence · Assumptions)
        Hard limit: 4 total turns

    → PHASE 2: TIPS Evaluation Agent
        Evaluates structured problem against T/I/P/S criteria
        Green / Yellow / Red scoring per manager rubric
        C = soft contextual awareness check (max 1 turn, not a gate)
        Deterministic Python-controlled array: ["T", "I", "P", "S", "C"]

    → Solution Input

    → PHASE 3: PSEA Idea Evaluation Agent (1-turn global check)
        Problem-Solution Fit · Simplicity · Ethics · Assumptions
        Agent asks one comprehensive question if issues exist, then finalizes.

    → JSON Output → DFV Team
"""

import os
import sys
import json
import logging
import warnings
import re
import time
import functools
from pathlib import Path
from typing import List
from datetime import datetime

import litellm
import requests
import yaml
from pydantic import BaseModel
from crewai import Agent, Task, Crew, Process, LLM
from crewai.flow.flow import Flow, listen, start

# ── Disable telemetry & tracing ──────────────────────────────────
os.environ["CREWAI_TRACING_ENABLED"]  = "0"
os.environ["OTEL_SDK_DISABLED"]       = "true"
os.environ["CREWAI_TELEMETRY_OPT_OUT"] = "true"

logging.disable(logging.WARNING)
warnings.filterwarnings("ignore")


# ══════════════════════════════════════════════════════════════
# LOAD CONFIGURATION & PROMPTS
# ══════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent


def _load_yaml(rel_path: str) -> dict:
    """Load and return a YAML file relative to the project root."""
    with open(BASE_DIR / rel_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


cfg     = _load_yaml("config/settings.yaml")
prob_p  = _load_yaml("prompts/problem_agent.yaml")
opp_p   = _load_yaml("prompts/opportunity_agent.yaml")
idea_p  = _load_yaml("prompts/idea_agent.yaml")
ui      = _load_yaml("prompts/ui_strings.yaml")

# ── Convenience accessors ────────────────────────────────────
_val     = cfg["validation"]
_search  = cfg["search"]
_display = cfg["display"]
_memory  = cfg.get("memory", {})
_conv    = cfg.get("conversation", {})

MAX_TURNS_PER_CRITERION = _val["max_turns_per_criterion"]
MAX_TURNS_C             = 1            # C is a soft check — 1 turn only
MAX_PROB_TURNS          = 3            # Hard limit for Problem Structuring phase
MAX_HISTORY_TURNS       = _conv.get("max_history_turns", 8)
W                       = _display["console_width"]


# ══════════════════════════════════════════════════════════════
# CONFIGURATION — LLM & SEARCH
# ══════════════════════════════════════════════════════════════

llm_cfg = cfg["llm"]
llm = LLM(
    model=llm_cfg["model"],
    base_url=llm_cfg["base_url"],
    api_key=llm_cfg["api_key"],
    max_tokens=llm_cfg.get("max_tokens", 4000),
    timeout=llm_cfg.get("timeout", 120),
)

os.environ.setdefault("OPENAI_API_KEY",  llm_cfg["api_key"])
os.environ.setdefault("OPENAI_BASE_URL", llm_cfg["base_url"])

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
    # ── Phase 1: Problem Structuring ──────────────────────────
    raw_input:   str = ""
    prob_history: List[dict] = []   # {role: "agent"|"user", content: str}
    prob_turns:  int = 0
    prob_last_q: str = ""

    # Structured problem fields (assembled by Python from Agent 1 output)
    customer_segment:   str = ""
    qualified_problem:  str = ""
    consequence:        str = ""
    assumptions:        str = ""

    # ── Phase 2: TIPS Evaluation ──────────────────────────────
    opp_history:  List[dict] = []
    opp_status:   str = "PENDING"   # PENDING → IN_PROGRESS → APPROVED
    opp_report:   str = ""
    opp_last_q:   str = ""

    # Deterministic state machine
    unresolved_opp_criteria: List[str] = []
    current_criterion:       str = ""
    criterion_turns:         dict = {}

    # Per-criterion results for JSON output
    tips_ratings: dict = {}  # {"T": {"color": "Green", "summary": "..."}, ...}

    # ── Phase 3: PSEA Idea Evaluation ─────────────────────────
    solution:    str = ""
    idea_history: List[dict] = []
    idea_status:  str = "PENDING"   # PENDING → IN_PROGRESS → READY_FOR_DFV
    idea_report:  str = ""
    idea_last_q:  str = ""


# ══════════════════════════════════════════════════════════════
# CLI UTILITIES
# ══════════════════════════════════════════════════════════════

def hr(char: str = "=") -> None:
    print(char * W)


def section(title: str) -> None:
    print()
    hr()
    pad = max(0, (W - len(title) - 2) // 2)
    print(" " * pad + title)
    hr()
    print()


def ask(prompt: str = "", focus: str = None) -> str:
    """Prompt user for input. Captures multiline paste via select."""
    import select
    if prompt:
        print(f"\n  {prompt}")
    if focus:
        print(f"  [Focus: {focus}]")
        
    while True:
        print("\n  > ", end="", flush=True)
        try:
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            raise
        except Exception:
            line = ""
            
        if not line:
            print("\n  Session ended (EOF).\n")
            sys.exit(0)
            
        lines = [line.rstrip("\r\n")]
        
        # Buffer any rapidly pasted lines
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], 0.05)
            if ready:
                next_line = sys.stdin.readline()
                if not next_line:
                    break
                lines.append(next_line.rstrip("\r\n"))
            else:
                break
                
        combined = "\n".join(lines).strip()
        
        if combined.lower() in ("quit", "exit", "q"):
            print("\n  Session ended by user.\n")
            sys.exit(0)
            
        if combined:
            print() # Print a newline for spacing
            return combined
        print("  [Please enter a response, or type 'q' to quit]")


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


def extract_question(text: str, for_display: bool = False) -> str:
    if "QUESTION:" in text.upper():
        idx   = text.upper().rfind("QUESTION:")
        chunk = text[idx + len("QUESTION:"):].strip()
        if for_display:
            end = len(chunk)
            for marker in ("GOOD ANSWER EXAMPLE:", "WHAT HELPS:", "VERDICT:", "STATUS:", "PROBLEM SUMMARY:"):
                pos = chunk.upper().find(marker)
                if pos != -1:
                    end = min(end, pos)
            q = chunk[:end].strip()
            if len(q) > 5:
                return q
        else:
            return chunk.split("\n")[0].strip()
    if for_display:
        return ui["agent_display"]["fallback_question"]
    return text.strip()[:200]


def agent_says(text: str, print_question: bool = True) -> None:
    question = extract_question(text, for_display=True)
    label    = ui["agent_display"]["asking_label"]
    print()
    hr("-")
    print(text.strip())
    hr("-")
    print()
    if print_question:
        print("=" * W)
        print(f"  {label}")
        print("=" * W)
        for line in _wrap(question):
            print(f"  {line}")
        print("=" * W)
        print()


def format_history(history: List[dict]) -> str:
    if not history:
        return "(No prior conversation.)"
    verdict_lines = []
    for turn in history:
        if turn["role"] != "agent":
            continue
        for line in turn["content"].split("\n"):
            l = line.strip()
            if any(l.startswith(f"{c} —") or l.startswith(f"{c}:") for c in "TIPSCA"):
                if any(w in l.upper() for w in ("GREEN", "YELLOW", "RED", "STRONG", "ACCEPTED", "CONFIRMED")):
                    verdict_lines.append(f"  {l}")
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
    parts = []
    if verdict_lines:
        seen = list(dict.fromkeys(verdict_lines))
        parts.append("RESOLVED SO FAR:\n" + "\n".join(seen))
    if qa_pairs:
        parts.append("RECENT EXCHANGES:\n" + "\n\n".join(qa_pairs))
    return "\n\n".join(parts) if parts else "(No prior conversation.)"


def last_exchange(history: List[dict]) -> tuple:
    last_q, last_a = "(none)", "(none)"
    for turn in reversed(history):
        if turn["role"] == "user" and last_a == "(none)":
            last_a = turn["content"]
        elif turn["role"] == "agent" and last_q == "(none)" and last_a != "(none)":
            last_q = extract_question(turn["content"])
            break
    return last_q, last_a


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


# ══════════════════════════════════════════════════════════════
# AGENT OUTPUT CLEANING
# ══════════════════════════════════════════════════════════════

_RESPONSE_MARKERS = [
    "TIPS TRIAGE:",
    "SEARCH FINDINGS:",
    "PSEA EVALUATION:",
    "FEEDBACK:",
    "OBSERVATIONS:",
    "PROBLEM SUMMARY:",
    "VERDICT UPDATE:",
    "STATUS: NEEDS_MORE_INFO",
    "VERDICT: NEEDS_REFINEMENT",
    "VERDICT: READY_FOR_DFV",
]

# Patterns the 4B model sometimes echoes from prompt instructions
_META_NOISE_PATTERNS = [
    r"\(If the criterion is now resolved.*?\):\s*",
    r"\(If the criterion still needs.*?\):\s*",
    r"\(If specific evidence is still.*?\):\s*",
    r"\(If the evidence standard is met.*?\):\s*",
    r"\(If you still need more information.*?\):\s*",
]


def _strip_react_noise(text: str) -> str:
    if "OBSERVATION:" in text:
        text = text.split("OBSERVATION:")[0]
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Strip leaked prompt meta-instructions
    for pat in _META_NOISE_PATTERNS:
        text = re.sub(pat, "", text, flags=re.IGNORECASE)
    return text.strip()


def clean_agent_output(text: str) -> str:
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
    memory_on = _memory.get("enabled", False)
    crew_kwargs: dict = {
        "agents":  [agent],
        "tasks":   [task],
        "process": Process.sequential,
        "verbose": False,
        "memory":  memory_on,
    }
    if memory_on:
        crew_kwargs["embedder"] = {
            "provider": "sentence-transformer",
            "config":   {"model": _memory.get("embedder_model", "all-MiniLM-L6-v2")},
        }
    crew = Crew(**crew_kwargs)
    for attempt in range(3):
        try:
            raw = str(crew.kickoff())
            return clean_agent_output(_strip_react_noise(raw))
        except Exception as e:
            if attempt < 2:
                print(f"\n[!] LLM Connection Error (attempt {attempt+1}/3): {e}")
                print("[!] Waiting 5 seconds before retrying...")
                time.sleep(5)
            else:
                raise Exception(f"LLM completely failed after 3 attempts: {e}")


# ══════════════════════════════════════════════════════════════
# CRITERION / ISSUE NAME MAPS
# ══════════════════════════════════════════════════════════════

_CRITERION_NAMES = {
    "T": "Timely", "I": "Important", "P": "Profitable",
    "S": "Solvable", "C": "Contextual",
}

def force_close_instruction(criterion: str, turns: int, name: str = None) -> str:
    limit = MAX_TURNS_C if criterion == "C" else MAX_TURNS_PER_CRITERION
    if turns < limit:
        return ""
    if not name:
        name = _CRITERION_NAMES.get(criterion, criterion)
    template = ui["force_close_template"]
    return template.format(turns=turns, name=name, criterion=criterion)


# ══════════════════════════════════════════════════════════════
# DETERMINISTIC STATE MACHINE HELPERS
# ══════════════════════════════════════════════════════════════

def update_unresolved_list(unresolved: List[str], text: str) -> str:
    verdict_mode = "VERDICT UPDATE:" in text.upper()
    for line in text.split("\n"):
        l = line.strip().upper()
        if verdict_mode and not l.startswith("VERDICT UPDATE:"):
            continue
        for c in list(unresolved):
            pattern = rf"^(?:[-*]\s*)?(?:(?:VERDICT\s*)?UPDATE:\s*)?(?:\*\*)?{c}(?:\*\*)?\s*(?:—|:|-)"
            if re.search(pattern, l):
                if any(w in l for w in ("GREEN", "🟢", "STRONG", "ACCEPTED", "CONFIRMED", "RESOLVED", "COMPLETE", "NO MAJOR", "PASS", "GOOD")):
                    if c in unresolved:
                        unresolved.remove(c)
    return unresolved


def extract_tips_rating(criterion: str, text: str) -> dict:
    """Extract the color rating for a single TIPSC criterion.
    
    Only matches lines containing 'VERDICT UPDATE:' to avoid false positives
    from the LLM mentioning criterion letters + colors in prose.
    """
    color, summary = "Unknown", ""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        l = line.strip()
        if "VERDICT UPDATE:" not in l.upper():
            continue
        pattern = rf"^(?:[-*]\s*)?(?:(?:VERDICT\s*)?UPDATE:\s*)?(?:\*\*)?{criterion}(?:\*\*)?\s*(?:—|:|-)"
        if re.search(pattern, l.upper()):
            if any(w in l.upper() for w in ("GREEN", "🟢")): color = "Green"
            elif any(w in l.upper() for w in ("YELLOW", "🟡")): color = "Yellow"
            elif any(w in l.upper() for w in ("RED", "🔴")): color = "Red"
            if i + 1 < len(lines) and "CRITERION SUMMARY:" in lines[i+1].upper():
                summary = lines[i+1].split(":", 1)[-1].strip()
            elif "—" in l:
                summary = l.split("—", 1)[-1].strip()
            break
    return {"color": color, "summary": summary}


def parse_problem_summary(text: str) -> dict:
    result = {
        "customer_segment":  "",
        "qualified_problem": "",
        "consequence":       "",
        "assumptions":       "",
    }
    if "PROBLEM SUMMARY" not in text.upper():
        return result
    idx  = text.upper().find("PROBLEM SUMMARY")
    block = text[idx:].strip()
    for line in block.split("\n"):
        l = line.strip()
        if match := re.search(r"\*?\*?CUSTOMER SEGMENT\*?\*?\s*(?:—|:|-)\s*(.*)", l, re.IGNORECASE):
            result["customer_segment"]  = match.group(1).strip()
        elif match := re.search(r"\*?\*?QUALIFIED PROBLEM\*?\*?\s*(?:—|:|-)\s*(.*)", l, re.IGNORECASE):
            result["qualified_problem"] = match.group(1).strip()
        elif match := re.search(r"\*?\*?CONSEQUENCE\*?\*?\s*(?:—|:|-)\s*(.*)", l, re.IGNORECASE):
            result["consequence"]       = match.group(1).strip()
        elif match := re.search(r"\*?\*?ASSUMPTIONS\*?\*?\s*(?:—|:|-)\s*(.*)", l, re.IGNORECASE):
            result["assumptions"]       = match.group(1).strip()
    return result


# ══════════════════════════════════════════════════════════════
# WEB SEARCH HELPERS
# ══════════════════════════════════════════════════════════════

def _serper_search(query: str) -> str:
    num     = _search["results_per_query"]
    timeout = _search["timeout_seconds"]
    web_cfg = ui["web_context"]
    if not _search_on:
        return web_cfg["search_off"]
    try:
        r = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": _serper_key, "Content-Type": "application/json"},
            json={"q": query, "num": num},
            timeout=timeout,
        )
        if r.status_code != 200:
            return web_cfg["error"].format(error=f"HTTP {r.status_code}: {r.text[:100]}")
        data  = r.json()
        lines = []
        ab      = data.get("answerBox", {})
        ab_text = ab.get("answer") or ab.get("snippet", "")
        if ab_text:
            lines.append(f"  [Direct answer] {ab_text.strip()}")
        for item in data.get("organic", [])[:num]:
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
    lines = ["", sep, web_cfg["header"].format(label=label, date=date_str), sep]
    for q in queries:
        lines.append(f'\nQuery: "{q}"')
        lines.append(_serper_search(q))
    lines += ["", sep, web_cfg["footer"].strip(), sep, ""]
    return "\n".join(lines)


@functools.lru_cache(maxsize=10)
def _extract_search_keyword(text: str) -> str:
    prompt = (
        f"Extract exactly one 3-5 word search query representing the core problem "
        f"and customer in this text: '{text}'. Reply with ONLY the keywords, nothing else."
    )
    try:
        resp = litellm.completion(
            model=llm_cfg["model"],
            base_url=llm_cfg["base_url"],
            api_key=llm_cfg["api_key"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=15,
            temperature=0.1,
        )
        kw = resp.choices[0].message.content.strip().strip("'\"")
        if kw:
            return kw
    except Exception:
        pass
    return text[:50].rstrip()


def problem_search_context(raw_input: str) -> str:
    yr    = datetime.now().year
    short = _extract_search_keyword(raw_input)
    return _fetch_web_context(
        [f"{short} problem evidence {yr}", f"{short} existing solutions"],
        label="Problem Structuring",
    )


def tipsc_search_context(problem: str) -> str:
    yr    = datetime.now().year
    short = _extract_search_keyword(problem)
    return _fetch_web_context(
        [
            f"{short} market trends {yr}",
            f"{short} existing apps OR solutions",
            f"{short} startup OR investment {yr}",
        ],
        label="TIPS Triage",
    )


def criterion_search_context(criterion: str, problem: str) -> str:
    yr    = datetime.now().year
    short = _extract_search_keyword(problem)
    queries_map = {
        "T": [f"{short} market trends {yr}", f"{short} growing problem evidence"],
        "I": [f"{short} user pain points", f"{short} consequences impact"],
        "P": [f"{short} business opportunity", f"{short} willingness to pay"],
        "S": [f"{short} technical feasibility", f"{short} existing technologies"],
        "C": [f"{short} regulations compliance", f"{short} industry context"],
    }
    queries = queries_map.get(criterion, [f"{short} market opportunity {yr}"])
    return _fetch_web_context(queries, label=f"Criterion: {_CRITERION_NAMES.get(criterion, criterion)}")


def psea_search_context(problem: str, solution: str) -> str:
    yr      = datetime.now().year
    short_p = _extract_search_keyword(problem)
    short_s = _extract_search_keyword(solution)
    
    queries = [
        f"{short_p} existing solutions competitors {yr}",
        f"{short_s} technical feasibility",
        f"{short_s} data privacy regulations"
    ]
    return _fetch_web_context(queries, label="Solution Evaluation")


# ══════════════════════════════════════════════════════════════
# AGENTS
# ══════════════════════════════════════════════════════════════

SKILLS_DIR = str(BASE_DIR / "skills")

problem_agent = Agent(
    role=prob_p["agent"]["role"],
    goal=prob_p["agent"]["goal"],
    backstory=prob_p["agent"]["backstory"],
    skills=[
        f"{SKILLS_DIR}/problem-structuring",
        f"{SKILLS_DIR}/question-quality",
    ],
    verbose=False,
    tools=agent_tools,
    llm=llm,
)

opportunity_agent = Agent(
    role=opp_p["agent"]["role"],
    goal=opp_p["agent"]["goal"],
    backstory=opp_p["agent"]["backstory"],
    skills=[
        f"{SKILLS_DIR}/tipsc-evaluation",
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
    # STEP 1 — Collect Raw Problem Input
    # ──────────────────────────────────────────────────────────

    @start()
    def collect_raw_input(self):
        section(ui["section_titles"]["main"])
        print(ui["startup"]["intro"])
        hr()
        self.state.raw_input = ask(ui["prompts"]["problem_input"])
        return self.state.raw_input

    # ──────────────────────────────────────────────────────────
    # STEP 2 — Problem Structuring Loop (Agent 1, max 4 turns)
    # ──────────────────────────────────────────────────────────

    @listen(collect_raw_input)
    def structure_problem(self, raw_input):
        section(ui["phases"]["problem_header"])
        print(ui["phases"]["problem_running"])

        # Turn 1: initial pass
        description = prob_p["tasks"]["initial"].format(
            raw_input=raw_input,
            search_context=problem_search_context(raw_input),
        )
        init_task = Task(
            description=description,
            expected_output="Observations, structured fields status, and guided question.",
            agent=problem_agent,
        )
        report = run_agent(init_task, problem_agent)
        self.state.prob_history.append({"role": "agent", "content": report})
        self.state.prob_last_q = extract_question(report)
        self.state.prob_turns  = 1

        # Always show Turn 1 output before asking for user input
        parsed = parse_problem_summary(report)
        if any(parsed.values()):
            self._apply_structured_problem(parsed)

        agent_says(report, print_question=not self._problem_complete())

        # Refinement loop — runs only if fields are still incomplete
        while self.state.prob_turns < MAX_PROB_TURNS and not self._problem_complete():

            answer = ask(ui["prompts"]["response_input"])
            self.state.prob_history.append({"role": "user", "content": answer})

            last_q, last_a = last_exchange(self.state.prob_history)
            turn_note = (
                f"[FINAL TURN — Turn {self.state.prob_turns + 1} of {MAX_PROB_TURNS}. "
                f"You MUST output a PROBLEM SUMMARY block now regardless of completeness.]\n"
                if self.state.prob_turns + 1 >= MAX_PROB_TURNS
                else f"[Turn {self.state.prob_turns + 1} of {MAX_PROB_TURNS}]\n"
            )
            current_fields = (
                f"Customer Segment: {self.state.customer_segment or '(not yet provided)'}\n"
                f"Qualified Problem: {self.state.qualified_problem or '(not yet provided)'}\n"
                f"Consequence: {self.state.consequence or '(not yet provided)'}\n"
                f"Assumptions: {self.state.assumptions or '(not yet provided)'}"
            )
            description = prob_p["tasks"]["refinement"].format(
                turn_note=turn_note,
                last_q=last_q,
                last_a=last_a,
                current_fields=current_fields,
            )
            refine_task = Task(
                description=description,
                expected_output="Feedback and either PROBLEM SUMMARY or a refinement question.",
                agent=problem_agent,
            )
            report = run_agent(refine_task, problem_agent)
            self.state.prob_history.append({"role": "agent", "content": report})
            self.state.prob_last_q = extract_question(report)
            self.state.prob_turns += 1

            parsed = parse_problem_summary(report)
            if any(parsed.values()):
                self._apply_structured_problem(parsed)

            agent_says(report, print_question=not self._problem_complete())

        # Print final structured problem
        section(ui["section_titles"]["prob_done"])
        print(f"  Customer Segment:   {self.state.customer_segment or '(approximate)'}")
        print(f"  Qualified Problem:  {self.state.qualified_problem or '(approximate)'}")
        print(f"  Consequence:        {self.state.consequence or '(approximate)'}")
        print(f"  Assumptions:        {self.state.assumptions or '(approximate)'}")
        hr("-")
        print(f"\n  {ui['transitions']['problem_complete']}\n")
        hr("-")

        return self.state.qualified_problem or self.state.raw_input

    def _problem_complete(self) -> bool:
        return all([
            self.state.customer_segment,
            self.state.qualified_problem,
            self.state.consequence,
            self.state.assumptions,
        ])

    def _apply_structured_problem(self, parsed: dict) -> None:
        if parsed.get("customer_segment"):
            self.state.customer_segment  = parsed["customer_segment"]
        if parsed.get("qualified_problem"):
            self.state.qualified_problem = parsed["qualified_problem"]
        if parsed.get("consequence"):
            self.state.consequence       = parsed["consequence"]
        if parsed.get("assumptions"):
            self.state.assumptions       = parsed["assumptions"]

    # ──────────────────────────────────────────────────────────
    # STEP 3 — TIPS Triage (first pass, Agent 2)
    # ──────────────────────────────────────────────────────────

    @listen(structure_problem)
    def tips_triage(self, qualified_problem):
        section(ui["phases"]["opportunity_header"])
        print(ui["phases"]["opportunity_running"])

        description = opp_p["tasks"]["triage"].format(
            customer_segment=self.state.customer_segment   or self.state.raw_input,
            qualified_problem=self.state.qualified_problem or self.state.raw_input,
            consequence=self.state.consequence             or "(not specified)",
            assumptions=self.state.assumptions             or "(not specified)",
            search_context=tipsc_search_context(qualified_problem),
        )
        triage_task = Task(
            description=description,
            expected_output="Search findings, TIPS triage ratings, and opening coaching question.",
            agent=opportunity_agent,
        )
        result = run_agent(triage_task, opportunity_agent)

        self.state.opp_history.append({"role": "agent", "content": result})
        self.state.opp_report = result
        self.state.opp_status = "IN_PROGRESS"
        self.state.opp_last_q = extract_question(result)

        return result

    # ──────────────────────────────────────────────────────────
    # STEP 4 — TIPS Deep-Dive Loop (Python-controlled array)
    #
    # T, I, P, S → full coaching (MAX_TURNS_PER_CRITERION each)
    # C → soft check, max 1 turn, never blocks
    # ──────────────────────────────────────────────────────────

    @listen(tips_triage)
    def opportunity_loop(self, triage_result):
        report = triage_result

        # Build deterministic array from triage output
        self.state.unresolved_opp_criteria = ["T", "I", "P", "S", "C"]
        self.state.unresolved_opp_criteria = update_unresolved_list(
            self.state.unresolved_opp_criteria, report
        )

        for c in ["T", "I", "P", "S", "C"]:
            if c not in self.state.unresolved_opp_criteria:
                rating = extract_tips_rating(c, report)
                if rating["color"] != "Unknown":
                    self.state.tips_ratings[c] = rating

        agent_says(report)

        while self.state.unresolved_opp_criteria:

            self.state.current_criterion = self.state.unresolved_opp_criteria[0]
            is_c = self.state.current_criterion == "C"

            self.state.criterion_turns[self.state.current_criterion] = (
                self.state.criterion_turns.get(self.state.current_criterion, 0) + 1
            )
            turns_on_current = self.state.criterion_turns[self.state.current_criterion]

            answer = ask(
                ui["prompts"]["response_input"], 
                focus=f"{self.state.current_criterion} — {_CRITERION_NAMES.get(self.state.current_criterion)}"
            )
            self.state.opp_history.append({"role": "user", "content": answer})

            last_q, last_a = last_exchange(self.state.opp_history)
            rep_warning    = repetition_warning(self.state.opp_last_q, last_q)
            force_close    = force_close_instruction(self.state.current_criterion, turns_on_current)
            history_text   = format_history(self.state.opp_history)

            # System override for max turns
            turn_limit = MAX_TURNS_C if is_c else MAX_TURNS_PER_CRITERION
            if turns_on_current >= turn_limit:
                history_text += (
                    f"\n\n[SYSTEM OVERRIDE]: MAX TURNS REACHED FOR {self.state.current_criterion}. "
                    f"YOU MUST OUTPUT 'VERDICT UPDATE: {self.state.current_criterion}: [color]' "
                    f"AND 'CRITERION SUMMARY: [...]' IMMEDIATELY AND STOP."
                )

            description = opp_p["tasks"]["followup"].format(
                force_close=force_close,
                rep_warning=rep_warning,
                last_q=last_q,
                last_a=last_a,
                customer_segment=self.state.customer_segment   or self.state.raw_input,
                qualified_problem=self.state.qualified_problem or self.state.raw_input,
                consequence=self.state.consequence             or "(not specified)",
                assumptions=self.state.assumptions             or "(not specified)",
                history=history_text,
                current_criterion=f"{self.state.current_criterion} — {_CRITERION_NAMES.get(self.state.current_criterion, self.state.current_criterion)}",
                criterion_name=_CRITERION_NAMES.get(self.state.current_criterion, self.state.current_criterion),
                search_context=criterion_search_context(self.state.current_criterion, self.state.qualified_problem or self.state.raw_input),
            )

            followup_task = Task(
                description=description,
                expected_output="Evaluation of team's answer and next coaching action.",
                agent=opportunity_agent,
            )

            report = run_agent(followup_task, opportunity_agent)
            self.state.opp_last_q = extract_question(report)
            self.state.opp_history.append({"role": "agent", "content": report})
            self.state.opp_report = report

            # Capture the rating if the criterion was just resolved
            rating = extract_tips_rating(self.state.current_criterion, report)
            if rating["color"] != "Unknown":
                self.state.tips_ratings[self.state.current_criterion] = rating

            self.state.unresolved_opp_criteria = update_unresolved_list(
                self.state.unresolved_opp_criteria, report
            )

            # Force resolution if max turns reached and LLM still didn't pass it
            if turns_on_current >= turn_limit and self.state.current_criterion in self.state.unresolved_opp_criteria:
                print(f"\n  [System] Max turns reached for {self.state.current_criterion}. Forcing resolution and moving on.")
                self.state.unresolved_opp_criteria.remove(self.state.current_criterion)

            agent_says(report, print_question=bool(self.state.unresolved_opp_criteria))

        # TIPS phase complete
        self.state.opp_status = "APPROVED"

        section(ui["section_titles"]["opp_valid"])
        hr("-")
        print("  YOUR TIPS RATINGS SUMMARY:")
        for c, name in _CRITERION_NAMES.items():
            rating = self.state.tips_ratings.get(c, {})
            color  = rating.get("color", "Not rated")
            icon   = {"Green": "\U0001f7e2", "Yellow": "\U0001f7e1", "Red": "\U0001f534"}.get(color, "\u26aa")
            print(f"  {c} \u2014 {name}: {icon} {color}")
        hr("-")
        print(f"\n  {ui['transitions']['opportunity_approved']}\n")
        hr("-")

        self.state.solution = ask(ui["prompts"]["solution_input"])
        return self.state.solution

    # ──────────────────────────────────────────────────────────
    # STEP 5 — Initial PSEA Evaluation (Agent 3, first pass)
    # ──────────────────────────────────────────────────────────

    @listen(opportunity_loop)
    def evaluate_idea(self, solution):
        section(ui["phases"]["idea_header"])
        print(ui["phases"]["idea_running"])

        description = idea_p["tasks"]["initial_eval"].format(
            problem=self.state.qualified_problem or self.state.raw_input,
            solution=solution,
            search_context=psea_search_context(
                self.state.qualified_problem or self.state.raw_input, solution
            ),
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
    # STEP 6 — PSEA Refinement Loop (unchanged logic)
    # ──────────────────────────────────────────────────────────

    @listen(evaluate_idea)
    def idea_loop(self, eval_result):
        report = eval_result

        if "VERDICT: READY_FOR_DFV" in report.upper() or "VERDICT UPDATE: READY_FOR_DFV" in report.upper():
            self.state.idea_status = "READY_FOR_DFV"
            section(ui["section_titles"]["idea_valid"])
            return report

        # Single global refinement turn
        agent_says(report, print_question=True)

        answer = ask(
            ui["prompts"]["response_input"],
            focus="PSEA Refinement"
        )
        self.state.idea_history.append({"role": "user", "content": answer})

        last_q, last_a = last_exchange(self.state.idea_history)
        history_text   = format_history(self.state.idea_history)

        description = idea_p["tasks"]["refinement"].format(
            last_q=last_q,
            last_a=last_a,
            problem=self.state.qualified_problem or self.state.raw_input,
            solution=self.state.solution,
            history=history_text,
            search_context=psea_search_context(
                self.state.qualified_problem or self.state.raw_input,
                self.state.solution
            ),
        )

        refinement_task = Task(
            description=description,
            expected_output="Final PSEA feedback and accepted verdict.",
            agent=idea_agent,
        )

        report = run_agent(refinement_task, idea_agent)
        self.state.idea_last_q = extract_question(report)
        self.state.idea_history.append({"role": "agent", "content": report})
        self.state.idea_report = report

        agent_says(report, print_question=False)

        self.state.idea_status = "READY_FOR_DFV"
        section(ui["section_titles"]["idea_valid"])
        return report

    # ──────────────────────────────────────────────────────────
    # STEP 7 — Final Report + JSON Output
    # ──────────────────────────────────────────────────────────

    @listen(idea_loop)
    def final_report(self, idea_result):
        labels = ui["report_labels"]

        section(ui["section_titles"]["final"])

        hr("-")
        print(f"  {labels['problem']}")
        hr("-")
        print(f"  Customer Segment:   {self.state.customer_segment}")
        print(f"  Qualified Problem:  {self.state.qualified_problem}")
        print(f"  Consequence:        {self.state.consequence}")
        print(f"  Assumptions:        {self.state.assumptions}")
        print()

        hr("-")
        print(f"  {labels['opportunity']}")
        hr("-")
        for c, name in _CRITERION_NAMES.items():
            rating = self.state.tips_ratings.get(c, {})
            color  = rating.get("color", "Not rated")
            icon   = {"Green": "🟢", "Yellow": "🟡", "Red": "🔴"}.get(color, "⚪")
            print(f"  {c} — {name}: {icon} {color}")
        print()

        hr("-")
        print(f"  {labels['solution']}")
        hr("-")
        print(f"  {self.state.solution}")
        print()

        hr("-")
        print(f"  {labels['idea']}")
        hr("-")
        # Extract initial PSEA evaluation
        if self.state.idea_history:
            initial_eval = self.state.idea_history[0]["content"]
            if "PSEA EVALUATION:" in initial_eval.upper():
                idx = initial_eval.upper().find("PSEA EVALUATION:")
                print(initial_eval[idx:].strip())
            else:
                print(initial_eval.strip())
        else:
            print(idea_result)

        # Extract refinement verdicts
        refinements = []
        for turn in self.state.idea_history[1:]:
            if turn["role"] == "agent":
                for line in turn["content"].split("\n"):
                    l = line.strip()
                    if "VERDICT UPDATE:" in l.upper():
                        refinements.append(f"  {l}")

        if refinements:
            print("\n  REFINEMENT VERDICTS ACHIEVED:")
            seen = set()
            for r in refinements:
                if r not in seen:
                    print(r)
                    seen.add(r)
        print()

        # ── Assemble JSON ────────────────────────────────────
        def _tips_field(c: str) -> str:
            r = self.state.tips_ratings.get(c, {})
            color   = r.get("color", "Not rated")
            summary = r.get("summary", "")
            name    = _CRITERION_NAMES.get(c, c)
            return f"{name}: {color}. {summary}".strip()

        output = {
            "refined_idea": {
                "customer_segment":  self.state.customer_segment,
                "qualified_problem": self.state.qualified_problem,
                "consequence":       self.state.consequence,
                "assumptions":       self.state.assumptions,
                "proposed_solution": self.state.solution,
            },
            "tips_validated_metrics": {
                "timely_factor":          _tips_field("T"),
                "importance_metric":      _tips_field("I"),
                "profitability_pivot":    _tips_field("P"),
                "solvability_constraint": _tips_field("S"),
                "contextual_note":        _tips_field("C"),
            },
        }

        os.makedirs(BASE_DIR / "output", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"session_output_{timestamp}.json"
        json_path = BASE_DIR / "output" / filename
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        hr("-")
        print(f"  {labels['json_output']}")
        hr("-")
        print(json.dumps(output, indent=2, ensure_ascii=False))
        print()
        print(f"  ✓  Saved to: output/{filename}")
        hr("-")

        section(ui["section_titles"]["dfv_ready"])
        print(f"  {ui['transitions']['final_status']}\n")
        hr()

        self.state.idea_status = "COMPLETE"
        return output


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
