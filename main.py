"""
Entrepreneurial Opportunity Validation System — v2.1
=====================================================

Two-Phase Pipeline with hard iteration caps for local LLM stability.

  Phase 1 — Problem Definition Agent  (max_prob_turns student answers)
  ─────────────────────────────────────────────────────────────────────
  Steers student teams to articulate four components:
    • Problem Statement  — What is the problem, specifically?
    • Customer Segment   — Who has the problem?
    • Consequence        — What bad outcome results if it goes unsolved?
    • Assumptions        — What are they currently assuming to be true?

  Asks ONE targeted question per turn to fill the most critical gap.
  Terminates with a STRUCTURED DEFINITION block after the cap is hit,
  even if some fields are incomplete — prevents infinite LLM loops.

  Phase 1.5 — Solution Collection  (one-shot input)
  ──────────────────────────────────────────────────
  Student describes their proposed solution concept in free text.
  Feeds the S (Solvable) and P (Profitable) criteria in Phase 2.

  Phase 2 — TIPS Evaluation Agent  (max_tips_turns student answers)
  ──────────────────────────────────────────────────────────────────
  C (Context) is IGNORED in this phase.

  Scores each criterion GREEN / YELLOW / RED:
    T — Timely     : time horizon and urgency
    I — Important  : Must Have vs Should Have vs Nice to Have
    P — Profitable : customer/partner willingness to pay
    S — Solvable   : team skills, data, compute, and resources

  Coaches students on YELLOW/RED criteria each turn.
  On the final iteration, emits VERDICT: READY_FOR_DFV and the JSON.

Output JSON schema
──────────────────
{
  "refined_idea": {
    "customer_segment":   "...",
    "qualified_problem":  "...",
    "consequence":        "...",
    "proposed_solution":  "..."
  },
  "tips_validated_metrics": {
    "timely_factor":          "...",
    "importance_metric":      "...",
    "profitability_pivot":    "...",
    "solvability_constraint": "..."
  },
  "tips_scores": { "T": "...", "I": "...", "P": "...", "S": "..." }
}
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import requests
import yaml
from crewai import Agent, Crew, LLM, Process, Task
from crewai.flow.flow import Flow, listen, start
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent


# ══════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════

os.environ["OPENAI_API_KEY"]  = "lm-studio"
os.environ["OPENAI_BASE_URL"] = "http://localhost:1234/v1"
os.environ["SERPER_API_KEY"]  = "8b1f23fdd635ed883fc9c7a2e9a0b3ff48cb2650"


def _load_yaml(rel_path: str) -> dict:
    with open(BASE_DIR / rel_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


cfg = _load_yaml("config/settings.yaml")

_search  = cfg["search"]
_display = cfg["display"]

# ── FIX 1: Read iteration caps from config so they can be tuned without code changes ──
_val           = cfg.get("validation", {})
MAX_PROB_TURNS: int = _val.get("max_prob_turns", 3)
MAX_TIPS_TURNS: int = _val.get("max_tips_turns", 4)

W: int = _display["console_width"]

llm_cfg = cfg["llm"]
llm = LLM(
    model    = llm_cfg["model"],
    base_url = llm_cfg["base_url"],
    api_key  = llm_cfg["api_key"],
)

# ── FIX 2: Gate web search on enabled_for_local_llm flag ─────
# Small local models (≤8B) cannot reliably prioritise injected web context.
# They treat search results as primary signal rather than supporting evidence,
# causing inconsistent ratings and schema drift in the final JSON.
# Set enabled_for_local_llm: false in settings.yaml when using LM Studio / Ollama.
_serper_key: str = (
    os.environ.get("SERPER_API_KEY")
    or _search.get("serper_api_key", "")
)
_search_on: bool = bool(_serper_key) and _search.get("enabled_for_local_llm", True)

print(f"\n  [Web search {'ENABLED' if _search_on else 'DISABLED'}]\n")


# ══════════════════════════════════════════════════════════════
# STATE MODEL
# ══════════════════════════════════════════════════════════════

class ValidationState(BaseModel):
    # ── Raw inputs ─────────────────────────────────────────────
    raw_idea:          str = ""
    proposed_solution: str = ""

    # ── Phase 1 structured outputs ─────────────────────────────
    problem_statement: str = ""
    customer_segment:  str = ""
    consequence:       str = ""
    assumptions:       str = ""
    prob_turns:        int = 0
    prob_history:      List[dict] = []

    # ── Phase 2 TIPS ratings ───────────────────────────────────
    timely_rating:     str = ""     # GREEN / YELLOW / RED
    important_rating:  str = ""
    profitable_rating: str = ""
    solvable_rating:   str = ""
    tips_turns:        int = 0
    tips_history:      List[dict] = []
    tips_report:       str = ""

    # ── Final output ───────────────────────────────────────────
    final_json: dict = {}


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


def _wrap(text: str, width: Optional[int] = None) -> List[str]:
    width = width or (W - 4)
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


def _pull_question(text: str) -> str:
    """Extract the QUESTION: block; fall back to last ?-terminated line."""
    upper = text.upper()
    if "QUESTION:" in upper:
        idx   = upper.rfind("QUESTION:")
        chunk = text[idx + 9:].strip()
        for stop in ["COACHING:", "FEEDBACK:", "STATUS:", "TIPS ANALYSIS:",
                     "VERDICT:", "NEXT STEP:", "STRUCTURED DEFINITION:"]:
            pos = chunk.upper().find(stop)
            if 0 < pos:
                chunk = chunk[:pos]
        first_line = chunk.split("\n")[0].strip()
        if len(first_line) > 5:
            return first_line
    for line in reversed(text.split("\n")):
        l = line.strip()
        if l.endswith("?") and len(l) > 10:
            return l
    return ""


def agent_says(text: str) -> None:
    """Print agent response, then highlight the question in a callout box."""
    question = _pull_question(text)
    print()
    hr("-")
    print(text.strip())
    hr("-")
    if question:
        print()
        print("=" * W)
        print("  ► QUESTION FOR YOU:")
        print("=" * W)
        for line in _wrap(question):
            print(f"     {line}")
        print("=" * W)
    print()


def ask(prompt: str = "") -> str:
    if prompt:
        print(f"\n  {prompt}")
    return input("\n  > ").strip()


def fmt_history(history: List[dict], last_n: int = 3) -> str:
    """Compact history: last N Q&A pairs, extracting QUESTION: values."""
    if not history:
        return "(No prior conversation.)"
    pairs: List[str] = []
    i = len(history) - 1
    while i >= 0 and len(pairs) < last_n:
        if history[i]["role"] == "user":
            ans = history[i]["content"]
            for j in range(i - 1, -1, -1):
                if history[j]["role"] == "agent":
                    q = history[j]["content"]
                    if "QUESTION:" in q.upper():
                        q = q[q.upper().rfind("QUESTION:") + 9:].strip().split("\n")[0].strip()
                    else:
                        q = q.strip()[:150]
                    pairs.insert(0, f"Q: {q}\nA: {ans}")
                    i = j - 1
                    break
            else:
                i -= 1
        else:
            i -= 1
    return "\n\n".join(pairs) if pairs else "(No prior conversation.)"


# ══════════════════════════════════════════════════════════════
# WEB SEARCH
# ══════════════════════════════════════════════════════════════

def _serper(query: str) -> str:
    if not _search_on:
        return "  [Search disabled]"
    try:
        r = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": _serper_key, "Content-Type": "application/json"},
            json={"q": query, "num": _search.get("results_per_query", 3)},
            timeout=_search.get("timeout_seconds", 10),
        )
        data = r.json()
        lines = []
        ab = data.get("answerBox", {})
        ab_text = ab.get("answer") or ab.get("snippet", "")
        if ab_text:
            lines.append(f"  [Direct answer] {ab_text.strip()}")
        for item in data.get("organic", [])[:3]:
            t = item.get("title", "").strip()
            s = item.get("snippet", "").strip()
            if t and s:
                lines.append(f"  • {t}: {s}")
        return "\n".join(lines) if lines else "  [No results found]"
    except Exception as exc:
        return f"  [Search error: {exc}]"


def web_ctx(queries: List[str], label: str) -> str:
    if not _search_on or not queries:
        return ""
    sep  = "─" * W
    date = datetime.now().strftime("%B %d, %Y")
    parts = [f"\n{sep}", f"WEB CONTEXT — {label} ({date})", sep]
    for q in queries:
        parts.append(f'\nQuery: "{q}"')
        parts.append(_serper(q))
    parts += [f"\n{sep}", "[Web context ends]", f"{sep}\n"]
    return "\n".join(parts)


def tips_web_ctx(problem: str, solution: str = "") -> str:
    yr   = datetime.now().year
    prob = problem[:60].rstrip()
    soln = solution[:50].rstrip()
    queries = [
        f"{prob} market trends {yr}",
        f"{prob} customers willingness to pay",
    ]
    if soln:
        queries.append(f"{soln} technical feasibility {yr}")
    return web_ctx(queries, "TIPS Evaluation")


# ══════════════════════════════════════════════════════════════
# CREW RUNNER & OUTPUT CLEANER
# ══════════════════════════════════════════════════════════════

_RESPONSE_MARKERS = [
    "PROBLEM DEFINITION STATUS:",
    "STRUCTURED DEFINITION:",
    "STATUS: DEFINITION_COMPLETE",
    "TIPS ANALYSIS:",
    "FEEDBACK:",
    "COACHING:",
    "QUESTION:",
    "VERDICT: READY_FOR_DFV",
    "FINAL JSON:",
]


def clean_output(raw: str) -> str:
    """Strip CrewAI tool-call noise from raw agent output."""
    upper = raw.upper()
    earliest = len(raw)
    for marker in _RESPONSE_MARKERS:
        idx = upper.find(marker.upper())
        if 0 <= idx < earliest:
            earliest = idx
    if earliest < len(raw):
        return raw[earliest:].strip()
    if "FINAL ANSWER:" in upper:
        return raw[upper.find("FINAL ANSWER:") + 13:].strip()
    return raw.strip()


def run_agent(task: Task, agent: Agent) -> str:
    """Run a single-agent crew and return cleaned plain-text output."""
    crew = Crew(
        agents  = [agent],
        tasks   = [task],
        process = Process.sequential,
        verbose = False,
        memory  = False,
    )
    return clean_output(str(crew.kickoff()))


# ══════════════════════════════════════════════════════════════
# AGENT DEFINITIONS
# ══════════════════════════════════════════════════════════════

_PROB_AGENT = Agent(
    role="Problem Definition Coach",
    goal=(
        "Help student entrepreneurial teams define their problem with precision.\n"
        "You extract four components one at a time:\n"
        "  1. PROBLEM STATEMENT — Specific, not generic. What exactly is broken?\n"
        "  2. CUSTOMER SEGMENT  — Named, specific group. Not 'everyone' or 'users'.\n"
        "  3. CONSEQUENCE       — What bad outcome results if unsolved? Quantify.\n"
        "  4. ASSUMPTIONS       — What are they taking for granted right now?\n\n"
        "Ask ONE targeted question per turn. Be encouraging and precise."
    ),
    backstory=(
        "You are a startup mentor who has coached hundreds of student teams.\n"
        "You know that a vague problem statement is the leading cause of failed products.\n"
        "You use '5 Whys' instinctively and always push for specificity over generality."
    ),
    verbose=False,
    llm=llm,
)

_TIPS_RULES = """\
TIPS SCORING RUBRIC  (C = Context is IGNORED in this phase)
────────────────────────────────────────────────────────────
T — TIMELY   : Is this a current or near-term problem?
  GREEN  : Active daily/weekly problem  OR  time horizon ≤ 6 months
  YELLOW : Time horizon 6 months – 1 year
  RED    : Horizon > 1 year, undefined, or hazy

I — IMPORTANT : How critical is it to the customer?
  GREEN  : Explicitly "Must Have" + direct, measurable consequence
  YELLOW : "Should Have"  OR  consequence is indirect or vague
  RED    : "Nice to Have"  OR  consequence is trivial

P — PROFITABLE : Will someone pay for a solution?
  GREEN  : Clear YES + plausible named payment model (B2B, subscription, B2B2C…)
  YELLOW : Possibly yes, but model is undefined or indirect
  RED    : No willingness to pay identified; no monetisation path

S — SOLVABLE : Can THIS team build it with their resources?
  GREEN  : Team has the skills, data, compute, and domain knowledge for an MVP
  YELLOW : Can build a basic version but ONE clear gap exists
  RED    : Significant skill, data, or resource gaps; not feasible as stated
────────────────────────────────────────────────────────────
Score ONLY on evidence the student has actually stated. Never assume."""

_TIPS_AGENT = Agent(
    role="TIPS Opportunity Evaluator",
    goal=(
        "Evaluate startup problems against the TIPS framework.\n\n"
        + _TIPS_RULES
        + "\n\nFor every YELLOW or RED criterion, give ONE concrete coaching suggestion."
    ),
    backstory=(
        "You are a venture mentor who has scored thousands of student ideas.\n"
        "You are evidence-based, direct, and never invent facts.\n"
        "Your coaching turns YELLOW and RED ratings into GREEN over 2–4 iterations."
    ),
    verbose=False,
    llm=llm,
)


# ══════════════════════════════════════════════════════════════
# PARSE HELPERS
# ══════════════════════════════════════════════════════════════

def extract_structured_defn(history: List[dict]) -> dict:
    """
    Scrape the most recent STRUCTURED DEFINITION: block from agent history.

    FIX 3: Uses regex instead of plain string matching so it handles the
    'Field : value' format (space before colon) that agents commonly emit,
    not just 'Field:value'. Both formats are matched correctly.
    """
    out = {k: "" for k in ["problem_statement", "customer_segment",
                             "consequence", "assumptions"]}

    # Maps state key → list of label stems to search for (no colon — regex adds it)
    field_stems: dict = {
        "problem_statement": ["PROBLEM STATEMENT", "PROBLEM"],
        "customer_segment":  ["CUSTOMER SEGMENT", "CUSTOMER", "WHO"],
        "consequence":       ["CONSEQUENCE", "CONSEQUENCES", "IMPACT"],
        "assumptions":       ["ASSUMPTIONS", "ASSUMPTION"],
    }

    for turn in reversed(history):
        if turn["role"] != "agent":
            continue
        text  = turn["content"]
        upper = text.upper()
        if "STRUCTURED DEFINITION:" not in upper:
            continue

        # Isolate the STRUCTURED DEFINITION block only
        block_start = upper.find("STRUCTURED DEFINITION:")
        block = text[block_start:]

        for key, stems in field_stems.items():
            for stem in stems:
                # Match "Stem :" or "Stem:" (optional whitespace before colon)
                pat = re.compile(re.escape(stem) + r"\s*:", re.IGNORECASE)
                m   = pat.search(block)
                if m:
                    rest = block[m.end():].strip()
                    line = rest.split("\n")[0].strip()
                    # Clean leading punctuation the model sometimes adds
                    line = re.sub(r"^[-–•\[\]]+\s*", "", line).strip()
                    if line and line.lower() not in ("n/a", "none", "—", "-", ""):
                        out[key] = line
                        break
            if out[key]:
                break   # Don't try remaining stems once a field is filled

        break   # Use only the most recent STRUCTURED DEFINITION block
    return out


def parse_tips_ratings(text: str) -> dict:
    """
    Extract GREEN / YELLOW / RED from a TIPS ANALYSIS block.
    Uses regex first, falls back to proximity search.
    """
    ratings: dict = {k: "" for k in "TIPS"}

    patterns = [
        ("T", r"T\s*[—–\-]\s*Timely\s*[:\|]\s*(GREEN|YELLOW|RED)"),
        ("I", r"I\s*[—–\-]\s*Important\s*[:\|]\s*(GREEN|YELLOW|RED)"),
        ("P", r"P\s*[—–\-]\s*Profitable\s*[:\|]\s*(GREEN|YELLOW|RED)"),
        ("S", r"S\s*[—–\-]\s*Solvable\s*[:\|]\s*(GREEN|YELLOW|RED)"),
    ]
    for key, pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            ratings[key] = m.group(1).upper()

    # Fallback: find letter prefix then scan 80 chars ahead
    fallback_prefixes: dict = {
        "T": ["T —", "TIMELY:"],
        "I": ["I —", "IMPORTANT:"],
        "P": ["P —", "PROFITABLE:"],
        "S": ["S —", "SOLVABLE:"],
    }
    for key, prefixes in fallback_prefixes.items():
        if ratings[key]:
            continue
        for pre in prefixes:
            idx = text.upper().find(pre.upper())
            if idx >= 0:
                window = text[idx:idx + 80].upper()
                for col in ("GREEN", "YELLOW", "RED"):
                    if col in window:
                        ratings[key] = col
                        break
                if ratings[key]:
                    break
    return ratings


def extract_final_json(text: str) -> dict:
    """Parse the first JSON object: tries ```json fence, then bare FINAL JSON: {...}."""
    for pat in [r"```json\s*(\{.*?\})\s*```", r"```\s*(\{.*?\})\s*```"]:
        m = re.search(pat, text, re.DOTALL | re.IGNORECASE)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
    if "FINAL JSON:" in text.upper():
        rest = text[text.upper().find("FINAL JSON:") + 11:].strip()
        m = re.search(r"\{.*\}", rest, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return {}


def is_defn_complete(text: str) -> bool:
    return "STATUS: DEFINITION_COMPLETE" in text.upper()


def is_tips_final(text: str) -> bool:
    upper = text.upper()
    return (
        "VERDICT: READY_FOR_DFV" in upper
        or "VERDICT: DFV_READY"   in upper
        or "FINAL JSON:"          in upper
    )


# ══════════════════════════════════════════════════════════════
# PROMPT TEMPLATES
# ══════════════════════════════════════════════════════════════

def _prob_first_pass(raw_idea: str) -> str:
    return f"""\
You are coaching a student entrepreneurial team to define their problem clearly.

Their initial idea (raw, unrefined):
  "{raw_idea}"

Identify which of the four required components they have addressed:
  1. PROBLEM STATEMENT — What exactly is the problem? (specific, not generic)
  2. CUSTOMER SEGMENT  — Who has this problem? (a named, specific group)
  3. CONSEQUENCE       — What bad outcome happens if it stays unsolved?
  4. ASSUMPTIONS       — What are they currently assuming to be true?

Write your response in EXACTLY this format — no extra text before or after:

PROBLEM DEFINITION STATUS:
  Problem Statement : [Captured / Vague / Missing] — [one-line note on what you found]
  Customer Segment  : [Captured / Vague / Missing] — [one-line note]
  Consequence       : [Captured / Vague / Missing] — [one-line note]
  Assumptions       : [Captured / Vague / Missing] — [one-line note]

FEEDBACK:
[1–2 sentences of constructive, encouraging coaching on what they have so far]

QUESTION:
[ONE focused question to capture the most critical missing component. End with '?']\
"""


def _prob_followup(
    raw_idea: str,
    history_text: str,
    latest_answer: str,
    is_last: bool,
) -> str:
    force_block = (
        "\n⚠ FINAL ITERATION — You MUST now output STATUS: DEFINITION_COMPLETE "
        "and a STRUCTURED DEFINITION block. Do NOT ask another question. "
        "Synthesise the best possible definition from what has been shared, "
        "even if some fields are incomplete.\n"
    ) if is_last else ""

    question_line = "" if is_last else (
        "\nQUESTION:\n"
        "[ONE focused follow-up question to fill the next most critical gap. End with '?']"
    )

    structured_block = (
        "\nSTATUS: DEFINITION_COMPLETE\n\n"
        "STRUCTURED DEFINITION:\n"
        "  Problem Statement : [best one-sentence synthesis from the whole conversation]\n"
        "  Customer Segment  : [the specific named group who has this problem]\n"
        "  Consequence       : [what happens if unsolved — quantify where possible]\n"
        "  Assumptions       : [key assumptions the team is making, separated by semicolons]\n"
    ) if is_last else ""

    return f"""\
You are coaching a student team to define their problem.

Original idea: "{raw_idea}"

Conversation so far:
{history_text}

Their latest answer: "{latest_answer}"
{force_block}
Update your assessment of all four components:

PROBLEM DEFINITION STATUS:
  Problem Statement : [Captured / Vague / Missing] — [one-line note]
  Customer Segment  : [Captured / Vague / Missing] — [one-line note]
  Consequence       : [Captured / Vague / Missing] — [one-line note]
  Assumptions       : [Captured / Vague / Missing] — [one-line note]

FEEDBACK:
[1–2 sentences of constructive coaching]
{question_line}
{structured_block}\
"""


def _prob_force_synthesis(raw_idea: str, history_text: str) -> str:
    return f"""\
Synthesise a structured problem definition from this student conversation.

Original idea: "{raw_idea}"

Full conversation:
{history_text}

Output ONLY the following block — no preamble, no questions:

STATUS: DEFINITION_COMPLETE

STRUCTURED DEFINITION:
  Problem Statement : [concise, specific synthesis of the core problem]
  Customer Segment  : [the specific named group who experiences the problem]
  Consequence       : [quantified or clearly described negative outcome]
  Assumptions       : [comma-separated list of key assumptions]\
"""


def _tips_first_pass(
    problem_statement: str,
    customer_segment: str,
    consequence: str,
    assumptions: str,
    proposed_solution: str,
    search_ctx: str,
) -> str:
    return f"""\
You are evaluating a student startup opportunity using the TIPS framework.
C (Context) is IGNORED in this phase. Score T, I, P, and S only.

Student's structured problem definition:
  Problem Statement : {problem_statement}
  Customer Segment  : {customer_segment or "(not captured)"}
  Consequence       : {consequence or "(not captured)"}
  Assumptions       : {assumptions or "(not captured)"}
  Proposed Solution : {proposed_solution or "Not yet defined"}

{search_ctx}

Score each criterion based ONLY on what the student has actually stated.
Never assume information they have not given you.

SCORING RULES:
T — TIMELY:     GREEN if ≤6-month horizon or active daily problem | YELLOW if 6–12mo | RED if >12mo or unclear
I — IMPORTANT:  GREEN if Must Have + measurable consequence | YELLOW if indirect/vague | RED if Nice to Have
P — PROFITABLE: GREEN if customer clearly willing to pay + model named | YELLOW if possible | RED if no
S — SOLVABLE:   GREEN if team has full MVP capability | YELLOW if one gap | RED if major gaps

Write your response in EXACTLY this format:

TIPS ANALYSIS:
T — Timely     : [GREEN/YELLOW/RED] — [one-sentence evidence from student input]
  Coaching: [only if YELLOW or RED — one specific improvement suggestion]
I — Important  : [GREEN/YELLOW/RED] — [evidence]
  Coaching: [only if YELLOW or RED]
P — Profitable : [GREEN/YELLOW/RED] — [evidence]
  Coaching: [only if YELLOW or RED]
S — Solvable   : [GREEN/YELLOW/RED] — [evidence]
  Coaching: [only if YELLOW or RED]

OVERALL: [X] Green, [Y] Yellow, [Z] Red

QUESTION:
[ONE question targeting the weakest criterion — first RED, then first YELLOW. End with '?']\
"""


def _tips_followup(
    problem_statement: str,
    customer_segment: str,
    consequence: str,
    assumptions: str,
    proposed_solution: str,
    current_ratings: dict,
    history_text: str,
    latest_answer: str,
    is_last: bool,
) -> str:
    ratings_line = "  ".join(f"{k}={v or '?'}" for k, v in current_ratings.items())

    final_instruction = (
        "\n⚠ FINAL ITERATION — You MUST now:\n"
        "  1. Re-score any criteria that improved based on new information.\n"
        "  2. Output VERDICT: READY_FOR_DFV\n"
        "  3. Output the complete FINAL JSON block (template below).\n"
        "  Do NOT ask any more questions.\n"
    ) if is_last else ""

    question_block = "" if is_last else (
        "\nQUESTION:\n"
        "[ONE focused question about the weakest remaining criterion. End with '?']"
    )

    ps_e  = problem_statement.replace('"', "'")
    cs_e  = customer_segment.replace('"', "'") or "(not captured)"
    con_e = consequence.replace('"', "'")       or "(not captured)"
    sol_e = (proposed_solution or "To be defined in DFV phase").replace('"', "'")

    json_template = (
        "\nVERDICT: READY_FOR_DFV\n\n"
        "FINAL JSON:\n"
        "```json\n"
        "{\n"
        '  "refined_idea": {\n'
        f'    "customer_segment": "{cs_e}",\n'
        f'    "qualified_problem": "{ps_e}",\n'
        f'    "consequence": "{con_e}",\n'
        f'    "proposed_solution": "{sol_e}"\n'
        "  },\n"
        '  "tips_validated_metrics": {\n'
        '    "timely_factor":          "[one sentence explaining T rating and time horizon evidence]",\n'
        '    "importance_metric":      "[one sentence explaining I rating and consequence severity]",\n'
        '    "profitability_pivot":    "[one sentence explaining P rating or the payment model to pursue]",\n'
        '    "solvability_constraint": "[one sentence on S rating and the key resource gap if any]"\n'
        "  },\n"
        '  "tips_scores": {\n'
        '    "T": "[use your updated T rating above: GREEN, YELLOW, or RED]",\n'
        '    "I": "[use your updated I rating above: GREEN, YELLOW, or RED]",\n'
        '    "P": "[use your updated P rating above: GREEN, YELLOW, or RED]",\n'
        '    "S": "[use your updated S rating above: GREEN, YELLOW, or RED]"\n'
        "  }\n"
        "}\n"
        "```"
    ) if is_last else ""

    return f"""\
You are continuing TIPS evaluation for a student team.

Problem definition:
  Problem Statement : {problem_statement}
  Customer Segment  : {customer_segment or "(not captured)"}
  Consequence       : {consequence or "(not captured)"}
  Assumptions       : {assumptions or "(not captured)"}
  Proposed Solution : {proposed_solution or "Not yet defined"}

Current TIPS ratings: {ratings_line}

Conversation history:
{history_text}

Their latest answer: "{latest_answer}"
{final_instruction}
Re-score all four criteria with updated evidence:

TIPS ANALYSIS:
T — Timely     : [GREEN/YELLOW/RED] — [evidence]
  Coaching: [only if YELLOW or RED]
I — Important  : [GREEN/YELLOW/RED] — [evidence]
  Coaching: [only if YELLOW or RED]
P — Profitable : [GREEN/YELLOW/RED] — [evidence]
  Coaching: [only if YELLOW or RED]
S — Solvable   : [GREEN/YELLOW/RED] — [evidence]
  Coaching: [only if YELLOW or RED]

OVERALL: [X] Green, [Y] Yellow, [Z] Red
{question_block}
{json_template}\
"""


def _tips_force_final(
    problem_statement: str,
    customer_segment: str,
    consequence: str,
    proposed_solution: str,
    current_ratings: dict,
    history_text: str,
) -> str:
    t_r   = current_ratings.get("T", "YELLOW")
    i_r   = current_ratings.get("I", "YELLOW")
    p_r   = current_ratings.get("P", "YELLOW")
    s_r   = current_ratings.get("S", "YELLOW")
    ps_e  = problem_statement.replace('"', "'")
    cs_e  = customer_segment.replace('"', "'")   or "(not captured)"
    con_e = consequence.replace('"', "'")         or "(not captured)"
    sol_e = (proposed_solution or "To be defined in DFV phase").replace('"', "'")

    return (
        "Evaluation limit reached. Produce the final TIPS output now.\n\n"
        f"Problem definition:\n"
        f"  Problem Statement : {problem_statement}\n"
        f"  Customer Segment  : {customer_segment or '(not captured)'}\n"
        f"  Consequence       : {consequence or '(not captured)'}\n"
        f"  Proposed Solution : {proposed_solution or 'Not yet defined'}\n\n"
        f"Current TIPS ratings: T={t_r} I={i_r} P={p_r} S={s_r}\n\n"
        f"Full conversation:\n{history_text}\n\n"
        "Output ONLY the following (no new questions):\n\n"
        "TIPS ANALYSIS:\n"
        f"T — Timely     : {t_r} — [summarise the evidence from the conversation]\n"
        f"I — Important  : {i_r} — [summarise the evidence]\n"
        f"P — Profitable : {p_r} — [summarise the evidence]\n"
        f"S — Solvable   : {s_r} — [summarise the evidence]\n\n"
        "OVERALL: [X] Green, [Y] Yellow, [Z] Red\n\n"
        "VERDICT: READY_FOR_DFV\n\n"
        "FINAL JSON:\n"
        "```json\n"
        "{\n"
        '  "refined_idea": {\n'
        f'    "customer_segment": "{cs_e}",\n'
        f'    "qualified_problem": "{ps_e}",\n'
        f'    "consequence": "{con_e}",\n'
        f'    "proposed_solution": "{sol_e}"\n'
        "  },\n"
        '  "tips_validated_metrics": {\n'
        '    "timely_factor":          "[explain T]",\n'
        '    "importance_metric":      "[explain I]",\n'
        '    "profitability_pivot":    "[explain P or suggest a model]",\n'
        '    "solvability_constraint": "[explain S]"\n'
        "  },\n"
        '  "tips_scores": {\n'
        f'    "T": "{t_r}",\n'
        f'    "I": "{i_r}",\n'
        f'    "P": "{p_r}",\n'
        f'    "S": "{s_r}"\n'
        "  }\n"
        "}\n"
        "```"
    )


# ══════════════════════════════════════════════════════════════
# VALIDATION FLOW
# ══════════════════════════════════════════════════════════════

class ValidationFlow(Flow[ValidationState]):

    @start()
    def collect_idea(self) -> str:
        section("ENTREPRENEURIAL OPPORTUNITY VALIDATOR")
        print(
            f"  Welcome! This tool guides you through a structured opportunity validation.\n\n"
            f"  Phase 1 — Define your problem clearly       (max {MAX_PROB_TURNS} Q&A rounds)\n"
            f"  Phase 2 — Evaluate against TIPS framework   (max {MAX_TIPS_TURNS} Q&A rounds)\n\n"
            "  At the end you will receive a DFV-ready JSON report.\n"
        )
        hr()
        self.state.raw_idea = ask(
            "Describe your startup idea or the problem you want to solve.\n"
            "  (Rough is fine — we will sharpen it together):"
        )
        return self.state.raw_idea

    @listen(collect_idea)
    def problem_definition_start(self, raw_idea: str) -> str:
        section("PHASE 1 — PROBLEM DEFINITION")
        print(
            "  The agent will guide you to articulate four components:\n"
            "  Problem Statement  •  Customer Segment  •  Consequence  •  Assumptions\n"
        )
        task = Task(
            description    = _prob_first_pass(raw_idea),
            expected_output= "Problem Definition Status block, feedback, and one question.",
            agent          = _PROB_AGENT,
        )
        result = run_agent(task, _PROB_AGENT)
        self.state.prob_history.append({"role": "agent", "content": result})
        self.state.prob_turns = 0
        return result

    @listen(problem_definition_start)
    def problem_definition_loop(self, first_response: str) -> str:
        report = first_response

        while self.state.prob_turns < MAX_PROB_TURNS and not is_defn_complete(report):
            agent_says(report)

            answer = ask("Your response:")
            self.state.prob_history.append({"role": "user", "content": answer})
            self.state.prob_turns += 1

            is_last = self.state.prob_turns >= MAX_PROB_TURNS

            task = Task(
                description    = _prob_followup(
                    raw_idea      = self.state.raw_idea,
                    history_text  = fmt_history(self.state.prob_history),
                    latest_answer = answer,
                    is_last       = is_last,
                ),
                expected_output= (
                    "STATUS: DEFINITION_COMPLETE and STRUCTURED DEFINITION block."
                    if is_last else
                    "Updated status, feedback, and one follow-up question."
                ),
                agent          = _PROB_AGENT,
            )
            result = run_agent(task, _PROB_AGENT)
            self.state.prob_history.append({"role": "agent", "content": result})
            report = result

        # The final agent response is always un-shown here — show it now
        agent_says(report)

        # Safety net: if no STRUCTURED DEFINITION was produced, force synthesis
        all_agent_text = " ".join(
            t["content"] for t in self.state.prob_history if t["role"] == "agent"
        )
        if "STRUCTURED DEFINITION:" not in all_agent_text.upper():
            print("\n  [Synthesising problem definition from conversation…]\n")
            task = Task(
                description    = _prob_force_synthesis(
                    raw_idea     = self.state.raw_idea,
                    history_text = fmt_history(self.state.prob_history, last_n=8),
                ),
                expected_output= "STATUS: DEFINITION_COMPLETE and STRUCTURED DEFINITION block.",
                agent          = _PROB_AGENT,
            )
            synthesis = run_agent(task, _PROB_AGENT)
            self.state.prob_history.append({"role": "agent", "content": synthesis})
            agent_says(synthesis)

        # Extract structured components (uses regex-based parser — FIX 3)
        defn = extract_structured_defn(self.state.prob_history)
        self.state.problem_statement = defn["problem_statement"] or self.state.raw_idea
        self.state.customer_segment  = defn["customer_segment"]
        self.state.consequence       = defn["consequence"]
        self.state.assumptions       = defn["assumptions"]

        section("PROBLEM DEFINITION — CONFIRMED")
        print(f"  Problem Statement : {self.state.problem_statement}")
        print(f"  Customer Segment  : {self.state.customer_segment  or '(not captured — will proceed)'}")
        print(f"  Consequence       : {self.state.consequence        or '(not captured — will proceed)'}")
        print(f"  Assumptions       : {self.state.assumptions        or '(not captured — will proceed)'}")
        hr()

        return self.state.problem_statement

    @listen(problem_definition_loop)
    def collect_solution(self, _problem_statement: str) -> str:
        print(
            "\n  Problem definition complete. Before evaluating TIPS, describe\n"
            "  your proposed solution — this feeds the P (Profitable) and\n"
            "  S (Solvable) criteria scoring.\n"
        )
        self.state.proposed_solution = ask(
            "What is your proposed solution?\n"
            "  (rough concept is fine — just describe what you want to build):"
        )
        return self.state.proposed_solution

    @listen(collect_solution)
    def tips_evaluation_start(self, _solution: str) -> str:
        section("PHASE 2 — TIPS EVALUATION")
        print(
            "  The agent will now score your opportunity:\n"
            "  T (Timely)  ·  I (Important)  ·  P (Profitable)  ·  S (Solvable)\n"
            "  C (Context) is skipped in this phase.\n"
            "  Each criterion is rated  GREEN · YELLOW · RED  with coaching on weak areas.\n"
        )

        search_ctx = tips_web_ctx(
            self.state.problem_statement,
            self.state.proposed_solution,
        )

        task = Task(
            description    = _tips_first_pass(
                problem_statement = self.state.problem_statement,
                customer_segment  = self.state.customer_segment,
                consequence       = self.state.consequence,
                assumptions       = self.state.assumptions,
                proposed_solution = self.state.proposed_solution,
                search_ctx        = search_ctx,
            ),
            expected_output= "TIPS analysis with GREEN/YELLOW/RED ratings and one question.",
            agent          = _TIPS_AGENT,
        )
        result = run_agent(task, _TIPS_AGENT)
        self.state.tips_history.append({"role": "agent", "content": result})
        self.state.tips_turns = 0

        ratings = parse_tips_ratings(result)
        self.state.timely_rating     = ratings["T"]
        self.state.important_rating  = ratings["I"]
        self.state.profitable_rating = ratings["P"]
        self.state.solvable_rating   = ratings["S"]
        self.state.tips_report       = result

        return result

    @listen(tips_evaluation_start)
    def tips_loop(self, first_tips_response: str) -> str:
        report = first_tips_response

        while self.state.tips_turns < MAX_TIPS_TURNS and not is_tips_final(report):
            agent_says(report)

            answer = ask("Your response:")
            self.state.tips_history.append({"role": "user", "content": answer})
            self.state.tips_turns += 1

            is_last = self.state.tips_turns >= MAX_TIPS_TURNS
            current_ratings = {
                "T": self.state.timely_rating,
                "I": self.state.important_rating,
                "P": self.state.profitable_rating,
                "S": self.state.solvable_rating,
            }

            task = Task(
                description    = _tips_followup(
                    problem_statement = self.state.problem_statement,
                    customer_segment  = self.state.customer_segment,
                    consequence       = self.state.consequence,
                    assumptions       = self.state.assumptions,
                    proposed_solution = self.state.proposed_solution,
                    current_ratings   = current_ratings,
                    history_text      = fmt_history(self.state.tips_history),
                    latest_answer     = answer,
                    is_last           = is_last,
                ),
                expected_output= (
                    "Updated TIPS analysis, VERDICT: READY_FOR_DFV, and FINAL JSON."
                    if is_last else
                    "Updated TIPS analysis and one follow-up question."
                ),
                agent          = _TIPS_AGENT,
            )
            result = run_agent(task, _TIPS_AGENT)
            self.state.tips_history.append({"role": "agent", "content": result})

            new_r = parse_tips_ratings(result)
            if new_r["T"]: self.state.timely_rating     = new_r["T"]
            if new_r["I"]: self.state.important_rating  = new_r["I"]
            if new_r["P"]: self.state.profitable_rating = new_r["P"]
            if new_r["S"]: self.state.solvable_rating   = new_r["S"]

            self.state.tips_report = result
            report = result

        agent_says(report)

        if not is_tips_final(report):
            print("\n  [Generating final TIPS report…]\n")
            current_ratings = {
                "T": self.state.timely_rating     or "YELLOW",
                "I": self.state.important_rating  or "YELLOW",
                "P": self.state.profitable_rating or "YELLOW",
                "S": self.state.solvable_rating   or "YELLOW",
            }
            task = Task(
                description    = _tips_force_final(
                    problem_statement = self.state.problem_statement,
                    customer_segment  = self.state.customer_segment,
                    consequence       = self.state.consequence,
                    proposed_solution = self.state.proposed_solution,
                    current_ratings   = current_ratings,
                    history_text      = fmt_history(self.state.tips_history, last_n=8),
                ),
                expected_output= "TIPS analysis, VERDICT: READY_FOR_DFV, and FINAL JSON.",
                agent          = _TIPS_AGENT,
            )
            result = run_agent(task, _TIPS_AGENT)
            self.state.tips_history.append({"role": "agent", "content": result})
            self.state.tips_report = result
            report = result
            agent_says(report)

        return report

    @listen(tips_loop)
    def final_report(self, tips_result: str) -> dict:
        section("FINAL VALIDATION REPORT")

        final_json = extract_final_json(tips_result)
        if not final_json:
            cr = {
                "T": self.state.timely_rating     or "YELLOW",
                "I": self.state.important_rating  or "YELLOW",
                "P": self.state.profitable_rating or "YELLOW",
                "S": self.state.solvable_rating   or "YELLOW",
            }
            final_json = {
                "refined_idea": {
                    "customer_segment":  self.state.customer_segment  or "(not captured)",
                    "qualified_problem": self.state.problem_statement,
                    "consequence":       self.state.consequence        or "(not captured)",
                    "proposed_solution": self.state.proposed_solution or "To be defined in DFV phase",
                },
                "tips_validated_metrics": {
                    "timely_factor":          f"T rated {cr['T']} — see TIPS analysis above.",
                    "importance_metric":      f"I rated {cr['I']} — see TIPS analysis above.",
                    "profitability_pivot":    f"P rated {cr['P']} — see TIPS analysis above.",
                    "solvability_constraint": f"S rated {cr['S']} — see TIPS analysis above.",
                },
                "tips_scores": cr,
            }
        self.state.final_json = final_json

        icons = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴", "": "⚪"}
        hr("-")
        print("  TIPS SCORECARD")
        hr("-")
        for letter, name, attr in [
            ("T", "Timely",     "timely_rating"),
            ("I", "Important",  "important_rating"),
            ("P", "Profitable", "profitable_rating"),
            ("S", "Solvable",   "solvable_rating"),
        ]:
            rating = getattr(self.state, attr) or "UNKNOWN"
            icon   = icons.get(rating, "⚪")
            print(f"    {icon}  {letter} — {name:<12}  :  {rating}")
        print()

        hr("-")
        print("  DFV-READY JSON OUTPUT")
        hr("-")
        print(json.dumps(self.state.final_json, indent=2))
        hr("-")

        section("READY FOR DESIGN-FOR-VALIDATION (DFV)")
        print(
            "  The JSON above is your structured input for the DFV stage.\n"
            "  Carry the tips_scores ratings into your validation experiment design:\n"
            "    GREEN  → assumption confirmed; move forward\n"
            "    YELLOW → assumption likely; design a quick test to confirm\n"
            "    RED    → assumption unproven; design a focused experiment before building\n"
        )
        hr()

        return self.state.final_json


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
