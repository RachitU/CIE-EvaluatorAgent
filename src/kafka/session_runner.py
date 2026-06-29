import json
from kafka.producer import publish_question, publish_result, publish_status, publish_error
from kafka.consumer import wait_for_answer
from main import (
    load_yaml, load_text, load_llm,
    run_validation, run_ethics, run_tipsc, run_followup,
    clean_json, call_llm_for_json, save_json,
    print_validation_summary, print_ethics_result, print_tipsc_summary,
)
from models import PreEvalOutput

BASE_DIR = ...  # same as main.py

def run_preeval_kafka(llm, skill_text: str, session_id: str) -> PreEvalOutput:
    """Preeval interview over Kafka instead of stdin."""
    MAX_TURNS = 6
    messages = [
        {"role": "system", "content": skill_text},
        {"role": "user", "content": "Begin the interview. Ask the first question."},
    ]

    for turn in range(MAX_TURNS):
        ai_text = llm.call(messages).strip()

        # Publish question to Kafka → frontend shows it to founder
        publish_question(session_id, ai_text, turn + 1)

        # Wait for founder's answer (comes back via cie.eval.answers)
        answer = wait_for_answer(session_id, timeout=600)

        messages.append({"role": "assistant", "content": ai_text})
        messages.append({"role": "user", "content": answer or "(skipped)"})

    # Summarise into JSON — same logic as original run_preeval()
    summary_prompt = "..." # same as in main.py
    summary_messages = messages + [{"role": "user", "content": summary_prompt}]
    raw = clean_json(call_llm_for_json(llm, summary_messages))
    return PreEvalOutput.model_validate_json(raw)


def run_session(session_id: str, payload: dict):
    """Full pipeline for one session — called in its own thread."""
    try:
        agents_cfg = load_yaml("config/agents.yaml")
        task_cfg   = load_yaml("config/tasks.yaml")
        preeval_skill  = load_text("skills/preeval/SKILL.md")
        tipsc_rubric   = load_text("skills/tipsc/SKILL.md")
        ethics_rubric  = load_text("skills/ethics/SKILL.md")
        llm = load_llm()

        # Phase 1
        publish_status(session_id, "PHASE_1_PREEVAL")
        preeval_out = run_preeval_kafka(llm, preeval_skill, session_id)
        publish_result(session_id, "preeval", preeval_out.model_dump())

        # Phase 2
        publish_status(session_id, "PHASE_2_VALIDATION")
        validation_out = run_validation(llm, preeval_out, agents_cfg, task_cfg)
        validation_context = validation_out.model_dump_json(indent=2)
        publish_result(session_id, "validation", validation_out.model_dump())

        # Phase 3
        publish_status(session_id, "PHASE_3_ETHICS")
        ethics_out = run_ethics(llm, preeval_out, agents_cfg, task_cfg,
                                ethics_rubric, validation_context=validation_context)
        publish_result(session_id, "ethics", ethics_out.model_dump())

        if not ethics_out.ethics_pass:
            publish_status(session_id, "REJECTED_ETHICS")
            return

        # Phase 4
        publish_status(session_id, "PHASE_4_TIPSC")
        tips_out = run_tipsc(llm, preeval_out, agents_cfg, task_cfg,
                             tipsc_rubric, validation_context=validation_context)
        publish_result(session_id, "tipsc", tips_out.model_dump())

        # Follow-up loop
        followup_context = ""
        for turn in range(3):
            followup = run_followup(llm, tips_out, agents_cfg, task_cfg,
                                    followup_context=followup_context)
            if not followup.needs_followup or not followup.questions:
                break

            question = followup.questions[0]
            publish_question(session_id, question, turn + 1)
            answer = wait_for_answer(session_id, timeout=600)

            followup_context += f"\nQ{turn+1}: {question}\nA: {answer}\n"
            tips_out = run_tipsc(llm, preeval_out, agents_cfg, task_cfg,
                                 tipsc_rubric, validation_context=validation_context,
                                 followup_context=followup_context)

        publish_result(session_id, "tipsc_final", tips_out.model_dump())
        publish_status(session_id, "COMPLETE")

    except Exception as e:
        publish_error(session_id, str(e))
        raise