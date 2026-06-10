"""
Entrepreneurial Opportunity Validation System
=============================================

A two-agent AI system that validates entrepreneurial opportunities
before the founder commits to building a solution.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AGENT 1 — Opportunity Evaluation Agent
  Phase 1a: TIPSC Triage (first-pass: Strong / Weak / Unclear)
  Phase 1b: TIPSC Deep-Dive (one criterion at a time)
  Phase 1c: Need Validation
  Phase 1d: COP (inferred organically throughout)

AGENT 2 — Idea Evaluation Agent
  Phase 2a: PSEA Evaluation (initial pass)
  Phase 2b: Refinement Loop (until READY_FOR_DFV)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Workflow:
  Problem Input
    → Opportunity Evaluation (TIPSC → Need → COP)
    → [APPROVED] → Solution Input
    → Idea Evaluation (PSEA + Feasibility)
    → [READY_FOR_DFV]
"""

import os
import sys
from pathlib import Path

import requests
import yaml
from datetime import datetime
from typing import List
from pydantic import BaseModel
from crewai import Agent, Task, Crew, Process, LLM
from crewai.memory import Memory
from crewai.flow.flow import Flow, listen, start
from crewai_tools import SerperDevTool


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
scout_p  = _load_yaml("prompts/market_scout_agent.yaml")
ui       = _load_yaml("prompts/ui_strings.yaml")

# ── Convenience accessors ──────────────────────────────────────
_val     = cfg["validation"]
_conv    = cfg["conversation"]
_search  = cfg["search"]
_display = cfg["display"]

MAX_TURNS_PER_CRITERION = _val["max_turns_per_criterion"]
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

# ── Memory setup ─────────────────────────────────────────────
# Memory is instantiated once and used directly via .remember()
# and .recall(). It is NOT passed to Crew() — that causes an
# async event loop conflict. Memory handles its own threading
# internally via a background ThreadPoolExecutor.
_mem_cfg = cfg.get("memory", {})
_mem_storage_path = str(
    BASE_DIR / _mem_cfg.get("storage_path", ".crewai/memory")
)

try:
    memory = Memory(
        llm=llm,
        storage=_mem_storage_path,
        embedder={
            "provider": "sentence-transformer",
            "config": {
                "model_name": _mem_cfg.get(
                    "embedder_model",
                    "all-MiniLM-L6-v2"
                )
            },
        },
        recency_weight=_mem_cfg.get("recency_weight", 0.3),
        semantic_weight=_mem_cfg.get("semantic_weight", 0.5),
        importance_weight=_mem_cfg.get("importance_weight", 0.2),
        recency_half_life_days=_mem_cfg.get(
            "recency_half_life_days",
            30
        ),
    )

    print("✓ CrewAI Memory initialized")

except Exception as e:
    print(f"⚠ Memory initialization failed: {e}")
    memory = None

agent_tools = [SerperDevTool()]

# Serper web search — Python-direct (no agent tool calls).
# Search results are pre-fetched in Python and injected as context
# into each task description.  Small LLMs cannot reliably trigger
# function/tool calls; pre-fetching bypasses that entirely.
#
# Free key (2,500 searches/month): https://serper.dev
# Set before running:  export SERPER_API_KEY=your_key_here
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

    # ── Market Scout (Phase 0) ─────────────────────────────────
    market_verdict: str = ""   # "REJECT" or "PROCEED"
    market_report:  str = ""   # Full competitive landscape report
    market_angle:   str = ""   # Founder's creative differentiation angle

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
    idea_status: str = "PENDING"          # PENDING → IN_PROGRESS → READY_FOR_DFV
    idea_report: str = ""
    idea_last_q: str = ""                 # Tracks last question to detect repetition


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


def extract_displayed_question(text: str) -> str:
    """
    Pull the question the agent is asking out of any response format.

    Strategy 1 — Explicit QUESTION: section (expected format).
    Strategy 2 — Last line in the response that ends with '?'.
    Strategy 3 — Substring around the last '?' anywhere in the text.
    Strategy 4 — Generic fallback.
    """
    if "QUESTION:" in text.upper():
        idx   = text.upper().rfind("QUESTION:")
        chunk = text[idx + len("QUESTION:"):].strip()
        line  = chunk.split("\n")[0].strip()
        if len(line) > 5:
            return line

<<<<<<< Updated upstream
    # Strategy 2: last non-empty line that ends with a '?'
=======
>>>>>>> Stashed changes
    for line in reversed(text.strip().split("\n")):
        line = line.strip()
        if line.endswith("?") and len(line) > 10:
            return line

<<<<<<< Updated upstream
    # Strategy 3: substring from last sentence boundary to last '?'
=======
>>>>>>> Stashed changes
    if "?" in text:
        last_q  = text.rfind("?")
        before  = text[:last_q]
        start   = max(before.rfind("."), before.rfind(":"), before.rfind("\n"))
        fragment = text[start + 1: last_q + 1].strip()
        if len(fragment) > 10:
            return fragment

<<<<<<< Updated upstream
    # Strategy 4: generic fallback from UI strings
=======
>>>>>>> Stashed changes
    return ui["agent_display"]["fallback_question"]


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
    """Print agent response and highlight the question."""
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
    """Prompt the user for input and return the trimmed response."""
    if prompt:
        print(f"\n  {prompt}")
    response = input("\n  > ").strip()
    print()
    return response


def format_history(history: List[dict]) -> str:
    """
    Build a minimal context block for the agent — NOT a full transcript.
    Extracts resolved verdicts and the last 2 Q&A pairs only.
    """
    if not history:
        return "(No prior conversation.)"

    verdict_lines = []
    for turn in history:
        if turn["role"] != "agent":
            continue
        text = turn["content"]
        for line in text.split("\n"):
            l = line.strip()
            if any(l.startswith(f"{c} —") or l.startswith(f"{c}:") for c in "TIPSCN"):
                if any(w in l.upper() for w in ("STRONG", "WEAK", "ACCEPTED", "UNCLEAR", "CONFIRMED", "RESOLVED")):
                    verdict_lines.append(f"  {l}")

    qa_pairs = []
    i = len(history) - 1
    while i >= 0 and len(qa_pairs) < 2:
        if history[i]["role"] == "user":
            answer = history[i]["content"]
            for j in range(i - 1, -1, -1):
                if history[j]["role"] == "agent":
                    q_text = history[j]["content"]
                    if "QUESTION:" in q_text.upper():
                        q_start = q_text.upper().find("QUESTION:")
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

    parts = []
    if verdict_lines:
        seen = list(dict.fromkeys(verdict_lines))
        parts.append("RESOLVED SO FAR:\n" + "\n".join(seen))
    if qa_pairs:
        parts.append("RECENT EXCHANGES:\n" + "\n\n".join(qa_pairs))

    return "\n\n".join(parts) if parts else "(No prior conversation.)"


def run_agent(task: Task, agent: Agent) -> str:
    """
    Run a single-agent crew and return a clean plain-text string.
<<<<<<< Updated upstream

    When agents use tools (e.g. SerperDevTool), CrewAI includes the full
    reasoning trace in the string output — raw JSON dumps from Serper, chain-
    of-thought lines starting with 'Thought:', and tool result blobs all appear
    before the agent's actual formatted answer.

    clean_agent_output() strips that noise by finding the first known section
    header in our structured format and returning only what follows it.
=======
    Memory is handled externally via memory.remember() / memory.recall()
    and is never passed to Crew() to avoid async event loop conflicts.
>>>>>>> Stashed changes
    """
    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
<<<<<<< Updated upstream
        verbose=False
=======
        verbose=False,
>>>>>>> Stashed changes
    )
    raw = str(crew.kickoff())
    return clean_agent_output(raw)


_RESPONSE_MARKERS = [
    "VERDICT: REJECT",
    "VERDICT: PROCEED",
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
    """Strip tool-call noise from a raw CrewAI agent output string."""
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


def last_exchange(history: List[dict]) -> tuple[str, str]:
    """Extract the most recent (agent_question, user_answer) pair from history."""
    last_q = "(none)"
    last_a = "(none)"
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


def extract_question(text: str) -> str:
    """Pull the QUESTION: value out of a response for repetition tracking."""
    if "QUESTION:" in text.upper():
        idx = text.upper().find("QUESTION:")
        return text[idx + len("QUESTION:"):].strip().split("\n")[0].strip()
    return text.strip()[:200]


def repetition_warning(last_q: str, current_q: str) -> str:
    """Return warning string if new question is too similar to the last one."""
    if not last_q or last_q == "(none)":
        return ""
    lq_words = set(last_q.lower().split())
    cq_words = set(current_q.lower().split())
    if not lq_words:
        return ""
    overlap = len(lq_words & cq_words) / len(lq_words)
    if overlap > 0.7:
        return ui["prompts"].get(
            "repetition_warning",
            ui["repetition_warning"],
        )
    return ""


def opp_approved(text: str) -> bool:
    return "STATUS: APPROVED" in text.upper()


def idea_ready(text: str) -> bool:
    return "VERDICT: READY_FOR_DFV" in text.upper()


def parse_criterion(text: str) -> str:
    """Extract the single-letter criterion code from a CRITERION IN FOCUS: line."""
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

_CRITERION_SEARCH_GUIDANCE = {
    "T": "Search for market readiness and adoption trends.",
    "I": "Search for evidence of user pain points.",
    "P": "Search for monetization models and revenue opportunities.",
    "S": "Search for technical feasibility and available technology.",
    "C": "Search for regulations and contextual constraints.",
    "N": "Search for evidence that the need is real.",
}

_PSEA_SEARCH_GUIDANCE = {
    "P": "Search for competitors and problem-solution fit evidence.",
    "S": "Search for simpler alternatives.",
    "E": "Search for legal, privacy, and ethical requirements.",
    "A": "Search for market assumptions and adoption data.",
}


def force_close_instruction(criterion: str, turns: int) -> str:
    """Return forced-close instruction block when a criterion is stuck."""
    if turns < MAX_TURNS_PER_CRITERION:
        return ""
    name     = _CRITERION_NAMES.get(criterion, criterion)
    template = ui["force_close_template"]
    return template.format(turns=turns, name=name, criterion=criterion)


def _safe_memory_save(content: str, scope: str) -> None:
    if memory is None:
        return

    try:
        memory.remember(
            content=content,
            scope=scope,
            categories=[scope.split("/")[0]],
            importance=0.7,
        )
        print(f"  [memory] saved -> {scope}")

    except Exception as e:
        print(f"  [memory] save failed: {e}")


def _safe_memory_recall(query: str, scope: str, limit: int = 3) -> str:
    if memory is None:
        return ""

    try:
        matches = memory.recall(
            query=query,
            scope=scope,
            limit=limit,
            depth="shallow",
        )

        if not matches:
            print("  [memory] no matches found")
            return ""

        print(f"  [memory] recalled {len(matches)} records")

        lines = [
            f"  - {m.record.content[:200]}"
            for m in matches
        ]

        return (
            "\nPAST CONTEXT FROM MEMORY:\n"
            + "\n".join(lines)
            + "\n"
        )

    except Exception as e:
        print(f"  [memory] recall failed: {e}")
        return ""


# ══════════════════════════════════════════════════════════════
# WEB SEARCH HELPERS
# ══════════════════════════════════════════════════════════════

def _serper_search(query: str) -> str:
    """Execute one Serper search and return compact formatted results."""
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
    """Run queries and return a formatted block for task injection."""
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
            f"students college notifications communication pain points",
            f"{short} startup OR investment {yr}",
        ],
        label="TIPSC Triage",
    )


def criterion_search_context(criterion: str, problem: str) -> str:
    """Targeted context for the specific TIPSC criterion being probed."""
    yr    = datetime.now().year
    short = problem[:55].rstrip()
    queries_map = {
        "T": [f"{short} market readiness {yr}", f"{short} technology adoption rate"],
        "I": [f"{short} user pain points OR complaints", "students missing college updates notifications problem"],
        "P": [f"{short} business model monetization examples", f"EdTech student platform revenue model {yr}"],
        "S": [f"{short} technical feasibility build", f"{short} API OR integration tools available"],
        "C": [f"{short} data privacy regulations {yr}", "student app compliance requirements"],
        "N": [f"{short} unmet need evidence", f"{short} why existing solutions fail students"],
    }
    queries = queries_map.get(criterion, [f"{short} market opportunity {yr}"])
    return _fetch_web_context(queries, label=f"Criterion: {_CRITERION_NAMES.get(criterion, criterion)}")


def psea_search_context(problem: str, solution: str, issue: str = "") -> str:
    """Context for PSEA idea evaluation."""
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


def market_scout_search_context(problem: str) -> str:
    """Comprehensive competitive intelligence searches for the Market Scout Agent."""
    yr    = datetime.now().year
    short = problem[:55].rstrip()
    return _fetch_web_context(
        [
            f"{short} existing apps OR platforms OR solutions {yr}",
            f"{short} market leaders competitors funding",
            f"{short} user complaints OR limitations OR missing features",
            f"{short} market size OR growth rate {yr}",
            f"{short} new startup launch OR recent entrant {yr}",
        ],
        label="Competitive Landscape",
    )


# ══════════════════════════════════════════════════════════════
# AGENTS
# ══════════════════════════════════════════════════════════════

opportunity_agent = Agent(
    role=opp_p["agent"]["role"],
    goal=opp_p["agent"]["goal"],
    backstory=opp_p["agent"]["backstory"],
    verbose=False,
    tools=agent_tools,
    llm=llm,
)

idea_agent = Agent(
    role=idea_p["agent"]["role"],
    goal=idea_p["agent"]["goal"],
    backstory=idea_p["agent"]["backstory"],
    verbose=False,
    tools=agent_tools,
    llm=llm,
)

market_scout_agent = Agent(
    role=scout_p["agent"]["role"],
    goal=scout_p["agent"]["goal"],
    backstory=scout_p["agent"]["backstory"],
    verbose=False,
    tools=[],
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
    # STEP 2 — Market Intelligence Scan  (Phase 0)
    # ──────────────────────────────────────────────────────────

    @listen(collect_problem)
    def market_scan(self, problem):
        ms  = ui["market_scout"]
        section(ms["header"])
        print(ms["running"])

        web_ctx     = market_scout_search_context(problem)
        description = scout_p["tasks"]["scan"].format(
            problem     = problem,
            web_context = web_ctx,
        )

        scan_task = Task(
            description=description,
            expected_output="Competitive landscape report with VERDICT: REJECT or VERDICT: PROCEED.",
            agent=market_scout_agent,
        )

        result = run_agent(scan_task, market_scout_agent)
        self.state.market_report = result

        verdict = "PROCEED"
        if "VERDICT: REJECT" in result.upper():
            verdict = "REJECT"
        elif "VERDICT: PROCEED" in result.upper():
            verdict = "PROCEED"
        self.state.market_verdict = verdict

        # ── Save market scan to CrewAI Memory ─────────────────
        _safe_memory_save(
            content=f"Market scan verdict: {verdict} | Problem: {problem[:100]}",
            scope="market/scout",
        )

        if verdict == "REJECT":
            section(ms["section_reject"])
            print(result)
            hr("-")
            print(f"\n  {ms['reject_banner']}\n")
            hr("-")

            choice = ask(ms["reject_prompt"])
            if choice.strip().lower() != ms["reject_continue_keyword"]:
                print(f"\n  {ms['reject_exit_message']}\n")
                sys.exit(0)

            print("\n  Proceeding at founder's discretion.\n")

        else:
            section(ms["section_proceed"])
            print(result)
            hr("-")
            print(f"\n  {ms['proceed_banner']}\n")
            hr("-")

        print()
        print("=" * W)
        print(f"  {ms['creative_header']}")
        print("=" * W)
        for line in _wrap(ms["creative_prompt"]):
            print(f"  {line}")
        print("=" * W)
        print()

        angle = ask()
        self.state.market_angle = angle

        return problem

    # ──────────────────────────────────────────────────────────
    # STEP 3 — TIPSC Triage (first pass)
    # ──────────────────────────────────────────────────────────

    @listen(market_scan)
    def tipsc_triage(self, problem):
        section(ui["phases"]["opportunity_header"])
        print(ui["phases"]["opportunity_running"])

        # ── Recall past TIPSC evaluations for similar problems ─
        past_context = _safe_memory_recall(
            query=f"TIPSC evaluation for: {problem[:80]}",
            scope="opportunity/tipsc",
            limit=2,
        )

        angle_block = ""
        if self.state.market_angle:
            angle_block = (
                f"\nFOUNDER'S DIFFERENTIATION ANGLE:\n"
                f"{self.state.market_angle}\n"
                f"(Keep this in mind when evaluating T, I, P, and S criteria.)\n"
            )

        description = (
            opp_p["tasks"]["triage"].format(problem=problem)
            + angle_block
            + past_context
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

        # ── Save triage result to CrewAI Memory ───────────────
        _safe_memory_save(
            content=f"TIPSC triage | Problem: {problem[:100]}\n{result[:300]}",
            scope="opportunity/tipsc",
        )

        return result

    # ──────────────────────────────────────────────────────────
    # STEP 4 — Opportunity Deep-Dive Loop
    # ──────────────────────────────────────────────────────────

    @listen(tipsc_triage)
    def opportunity_loop(self, triage_result):
        report = triage_result

        while not opp_approved(report):

            agent_says(report)

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
            srch_guidance  = _CRITERION_SEARCH_GUIDANCE.get(
                                 self.state.current_criterion, ""
                             )
            history_text   = format_history(self.state.opp_history)

            description = opp_p["tasks"]["followup"].format(
                force_close     = force_close,
                rep_warning     = rep_warning,
                search_guidance = srch_guidance,
                last_q          = last_q,
                last_a          = last_a,
                problem         = self.state.problem,
                history         = history_text,
            )

            followup_task = Task(
                description=description,
                expected_output="Feedback, criterion tracking, then next question or approval.",
                agent=opportunity_agent,
            )

            report = run_agent(followup_task, opportunity_agent)
            self.state.opp_last_q = extract_question(report)

            # ── Save per-criterion verdict to CrewAI Memory ───
            criterion = parse_criterion(report)
            if criterion:
                _safe_memory_save(
                    content=(
                        f"Criterion {criterion} "
                        f"({_CRITERION_NAMES.get(criterion, criterion)}) evaluated "
                        f"| Problem: {self.state.problem[:80]}"
                    ),
                    scope=f"opportunity/tipsc/{criterion.lower()}",
                )

            self.state.opp_history.append({"role": "agent", "content": report})
            self.state.opp_report = report

        # Opportunity approved
        self.state.opp_status = "APPROVED"

        # ── Save approval to CrewAI Memory ────────────────────
        _safe_memory_save(
            content=(
                f"OPPORTUNITY APPROVED | Problem: {self.state.problem}\n"
                f"Summary: {report[:400]}"
            ),
            scope="opportunity/tipsc",
        )

        section(ui["section_titles"]["opp_valid"])
        print(report)

        hr("-")
        print(f"\n  {ui['transitions']['opportunity_approved']}\n")
        hr("-")

        self.state.solution = ask(ui["prompts"]["solution_input"])
        return self.state.solution

    # ──────────────────────────────────────────────────────────
    # STEP 5 — Initial PSEA Evaluation (first pass)
    # ──────────────────────────────────────────────────────────

    @listen(opportunity_loop)
    def evaluate_idea(self, solution):
        section(ui["phases"]["idea_header"])
        print(ui["phases"]["idea_running"])

        description = idea_p["tasks"]["initial_eval"].format(
            problem=self.state.problem,
            solution=solution,
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

        return result

    # ──────────────────────────────────────────────────────────
    # STEP 6 — Idea Refinement Loop
    # ──────────────────────────────────────────────────────────

    @listen(evaluate_idea)
    def idea_loop(self, eval_result):
        report = eval_result

        while not idea_ready(report):

            agent_says(report)

            answer = ask(ui["prompts"]["response_input"])
            self.state.idea_history.append({"role": "user", "content": answer})

            last_q, last_a = last_exchange(self.state.idea_history)
            rep_warning    = repetition_warning(self.state.idea_last_q, last_q)
            history_text   = format_history(self.state.idea_history)

            active_issue = ""
            if "ISSUE IN FOCUS:" in report.upper():
                idx  = report.upper().find("ISSUE IN FOCUS:")
                line = report[idx + len("ISSUE IN FOCUS:"):].strip().split("\n")[0].upper()
                for ch in line:
                    if ch in "PSEA":
                        active_issue = ch
                        break
            psea_search = _PSEA_SEARCH_GUIDANCE.get(active_issue, "")

            description = idea_p["tasks"]["refinement"].format(
                rep_warning     = rep_warning,
                search_guidance = psea_search,
                last_q          = last_q,
                last_a          = last_a,
                problem         = self.state.problem,
                solution        = self.state.solution,
                history         = history_text,
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

        # ── Save idea approval to CrewAI Memory ───────────────
        _safe_memory_save(
            content=(
                f"IDEA APPROVED FOR DFV | Problem: {self.state.problem[:80]} "
                f"| Solution: {self.state.solution[:80]}"
            ),
            scope="idea/psea",
        )

        return report

    # ──────────────────────────────────────────────────────────
    # STEP 7 — Final Report
    # ──────────────────────────────────────────────────────────

    @listen(idea_loop)
    def final_report(self, idea_result):
        labels = ui["report_labels"]
        ms     = ui["market_scout"]

        section(ui["section_titles"]["final"])

        hr("-")
        print(f"  {labels['problem']}")
        hr("-")
        print(f"  {self.state.problem}")
        print()

        hr("-")
        print(f"  {ui['section_titles'].get('market_scan', 'MARKET INTELLIGENCE')}")
        hr("-")
        if self.state.market_angle:
            print(f"  Verdict: {self.state.market_verdict}")
            print(f"  {ms['angle_label']}: {self.state.market_angle}")
        else:
            print(f"  Verdict: {self.state.market_verdict}")
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