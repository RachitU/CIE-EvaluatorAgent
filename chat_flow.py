"""
Chat-Adapted Validation Flow
============================
Wraps the core validation logic with queue-based I/O for the web chat UI.
Runs in a background thread per session and communicates via two queues:

  input_queue  : Flask app  → Flow  (user messages put here)
  event_queue  : Flow       → Flask (events to stream to browser)

Event schema (JSON-serialisable dicts):
  {"type": "phase",        "phase": str, "label": str, "status": str}
  {"type": "agent",        "content": str, "agent": str, "phase": str}
  {"type": "system",       "content": str, "style": "info|success|warning|error"}
  {"type": "input_needed", "prompt": str, "context": str}
  {"type": "complete",     "summary": {...}}
  {"type": "exit"}
  {"type": "error",        "content": str}
  None  ← sentinel: flow has finished (put last on event_queue)
"""

import os
import sys
import time
import queue
import threading
from pathlib import Path
from typing import List

# ── Fix Windows console encoding ───────────────────────────────────────────────
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import requests
import yaml
from datetime import datetime
from pydantic import BaseModel
from crewai import Agent, Task, Crew, Process, LLM, Memory
from crewai.flow.flow import Flow, listen, start
from crewai_tools import TavilySearchTool


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

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


cfg    = _load_yaml("config/settings.yaml")
ui_str = _load_yaml("prompts/ui_strings.yaml")

_search  = cfg["search"]
_display = cfg["display"]

MAX_OPP_QUESTIONS = 3


# ══════════════════════════════════════════════════════════════════════════════
# STATE MODEL
# ══════════════════════════════════════════════════════════════════════════════

class ValidationState(BaseModel):
    # ── initial rough idea ────────────────────────────────────────────────
    initial_idea: str = ""

    # ── Problem Definition Agent output ──────────────────────────────────
    problem_definition: dict = {}   # structured JSON from Problem Def Agent
    prob_def_history:   List[dict] = []
    prob_def_turns:     int = 0

    # ── shorthand fields used by market_scan ─────────────────────────────
    problem:  str = ""
    solution: str = ""

    # ── market scan ───────────────────────────────────────────────────────
    market_verdict: str = ""
    market_report:  str = ""
    market_angle:   str = ""

    # ── Ethics Pre-Screener output ────────────────────────────────────────
    ethics_output: dict = {}        # final ETHICS_OUTPUT JSON

    # ── TIPS Agent output ─────────────────────────────────────────────────
    tips_history: List[dict] = []
    tips_turns:   int = 0
    tips_output:  dict = {}         # final TIPS_OUTPUT JSON

    # ── DFV Agent output ──────────────────────────────────────────────────
    dfv_output:   dict = {}         # final DFV_OUTPUT JSON


# ══════════════════════════════════════════════════════════════════════════════
# CHAT VALIDATION FLOW
# ══════════════════════════════════════════════════════════════════════════════

class ChatValidationFlow(Flow[ValidationState]):
    """ValidationFlow adapted for a web chat UI."""

    # ── Response markers used to strip crewai preamble ────────────────────────
    _RESPONSE_MARKERS = [
        "PROBLEM_DEFINITION:", "TIPS_OUTPUT:", "TIPS_TRIAGE:",
        "COACHING_NOTE:", "MISSING_FIELDS:", "VERDICT: REJECT",
        "VERDICT: PROCEED", "SEARCH FINDINGS:",
    ]

    def __init__(self, input_queue: queue.Queue, event_queue: queue.Queue):
        super().__init__()
        self._iq = input_queue    # user messages come in here
        self._eq = event_queue    # events go out here
        self._exiting = False
        self._setup_components()

    # ──────────────────────────────────────────────────────────────────────────
    # Setup
    # ──────────────────────────────────────────────────────────────────────────

    def _setup_components(self):
        llm_cfg = cfg["llm"]
        self._llm = LLM(
            model=llm_cfg["model"],
            base_url=llm_cfg["base_url"],
            api_key=llm_cfg["api_key"],
        )

        self._serper_key = (
            os.environ.get("SERPER_API_KEY")
            or _search.get("serper_api_key", "")
        )
        self._search_on = bool(self._serper_key)

        try:
            self._agent_tools = [TavilySearchTool()]
        except Exception:
            self._agent_tools = []

        try:
            # Memory requires an embedder (like OpenAI) which causes issues when using
            # local LLMs like LM Studio without an OPENAI_API_KEY. We disable it.
            import types
            self._shared_memory = types.SimpleNamespace(
                recall=lambda *a, **kw: [],
                remember=lambda *a, **kw: None,
                extract_memories=lambda *a, **kw: [],
            )
        except Exception:
            pass

        opp_skill   = _load_skill("opportunity_agent")
        idea_skill  = _load_skill("idea_agent")
        scout_skill = _load_skill("market_scout_agent")
        self._scout_skill = scout_skill

        self._tips_agent = Agent(
            role="TIPS Evaluation Agent",
            goal=opp_skill,
            backstory=(
                "Startup evaluation coach at an entrepreneurship programme. "
                "You score student problems using the deterministic TIPS framework "
                "(Timely, Important, Profitable, Solvable — C is ignored). "
                "You are precise, structured, and always apply the scoring table exactly."
            ),
            verbose=False,
            tools=[],
            llm=self._llm,
            memory=False,
        )

        self._prob_def_agent = Agent(
            role="Problem Definition Coach",
            goal=idea_skill,
            backstory=(
                "Experienced entrepreneurship mentor who helps student teams move "
                "from a vague idea to a precisely structured problem definition. "
                "You elicit exactly 5 fields: customer segment, qualified problem, "
                "consequence, proposed solution, and assumptions. "
                "You never evaluate — only structure and clarify."
            ),
            verbose=False,
            tools=[],
            llm=self._llm,
            memory=False,
        )

        self._market_scout_agent = Agent(
            role="Market Scout Agent",
            goal=scout_skill,
            backstory=(
                "Competitive intelligence analyst. You map existing solutions, "
                "funding levels, and market gaps before the team wastes time "
                "building something that already exists."
            ),
            verbose=False,
            tools=[],
            llm=self._llm,
            memory=False,
        )

        ethics_skill = _load_skill("ethics_agent")
        self._ethics_agent = Agent(
            role="Ethics Pre-Screener",
            goal=ethics_skill,
            backstory=(
                "Principled ethics reviewer embedded in a startup evaluation pipeline. "
                "You are not a regulator and not overly cautious — you pass legitimate "
                "ideas in sensitive spaces. You block only ideas whose core business model "
                "requires harming the people it claims to serve, is facially illegal, or "
                "where the proposed solution replicates the very problem it claims to solve."
            ),
            verbose=False,
            tools=[],
            llm=self._llm,
            memory=False,
        )

        dfv_skill = _load_skill("dfv_agent")
        self._dfv_agent = Agent(
            role="DFV Evaluator Agent",
            goal=dfv_skill,
            backstory=(
                "Expert venture architect tasked with producing a final Desirability, "
                "Feasibility, and Viability (DFV) report for a startup idea. "
                "You base your analysis entirely on the Problem Definition, Market Scan, "
                "and TIPS Scorecard."
            ),
            verbose=False,
            tools=[],
            llm=self._llm,
            memory=False,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Event emitters
    # ──────────────────────────────────────────────────────────────────────────

    def _emit(self, event: dict):
        self._eq.put(event)

    def _emit_phase(self, phase: str, label: str, status: str = "active"):
        self._emit({"type": "phase", "phase": phase, "label": label, "status": status})

    def _emit_agent(self, content: str, agent_name: str = "Agent", phase: str = ""):
        self._emit({"type": "agent", "content": content, "agent": agent_name, "phase": phase})

    def _emit_system(self, content: str, style: str = "info"):
        self._emit({"type": "system", "content": content, "style": style})

    def _emit_input_needed(self, prompt: str, context: str = ""):
        self._emit({"type": "input_needed", "prompt": prompt, "context": context})

    def _emit_complete(self):
        self._emit({
            "type": "complete",
            "summary": {
                "problem_definition": self.state.problem_definition,
                "ethics_output":      self.state.ethics_output,
                "market_verdict":     self.state.market_verdict,
                "market_angle":       self.state.market_angle,
                "market_report":      self.state.market_report,
                "tips_output":        self.state.tips_output,
                "dfv_output":         self.state.dfv_output,
            }
        })

    # ──────────────────────────────────────────────────────────────────────────
    # I/O replacement
    # ──────────────────────────────────────────────────────────────────────────

    def _ask(self, prompt: str = "", context: str = "", timeout: int = 1800) -> str:
        """Chat replacement for CLI ask() — emits input_needed then blocks."""
        self._emit_input_needed(prompt, context)
        try:
            return self._iq.get(timeout=timeout).strip()
        except queue.Empty:
            return ""

    # ──────────────────────────────────────────────────────────────────────────
    # Pure utilities (ported from validate_opportunity.py)
    # ──────────────────────────────────────────────────────────────────────────

    def _extract_displayed_question(self, text: str) -> str:
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
        return "Please share more information to continue the evaluation."

    def _format_history(self, history: List[dict]) -> str:
        if not history:
            return "(No prior conversation.)"
        verdict_lines = []
        for turn in history:
            if turn["role"] != "agent":
                continue
            for line in turn["content"].split("\n"):
                l = line.strip()
                if any(l.startswith(f"{c} \u2014") or l.startswith(f"{c}:") for c in "TIPSC"):
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

    def _run_agent(self, task: Task, agent: Agent,
                   retries: int = 3, retry_delay: float = 5.0) -> str:
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                crew = Crew(
                    agents=[agent],
                    tasks=[task],
                    process=Process.sequential,
                    verbose=False,
                )
                raw = str(crew.kickoff())
                return self._clean_agent_output(raw)
            except Exception as e:
                last_error = e
                err_str = str(e)
                # Retryable: LM Studio model reload or context overflow
                if "Model reloaded" in err_str or "context_length_exceeded" in err_str:
                    if attempt < retries:
                        self._emit_system(
                            f"LM Studio model reloaded — retrying ({attempt}/{retries})…",
                            "warning"
                        )
                        time.sleep(retry_delay)
                        continue
                # Surface connection errors with a clear, actionable message
                if "Connection error" in err_str or "ConnectionError" in err_str:
                    raise ConnectionError(
                        "Cannot reach the LLM endpoint. "
                        "Please make sure LM Studio is running at "
                        f"{cfg['llm'].get('base_url','http://localhost:1234/v1')} "
                        "with a model loaded, then click 'New Validation' to try again."
                    ) from e
                raise
        raise last_error

    def _clean_agent_output(self, text: str) -> str:
        text_upper = text.upper()
        earliest   = len(text)
        for marker in self._RESPONSE_MARKERS:
            idx = text_upper.find(marker.upper())
            if 0 <= idx < earliest:
                earliest = idx
        if earliest < len(text):
            return text[earliest:].strip()
        if "FINAL ANSWER:" in text_upper:
            idx = text_upper.find("FINAL ANSWER:")
            return text[idx + len("FINAL ANSWER:"):].strip()
        return text.strip()

    def _last_exchange(self, history: List[dict]):
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

    def _extract_question(self, text: str) -> str:
        if "QUESTION:" in text.upper():
            idx = text.upper().find("QUESTION:")
            return text[idx + len("QUESTION:"):].strip().split("\n")[0].strip()
        return text.strip()[:200]

    def _repetition_warning(self, last_q: str, current_q: str) -> str:
        if not last_q or last_q == "(none)":
            return ""
        lq_words = set(last_q.lower().split())
        cq_words = set(current_q.lower().split())
        if not lq_words:
            return ""
        overlap = len(lq_words & cq_words) / len(lq_words)
        if overlap > 0.7:
            return ui_str.get("repetition_warning", "")
        return ""

    def _opp_approved(self, text: str) -> bool:
        return "STATUS: APPROVED" in text.upper()

    def _idea_ready(self, text: str) -> bool:
        return "VERDICT: READY_FOR_DFV" in text.upper()

    # ──────────────────────────────────────────────────────────────────────────
    # Web search
    # ──────────────────────────────────────────────────────────────────────────

    def _serper_search(self, query: str) -> str:
        if not self._search_on:
            return "(web search not configured)"
        try:
            r = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": self._serper_key, "Content-Type": "application/json"},
                json={"q": query, "num": _search["results_per_query"]},
                timeout=_search["timeout_seconds"],
            )
            data  = r.json()
            lines = []
            ab_text = (data.get("answerBox") or {}).get("answer") or \
                      (data.get("answerBox") or {}).get("snippet", "")
            if ab_text:
                lines.append(f"[Direct answer] {ab_text.strip()}")
            for item in data.get("organic", [])[:_search["results_per_query"]]:
                title   = item.get("title", "").strip()
                snippet = item.get("snippet", "").strip()
                if title and snippet:
                    lines.append(f"• {title}: {snippet}")
            return "\n".join(lines) if lines else "(no results found)"
        except Exception as e:
            return f"(search error: {e})"

    def _fetch_web_context(self, queries: List[str], label: str) -> str:
        if not self._search_on or not queries:
            return ""
        date_str = datetime.now().strftime("%B %d, %Y")
        lines = [f"WEB SEARCH CONTEXT [{label}] (fetched {date_str})", "\u2501" * 50]
        for q in queries:
            lines.append(f'\nQuery: "{q}"')
            lines.append(self._serper_search(q))
        lines += ["", "\u2501" * 50, ""]
        return "\n".join(lines)

    def _market_scout_search_context(self, problem: str) -> str:
        yr    = datetime.now().year
        short = problem[:55].rstrip()
        return self._fetch_web_context(
            [
                f"{short} existing apps OR platforms OR solutions {yr}",
                f"{short} market leaders competitors funding",
                f"{short} user complaints OR limitations OR missing features",
                f"{short} market size OR growth rate {yr}",
                f"{short} new startup launch OR recent entrant {yr}",
            ],
            label="Competitive Landscape",
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Memory helpers
    # ──────────────────────────────────────────────────────────────────────────

    VALIDATION_SCOPE = "/validations"

    def _recall_similar_problems(self, problem: str) -> str:
        try:
            matches = self._shared_memory.recall(
                f"problem validation result: {problem}",
                scope=self.VALIDATION_SCOPE, limit=3, depth="shallow",
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

    def _recall_similar_solutions(self, problem: str, solution: str) -> str:
        try:
            matches = self._shared_memory.recall(
                f"solution evaluation: {solution} for problem: {problem}",
                scope=self.VALIDATION_SCOPE, limit=3, depth="shallow",
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

    def _save_validation_result(self, problem, solution, opp_report, idea_report):
        try:
            opp_facts  = self._shared_memory.extract_memories(opp_report)
            idea_facts = self._shared_memory.extract_memories(idea_report)
            self._shared_memory.remember(
                f"VALIDATED OPPORTUNITY — Problem: {problem[:120]} | "
                f"Solution: {solution[:120]} | Opportunity: APPROVED | Idea: READY_FOR_DFV",
                scope=self.VALIDATION_SCOPE,
            )
            for fact in opp_facts:
                self._shared_memory.remember(fact, scope=self.VALIDATION_SCOPE + "/opportunity")
            for fact in idea_facts:
                self._shared_memory.remember(fact, scope=self.VALIDATION_SCOPE + "/idea")
            n = len(opp_facts) + len(idea_facts) + 1
            self._emit_system(f"Session saved to memory ({n} records).", "success")
        except Exception as e:
            self._emit_system(f"Memory save skipped: {e}", "warning")

    def _save_rejection(self, problem, reason):
        try:
            self._shared_memory.remember(
                f"REJECTED PROBLEM — {problem[:150]} — Reason: {reason[:200]}",
                scope=self.VALIDATION_SCOPE + "/rejections",
            )
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════════════
    # HELPERS — Problem Definition
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _extract_json_block(text: str, marker: str) -> dict:
        """Brace-counting JSON extractor — handles nested objects correctly."""
        import json
        idx = text.upper().find(marker.upper())
        if idx == -1:
            return {}
        fragment = text[idx + len(marker):].strip()
        # Find the opening brace
        start = fragment.find('{')
        if start == -1:
            return {}
        depth = 0
        end = -1
        for i, ch in enumerate(fragment[start:], start=start):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end == -1:
            return {}
        try:
            return json.loads(fragment[start:end])
        except Exception:
            try:
                import json_repair
                return json_repair.loads(fragment[start:end])
            except Exception as e:
                print(f"JSON Parsing Error in _extract_json_block: {e}")
                print(f"--- FRAGMENT ATTEMPTED TO PARSE ---\n{fragment[start:end]}\n-----------------------------------")
                return {}

    def _extract_problem_definition(self, text: str) -> dict:
        """Parse PROBLEM_DEFINITION: JSON block from agent output."""
        return self._extract_json_block(text, "PROBLEM_DEFINITION:")

    def _prob_def_complete(self, text: str) -> bool:
        return "PROBLEM_DEFINITION:" in text.upper()

    def _build_prob_def_task(self, initial_idea: str,
                              history: List[dict], force: bool) -> str:
        history_text = ""
        if history:
            turns = []
            for h in history:
                role = "Coach" if h["role"] == "agent" else "Student"
                turns.append(f"{role}: {h['content'].strip()[:400]}")
            history_text = "\n".join(turns)

        base = (
            f"The student's initial idea (may be rough): {initial_idea}\n\n"
            f"Conversation so far:\n{history_text}\n\n"
        )
        if force:
            return (
                base
                + "You have reached the question limit. "
                + "Using everything shared so far, produce the PROBLEM_DEFINITION: JSON block "
                + "now — fill in your best inference for any missing fields. "
                + "Do NOT ask another question."
            )
        turns_left = 3 - len([h for h in history if h["role"] == "agent"])
        return (
            base
            + f"Questions remaining: {max(0, turns_left)}.\n"
            + "If all 5 fields are clear, output PROBLEM_DEFINITION: JSON now. "
            + "Otherwise ask ONE focused question about the most critical missing field."
        )

    # ══════════════════════════════════════════════════════════════════════════
    # HELPERS — TIPS
    # ══════════════════════════════════════════════════════════════════════════

    def _extract_tips_output(self, text: str) -> dict:
        """Parse TIPS_OUTPUT: JSON block from agent output."""
        return self._extract_json_block(text, "TIPS_OUTPUT:")

    def _tips_complete(self, text: str) -> bool:
        return "TIPS_OUTPUT:" in text.upper()

    def _build_tips_task(self, history: List[dict], force: bool) -> str:
        import json
        prob_def = json.dumps(self.state.problem_definition, indent=2) \
                   if self.state.problem_definition else \
                   f"Problem: {self.state.problem}\nSolution: {self.state.solution}"

        market_ctx = ""
        if self.state.market_report:
            market_ctx = (
                f"\nMARKET SCOUT REPORT (for context only):\n"
                f"{self.state.market_report[:600]}\n"
            )

        history_text = ""
        if history:
            turns = []
            for h in history:
                role = "TIPS Agent" if h["role"] == "agent" else "Student"
                turns.append(f"{role}: {h['content'].strip()[:400]}")
            history_text = "Conversation so far:\n" + "\n".join(turns) + "\n\n"

        base = (
            f"PROBLEM DEFINITION:\n{prob_def}\n"
            f"{market_ctx}\n"
            f"{history_text}"
        )
        if force:
            return (
                base
                + "You have reached the 2-question limit. "
                + "Apply the TIPS scoring rules to everything shared and produce "
                + "the TIPS_OUTPUT: JSON block now. Do NOT ask another question."
            )
        turns_left = 2 - len([h for h in history if h["role"] == "agent"])
        return (
            base
            + f"Questions remaining: {max(0, turns_left)}.\n"
            + "Apply the scoring table deterministically. "
            + "If all TIPS dimensions are clear, produce TIPS_OUTPUT: JSON now. "
            + "Otherwise ask ONE clarifying question about the weakest dimension."
        )

    # ══════════════════════════════════════════════════════════════════════════
    # FLOW STEPS
    # ══════════════════════════════════════════════════════════════════════════

    def _check_llm_connection(self):
        """Quick sanity-check: hit the LLM with a tiny prompt to confirm it's reachable."""
        base_url = cfg["llm"].get("base_url", "http://localhost:1234/v1")
        try:
            import requests as _req
            resp = _req.get(f"{base_url}/models",
                            headers={"Authorization": f"Bearer {cfg['llm'].get('api_key','')}"},
                            timeout=5)
            # Any HTTP response (even 401) means the server is up
        except Exception:
            raise ConnectionError(
                f"Cannot connect to the LLM server at {base_url}. "
                "Please start LM Studio (or your local LLM server), load a model, "
                "then click 'New Validation' to try again."
            )

    @start()
    def collect_initial_input(self):
        """Step 1 — LLM check + get rough idea in one free-form turn."""
        self._emit_phase("intro", "Getting Started", "active")

        self._emit_system("Checking LLM connection\u2026", "info")
        try:
            self._check_llm_connection()
            self._emit_system(
                f"\u2713  LLM connected ({cfg['llm'].get('base_url','localhost:1234')})",
                "success"
            )
        except ConnectionError as ce:
            self._emit_system(str(ce), "error")
            self._emit({"type": "error", "content": str(ce)})
            return None

        self._emit_system(
            "Welcome! I\u2019ll guide your team through Problem Definition \u2192 "
            "TIPS Evaluation so you know if your idea is worth building.",
            "info"
        )
        status_msg = (
            "\u2713  Web search enabled \u2014 live market data will be used in analysis"
            if self._search_on else
            "\u26a0  Web search not configured \u2014 agents will evaluate without web data"
        )
        self._emit_system(status_msg, "success" if self._search_on else "warning")

        problem = self._ask(
            "What is the specific problem you are trying to solve?",
            context="initial_problem",
        )
        self.state.problem = problem

        solution = self._ask(
            "What is your proposed solution or idea for this problem?",
            context="initial_solution",
        )
        self.state.solution = solution

        self.state.initial_idea = f"Problem: {problem}\nSolution: {solution}"
        return self.state.initial_idea

    @listen(collect_initial_input)
    def problem_definition_loop(self, initial_idea):
        """Step 2 \u2014 Problem Definition Agent, max 4 turns."""
        if initial_idea is None:
            return None

        MAX_TURNS = 4
        self._emit_phase("problem_definition", "Problem Definition", "active")
        self._emit_system(
            "The Problem Definition Coach will help you articulate your problem precisely. "
            "We\u2019ll collect: Customer, Problem, Consequence, Solution, and Assumptions.",
            "info"
        )

        history: List[dict] = []

        for turn in range(1, MAX_TURNS + 1):
            force = (turn == MAX_TURNS)
            desc  = self._build_prob_def_task(initial_idea, history, force=force)

            task = Task(
                description=desc,
                expected_output=(
                    "PROBLEM_DEFINITION: JSON block with all 5 fields."
                    if force else
                    "Either a single coaching question, or PROBLEM_DEFINITION: JSON if all fields are clear."
                ),
                agent=self._prob_def_agent,
            )
            result = self._run_agent(task, self._prob_def_agent)
            history.append({"role": "agent", "content": result})
            self.state.prob_def_history = history

            if self._prob_def_complete(result):
                # Done — extract and display
                self._emit_agent(result, agent_name="Problem Definition Coach",
                                 phase="problem_definition")
                break

            # Show agent response and ask for student input
            self._emit_agent(result, agent_name="Problem Definition Coach",
                             phase="problem_definition")

            if turn < MAX_TURNS:
                answer = self._ask(
                    "Your response:",
                    context="prob_def",
                )
                history.append({"role": "user", "content": answer})

        # Extract structured definition
        final_text = history[-1]["content"] if history else ""
        prob_def = self._extract_problem_definition(final_text)

        if not prob_def:
            # Fallback: use raw initial idea
            prob_def = {
                "customer_segment": "(not specified)",
                "qualified_problem": initial_idea,
                "consequence": "(not specified)",
                "proposed_solution": "(not specified)",
                "assumptions": [],
            }

        self.state.problem_definition = prob_def
        self.state.problem  = prob_def.get("qualified_problem", initial_idea)
        self.state.solution = prob_def.get("proposed_solution", "")

        self._emit_system(
            "\u2713  Problem definition complete. Running market intelligence scan\u2026",
            "success"
        )
        return self.state.problem

    @listen(problem_definition_loop)
    def ethics_screening(self, problem):
        """Step 2b — Ethics Pre-Screener (automatic, no user input)."""
        if problem is None:
            return None

        import json
        self._emit_phase("ethics", "Ethics Pre-Screen", "active")
        self._emit_system(
            "Running automatic ethics pre-screen \u2014 checking harm, legal risk, and problem-solution integrity...",
            "info"
        )

        prob_def = json.dumps(self.state.problem_definition, indent=2) \
                   if self.state.problem_definition else \
                   f"Problem: {self.state.problem}\nSolution: {self.state.solution}"

        task = Task(
            description=(
                f"Evaluate this startup idea through the three ethics gates.\n\n"
                f"STARTUP IDEA:\n{prob_def}\n\n"
                "Apply all three gates exactly as defined in your skill. "
                "Produce the ETHICS_OUTPUT: JSON block. No questions, no preamble."
            ),
            expected_output="ETHICS_OUTPUT: JSON block with all required fields.",
            agent=self._ethics_agent,
        )
        result = self._run_agent(task, self._ethics_agent)
        ethics_out = self._extract_json_block(result, "ETHICS_OUTPUT:")

        if not ethics_out:
            # If parsing failed, emit the raw result and pass through
            self._emit_system("Ethics screen completed (could not parse structured output — proceeding).", "warning")
            self.state.ethics_output = {"ethics_pass": True, "compliance_flag": False, "rejection_reason": ""}
            self._emit_phase("ethics", "Ethics Pre-Screen", "done")
            return problem

        self.state.ethics_output = ethics_out
        ethics_pass     = ethics_out.get("ethics_pass", True)
        compliance_flag = ethics_out.get("compliance_flag", False)
        rejection_reason= ethics_out.get("rejection_reason", "")

        # Emit the structured ethics card to the UI
        self._emit({"type": "ethics", "content": ethics_out})

        if not ethics_pass:
            self._emit_phase("ethics", "Ethics Pre-Screen", "blocked")
            self._emit_system(
                f"\u274c  This idea did not pass the ethics pre-screen and cannot proceed. "
                f"Reason: {rejection_reason}",
                "error"
            )
            self._emit({"type": "exit"})
            self._exiting = True
            return None

        if compliance_flag:
            self._emit_system(
                "\u26a0  Ethics gate passed with a compliance flag \u2014 "
                "this idea operates in a regulated space and will need legal review before launch.",
                "warning"
            )
        else:
            self._emit_system("\u2713  Ethics pre-screen passed.", "success")

        self._emit_phase("ethics", "Ethics Pre-Screen", "done")
        return problem

    @listen(ethics_screening)
    def market_scan(self, problem):
        """Step 3 \u2014 Market Scout (optional, same logic as before)."""
        if problem is None:
            return None

        self._emit_phase("market_scan", "Market Intelligence Scan", "active")
        self._emit_system("Scanning competitive landscape\u2026", "info")

        prior_memory = self._recall_similar_problems(problem)
        if prior_memory:
            self._emit_system("Found similar past validations in memory.", "info")

        web_ctx     = self._market_scout_search_context(problem)
        description = (
            f"PROBLEM: {problem}\n\n"
            f"{prior_memory}\n"
            f"WEB CONTEXT:\n{web_ctx}\n\n"
            f"{self._scout_skill}\n\n"
            "Produce a competitive landscape report. "
            "End with VERDICT: REJECT or VERDICT: PROCEED."
        )

        scan_task = Task(
            description=description,
            expected_output="Competitive landscape report with VERDICT: REJECT or VERDICT: PROCEED.",
            agent=self._market_scout_agent,
        )

        result = self._run_agent(scan_task, self._market_scout_agent)
        self.state.market_report = result

        verdict = "REJECT" if "VERDICT: REJECT" in result.upper() else "PROCEED"
        self.state.market_verdict = verdict

        self._emit_agent(result, agent_name="Market Scout", phase="market_scan")

        if verdict == "REJECT":
            self._emit_system(
                "\u26a0  Market Scout flagged this market as highly saturated. Review the report above.",
                "warning",
            )
            self._save_rejection(problem, result[:300])
            choice = self._ask(
                "Type  continue  to proceed anyway, or anything else to exit.",
                context="market_rejection",
            )
            if choice.strip().lower() != "continue":
                self._emit_system(
                    "Session ended. Consider refining your problem or exploring adjacent markets.",
                    "info",
                )
                self._emit({"type": "exit"})
                self._exiting = True
                return None
            self._emit_system("Proceeding at team\u2019s discretion.", "info")
        else:
            self._emit_system(
                "\u2713  Market Scout found meaningful room in this market.",
                "success",
            )

        angle = self._ask(
            "Based on the market analysis, what is your differentiation angle? "
            "Which gap will you target, who will you serve, and what will you do differently?",
            context="market_angle",
        )
        self.state.market_angle = angle
        return problem

    @listen(market_scan)
    def tips_evaluation_loop(self, problem):
        """Step 4 \u2014 TIPS Evaluation Agent, max 2 clarifying questions."""
        if problem is None:
            return None

        MAX_TURNS = 2
        self._emit_phase("tips", "TIPS Evaluation", "active")
        self._emit_system(
            "The TIPS Agent will now score your problem on Timely, Important, "
            "Profitable, and Solvable dimensions (C is skipped for now).",
            "info"
        )

        prior_idea = self._recall_similar_solutions(self.state.problem, self.state.solution)
        if prior_idea:
            self._emit_system("Found similar past evaluations in memory.", "info")

        history: List[dict] = []

        for turn in range(1, MAX_TURNS + 2):   # max 2 questions + 1 final
            force = (turn > MAX_TURNS)
            desc  = self._build_tips_task(history, force=force)

            task = Task(
                description=desc,
                expected_output=(
                    "TIPS_OUTPUT: JSON block with scores and verdict."
                    if force else
                    "Either a single TIPS clarifying question (TIPS_TRIAGE format), "
                    "or TIPS_OUTPUT: JSON if all dimensions are clear."
                ),
                agent=self._tips_agent,
            )
            result = self._run_agent(task, self._tips_agent)
            history.append({"role": "agent", "content": result})
            self.state.tips_history = history

            if self._tips_complete(result):
                self._emit_agent(result, agent_name="TIPS Evaluator", phase="tips")
                break

            self._emit_agent(result, agent_name="TIPS Evaluator", phase="tips")

            if not force:
                answer = self._ask("Your response:", context="tips")
                history.append({"role": "user", "content": answer})

        # Extract TIPS JSON
        final_text = history[-1]["content"] if history else ""
        tips_out   = self._extract_tips_output(final_text)
        self.state.tips_output = tips_out

        verdict = tips_out.get("overall_verdict", "PROCEED_TO_DFV")
        if verdict == "PROCEED_TO_DFV":
            self._emit_system(
                "\u2713  TIPS evaluation complete \u2014 your idea is cleared for DFV analysis!",
                "success"
            )
        elif verdict == "NOT_VIABLE":
            self._emit_system(
                "\u26a0  The TIPS agent has found critical gaps. Review coaching notes before proceeding.",
                "warning"
            )
        else:
            self._emit_system(
                "\u26a0  Some TIPS dimensions need refinement. Review coaching notes.",
                "warning"
            )

        # ── TIPS Follow-Up Round (1 round to strengthen weak dimensions) ────
        # Only run if there are YELLOW or RED scores and TIPSC is not a hard failure
        if tips_out and tips_out.get("overall_verdict") != "NOT_VIABLE":
            tips_scores = tips_out.get("tips_scores", {})
            weak_dims = [k for k, v in tips_scores.items() if v in ("YELLOW", "RED")]
            if weak_dims:
                dims_str = ", ".join(weak_dims)
                self._emit_system(
                    f"Your idea has weak dimensions ({dims_str}). "
                    "You have ONE opportunity to provide additional evidence to improve your scores.",
                    "info"
                )
                followup_answer = self._ask(
                    f"Provide additional evidence or context for: {dims_str}. "
                    "Be specific — what data, research, or plan addresses these gaps?",
                    context="tips_followup",
                )

                if followup_answer and followup_answer.strip():
                    import json as _json
                    followup_desc = (
                        f"ORIGINAL TIPS SCORES:\n{_json.dumps(tips_out, indent=2)}\n\n"
                        f"WEAK DIMENSIONS: {dims_str}\n\n"
                        f"ADDITIONAL FOUNDER EVIDENCE:\n{followup_answer}\n\n"
                        "Re-evaluate ONLY the weak dimensions using the new evidence. "
                        "Apply scoring rules exactly. Upgrade if evidence is strong, keep RED if not. "
                        "Recompute overall_verdict. Produce TIPS_OUTPUT: JSON now."
                    )
                    followup_task = Task(
                        description=followup_desc,
                        expected_output="TIPS_OUTPUT: JSON with re-scored dimensions and updated overall_verdict.",
                        agent=self._tips_agent,
                    )
                    followup_result = self._run_agent(followup_task, self._tips_agent)
                    updated_tips = self._extract_json_block(followup_result, "TIPS_OUTPUT:")
                    if updated_tips:
                        self._emit_agent(followup_result, agent_name="TIPS Evaluator", phase="tips")
                        tips_out = updated_tips
                        self.state.tips_output = tips_out
                        self._emit_system("\u2713  TIPS scores updated based on your additional evidence.", "success")

        return tips_out

    @listen(tips_evaluation_loop)
    def dfv_report_loop(self, tips_result):
        """Step 5 — Generate the final DFV Report using the DFV Agent."""
        if tips_result is None:
            return None

        self._emit_phase("report", "Final Report", "active")
        self._emit_system("Generating final Desirability, Feasibility, Viability (DFV) report...", "info")

        import json
        prob_def = json.dumps(self.state.problem_definition, indent=2)
        market_rep = self.state.market_report or "No market scan conducted."
        tips_rep = json.dumps(self.state.tips_output, indent=2)

        task = Task(
            description=(
                f"Generate a final DFV report for this startup idea.\n\n"
                f"PROBLEM DEFINITION:\n{prob_def}\n\n"
                f"MARKET SCAN:\n{market_rep}\n\n"
                f"TIPS SCORECARD:\n{tips_rep}\n\n"
                "Return the DFV_OUTPUT: JSON block. No markdown, no explanations."
            ),
            expected_output="DFV_OUTPUT: JSON block with desirability, feasibility, viability.",
            agent=self._dfv_agent,
        )
        result = self._run_agent(task, self._dfv_agent)
        dfv_out = self._extract_json_block(result, "DFV_OUTPUT:")
        
        if dfv_out:
            self.state.dfv_output = dfv_out
            self._emit_system("✓  DFV report successfully generated.", "success")
        else:
            self._emit_system("DFV generation completed (could not parse output).", "warning")
            self.state.dfv_output = {"desirability": "Error generating.", "feasibility": "Error generating.", "viability": "Error generating."}

        return dfv_out

    @listen(dfv_report_loop)
    def final_json_output(self, dfv_result):
        """Step 6 \u2014 Save to memory and emit final JSON."""
        if dfv_result is None:
            return None

        self._emit_phase("report", "Final Report", "active")

        # Save to cross-session memory
        try:
            import json
            self._save_validation_result(
                problem    = self.state.problem,
                solution   = self.state.solution,
                opp_report = json.dumps(self.state.tips_output, indent=2),
                idea_report= json.dumps(self.state.problem_definition, indent=2),
            )
        except Exception:
            pass

        self._emit_complete()
        return dfv_result


# ══════════════════════════════════════════════════════════════════════════════
# THREAD ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def _friendly_error(e: Exception) -> str:
    """Convert a raw exception into a user-readable message."""
    s = str(e)
    if "Connection error" in s or "ConnectionError" in s or "connection" in s.lower():
        base_url = cfg.get("llm", {}).get("base_url", "http://localhost:1234/v1")
        return (
            f"Cannot connect to the LLM endpoint ({base_url}). "
            "Make sure LM Studio (or your local LLM server) is running and a model is loaded, "
            "then click \u2018New Validation\u2019 to try again."
        )
    if "InternalServerError" in s or "litellm" in s.lower():
        return f"LLM server error: {s.split(chr(10))[0]}"
    return s


def run_chat_flow(input_queue: queue.Queue, event_queue: queue.Queue):
    """Run the validation flow in a background thread."""
    try:
        flow = ChatValidationFlow(input_queue, event_queue)
        flow.kickoff()
    except Exception as e:
        event_queue.put({"type": "error", "content": _friendly_error(e)})
    finally:
        event_queue.put(None)   # sentinel: stream is done

        """Quick sanity-check: hit the LLM with a tiny prompt to confirm it's reachable."""
        base_url = cfg["llm"].get("base_url", "http://localhost:1234/v1")
        try:
            import requests as _req
            resp = _req.get(f"{base_url}/models",
                            headers={"Authorization": f"Bearer {cfg['llm'].get('api_key','')}"},
                            timeout=5)
            # Any HTTP response (even 401) means the server is up
        except Exception:
            raise ConnectionError(
                f"Cannot connect to the LLM server at {base_url}. "
                "Please start LM Studio (or your local LLM server), load a model, "
                "then click 'New Validation' to try again."
            )

    @start()
    def collect_inputs(self):
        self._emit_phase("intro", "Getting Started", "active")

        # ── Pre-flight LLM connectivity check ──────────────────────────────
        self._emit_system("Checking LLM connection…", "info")
        try:
            self._check_llm_connection()
            self._emit_system(
                f"✓  LLM connected ({cfg['llm'].get('base_url','localhost:1234')})",
                "success"
            )
        except ConnectionError as ce:
            self._emit_system(str(ce), "error")
            self._emit({"type": "error", "content": str(ce)})
            return None   # abort the flow cleanly

        self._emit_system(ui_str["startup"]["intro"], "info")

        status_msg = (
            "✓  Web search enabled — live market data will be injected"
            if self._search_on else
            "⚠  Web search not configured — agents will evaluate without web data"
        )
        self._emit_system(status_msg, "success" if self._search_on else "warning")

        self.state.problem = self._ask(
            "Describe the problem you want to solve:",
            context="problem",
        )
        self.state.solution = self._ask(
            "Describe your idea or proposed solution:",
            context="solution",
        )
        return self.state.problem

    @listen(collect_inputs)
    def market_scan(self, problem):
        if problem is None:
            return None   # LLM connection failed upstream
        self._emit_phase("market_scan", "Market Intelligence Scan", "active")
        self._emit_system("Scanning competitive landscape…", "info")

        prior_memory = self._recall_similar_problems(problem)
        if prior_memory:
            self._emit_system("Found similar past validations in memory.", "info")

        web_ctx     = self._market_scout_search_context(problem)
        description = (
            f"PROBLEM: {problem}\n\n"
            f"{prior_memory}\n"
            f"WEB CONTEXT:\n{web_ctx}\n\n"
            f"{self._scout_skill}\n\n"
            "Produce a competitive landscape report. "
            "End with VERDICT: REJECT or VERDICT: PROCEED."
        )

        scan_task = Task(
            description=description,
            expected_output="Competitive landscape report with VERDICT: REJECT or VERDICT: PROCEED.",
            agent=self._market_scout_agent,
        )

        result = self._run_agent(scan_task, self._market_scout_agent)
        self.state.market_report = result

        verdict = "REJECT" if "VERDICT: REJECT" in result.upper() else "PROCEED"
        self.state.market_verdict = verdict

        self._emit_agent(result, agent_name="Market Scout", phase="market_scan")

        if verdict == "REJECT":
            self._emit_system(
                "⚠  Market Scout flagged this market as highly saturated. Review the report above.",
                "warning",
            )
            self._save_rejection(problem, result[:300])
            choice = self._ask(
                "Type  continue  to proceed with the evaluation anyway, or anything else to exit.",
                context="market_rejection",
            )
            if choice.strip().lower() != "continue":
                self._emit_system(
                    "Session ended. Consider refining your problem or exploring adjacent opportunities.",
                    "info",
                )
                self._emit({"type": "exit"})
                self._exiting = True
                return None
            self._emit_system("Proceeding at founder's discretion.", "info")
        else:
            self._emit_system(
                "✓  Market Scout found meaningful room in this market. Study the report before proceeding.",
                "success",
            )

        angle = self._ask(
            "Based on the market analysis above, describe your differentiation angle — which gap will you target, who will you serve, and what will you do differently?",
            context="market_angle",
        )
        self.state.market_angle = angle
        return problem

    @listen(market_scan)
    def tipsc_triage(self, problem):
        if problem is None:
            return None

        self._emit_phase("opportunity", "Opportunity Evaluation", "active")
        self._emit_system("Running TIPSC triage…", "info")

        prior_opp   = self._recall_similar_problems(problem)
        angle_block = (
            f"\nFOUNDER'S DIFFERENTIATION ANGLE:\n{self.state.market_angle}\n"
            if self.state.market_angle else ""
        )
        budget_note = (
            f"\nIMPORTANT: You have a maximum of {MAX_OPP_QUESTIONS} questions total "
            "to validate this opportunity. Choose wisely — target the weakest TIPSC "
            "criteria only. Do NOT probe COP.\n"
        )

        description = (
            f"PROBLEM: {problem}\n"
            f"{budget_note}"
            f"{angle_block}"
            f"{prior_opp}\n"
            "Using your TIPSC framework (T/I/P/S/C only — no COP), "
            "triage the problem and ask your FIRST question targeting the weakest criterion.\n\n"
            "FORMAT:\n"
            "TIPSC TRIAGE:\n"
            "T — Timely:     [Strong/Weak/Unclear] — [one sentence]\n"
            "I — Important:  [Strong/Weak/Unclear] — [one sentence]\n"
            "P — Profitable: [Strong/Weak/Unclear] — [one sentence]\n"
            "S — Solvable:   [Strong/Weak/Unclear] — [one sentence]\n"
            "C — Contextual: [Strong/Weak/Unclear] — [one sentence]\n\n"
            "STATUS: NEEDS_MORE_INFO\n"
            "CRITERION IN FOCUS: [letter]\n"
            "QUESTION:\n[single focused question]"
        )

        triage_task = Task(
            description=description,
            expected_output="TIPSC triage table and opening question.",
            agent=self._opportunity_agent,
        )

        result = self._run_agent(triage_task, self._opportunity_agent)
        self.state.opp_history.append({"role": "agent", "content": result})
        self.state.opp_report          = result
        self.state.opp_status          = "IN_PROGRESS"
        self.state.opp_questions_asked = 1
        return result

    @listen(tipsc_triage)
    def opportunity_loop(self, triage_result):
        if triage_result is None:
            return None

        report = triage_result

        while not self._opp_approved(report):
            # Hard cap — force approval
            if self.state.opp_questions_asked >= MAX_OPP_QUESTIONS:
                self._emit_system(
                    f"Question budget ({MAX_OPP_QUESTIONS}) reached — forcing verdict now.",
                    "info",
                )
                force_task = Task(
                    description=(
                        f"You have asked {MAX_OPP_QUESTIONS} questions. "
                        "MANDATORY: issue STATUS: APPROVED now with a summary. "
                        "Do not ask any more questions.\n\n"
                        f"PROBLEM: {self.state.problem}\n"
                        f"CONVERSATION:\n{self._format_history(self.state.opp_history)}"
                    ),
                    expected_output="STATUS: APPROVED with summary.",
                    agent=self._opportunity_agent,
                )
                report = self._run_agent(force_task, self._opportunity_agent)
                self.state.opp_history.append({"role": "agent", "content": report})
                self.state.opp_report = report
                break

            self._emit_agent(report, agent_name="Opportunity Evaluator", phase="opportunity")

            answer = self._ask(ui_str["prompts"]["response_input"], context="opp_answer")
            self.state.opp_history.append({"role": "user", "content": answer})

            last_q, last_a = self._last_exchange(self.state.opp_history)
            rep_warning    = self._repetition_warning(self.state.opp_last_q, last_q)
            history_text   = self._format_history(self.state.opp_history)
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
                    "Do NOT probe COP.\n"
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
                agent=self._opportunity_agent,
            )

            report = self._run_agent(followup_task, self._opportunity_agent)
            self.state.opp_last_q = self._extract_question(report)
            self.state.opp_history.append({"role": "agent", "content": report})
            self.state.opp_report = report

            if not self._opp_approved(report):
                self.state.opp_questions_asked += 1

        self.state.opp_status = "APPROVED"
        self._emit_agent(report, agent_name="Opportunity Evaluator", phase="opportunity")
        self._emit_system("✓  Opportunity validated! Moving to idea evaluation…", "success")
        return self.state.solution

    @listen(opportunity_loop)
    def evaluate_idea(self, solution):
        if solution is None:
            return None

        self._emit_phase("idea", "Idea Evaluation", "active")
        self._emit_system("Running PSEA evaluation…", "info")

        prior_idea = self._recall_similar_solutions(self.state.problem, solution)
        if prior_idea:
            self._emit_system("Recalling similar past solution evaluations from memory.", "info")

        description = (
            f"VALIDATED PROBLEM: {self.state.problem}\n"
            f"PROPOSED SOLUTION: {solution}\n\n"
            f"{prior_idea}\n"
            "Evaluate using PSEA + initial feasibility check.\n\n"
            "FORMAT:\n"
            "PSEA EVALUATION:\n"
            "Problem-Solution Fit: [Strong/Weak/Unclear] — [explanation]\n"
            "Simplicity:           [Good/Over-engineered/Unclear] — [explanation]\n"
            "Ethics:               [Pass/Concern/Fail] — [explanation]\n"
            "Key Assumptions:\n"
            "  1. [assumption]\n"
            "Initial Feasibility:  [Viable/Questionable/Infeasible] — [explanation]\n\n"
            "If critical issues:\n"
            "VERDICT: NEEDS_REFINEMENT\n"
            "ISSUES: [bullet list]\n"
            "QUESTION: [single question]\n\n"
            "If acceptable:\n"
            "VERDICT: READY_FOR_DFV\n"
            "EVALUATION SUMMARY: [full verdicts]\n"
            "NEXT STEP: DFV Evaluation"
        )

        eval_task = Task(
            description=description,
            expected_output="PSEA evaluation with verdict.",
            agent=self._idea_agent,
        )

        result = self._run_agent(eval_task, self._idea_agent)
        self.state.idea_history.append({"role": "agent", "content": result})
        self.state.idea_report = result
        self.state.idea_status = "IN_PROGRESS"
        return result

    @listen(evaluate_idea)
    def idea_loop(self, eval_result):
        if eval_result is None:
            return None

        report = eval_result

        while not self._idea_ready(report):
            self._emit_agent(report, agent_name="Idea Evaluator", phase="idea")

            answer = self._ask(ui_str["prompts"]["response_input"], context="idea_answer")
            self.state.idea_history.append({"role": "user", "content": answer})

            last_q, last_a = self._last_exchange(self.state.idea_history)
            rep_warning    = self._repetition_warning(self.state.idea_last_q, last_q)
            history_text   = self._format_history(self.state.idea_history)

            description = (
                f"{rep_warning}\n"
                f"QUESTION: {last_q}\n"
                f"ANSWER:   {last_a}\n"
                f"PROBLEM:  {self.state.problem}\n"
                f"SOLUTION: {self.state.solution}\n"
                f"HISTORY:\n{history_text}\n\n"
                "Evaluate and decide. If all PSEA criteria met, approve.\n\n"
                "FORMAT if refinement needed:\n"
                "FEEDBACK: [2-3 sentences]\n"
                "ISSUE IN FOCUS: [PSEA criterion]\n"
                "VERDICT: NEEDS_REFINEMENT\n"
                "QUESTION: [single question]\n\n"
                "FORMAT if approved:\n"
                "FEEDBACK: [acknowledgment]\n"
                "VERDICT: READY_FOR_DFV\n"
                "EVALUATION SUMMARY: [full PSEA verdicts]\n"
                "NEXT STEP: DFV Evaluation"
            )

            refinement_task = Task(
                description=description,
                expected_output="Feedback + next question or DFV clearance.",
                agent=self._idea_agent,
            )

            report = self._run_agent(refinement_task, self._idea_agent)
            self.state.idea_last_q = self._extract_question(report)
            self.state.idea_history.append({"role": "agent", "content": report})
            self.state.idea_report = report

        self.state.idea_status = "READY_FOR_DFV"
        self._emit_agent(report, agent_name="Idea Evaluator", phase="idea")
        return report

    @listen(idea_loop)
    def final_report(self, idea_result):
        if idea_result is None:
            return None

        self._emit_phase("report", "Final Report", "active")
        self._emit_system(
            "🎉  Both opportunity and solution have passed validation! "
            "You are ready for DFV (Desirability · Feasibility · Viability) analysis.",
            "success",
        )

        self._save_validation_result(
            problem    = self.state.problem,
            solution   = self.state.solution,
            opp_report = self.state.opp_report,
            idea_report= idea_result,
        )

        self.state.idea_status = "COMPLETE"
        self._emit_complete()
        return idea_result


# ══════════════════════════════════════════════════════════════════════════════
# THREAD ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def _friendly_error(e: Exception) -> str:
    """Convert a raw exception into a user-readable message."""
    s = str(e)
    if "Connection error" in s or "ConnectionError" in s or "connection" in s.lower():
        base_url = cfg.get("llm", {}).get("base_url", "http://localhost:1234/v1")
        return (
            f"Cannot connect to the LLM endpoint ({base_url}). "
            "Make sure LM Studio (or your local LLM server) is running and a model is loaded, "
            "then click \u2018New Validation\u2019 to try again."
        )
    if "InternalServerError" in s or "litellm" in s.lower():
        return f"LLM server error: {s.split(chr(10))[0]}"
    return s


def run_chat_flow(input_queue: queue.Queue, event_queue: queue.Queue):
    """Run the validation flow in a background thread."""
    try:
        flow = ChatValidationFlow(input_queue, event_queue)
        flow.kickoff()
    except Exception as e:
        event_queue.put({"type": "error", "content": _friendly_error(e)})
    finally:
        event_queue.put(None)   # sentinel: stream is done
