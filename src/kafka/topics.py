import yaml, os

with open("config/kafka.yaml") as f:
    cfg = yaml.safe_load(f)

env = os.environ.get("APP_ENV", "local")
kafka = cfg["kafka"]
env_overrides = kafka["environments"].get(env, {})

BOOTSTRAP_SERVERS = os.environ.get(
    "KAFKA_BOOTSTRAP_SERVERS",
    env_overrides.get("bootstrap_servers", kafka["bootstrap_servers"])
)
# Central topic registry — change broker/topic names here only
BOOTSTRAP_SERVERS = "localhost:9092"
GROUP_ID = "cie-evaluator-group"

# Inbound
EVAL_REQUESTS = "cie.eval.requests"   # trigger: {"session_id": "...", "type": "start"}
EVAL_ANSWERS  = "cie.eval.answers"    # founder answers: {"session_id": "...", "answer": "..."}

# Outbound
EVAL_QUESTIONS = "cie.eval.questions" # agent asks founder: {"session_id": "...", "question": "..."}
EVAL_RESULTS   = "cie.eval.results"   # phase results: {"session_id": "...", "phase": "preeval", "result": {...}}
EVAL_ERRORS    = "cie.eval.errors"    # failures: {"session_id": "...", "error": "..."}
EVAL_STATUS    = "cie.eval.status"    # progress updates: {"session_id": "...", "status": "PHASE_2_VALIDATION"}