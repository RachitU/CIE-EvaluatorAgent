"""
Entrepreneurial Opportunity Validation System  — Memory Edition
===============================================================

Changes vs original:
  1. CrewAI Memory added to all 3 agents (scoped per agent)
  2. Flow-level memory (self.remember / self.recall) saves validated
     opportunities, rejected problems, and approved solutions
  3. Past validations are recalled at the start of every new session
     so agents learn from prior runs — similar problems get faster,
     sharper evaluations
  4. Final report is saved to memory for future reference
  5. All memory is LOCAL (LanceDB + sentence-transformers) — no API key needed
     for the embedder if you install sentence-transformers

INSTALL (new deps only):
  pip install lancedb sentence-transformers chromadb

Everything else identical to the original.
"""

import os
import sys
from pathlib import Path

import requests
import yaml
from datetime import datetime
from typing import List
from pydantic import BaseModel
from crewai import Agent, Task, Crew, Process, LLM, Memory
from crewai.flow.flow import Flow, listen, start
from crewai_tools import TavilySearchTool


# ══════════════════════════════════════════════════════════════
# LOAD CONFIGURATION & PROMPTS  (unchanged)
# ══════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent


def _load_yaml(rel_path: str) -> dict:
    with open(BASE_DIR / rel_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


cfg      = _load_yaml("config/settings.yaml")
opp_p    = _load_yaml("prompts/opportunity_agent.yaml")
idea_p   = _load_yaml("prompts/idea_agent.yaml")
scout_p  = _load_yaml("prompts/market_scout_agent.yaml")
ui       = _load_yaml("prompts/ui_strings.yaml")

_val     = cfg["validation"]
_conv    = cfg["conversation"]
_search  = cfg["search"]
_display = cfg["display"]

MAX_TURNS_PER_CRITERION = _val["max_turns_per_criterion"]
W                       = _display["console_width"]


# ══════════════════════════════════════════════════════════════
# CONFIGURATION — LLM
# ══════════════════════════════════════════════════════════════

llm_cfg = cfg["llm"]
llm = LLM(
    model=llm_cfg["model"],
    base_url=llm_cfg["base_url"],
    api_key=llm_cfg["api_key"],
)

search_tool = TavilySearchTool()
agent_tools = [search_tool]

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
# MEMORY SETUP
# ══════════════════════════════════════════════════════════════
#
# ONE shared Memory instance for the whole system.
# Storage is LOCAL under ./.crewai/memory — no cloud, no API key.
#
# Scoring is tuned for a validation tool:
#   - importance_weight raised to 0.4 (approved/rejected verdicts matter a lot)
#   - recency_half_life_days = 60 (ideas stay relevant for months)
#   - semantic_weight = 0.4 (find similar problem domains)
#
# Embedder: sentence-transformers (fully local, no API key).
# If you don't have sentence-transformers installed, swap the embedder block
# for {"provider": "openai", "config": {"model_name": "text-embedding-3-small"}}
# and set OPENAI_API_KEY.

shared_memory = Memory(
    llm=llm_cfg["model"],                      # uses your local LLM for scope inference
    storage=str(BASE_DIR / ".crewai" / "memory"),
    embedder={
        "provider": "sentence-transformer",
        "config": {"model_name": "all-MiniLM-L6-v2"},   # ~80MB, downloads once
    },
    recency_weight=0.2,
    semantic_weight=0.4,
    importance_weight=0.4,
    recency_half_life_days=60,
)

# Each agent gets a SCOPED VIEW — reads from its own branch + shared root.
# The opportunity agent cannot pollute the idea agent's memory and vice versa.
opp_memory   = shared_memory.scope("/agent/opportunity")
idea_memory  = shared_memory.scope("/agent/idea")
scout_memory = shared_memory.scope("/agent/market_scout")

# Shared validation history (past approved problems/solutions) lives here.
# Both agents get read access via recall() calls in the flow.
VALIDATION_SCOPE = "/validations"


# ══════════════════════════════════════════════════════════════
# STATE MODEL  (unchanged from original)
# ══════════════════════════════════════════════════════════════

class ValidationState(BaseModel):
    problem:  str = ""
    solution: str = ""

    market_verdict: str = ""
    market_report:  str = ""
    market_angle:   str = ""

    opp_history:  List[dict] = []
    idea_history: List[dict] = []

    opp_status:        str  = "PENDING"
    opp_report:        str  = ""
    opp_last_q:        str  = ""
    criterion_turns:   dict = {}
    current_criterion: str  = ""

    idea_status: str = "PENDING"
    idea_report: str = ""
    idea_last_q: str = ""


# ══════════════════════════════════════════════════════════════
# CLI UTILITIES  (unchanged)
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

def extract_displayed_question(text: str) -> str:
    if "QUESTION:" in text.upper():
        idx   = text.upper().rfind("QUESTION:")
        chunk = text[idx + len("QUESTION:"):].strip()
        line  = chunk.split("\n")[0].strip()
        if len(line) > 5:
            return line
    for line in reversed(text.strip().split("\n")):
        line = line.strip()
        if line.endswith("?") and len(line) > 10:
            return line
    if "?" in text:
        last_q  = text.rfind("?")
        before  = text[:last_q]
        start   = max(before.rfind("."), before.rfind(":"), before.rfind("\n"))
        fragment = text[start + 1: last_q + 1].strip()
        if len(fragment) > 10:
            return fragment
    return ui["agent_display"]["fallback_question"]

def _wrap(text: str, width: int = W - 4) -> List[str]:
    words, lines, cur = text.split(), [], ""
    for word in words:
        if len(cur) + len(word) + 1 <= width:
            cur = (cur + " " + word).strip()
        else:
            if cur: lines.append(cur)
            cur = word
    if cur: lines.append(cur)
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

def format_history(history: List[dict]) -> str:
    if not history:
        return "(No prior conversation.)"
    verdict_lines = []
    for turn in history:
        if turn["role"] != "agent":
            continue
        for line in turn["content"].split("\n"):
            l = line.strip()
            if any(l.startswith(f"{c} —") or l.startswith(f"{c}:") for c in "TIPSCN"):
                if any(w in l.upper() for w in ("STRONG","WEAK","ACCEPTED","UNCLEAR","CONFIRMED","RESOLVED")):
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
    parts = []
    if verdict_lines:
        seen = list(dict.fromkeys(verdict_lines))
        parts.append("RESOLVED SO FAR:\n" + "\n".join(seen))
    if qa_pairs:
        parts.append("RECENT EXCHANGES:\n" + "\n\n".join(qa_pairs))
    return "\n\n".join(parts) if parts else "(No prior conversation.)"

def run_agent(task: Task, agent: Agent) -> str:
    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
        memory=shared_memory,     # ← crew-level memory so agents auto-save task outputs
    )
    raw = str(crew.kickoff())
    return clean_agent_output(raw)

_RESPONSE_MARKERS = [
    "VERDICT: REJECT", "VERDICT: PROCEED", "TIPSC TRIAGE:",
    "SEARCH FINDINGS:", "PSEA EVALUATION:", "FEEDBACK:",
    "STATUS: APPROVED", "STATUS: NEEDS_MORE_INFO",
    "VERDICT: READY_FOR_DFV", "VERDICT: NEEDS_REFINEMENT",
]

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

def last_exchange(history: List[dict]) -> tuple[str, str]:
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
        return ui["prompts"].get("repetition_warning", ui["repetition_warning"])
    return ""

def opp_approved(text: str) -> bool:
    return "STATUS: APPROVED" in text.upper()

def idea_ready(text: str) -> bool:
    return "VERDICT: READY_FOR_DFV" in text.upper()

def parse_criterion(text: str) -> str:
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
    if turns < MAX_TURNS_PER_CRITERION:
        return ""
    name     = _CRITERION_NAMES.get(criterion, criterion)
    template = ui["force_close_template"]
    return template.format(turns=turns, name=name, criterion=criterion)


# ══════════════════════════════════════════════════════════════
# WEB SEARCH HELPERS  (unchanged)
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
    lines    = ["", sep, web_cfg["header"].format(label=label, date=date_str), sep]
    for q in queries:
        lines.append(f'\nQuery: "{q}"')
        lines.append(_serper_search(q))
    lines += ["", sep, web_cfg["footer"].strip(), sep, ""]
    return "\n".join(lines)

def tipsc_search_context(problem: str) -> str:
    yr    = datetime.now().year
    short = problem[:60].rstrip()
    return _fetch_web_context(
        [f"{short} market trends {yr}", f"{short} existing apps OR solutions",
         f"students college notifications communication pain points",
         f"{short} startup OR investment {yr}"],
        label="TIPSC Triage",
    )

def criterion_search_context(criterion: str, problem: str) -> str:
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
    yr      = datetime.now().year
    short_p = problem[:50].rstrip()
    short_s = solution[:50].rstrip()
    config = {
        "P": ([f"{short_p} competitors OR existing solutions {yr}", f"{short_s} market differentiation"], "Problem-Solution Fit"),
        "S": ([f"{short_s} minimum viable product examples", f"{short_p} simplest solution approach"], "Simplicity"),
        "E": ([f"{short_s} privacy regulations data protection {yr}", f"{short_s} legal compliance requirements"], "Ethics & Compliance"),
        "A": ([f"{short_p} market size statistics {yr}", f"{short_s} user adoption rate research"], "Assumptions"),
    }
    queries, label = config.get(issue, (
        [f"{short_p} existing solutions competitors {yr}",
         f"{short_s} technical feasibility",
         f"{short_s} data privacy regulations"],
        "Solution Evaluation",
    ))
    return _fetch_web_context(queries, label=label)

def market_scout_search_context(problem: str) -> str:
    yr    = datetime.now().year
    short = problem[:55].rstrip()
    return _fetch_web_context(
        [f"{short} existing apps OR platforms OR solutions {yr}",
         f"{short} market leaders competitors funding",
         f"{short} user complaints OR limitations OR missing features",
         f"{short} market size OR growth rate {yr}",
         f"{short} new startup launch OR recent entrant {yr}"],
        label="Competitive Landscape",
    )


# ══════════════════════════════════════════════════════════════
# MEMORY HELPERS
# ══════════════════════════════════════════════════════════════

def _recall_similar_problems(problem: str) -> str:
    """
    Before TIPSC triage, recall any previously validated or rejected
    problems that are semantically similar to the current one.
    Returns a formatted context block (empty string if nothing found).
    """
    try:
        matches = shared_memory.recall(
            f"problem validation result: {problem}",
            scope=VALIDATION_SCOPE,
            limit=3,
            depth="shallow",     # fast, no LLM call
        )
        if not matches:
            return ""
        lines = ["\nPRIOR VALIDATION MEMORY (similar problems evaluated before):"]
        for m in matches:
            lines.append(f"  [{m.score:.2f}] {m.record.content}")
        lines.append("")
        return "\n".join(lines)
    except Exception:
        return ""   # memory failure is non-fatal


def _recall_similar_solutions(problem: str, solution: str) -> str:
    """
    Before PSEA, recall any previously approved or rejected solutions
    for similar problems.
    """
    try:
        matches = shared_memory.recall(
            f"solution evaluation: {solution} for problem: {problem}",
            scope=VALIDATION_SCOPE,
            limit=3,
            depth="shallow",
        )
        if not matches:
            return ""
        lines = ["\nPRIOR SOLUTION MEMORY (similar ideas evaluated before):"]
        for m in matches:
            lines.append(f"  [{m.score:.2f}] {m.record.content}")
        lines.append("")
        return "\n".join(lines)
    except Exception:
        return ""


def _save_validation_result(problem: str, solution: str, opp_report: str, idea_report: str) -> None:
    """
    After a completed validation, extract and store atomic facts so future
    sessions can benefit from this run.
    """
    try:
        # Extract facts from both reports into discrete memory items
        opp_facts  = shared_memory.extract_memories(opp_report)
        idea_facts = shared_memory.extract_memories(idea_report)

        # Save high-importance summary of the whole validation
        summary = (
            f"VALIDATED OPPORTUNITY — Problem: {problem[:120]} | "
            f"Solution: {solution[:120]} | "
            f"Opportunity: APPROVED | Idea: READY_FOR_DFV"
        )
        shared_memory.remember(summary, scope=VALIDATION_SCOPE)

        # Save individual extracted facts at lower importance
        for fact in opp_facts:
            shared_memory.remember(fact, scope=VALIDATION_SCOPE + "/opportunity")
        for fact in idea_facts:
            shared_memory.remember(fact, scope=VALIDATION_SCOPE + "/idea")

        print(f"\n  [Memory] Saved {len(opp_facts) + len(idea_facts) + 1} memory records from this session.")
    except Exception as e:
        print(f"\n  [Memory] Save skipped: {e}")


def _save_rejection(problem: str, reason: str) -> None:
    """Save a market-rejection so future similar problems get a faster warning."""
    try:
        shared_memory.remember(
            f"REJECTED PROBLEM — {problem[:150]} — Reason: {reason[:200]}",
            scope=VALIDATION_SCOPE + "/rejections",
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
# AGENTS  — memory scoped per agent
# ══════════════════════════════════════════════════════════════

opportunity_agent = Agent(
    role=opp_p["agent"]["role"],
    goal=opp_p["agent"]["goal"],
    backstory=opp_p["agent"]["backstory"],
    verbose=False,
    tools=agent_tools,
    llm=llm,
    memory=opp_memory,          # ← private scope for opportunity agent
)

idea_agent = Agent(
    role=idea_p["agent"]["role"],
    goal=idea_p["agent"]["goal"],
    backstory=idea_p["agent"]["backstory"],
    verbose=False,
    tools=agent_tools,
    llm=llm,
    memory=idea_memory,         # ← private scope for idea agent
)

market_scout_agent = Agent(
    role=scout_p["agent"]["role"],
    goal=scout_p["agent"]["goal"],
    backstory=scout_p["agent"]["backstory"],
    verbose=False,
    tools=[],
    llm=llm,
    memory=scout_memory,        # ← private scope for market scout
)


# ══════════════════════════════════════════════════════════════
# VALIDATION FLOW
# ══════════════════════════════════════════════════════════════

class ValidationFlow(Flow[ValidationState]):

    # ── STEP 1 — Collect Problem ───────────────────────────────

    @start()
    def collect_problem(self):
        section(ui["section_titles"]["main"])
        print(ui["startup"]["intro"])
        hr()
        self.state.problem = ask(ui["prompts"]["problem_input"])
        return self.state.problem

    # ── STEP 2 — Market Intelligence Scan ─────────────────────

    @listen(collect_problem)
    def market_scan(self, problem):
        ms = ui["market_scout"]
        section(ms["header"])
        print(ms["running"])

        # ── MEMORY: recall any prior rejections for similar space ──
        prior_memory = _recall_similar_problems(problem)
        if prior_memory:
            print("\n  [Memory] Found similar past validations — informing market scan.\n")

        web_ctx     = market_scout_search_context(problem)
        description = scout_p["tasks"]["scan"].format(
            problem     = problem,
            web_context = web_ctx,
        )

        # Inject prior memory as additional context at the top
        if prior_memory:
            description = prior_memory + "\n" + description

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

        if verdict == "REJECT":
            section(ms["section_reject"])
            print(result)
            hr("-")
            print(f"\n  {ms['reject_banner']}\n")
            hr("-")

            # ── MEMORY: save rejection ─────────────────────────
            _save_rejection(problem, result[:300])

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

    # ── STEP 3 — TIPSC Triage ─────────────────────────────────

    @listen(market_scan)
    def tipsc_triage(self, problem):
        section(ui["phases"]["opportunity_header"])
        print(ui["phases"]["opportunity_running"])

        angle_block = ""
        if self.state.market_angle:
            angle_block = (
                f"\nFOUNDER'S DIFFERENTIATION ANGLE:\n"
                f"{self.state.market_angle}\n"
                f"(Keep this in mind when evaluating T, I, P, and S criteria.)\n"
            )

        # ── MEMORY: recall similar validated problems ──────────
        prior_opp = _recall_similar_problems(problem)
        if prior_opp:
            print("\n  [Memory] Recalling similar past opportunity evaluations.\n")
            angle_block = prior_opp + angle_block

        description = opp_p["tasks"]["triage"].format(problem=problem) + angle_block

        triage_task = Task(
            description=description,
            expected_output="Search findings, TIPSC triage, and opening question.",
            agent=opportunity_agent,
        )

        result = run_agent(triage_task, opportunity_agent)
        self.state.opp_history.append({"role": "agent", "content": result})
        self.state.opp_report  = result
        self.state.opp_status  = "IN_PROGRESS"
        return result

    # ── STEP 4 — Opportunity Deep-Dive Loop ───────────────────

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
            turns_on_current = self.state.criterion_turns.get(self.state.current_criterion, 0)

            answer = ask(ui["prompts"]["response_input"])
            self.state.opp_history.append({"role": "user", "content": answer})

            last_q, last_a = last_exchange(self.state.opp_history)
            rep_warning    = repetition_warning(self.state.opp_last_q, last_q)
            force_close    = force_close_instruction(self.state.current_criterion, turns_on_current)
            srch_guidance  = _CRITERION_SEARCH_GUIDANCE.get(self.state.current_criterion, "")
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
            self.state.opp_history.append({"role": "agent", "content": report})
            self.state.opp_report = report

        self.state.opp_status = "APPROVED"

        section(ui["section_titles"]["opp_valid"])
        print(report)
        hr("-")
        print(f"\n  {ui['transitions']['opportunity_approved']}\n")
        hr("-")

        self.state.solution = ask(ui["prompts"]["solution_input"])
        return self.state.solution

    # ── STEP 5 — Initial PSEA Evaluation ──────────────────────

    @listen(opportunity_loop)
    def evaluate_idea(self, solution):
        section(ui["phases"]["idea_header"])
        print(ui["phases"]["idea_running"])

        # ── MEMORY: recall similar past solution evaluations ───
        prior_idea = _recall_similar_solutions(self.state.problem, solution)
        base_description = idea_p["tasks"]["initial_eval"].format(
            problem=self.state.problem,
            solution=solution,
        )
        if prior_idea:
            print("\n  [Memory] Recalling similar past solution evaluations.\n")
            base_description = prior_idea + "\n" + base_description

        eval_task = Task(
            description=base_description,
            expected_output="Search findings, PSEA evaluation with verdict.",
            agent=idea_agent,
        )

        result = run_agent(eval_task, idea_agent)
        self.state.idea_history.append({"role": "agent", "content": result})
        self.state.idea_report = result
        self.state.idea_status = "IN_PROGRESS"
        return result

    # ── STEP 6 — Idea Refinement Loop ─────────────────────────

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
        return report

    # ── STEP 7 — Final Report + Memory Save ───────────────────

    @listen(idea_loop)
    def final_report(self, idea_result):
        labels = ui["report_labels"]
        ms     = ui["market_scout"]

        section(ui["section_titles"]["final"])

        hr("-"); print(f"  {labels['problem']}");     hr("-")
        print(f"  {self.state.problem}"); print()

        hr("-"); print(f"  {ui['section_titles'].get('market_scan','MARKET INTELLIGENCE')}"); hr("-")
        if self.state.market_angle:
            print(f"  Verdict: {self.state.market_verdict}")
            print(f"  {ms['angle_label']}: {self.state.market_angle}")
        else:
            print(f"  Verdict: {self.state.market_verdict}")
        print()

        hr("-"); print(f"  {labels['opportunity']}"); hr("-")
        print(self.state.opp_report); print()

        hr("-"); print(f"  {labels['solution']}");    hr("-")
        print(f"  {self.state.solution}"); print()

        hr("-"); print(f"  {labels['idea']}");        hr("-")
        print(idea_result); print()

        section(ui["section_titles"]["dfv_ready"])
        print(f"  {ui['transitions']['final_status']}\n")
        hr()

        # ── MEMORY: persist this validation for future sessions ─
        _save_validation_result(
            problem    = self.state.problem,
            solution   = self.state.solution,
            opp_report = self.state.opp_report,
            idea_report= idea_result,
        )

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