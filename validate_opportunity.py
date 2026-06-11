"""
Entrepreneurial Opportunity Validation System  — v4 (Mentor Whiteboard Revision)
==================================================================================

Key changes from v3:
  1. NEW: Idea Structuring Agent (Phase 0) — runs BEFORE market scan
     Steers the student to define: Problem Statement, Customer Segment,
     Consequence, and Assumptions. Max 3-4 iterations. Outputs structured JSON.

  2. TIPSC Agent redesigned per whiteboard:
     — C (Contextual) is IGNORED for now
     — T, I, P, S each have explicit Green/Yellow/Red scoring rules:
         T (Timely)    : time horizon ≤1 year AND growing urgency → Green
                         time horizon >1 year or hazy → Yellow
                         no urgency → Red
         I (Important) : Must-Have + ≤1yr horizon → Green
                         Should-Have + ≤1yr → Green; Should-Have + >1yr → Yellow
                         Nice-to-Have → Red
         P (Profitable): Customer willing to pay (Y/N) → Green / Red
                         B2B2C or indirect monetization → Yellow
         S (Solvable)  : Team has skills + resources → Green
                         Partial capability → Yellow
                         No capability → Red
     — Output is a structured JSON matching the mentor's schema +
       a human-readable TIPS coaching summary

  3. Max 3-4 questions per agent phase (hard cap enforced)

  4. PSEA / Idea Eval loop retained, uses structured output from Phase 0

  5. Final output is the full JSON structure the mentor defined

INSTALL:
  pip install requests pyyaml lancedb sentence-transformers crewai "crewai[tools]"
"""

import os
import sys
import json
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests
import yaml
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel
from crewai import Agent, Task, Crew, Process, LLM, Memory
from crewai.flow.flow import Flow, listen, start
from crewai_tools import TavilySearchTool


# ══════════════════════════════════════════════════════════════
# CONFIG LOADERS
# ══════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent


def _load_yaml(rel_path: str) -> dict:
    with open(BASE_DIR / rel_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _load_skill(skill_name: str) -> str:
    path = BASE_DIR / "skills" / skill_name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3:].strip()
    return text


cfg      = _load_yaml("config/settings.yaml")
ui       = _load_yaml("prompts/ui_strings.yaml")

_val     = cfg["validation"]
_search  = cfg["search"]
_display = cfg["display"]

MAX_IDEA_STRUCT_QUESTIONS = 4   # Phase 0: idea structuring agent (3-4 per mentor)
MAX_TIPS_QUESTIONS        = 3   # Phase 1: TIPS coaching agent
MAX_PSEA_QUESTIONS        = 3   # Phase 2: PSEA evaluation
W                         = _display["console_width"]


# ══════════════════════════════════════════════════════════════
# LLM
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

shared_memory = Memory(
    llm=llm,
    storage=str(BASE_DIR / ".crewai" / "memory"),
    embedder={
        "provider": "sentence-transformer",
        "config": {"model_name": "all-MiniLM-L6-v2"},
    },
    recency_weight=0.2,
    semantic_weight=0.4,
    importance_weight=0.4,
    recency_half_life_days=60,
    consolidation_threshold=1.0,
    query_analysis_threshold=99999,
)

VALIDATION_SCOPE = "/validations"


# ══════════════════════════════════════════════════════════════
# STATE MODEL
# ══════════════════════════════════════════════════════════════

class RefinedIdea(BaseModel):
    customer_segment: str = ""
    qualified_problem: str = ""
    consequence: str = ""
    proposed_solution: str = ""
    assumptions: List[str] = []

class TIPSMetrics(BaseModel):
    timely_factor:        str = ""
    timely_rating:        str = ""   # Green / Yellow / Red
    importance_metric:    str = ""
    importance_rating:    str = ""
    profitability_pivot:  str = ""
    profitability_rating: str = ""
    solvability_constraint: str = ""
    solvability_rating:   str = ""

class ValidationState(BaseModel):
    # Raw inputs from student
    raw_problem:  str = ""
    raw_solution: str = ""

    # Phase 0 — Idea Structuring
    idea_struct_history:   List[dict] = []
    idea_struct_questions: int        = 0
    idea_struct_status:    str        = "PENDING"
    refined_idea:          RefinedIdea = RefinedIdea()

    # Market Scout
    market_verdict: str = ""
    market_report:  str = ""
    market_angle:   str = ""

    # Phase 1 — TIPS Coaching
    tips_history:   List[dict] = []
    tips_questions: int        = 0
    tips_status:    str        = "PENDING"
    tips_metrics:   TIPSMetrics = TIPSMetrics()
    tips_report:    str         = ""

    # Phase 2 — PSEA / Idea Eval
    idea_history: List[dict] = []
    idea_questions: int      = 0
    idea_status:  str        = "PENDING"
    idea_report:  str        = ""
    idea_last_q:  str        = ""


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
    return ui["agent_display"]["fallback_question"]

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
        return "\n⚠ WARNING: Your last question was nearly identical. Ask something DIFFERENT.\n"
    return ""

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

def format_history(history: List[dict]) -> str:
    if not history:
        return "(No prior conversation.)"
    qa_pairs = []
    i = len(history) - 1
    while i >= 0 and len(qa_pairs) < 3:
        if history[i]["role"] == "user":
            answer = history[i]["content"]
            for j in range(i - 1, -1, -1):
                if history[j]["role"] == "agent":
                    q_text = history[j]["content"]
                    if "QUESTION:" in q_text.upper():
                        q_start  = q_text.upper().find("QUESTION:")
                        question = q_text[q_start + len("QUESTION:"):].strip().split("\n")[0].strip()
                    else:
                        question = q_text.strip()[:150]
                    qa_pairs.insert(0, f"Q: {question}\nA: {answer}")
                    i = j - 1
                    break
            else:
                i -= 1
        else:
            i -= 1
    return "\n\n".join(qa_pairs) if qa_pairs else "(No prior conversation.)"

def run_agent(task: Task, agent: Agent) -> str:
    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
    )
    raw = str(crew.kickoff())
    return clean_agent_output(raw)

_RESPONSE_MARKERS = [
    "IDEA STRUCTURE COMPLETE","STRUCTURED IDEA:","QUESTION:",
    "TIPS COACHING:","TIPS ASSESSMENT:","TIPS VERDICT:",
    "PSEA EVALUATION:","VERDICT: READY_FOR_DFV","VERDICT: NEEDS_REFINEMENT",
    "COMPETITIVE LANDSCAPE:","VERDICT: REJECT","VERDICT: PROCEED",
    "FEEDBACK:","STATUS: APPROVED","STATUS: NEEDS_MORE_INFO",
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

def tips_complete(text: str) -> bool:
    return "TIPS VERDICT: COMPLETE" in text.upper() or "STATUS: APPROVED" in text.upper()

def idea_struct_complete(text: str) -> bool:
    return "IDEA STRUCTURE COMPLETE" in text.upper() or "STRUCTURED IDEA:" in text.upper()

def idea_ready(text: str) -> bool:
    return "VERDICT: READY_FOR_DFV" in text.upper()


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

def market_scout_search_context(problem: str) -> str:
    yr    = datetime.now().year
    short = problem[:55].rstrip()
    return _fetch_web_context(
        [f"{short} existing apps OR platforms OR solutions {yr}",
         f"{short} market leaders competitors funding",
         f"{short} user complaints OR limitations OR missing features",
         f"{short} market size OR growth rate {yr}"],
        label="Competitive Landscape",
    )


# ══════════════════════════════════════════════════════════════
# MEMORY HELPERS
# ══════════════════════════════════════════════════════════════

def _recall_similar(query: str, scope: str) -> str:
    try:
        matches = shared_memory.recall(query, scope=scope, limit=3, depth="shallow")
        if not matches:
            return ""
        lines = ["\n[Prior memory — similar past validation]:"]
        for m in matches:
            lines.append(f"  [{m.score:.2f}] {m.record.content}")
        lines.append("")
        return "\n".join(lines)
    except Exception:
        return ""

def _save_validation_result(state: "ValidationState") -> None:
    try:
        summary = (
            f"VALIDATED — Customer: {state.refined_idea.customer_segment[:80]} | "
            f"Problem: {state.refined_idea.qualified_problem[:100]} | "
            f"T:{state.tips_metrics.timely_rating} "
            f"I:{state.tips_metrics.importance_rating} "
            f"P:{state.tips_metrics.profitability_rating} "
            f"S:{state.tips_metrics.solvability_rating}"
        )
        shared_memory.remember(summary, scope=VALIDATION_SCOPE)
        print(f"\n  [Memory] Session saved.")
    except Exception as e:
        print(f"\n  [Memory] Save skipped: {e}")

def _save_rejection(problem: str, reason: str) -> None:
    try:
        shared_memory.remember(
            f"REJECTED — {problem[:150]} — {reason[:200]}",
            scope=VALIDATION_SCOPE + "/rejections",
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
# JSON PARSERS — extract structured data from agent output
# ══════════════════════════════════════════════════════════════

def _parse_refined_idea(text: str) -> RefinedIdea:
    """
    Try to parse agent output into RefinedIdea.
    The agent is prompted to output a labeled block — we extract by labels.
    """
    ri = RefinedIdea()

    def _extract(label: str) -> str:
        marker = label + ":"
        if marker.upper() not in text.upper():
            return ""
        idx   = text.upper().find(marker.upper())
        chunk = text[idx + len(marker):].strip()
        line  = chunk.split("\n")[0].strip()
        return line

    ri.customer_segment   = _extract("CUSTOMER SEGMENT")
    ri.qualified_problem  = _extract("QUALIFIED PROBLEM")
    ri.consequence        = _extract("CONSEQUENCE")
    ri.proposed_solution  = _extract("PROPOSED SOLUTION")

    # assumptions: look for numbered list after ASSUMPTIONS:
    if "ASSUMPTIONS:" in text.upper():
        idx   = text.upper().find("ASSUMPTIONS:")
        block = text[idx + len("ASSUMPTIONS:"):].strip()
        items = []
        for line in block.split("\n"):
            line = line.strip()
            if line and (line[0].isdigit() or line.startswith("-") or line.startswith("•")):
                items.append(line.lstrip("0123456789.-•) ").strip())
            elif items and not line:
                break
        ri.assumptions = items[:5]

    return ri


def _parse_tips_metrics(text: str) -> TIPSMetrics:
    """Extract TIPS ratings and explanations from agent output."""
    tm = TIPSMetrics()

    def _extract_rating(label: str) -> tuple:
        """Returns (explanation, rating) for a TIPS criterion."""
        marker = label + ":"
        if marker.upper() not in text.upper():
            return "", ""
        idx      = text.upper().find(marker.upper())
        chunk    = text[idx + len(marker):].strip()
        first_line = chunk.split("\n")[0].strip()
        rating = ""
        for color in ("GREEN", "YELLOW", "RED"):
            if color in first_line.upper() or color in chunk[:200].upper():
                rating = color.capitalize()
                break
        return first_line, rating

    tm.timely_factor,        tm.timely_rating        = _extract_rating("TIMELY")
    tm.importance_metric,    tm.importance_rating    = _extract_rating("IMPORTANCE")
    tm.profitability_pivot,  tm.profitability_rating = _extract_rating("PROFITABILITY")
    tm.solvability_constraint, tm.solvability_rating = _extract_rating("SOLVABILITY")

    return tm


def _build_final_json(state: "ValidationState") -> dict:
    """Assemble the mentor's JSON structure from validated state."""
    return {
        "refined_idea": {
            "customer_segment":   state.refined_idea.customer_segment,
            "qualified_problem":  state.refined_idea.qualified_problem,
            "consequence":        state.refined_idea.consequence,
            "proposed_solution":  state.refined_idea.proposed_solution,
            "assumptions":        state.refined_idea.assumptions,
        },
        "tips_validated_metrics": {
            "timely_factor":          state.tips_metrics.timely_factor,
            "timely_rating":          state.tips_metrics.timely_rating,
            "importance_metric":      state.tips_metrics.importance_metric,
            "importance_rating":      state.tips_metrics.importance_rating,
            "profitability_pivot":    state.tips_metrics.profitability_pivot,
            "profitability_rating":   state.tips_metrics.profitability_rating,
            "solvability_constraint": state.tips_metrics.solvability_constraint,
            "solvability_rating":     state.tips_metrics.solvability_rating,
        },
        "market_verdict": state.market_verdict,
        "market_angle":   state.market_angle,
        "psea_report":    state.idea_report,
    }


# ══════════════════════════════════════════════════════════════
# SKILL.md LOADER
# ══════════════════════════════════════════════════════════════

idea_struct_skill = _load_skill("idea_structuring_agent")
tips_skill        = _load_skill("tips_agent")
idea_eval_skill   = _load_skill("idea_agent")
scout_skill       = _load_skill("market_scout_agent")


# ══════════════════════════════════════════════════════════════
# AGENTS
# ══════════════════════════════════════════════════════════════

# NEW: Phase 0 agent — steers students to produce structured problem definition
idea_structuring_agent = Agent(
    role="Idea Structuring Coach",
    goal=idea_struct_skill,
    backstory=(
        "You are a startup coach who helps student founders articulate their "
        "problem clearly before any evaluation begins. You ask simple, direct "
        "questions and never evaluate — only clarify and structure. "
        "You are patient, encouraging, and efficient."
    ),
    verbose=False,
    tools=[],
    llm=llm,
    memory=False,
)

# REDESIGNED: Phase 1 agent — TIPS coaching (C ignored, RAG/color scoring)
tips_agent = Agent(
    role="TIPS Validation Coach",
    goal=tips_skill,
    backstory=(
        "You are a startup mentor who evaluates entrepreneurial opportunity "
        "using the TIPS framework. You use specific scoring rules "
        "(Green/Yellow/Red) to assess Timely, Important, Profitable, Solvable. "
        "You coach students on gaps and help them strengthen weak criteria."
    ),
    verbose=False,
    tools=agent_tools,
    llm=llm,
    memory=False,
)

idea_eval_agent = Agent(
    role="Idea Evaluation Agent",
    goal=idea_eval_skill,
    backstory=(
        "Startup investor and product strategist. You evaluate whether a proposed "
        "solution is fundable using PSEA criteria. You are focused and efficient."
    ),
    verbose=False,
    tools=agent_tools,
    llm=llm,
    memory=False,
)

market_scout_agent = Agent(
    role="Market Scout Agent",
    goal=scout_skill,
    backstory=(
        "Competitive intelligence analyst scanning the market landscape for "
        "incumbents, gaps, and saturation signals."
    ),
    verbose=False,
    tools=[],
    llm=llm,
    memory=False,
)


# ══════════════════════════════════════════════════════════════
# VALIDATION FLOW
# ══════════════════════════════════════════════════════════════

class ValidationFlow(Flow[ValidationState]):

    # ── PHASE 0 STEP 1 — Collect raw inputs ───────────────────

    @start()
    def collect_inputs(self):
        section(ui["section_titles"]["main"])
        print(ui["startup"]["intro"])
        hr()

        print("\n  Give us a rough description of your problem and idea.")
        print("  Don't worry about being perfect — the system will help you structure it.\n")
        self.state.raw_problem  = ask("Describe the problem you see:")
        self.state.raw_solution = ask("Describe your idea / proposed solution (rough is fine):")
        return self.state.raw_problem

    # ── PHASE 0 STEP 2 — Idea Structuring Loop ────────────────

    @listen(collect_inputs)
    def idea_structuring_loop(self, raw_problem):
        """
        The Idea Structuring Agent coaches the student over 3-4 turns to
        produce a structured definition:
          - Customer Segment
          - Qualified Problem
          - Consequence
          - Proposed Solution (refined)
          - Assumptions
        """
        section("PHASE 0 — IDEA STRUCTURING")
        print("  The agent will help you define your problem clearly before evaluation.\n")

        prior = _recall_similar(raw_problem, VALIDATION_SCOPE)

        # First call — kick off structuring
        description = (
            f"RAW PROBLEM: {raw_problem}\n"
            f"RAW SOLUTION: {self.state.raw_solution}\n\n"
            f"{prior}\n"
            f"{idea_struct_skill}\n\n"
            f"QUESTION BUDGET: You have maximum {MAX_IDEA_STRUCT_QUESTIONS} questions total.\n\n"
            f"Ask your FIRST question to clarify the most important missing piece.\n"
            f"Do NOT ask about all four elements at once.\n\n"
            f"FORMAT:\n"
            f"COACHING: [1-2 sentences of encouragement/context]\n"
            f"QUESTION:\n[single focused question]"
        )

        task = Task(
            description=description,
            expected_output="Coaching note and one clarifying question.",
            agent=idea_structuring_agent,
        )

        report = run_agent(task, idea_structuring_agent)
        self.state.idea_struct_history.append({"role": "agent", "content": report})
        self.state.idea_struct_questions = 1

        while not idea_struct_complete(report):

            # Hard cap
            if self.state.idea_struct_questions >= MAX_IDEA_STRUCT_QUESTIONS:
                print(f"\n  [System] Structuring budget ({MAX_IDEA_STRUCT_QUESTIONS}) reached. Generating structure now.\n")
                force_desc = (
                    f"You have now asked {MAX_IDEA_STRUCT_QUESTIONS} questions. "
                    f"MANDATORY: produce the final structured output now using what you have. "
                    f"Fill in any missing fields with your best inference.\n\n"
                    f"RAW PROBLEM: {raw_problem}\n"
                    f"RAW SOLUTION: {self.state.raw_solution}\n"
                    f"CONVERSATION:\n{format_history(self.state.idea_struct_history)}\n\n"
                    f"OUTPUT FORMAT:\n"
                    f"IDEA STRUCTURE COMPLETE\n"
                    f"CUSTOMER SEGMENT: [who has the problem — be specific]\n"
                    f"QUALIFIED PROBLEM: [precise one-sentence problem statement]\n"
                    f"CONSEQUENCE: [what happens if problem is not solved]\n"
                    f"PROPOSED SOLUTION: [refined one-sentence solution]\n"
                    f"ASSUMPTIONS:\n"
                    f"  1. [key assumption]\n"
                    f"  2. [key assumption]\n"
                    f"  3. [key assumption]"
                )
                task = Task(
                    description=force_desc,
                    expected_output="IDEA STRUCTURE COMPLETE block.",
                    agent=idea_structuring_agent,
                )
                report = run_agent(task, idea_structuring_agent)
                self.state.idea_struct_history.append({"role": "agent", "content": report})
                break

            agent_says(report)
            answer = ask(ui["prompts"]["response_input"])
            self.state.idea_struct_history.append({"role": "user", "content": answer})

            last_q, last_a = last_exchange(self.state.idea_struct_history)
            rep_warn       = repetition_warning("", last_q)
            history_text   = format_history(self.state.idea_struct_history)
            remaining      = MAX_IDEA_STRUCT_QUESTIONS - self.state.idea_struct_questions

            description = (
                f"{rep_warn}\n"
                f"QUESTION YOU ASKED: {last_q}\n"
                f"STUDENT'S ANSWER:   {last_a}\n"
                f"RAW PROBLEM: {raw_problem}\n"
                f"RAW SOLUTION: {self.state.raw_solution}\n"
                f"CONVERSATION SO FAR:\n{history_text}\n\n"
                f"REMAINING QUESTION BUDGET: {remaining}\n\n"
                + (
                    "All budget used. Output the final structure NOW.\n"
                    if remaining <= 0 else
                    f"If you have enough to produce a complete structure, do so. "
                    f"Otherwise ask ONE more clarifying question (budget: {remaining} left).\n"
                )
                + "\nFORMAT if more clarification needed:\n"
                  "COACHING: [brief acknowledgment]\n"
                  "QUESTION:\n[single focused question]\n\n"
                  "FORMAT if ready to produce structure:\n"
                  "IDEA STRUCTURE COMPLETE\n"
                  "CUSTOMER SEGMENT: [specific group]\n"
                  "QUALIFIED PROBLEM: [precise problem statement]\n"
                  "CONSEQUENCE: [impact of not solving]\n"
                  "PROPOSED SOLUTION: [refined solution]\n"
                  "ASSUMPTIONS:\n"
                  "  1. [assumption]\n"
                  "  2. [assumption]\n"
                  "  3. [assumption]"
            )

            task = Task(
                description=description,
                expected_output="Coaching + question OR complete idea structure.",
                agent=idea_structuring_agent,
            )

            report = run_agent(task, idea_structuring_agent)
            self.state.idea_struct_history.append({"role": "agent", "content": report})

            if not idea_struct_complete(report):
                self.state.idea_struct_questions += 1

        # Parse structured output
        self.state.refined_idea    = _parse_refined_idea(report)
        self.state.idea_struct_status = "COMPLETE"

        section("STRUCTURED PROBLEM DEFINITION")
        ri = self.state.refined_idea
        print(f"  Customer Segment  : {ri.customer_segment}")
        print(f"  Problem           : {ri.qualified_problem}")
        print(f"  Consequence       : {ri.consequence}")
        print(f"  Solution          : {ri.proposed_solution}")
        if ri.assumptions:
            print("  Assumptions       :")
            for a in ri.assumptions:
                print(f"    • {a}")
        hr("-")
        return raw_problem

    # ── MARKET SCAN ────────────────────────────────────────────

    @listen(idea_structuring_loop)
    def market_scan(self, problem):
        ms = ui["market_scout"]
        section(ms["header"])
        print(ms["running"])

        web_ctx     = market_scout_search_context(self.state.refined_idea.qualified_problem or problem)
        description = (
            f"PROBLEM: {self.state.refined_idea.qualified_problem or problem}\n"
            f"CUSTOMER: {self.state.refined_idea.customer_segment}\n\n"
            f"WEB CONTEXT:\n{web_ctx}\n\n"
            f"{scout_skill}\n\n"
            "Produce a competitive landscape report. End with VERDICT: REJECT or VERDICT: PROCEED."
        )

        scan_task = Task(
            description=description,
            expected_output="Competitive landscape report with VERDICT.",
            agent=market_scout_agent,
        )

        result = run_agent(scan_task, market_scout_agent)
        self.state.market_report = result

        verdict = "PROCEED"
        if "VERDICT: REJECT" in result.upper():
            verdict = "REJECT"
        self.state.market_verdict = verdict

        if verdict == "REJECT":
            section(ms["section_reject"])
            print(result)
            hr("-")
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

        print()
        print("=" * W)
        print(f"  {ms['creative_header']}")
        print("=" * W)
        angle = ask()
        self.state.market_angle = angle
        return problem

    # ── PHASE 1 — TIPS COACHING LOOP ──────────────────────────

    @listen(market_scan)
    def tips_coaching_loop(self, problem):
        """
        TIPS Agent evaluates Timely, Important, Profitable, Solvable.
        C (Contextual) is skipped per mentor instruction.
        Uses Green/Yellow/Red scoring rules.
        Max 3 questions, then issues final TIPS verdict.
        """
        section("PHASE 1 — TIPS EVALUATION")
        print("  Evaluating Timely, Important, Profitable, Solvable (C skipped).\n")

        ri    = self.state.refined_idea
        prior = _recall_similar(ri.qualified_problem, VALIDATION_SCOPE)

        # Initial TIPS assessment
        description = (
            f"STRUCTURED PROBLEM:\n"
            f"  Customer Segment  : {ri.customer_segment}\n"
            f"  Qualified Problem : {ri.qualified_problem}\n"
            f"  Consequence       : {ri.consequence}\n"
            f"  Proposed Solution : {ri.proposed_solution}\n"
            f"  Assumptions       : {'; '.join(ri.assumptions)}\n\n"
            f"Market Angle: {self.state.market_angle}\n\n"
            f"{prior}\n"
            f"{tips_skill}\n\n"
            f"QUESTION BUDGET: {MAX_TIPS_QUESTIONS} questions total. "
            f"Do NOT evaluate C (Contextual).\n\n"
            f"Perform initial TIPS assessment and ask ONE question about the weakest criterion.\n\n"
            f"SCORING RULES:\n"
            f"T — Timely: time horizon ≤1yr AND growing urgency → Green; >1yr or hazy → Yellow; no urgency → Red\n"
            f"I — Important: Must-Have + ≤1yr → Green; Should-Have + ≤1yr → Green; Should-Have + >1yr → Yellow; Nice-to-Have → Red\n"
            f"P — Profitable: Customers willing to pay directly (Y) → Green; Indirect/B2B2C → Yellow; No willingness → Red\n"
            f"S — Solvable: Team has skills + resources → Green; Partial → Yellow; No capability → Red\n\n"
            f"FORMAT:\n"
            f"TIPS ASSESSMENT:\n"
            f"TIMELY:        [Green/Yellow/Red] — [explanation with time horizon]\n"
            f"IMPORTANCE:    [Green/Yellow/Red] — [Must-Have/Should-Have/Nice-to-Have + rationale]\n"
            f"PROFITABILITY: [Green/Yellow/Red] — [willing to pay Y/N + monetization model]\n"
            f"SOLVABILITY:   [Green/Yellow/Red] — [team skills + resources assessment]\n\n"
            f"WEAKEST CRITERION: [letter]\n"
            f"COACHING NOTE: [what needs to be strengthened]\n"
            f"QUESTION:\n[single coaching question about the weakest criterion]"
        )

        task = Task(
            description=description,
            expected_output="TIPS assessment table and coaching question.",
            agent=tips_agent,
        )

        report = run_agent(task, tips_agent)
        self.state.tips_history.append({"role": "agent", "content": report})
        self.state.tips_questions = 1

        while not tips_complete(report):

            if self.state.tips_questions >= MAX_TIPS_QUESTIONS:
                print(f"\n  [System] TIPS budget ({MAX_TIPS_QUESTIONS}) reached. Generating final TIPS output.\n")
                force_desc = (
                    f"You have asked {MAX_TIPS_QUESTIONS} questions. "
                    f"MANDATORY: output the final TIPS verdict now. "
                    f"Use all information gathered. Do NOT ask more questions.\n\n"
                    f"STRUCTURED PROBLEM:\n"
                    f"  Customer: {ri.customer_segment}\n"
                    f"  Problem:  {ri.qualified_problem}\n"
                    f"  Consequence: {ri.consequence}\n\n"
                    f"CONVERSATION:\n{format_history(self.state.tips_history)}\n\n"
                    f"OUTPUT FORMAT:\n"
                    f"TIPS ASSESSMENT:\n"
                    f"TIMELY:        [Green/Yellow/Red] — [explanation]\n"
                    f"IMPORTANCE:    [Green/Yellow/Red] — [explanation]\n"
                    f"PROFITABILITY: [Green/Yellow/Red] — [explanation with monetization model]\n"
                    f"SOLVABILITY:   [Green/Yellow/Red] — [explanation with team assessment]\n\n"
                    f"OVERALL TIPS STRENGTH: [Strong/Moderate/Weak]\n"
                    f"TIPS VERDICT: COMPLETE\n"
                    f"COACHING SUMMARY: [2-3 sentences on what the team should focus on before DFV]"
                )
                task = Task(
                    description=force_desc,
                    expected_output="Final TIPS verdict.",
                    agent=tips_agent,
                )
                report = run_agent(task, tips_agent)
                self.state.tips_history.append({"role": "agent", "content": report})
                break

            agent_says(report)
            answer = ask(ui["prompts"]["response_input"])
            self.state.tips_history.append({"role": "user", "content": answer})

            last_q, last_a = last_exchange(self.state.tips_history)
            history_text   = format_history(self.state.tips_history)
            remaining      = MAX_TIPS_QUESTIONS - self.state.tips_questions

            description = (
                f"QUESTION YOU ASKED: {last_q}\n"
                f"STUDENT'S ANSWER:   {last_a}\n\n"
                f"STRUCTURED PROBLEM:\n"
                f"  Customer: {ri.customer_segment}\n"
                f"  Problem:  {ri.qualified_problem}\n"
                f"  Consequence: {ri.consequence}\n\n"
                f"CONVERSATION:\n{history_text}\n\n"
                f"REMAINING BUDGET: {remaining} question(s).\n"
                + (
                    "Budget exhausted. Issue TIPS VERDICT: COMPLETE now.\n"
                    if remaining <= 0 else
                    f"If all four TIPS criteria are sufficiently clear, issue verdict. "
                    f"Otherwise ask ONE more question (budget: {remaining} left).\n"
                )
                + "\nFORMAT if coaching needed:\n"
                  "TIPS ASSESSMENT: [updated ratings]\n"
                  "TIMELY:        [Green/Yellow/Red] — [explanation]\n"
                  "IMPORTANCE:    [Green/Yellow/Red] — [explanation]\n"
                  "PROFITABILITY: [Green/Yellow/Red] — [explanation]\n"
                  "SOLVABILITY:   [Green/Yellow/Red] — [explanation]\n\n"
                  "COACHING NOTE: [what to improve]\n"
                  "QUESTION:\n[single coaching question]\n\n"
                  "FORMAT if complete:\n"
                  "TIPS ASSESSMENT: [final ratings]\n"
                  "TIMELY:        [Green/Yellow/Red] — [explanation]\n"
                  "IMPORTANCE:    [Green/Yellow/Red] — [explanation]\n"
                  "PROFITABILITY: [Green/Yellow/Red] — [explanation]\n"
                  "SOLVABILITY:   [Green/Yellow/Red] — [explanation]\n\n"
                  "OVERALL TIPS STRENGTH: [Strong/Moderate/Weak]\n"
                  "TIPS VERDICT: COMPLETE\n"
                  "COACHING SUMMARY: [2-3 sentences]"
            )

            task = Task(
                description=description,
                expected_output="Updated TIPS assessment + question or verdict.",
                agent=tips_agent,
            )

            report = run_agent(task, tips_agent)
            self.state.tips_history.append({"role": "agent", "content": report})

            if not tips_complete(report):
                self.state.tips_questions += 1

        self.state.tips_metrics = _parse_tips_metrics(report)
        self.state.tips_report  = report
        self.state.tips_status  = "COMPLETE"

        section("TIPS EVALUATION COMPLETE")
        tm = self.state.tips_metrics
        print(f"  T — Timely       : {tm.timely_rating or '?'}  — {tm.timely_factor[:80]}")
        print(f"  I — Important    : {tm.importance_rating or '?'}  — {tm.importance_metric[:80]}")
        print(f"  P — Profitable   : {tm.profitability_rating or '?'}  — {tm.profitability_pivot[:80]}")
        print(f"  S — Solvable     : {tm.solvability_rating or '?'}  — {tm.solvability_constraint[:80]}")
        print(f"  C — Contextual   : SKIPPED (per mentor instruction)")
        hr("-")

        return self.state.refined_idea.proposed_solution or self.state.raw_solution

    # ── PHASE 2 — PSEA EVALUATION ─────────────────────────────

    @listen(tips_coaching_loop)
    def evaluate_idea(self, solution):
        section(ui["phases"]["idea_header"])
        print(ui["phases"]["idea_running"])

        ri = self.state.refined_idea

        description = (
            f"VALIDATED PROBLEM: {ri.qualified_problem}\n"
            f"CUSTOMER SEGMENT: {ri.customer_segment}\n"
            f"CONSEQUENCE: {ri.consequence}\n"
            f"PROPOSED SOLUTION: {solution}\n"
            f"ASSUMPTIONS: {'; '.join(ri.assumptions)}\n\n"
            f"TIPS STRENGTH: T={self.state.tips_metrics.timely_rating} "
            f"I={self.state.tips_metrics.importance_rating} "
            f"P={self.state.tips_metrics.profitability_rating} "
            f"S={self.state.tips_metrics.solvability_rating}\n\n"
            f"{idea_eval_skill}\n\n"
            f"QUESTION BUDGET: {MAX_PSEA_QUESTIONS} questions total.\n\n"
            f"Evaluate using PSEA + initial feasibility.\n\n"
            f"FORMAT:\n"
            f"PSEA EVALUATION:\n"
            f"Problem-Solution Fit: [Strong/Weak/Unclear] — [explanation]\n"
            f"Simplicity:           [Good/Over-engineered/Unclear] — [explanation]\n"
            f"Ethics:               [Pass/Concern/Fail] — [explanation]\n"
            f"Key Assumptions:\n"
            f"  1. [assumption]\n"
            f"Initial Feasibility:  [Viable/Questionable/Infeasible] — [explanation]\n\n"
            f"VERDICT: NEEDS_REFINEMENT\nISSUES: ...\nQUESTION:\n[single question]\n"
            f"  OR\n"
            f"VERDICT: READY_FOR_DFV\nEVALUATION SUMMARY: ...\nNEXT STEP: DFV Evaluation"
        )

        eval_task = Task(
            description=description,
            expected_output="PSEA evaluation with verdict.",
            agent=idea_eval_agent,
        )

        result = run_agent(eval_task, idea_eval_agent)
        self.state.idea_history.append({"role": "agent", "content": result})
        self.state.idea_report  = result
        self.state.idea_status  = "IN_PROGRESS"
        self.state.idea_questions = 1
        return result

    # ── PHASE 2 — PSEA LOOP ────────────────────────────────────

    @listen(evaluate_idea)
    def idea_loop(self, eval_result):
        report = eval_result

        while not idea_ready(report):
            if self.state.idea_questions >= MAX_PSEA_QUESTIONS:
                print(f"\n  [System] PSEA budget ({MAX_PSEA_QUESTIONS}) reached. Forcing verdict.\n")
                force_desc = (
                    f"MANDATORY: Issue VERDICT: READY_FOR_DFV now. "
                    f"Problem: {self.state.refined_idea.qualified_problem}\n"
                    f"Solution: {self.state.refined_idea.proposed_solution}\n"
                    f"CONVERSATION:\n{format_history(self.state.idea_history)}"
                )
                task = Task(
                    description=force_desc,
                    expected_output="VERDICT: READY_FOR_DFV",
                    agent=idea_eval_agent,
                )
                report = run_agent(task, idea_eval_agent)
                self.state.idea_history.append({"role": "agent", "content": report})
                break

            agent_says(report)
            answer = ask(ui["prompts"]["response_input"])
            self.state.idea_history.append({"role": "user", "content": answer})

            last_q, last_a = last_exchange(self.state.idea_history)
            rep_warning    = repetition_warning(self.state.idea_last_q, last_q)
            history_text   = format_history(self.state.idea_history)
            remaining      = MAX_PSEA_QUESTIONS - self.state.idea_questions

            description = (
                f"{rep_warning}\n"
                f"QUESTION: {last_q}\nANSWER: {last_a}\n"
                f"PROBLEM: {self.state.refined_idea.qualified_problem}\n"
                f"SOLUTION: {self.state.refined_idea.proposed_solution}\n"
                f"HISTORY:\n{history_text}\n\n"
                f"REMAINING BUDGET: {remaining}\n"
                + (
                    "Budget exhausted. Issue VERDICT: READY_FOR_DFV now.\n"
                    if remaining <= 0 else
                    f"If all PSEA criteria are clear, approve. Otherwise ask ONE more question.\n"
                )
                + "\nFORMAT if refinement needed:\n"
                  "FEEDBACK: ...\nISSUE IN FOCUS: [criterion]\n"
                  "VERDICT: NEEDS_REFINEMENT\nQUESTION:\n[single question]\n\n"
                  "FORMAT if approved:\n"
                  "FEEDBACK: ...\nVERDICT: READY_FOR_DFV\n"
                  "EVALUATION SUMMARY: ...\nNEXT STEP: DFV Evaluation"
            )

            task = Task(
                description=description,
                expected_output="Feedback + question or DFV clearance.",
                agent=idea_eval_agent,
            )

            report = run_agent(task, idea_eval_agent)
            self.state.idea_last_q = extract_question(report)
            self.state.idea_history.append({"role": "agent", "content": report})
            self.state.idea_report = report

            if not idea_ready(report):
                self.state.idea_questions += 1

        self.state.idea_status = "READY_FOR_DFV"
        return report

    # ── FINAL REPORT ───────────────────────────────────────────

    @listen(idea_loop)
    def final_report(self, idea_result):
        section("VALIDATION COMPLETE — FINAL OUTPUT")

        final_json = _build_final_json(self.state)

        hr("-"); print("  STRUCTURED OUTPUT (JSON):"); hr("-")
        print(json.dumps(final_json, indent=2))
        hr()

        # Save JSON to file
        out_path = BASE_DIR / "validation_output.json"
        out_path.write_text(json.dumps(final_json, indent=2), encoding="utf-8")
        print(f"\n  [Saved] {out_path}\n")

        _save_validation_result(self.state)
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