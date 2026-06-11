"""
Entrepreneurial Opportunity Validation System  — v3 (Mentor Revision)
======================================================================

Changes from memory edition:
  1. COP removed entirely — no more Capability/Opportunity/Passion probing
  2. Max 3 questions total across the entire opportunity evaluation
     (not per-criterion) before forcing a verdict
  3. Problem AND idea collected upfront at the start, before any agent runs
  4. Agents use SKILL.md prompt templates instead of YAML files
     (see skills/opportunity_agent/SKILL.md etc.)
  5. Memory retained from previous version (scoped per agent, LanceDB local)

INSTALL:
  pip install requests pyyaml lancedb sentence-transformers crewai "crewai[tools]"
"""

import os
import sys
from pathlib import Path

# ── Fix Windows console Unicode encoding (cp1252 → utf-8) ──────
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests
import yaml
from datetime import datetime
import time
from typing import List
from pydantic import BaseModel
from crewai import Agent, Task, Crew, Process, LLM, Memory
from crewai.flow.flow import Flow, listen, start
from crewai_tools import TavilySearchTool


# ══════════════════════════════════════════════════════════════
# LOAD CONFIGURATION
# ══════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent


def _load_yaml(rel_path: str) -> dict:
    with open(BASE_DIR / rel_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _load_skill(skill_name: str) -> str:
    """
    Load a SKILL.md from the skills/ folder and return the body text
    (everything after the YAML frontmatter).

    The frontmatter (--- ... ---) is stripped so only the markdown
    instructions reach the agent's goal/backstory/task description.
    """
    path = BASE_DIR / "skills" / skill_name / "SKILL.md"
    text = path.read_text(encoding="utf-8")

    # Strip YAML frontmatter if present
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

# ── v3 change: hard cap of 3 questions across the whole opp eval ──
MAX_OPP_QUESTIONS = 3          # was per-criterion; now total
W                 = _display["console_width"]


# ══════════════════════════════════════════════════════════════
# LLM
# ══════════════════════════════════════════════════════════════

llm_cfg = cfg["llm"]
llm = LLM(
    model=llm_cfg["model"],
    base_url=llm_cfg["base_url"],
    api_key=llm_cfg["api_key"],
)

search_tool   = TavilySearchTool()
agent_tools   = [search_tool]

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
# MEMORY SETUP  (unchanged from memory edition)
# ══════════════════════════════════════════════════════════════

shared_memory = Memory(
    llm=llm,                              # use local LLM instance, not model string
    storage=str(BASE_DIR / ".crewai" / "memory"),
    embedder={
        "provider": "sentence-transformer",
        "config": {"model_name": "all-MiniLM-L6-v2"},
    },
    recency_weight=0.2,
    semantic_weight=0.4,
    importance_weight=0.4,
    recency_half_life_days=60,
    consolidation_threshold=1.0,          # disables LLM consolidation (local model can't do strict JSON)
    query_analysis_threshold=99999,       # skips LLM query analysis entirely
)

opp_memory   = shared_memory.scope("/agent/opportunity")
idea_memory  = shared_memory.scope("/agent/idea")
scout_memory = shared_memory.scope("/agent/market_scout")
VALIDATION_SCOPE = "/validations"


# ══════════════════════════════════════════════════════════════
# STATE MODEL
# ══════════════════════════════════════════════════════════════

class ValidationState(BaseModel):
    # ── v3: both collected upfront ─────────────────────────────
    problem:  str = ""
    solution: str = ""          # collected at START, not after opp approval

    # Market Scout
    market_verdict: str = ""
    market_report:  str = ""
    market_angle:   str = ""

    # Opportunity evaluation
    opp_history:        List[dict] = []
    opp_status:         str        = "PENDING"
    opp_report:         str        = ""
    opp_last_q:         str        = ""
    opp_questions_asked: int       = 0   # v3: total questions counter (max 3)
    # NOTE: criterion_turns removed — COP removed, max-3 replaces per-criterion cap

    # Idea evaluation
    idea_history: List[dict] = []
    idea_status:  str        = "PENDING"
    idea_report:  str        = ""
    idea_last_q:  str        = ""


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
        last_q   = text.rfind("?")
        before   = text[:last_q]
        start    = max(before.rfind("."), before.rfind(":"), before.rfind("\n"))
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

def format_history(history: List[dict]) -> str:
    if not history:
        return "(No prior conversation.)"
    verdict_lines = []
    for turn in history:
        if turn["role"] != "agent":
            continue
        for line in turn["content"].split("\n"):
            l = line.strip()
            if any(l.startswith(f"{c} —") or l.startswith(f"{c}:") for c in "TIPSC"):
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

def run_agent(task: Task, agent: Agent, retries: int = 3, retry_delay: float = 5.0) -> str:
    """Run an agent task with retry logic for transient LM Studio errors."""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            crew = Crew(
                agents=[agent],
                tasks=[task],
                process=Process.sequential,
                verbose=False,
                # memory removed — Crew only accepts True/False, not a Memory instance
            )
            raw = str(crew.kickoff())
            return clean_agent_output(raw)
        except Exception as e:
            last_error = e
            err_str = str(e)
            # Retry on transient LM Studio model-reload errors
            if "Model reloaded" in err_str or "context_length_exceeded" in err_str:
                if attempt < retries:
                    print(f"\n  [Retry {attempt}/{retries}] LM Studio model reloaded — retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                    continue
            # Non-retryable or exhausted retries
            raise
    raise last_error

_RESPONSE_MARKERS = [
    "VERDICT: REJECT","VERDICT: PROCEED","TIPSC TRIAGE:",
    "SEARCH FINDINGS:","PSEA EVALUATION:","FEEDBACK:",
    "STATUS: APPROVED","STATUS: NEEDS_MORE_INFO",
    "VERDICT: READY_FOR_DFV","VERDICT: NEEDS_REFINEMENT",
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
        return ui["prompts"].get("repetition_warning", ui.get("repetition_warning", ""))
    return ""

def opp_approved(text: str) -> bool:
    return "STATUS: APPROVED" in text.upper()

def idea_ready(text: str) -> bool:
    return "VERDICT: READY_FOR_DFV" in text.upper()


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
            title   = item.get("title","").strip()
            snippet = item.get("snippet","").strip()
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
         f"{short} market size OR growth rate {yr}",
         f"{short} new startup launch OR recent entrant {yr}"],
        label="Competitive Landscape",
    )


# ══════════════════════════════════════════════════════════════
# MEMORY HELPERS  (unchanged from memory edition)
# ══════════════════════════════════════════════════════════════

def _recall_similar_problems(problem: str) -> str:
    try:
        matches = shared_memory.recall(
            f"problem validation result: {problem}",
            scope=VALIDATION_SCOPE, limit=3, depth="shallow",
        )
        if not matches:
            return ""
        lines = ["\nPRIOR VALIDATION MEMORY (similar problems evaluated before):"]
        for m in matches:
            lines.append(f"  [{m.score:.2f}] {m.record.content}")
        lines.append("")
        return "\n".join(lines)
    except Exception:
        return ""

def _recall_similar_solutions(problem: str, solution: str) -> str:
    try:
        matches = shared_memory.recall(
            f"solution evaluation: {solution} for problem: {problem}",
            scope=VALIDATION_SCOPE, limit=3, depth="shallow",
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

def _save_validation_result(problem, solution, opp_report, idea_report):
    try:
        opp_facts  = shared_memory.extract_memories(opp_report)
        idea_facts = shared_memory.extract_memories(idea_report)
        summary = (
            f"VALIDATED OPPORTUNITY — Problem: {problem[:120]} | "
            f"Solution: {solution[:120]} | "
            f"Opportunity: APPROVED | Idea: READY_FOR_DFV"
        )
        shared_memory.remember(summary, scope=VALIDATION_SCOPE)
        for fact in opp_facts:
            shared_memory.remember(fact, scope=VALIDATION_SCOPE + "/opportunity")
        for fact in idea_facts:
            shared_memory.remember(fact, scope=VALIDATION_SCOPE + "/idea")
        print(f"\n  [Memory] Saved {len(opp_facts)+len(idea_facts)+1} records from this session.")
    except Exception as e:
        print(f"\n  [Memory] Save skipped: {e}")

def _save_rejection(problem, reason):
    try:
        shared_memory.remember(
            f"REJECTED PROBLEM — {problem[:150]} — Reason: {reason[:200]}",
            scope=VALIDATION_SCOPE + "/rejections",
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
# SKILL.md LOADER  — reads agent instructions from skills/ folder
# ══════════════════════════════════════════════════════════════
#
# Instead of YAML prompt files, each agent now has a SKILL.md in:
#   skills/
#     opportunity_agent/SKILL.md
#     idea_agent/SKILL.md
#     market_scout_agent/SKILL.md
#
# The SKILL.md body becomes the agent's goal. This makes prompts
# version-controllable, human-readable, and easy to iterate on
# without touching Python code.

opp_skill   = _load_skill("opportunity_agent")
idea_skill  = _load_skill("idea_agent")
scout_skill = _load_skill("market_scout_agent")


# ══════════════════════════════════════════════════════════════
# AGENTS  — goals now come from SKILL.md files
# ══════════════════════════════════════════════════════════════

opportunity_agent = Agent(
    role="Opportunity Evaluation Agent",
    goal=opp_skill,          # ← from skills/opportunity_agent/SKILL.md
    backstory=(
        "Veteran startup mentor with 20 years experience stress-testing "
        "business ideas. You are direct, structured, and time-efficient. "
        "You never waste a founder's time with unnecessary questions."
    ),
    verbose=False,
    tools=agent_tools,
    llm=llm,
    memory=False,           # agent-level memory disabled (local model can't produce valid JSON for analysis)
)

idea_agent = Agent(
    role="Idea Evaluation Agent",
    goal=idea_skill,         # ← from skills/idea_agent/SKILL.md
    backstory=(
        "Startup investor and product strategist. You spot critical flaws "
        "quickly and help founders refine ideas to be simple, ethical, and "
        "fundable. You focus on PSEA criteria only."
    ),
    verbose=False,
    tools=agent_tools,
    llm=llm,
    memory=False,           # same — re-enable when switching to Groq/GPT-4o
)

market_scout_agent = Agent(
    role="Market Scout Agent",
    goal=scout_skill,        # ← from skills/market_scout_agent/SKILL.md
    backstory=(
        "Competitive intelligence analyst. You map existing solutions, "
        "funding levels, and market gaps before the founder wastes time "
        "building something that already exists."
    ),
    verbose=False,
    tools=[],
    llm=llm,
    memory=False,           # same
)


# ══════════════════════════════════════════════════════════════
# VALIDATION FLOW
# ══════════════════════════════════════════════════════════════

class ValidationFlow(Flow[ValidationState]):

    # ── STEP 1 — Collect Problem AND Idea upfront (v3 change) ─

    @start()
    def collect_inputs(self):
        section(ui["section_titles"]["main"])
        print(ui["startup"]["intro"])
        hr()

        # v3: ask for both upfront
        print("\n  STEP 1 of 2 — Describe the PROBLEM you want to solve.")
        print("  Be specific: who has this problem, and why don't existing solutions work?\n")
        self.state.problem = ask(ui["prompts"]["problem_input"])

        print("\n  STEP 2 of 2 — Describe your proposed SOLUTION / IDEA.")
        print("  This will be evaluated after the opportunity is validated.\n")
        self.state.solution = ask("Describe your idea or proposed solution:")

        return self.state.problem

    # ── STEP 2 — Market Intelligence Scan ─────────────────────

    @listen(collect_inputs)
    def market_scan(self, problem):
        ms = ui["market_scout"]
        section(ms["header"])
        print(ms["running"])

        prior_memory = _recall_similar_problems(problem)
        if prior_memory:
            print("\n  [Memory] Found similar past validations.\n")

        web_ctx     = market_scout_search_context(problem)
        description = (
            f"PROBLEM: {problem}\n\n"
            f"{prior_memory}\n"
            f"WEB CONTEXT:\n{web_ctx}\n\n"
            f"{scout_skill}\n\n"
            "Produce a competitive landscape report. "
            "End with VERDICT: REJECT or VERDICT: PROCEED."
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
        self.state.market_verdict = verdict

        if verdict == "REJECT":
            section(ms["section_reject"])
            print(result)
            hr("-")
            print(f"\n  {ms['reject_banner']}\n")
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

    # ── STEP 3 — TIPSC Triage (first pass) ────────────────────

    @listen(market_scan)
    def tipsc_triage(self, problem):
        section(ui["phases"]["opportunity_header"])
        print(ui["phases"]["opportunity_running"])

        prior_opp = _recall_similar_problems(problem)
        if prior_opp:
            print("\n  [Memory] Recalling similar past evaluations.\n")

        angle_block = ""
        if self.state.market_angle:
            angle_block = (
                f"\nFOUNDER'S DIFFERENTIATION ANGLE:\n{self.state.market_angle}\n"
            )

        # v3: tell agent the question budget upfront
        budget_note = (
            f"\nIMPORTANT: You have a maximum of {MAX_OPP_QUESTIONS} questions "
            f"total to validate this opportunity. Choose your questions wisely — "
            f"target the weakest TIPSC criteria only. Do NOT probe COP.\n"
        )

        description = (
            f"PROBLEM: {problem}\n"
            f"{budget_note}"
            f"{angle_block}"
            f"{prior_opp}\n"
            f"Using your TIPSC framework (T/I/P/S/C only — no COP), "
            f"triage the problem and ask your FIRST question targeting the weakest criterion.\n\n"
            f"FORMAT:\n"
            f"TIPSC TRIAGE:\n"
            f"T — Timely:     [Strong/Weak/Unclear] — [one sentence]\n"
            f"I — Important:  [Strong/Weak/Unclear] — [one sentence]\n"
            f"P — Profitable: [Strong/Weak/Unclear] — [one sentence]\n"
            f"S — Solvable:   [Strong/Weak/Unclear] — [one sentence]\n"
            f"C — Contextual: [Strong/Weak/Unclear] — [one sentence]\n\n"
            f"STATUS: NEEDS_MORE_INFO\n"
            f"CRITERION IN FOCUS: [letter]\n"
            f"QUESTION:\n[single focused question]"
        )

        triage_task = Task(
            description=description,
            expected_output="TIPSC triage table and opening question.",
            agent=opportunity_agent,
        )

        result = run_agent(triage_task, opportunity_agent)
        self.state.opp_history.append({"role": "agent", "content": result})
        self.state.opp_report  = result
        self.state.opp_status  = "IN_PROGRESS"
        self.state.opp_questions_asked = 1
        return result

    # ── STEP 4 — Opportunity Loop (max 3 questions total) ─────

    @listen(tipsc_triage)
    def opportunity_loop(self, triage_result):
        report = triage_result

        while not opp_approved(report):

            # ── Hard cap: force approval after MAX_OPP_QUESTIONS ──
            if self.state.opp_questions_asked >= MAX_OPP_QUESTIONS:
                print(
                    f"\n  [System] Question budget ({MAX_OPP_QUESTIONS}) reached. "
                    f"Forcing opportunity verdict now.\n"
                )
                force_desc = (
                    f"You have now asked {MAX_OPP_QUESTIONS} questions. "
                    f"MANDATORY: issue STATUS: APPROVED now with a summary of "
                    f"what was validated. Do not ask any more questions.\n\n"
                    f"PROBLEM: {self.state.problem}\n"
                    f"CONVERSATION SO FAR:\n{format_history(self.state.opp_history)}"
                )
                force_task = Task(
                    description=force_desc,
                    expected_output="STATUS: APPROVED with summary.",
                    agent=opportunity_agent,
                )
                report = run_agent(force_task, opportunity_agent)
                self.state.opp_history.append({"role": "agent", "content": report})
                self.state.opp_report = report
                break

            agent_says(report)

            answer = ask(ui["prompts"]["response_input"])
            self.state.opp_history.append({"role": "user", "content": answer})

            last_q, last_a = last_exchange(self.state.opp_history)
            rep_warning    = repetition_warning(self.state.opp_last_q, last_q)
            history_text   = format_history(self.state.opp_history)
            remaining      = MAX_OPP_QUESTIONS - self.state.opp_questions_asked

            description = (
                f"{rep_warning}\n"
                f"QUESTION YOU ASKED: {last_q}\n"
                f"FOUNDER'S ANSWER:   {last_a}\n"
                f"PROBLEM: {self.state.problem}\n"
                f"CONVERSATION:\n{history_text}\n\n"
                f"REMAINING QUESTION BUDGET: {remaining} question(s) left.\n"
                + (
                    "You have used all your questions. Issue STATUS: APPROVED now.\n"
                    if remaining <= 0 else
                    f"If the opportunity is sufficiently clear, issue STATUS: APPROVED. "
                    f"Otherwise ask ONE more targeted question (budget: {remaining} left). "
                    f"Do NOT probe COP (Capability/Opportunity/Passion).\n"
                )
                + "\nFORMAT if more info needed:\n"
                  "FEEDBACK: [2-3 sentences]\n"
                  "CRITERION IN FOCUS: [letter T/I/P/S/C]\n"
                  "STATUS: NEEDS_MORE_INFO\n"
                  "QUESTION: [single question]\n\n"
                  "FORMAT if approving:\n"
                  "FEEDBACK: [acknowledgment]\n"
                  "STATUS: APPROVED\n"
                  "SUMMARY: [concise opportunity summary with TIPSC verdicts]"
            )

            followup_task = Task(
                description=description,
                expected_output="Feedback + next question or approval.",
                agent=opportunity_agent,
            )

            report = run_agent(followup_task, opportunity_agent)
            self.state.opp_last_q = extract_question(report)
            self.state.opp_history.append({"role": "agent", "content": report})
            self.state.opp_report = report

            if not opp_approved(report):
                self.state.opp_questions_asked += 1

        self.state.opp_status = "APPROVED"

        section(ui["section_titles"]["opp_valid"])
        print(report)
        hr("-")
        print(f"\n  Opportunity validated! Moving to idea evaluation...\n")
        hr("-")

        # v3: solution was already collected at the start
        return self.state.solution

    # ── STEP 5 — Initial PSEA Evaluation ──────────────────────

    @listen(opportunity_loop)
    def evaluate_idea(self, solution):
        section(ui["phases"]["idea_header"])
        print(ui["phases"]["idea_running"])

        prior_idea = _recall_similar_solutions(self.state.problem, solution)
        if prior_idea:
            print("\n  [Memory] Recalling similar past solution evaluations.\n")

        description = (
            f"VALIDATED PROBLEM: {self.state.problem}\n"
            f"PROPOSED SOLUTION: {solution}\n\n"
            f"{prior_idea}\n"
            f"Evaluate using PSEA + initial feasibility check.\n\n"
            f"FORMAT:\n"
            f"PSEA EVALUATION:\n"
            f"Problem-Solution Fit: [Strong/Weak/Unclear] — [explanation]\n"
            f"Simplicity:           [Good/Over-engineered/Unclear] — [explanation]\n"
            f"Ethics:               [Pass/Concern/Fail] — [explanation]\n"
            f"Key Assumptions:\n"
            f"  1. [assumption]\n"
            f"Initial Feasibility:  [Viable/Questionable/Infeasible] — [explanation]\n\n"
            f"If critical issues:\n"
            f"VERDICT: NEEDS_REFINEMENT\n"
            f"ISSUES: [bullet list]\n"
            f"QUESTION: [single question]\n\n"
            f"If acceptable:\n"
            f"VERDICT: READY_FOR_DFV\n"
            f"EVALUATION SUMMARY: [full verdicts]\n"
            f"NEXT STEP: DFV Evaluation"
        )

        eval_task = Task(
            description=description,
            expected_output="PSEA evaluation with verdict.",
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

            description = (
                f"{rep_warning}\n"
                f"QUESTION: {last_q}\n"
                f"ANSWER:   {last_a}\n"
                f"PROBLEM:  {self.state.problem}\n"
                f"SOLUTION: {self.state.solution}\n"
                f"HISTORY:\n{history_text}\n\n"
                f"Evaluate and decide. If all PSEA criteria met, approve.\n\n"
                f"FORMAT if refinement needed:\n"
                f"FEEDBACK: [2-3 sentences]\n"
                f"ISSUE IN FOCUS: [PSEA criterion]\n"
                f"VERDICT: NEEDS_REFINEMENT\n"
                f"QUESTION: [single question]\n\n"
                f"FORMAT if approved:\n"
                f"FEEDBACK: [acknowledgment]\n"
                f"VERDICT: READY_FOR_DFV\n"
                f"EVALUATION SUMMARY: [full PSEA verdicts]\n"
                f"NEXT STEP: DFV Evaluation"
            )

            refinement_task = Task(
                description=description,
                expected_output="Feedback + next question or DFV clearance.",
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

        hr("-"); print(f"  {labels['problem']}"); hr("-")
        print(f"  {self.state.problem}"); print()

        hr("-"); print(f"  MARKET INTELLIGENCE"); hr("-")
        print(f"  Verdict: {self.state.market_verdict}")
        if self.state.market_angle:
            print(f"  Differentiation angle: {self.state.market_angle}")
        print()

        hr("-"); print(f"  {labels['opportunity']}"); hr("-")
        print(self.state.opp_report); print()

        hr("-"); print(f"  {labels['solution']}"); hr("-")
        print(f"  {self.state.solution}"); print()

        hr("-"); print(f"  {labels['idea']}"); hr("-")
        print(idea_result); print()

        section(ui["section_titles"]["dfv_ready"])
        print(f"  {ui['transitions']['final_status']}\n")
        hr()

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