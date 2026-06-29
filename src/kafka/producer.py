import json
from confluent_kafka import Producer
from kafka.topics import BOOTSTRAP_SERVERS

_producer = None

def get_producer() -> Producer:
    global _producer
    if _producer is None:
        _producer = Producer({"bootstrap.servers": BOOTSTRAP_SERVERS})
    return _producer

def publish(topic: str, payload: dict, session_id: str = None):
    p = get_producer()
    if session_id:
        payload["session_id"] = session_id
    p.produce(topic, json.dumps(payload).encode("utf-8"))
    p.flush()

def publish_question(session_id: str, question: str, turn: int):
    from kafka.topics import EVAL_QUESTIONS
    publish(EVAL_QUESTIONS, {"question": question, "turn": turn}, session_id)

def publish_result(session_id: str, phase: str, result: dict):
    from kafka.topics import EVAL_RESULTS
    publish(EVAL_RESULTS, {"phase": phase, "result": result}, session_id)

def publish_status(session_id: str, status: str):
    from kafka.topics import EVAL_STATUS
    publish(EVAL_STATUS, {"status": status}, session_id)

def publish_error(session_id: str, error: str):
    from kafka.topics import EVAL_ERRORS
    publish(EVAL_ERRORS, {"error": error}, session_id)