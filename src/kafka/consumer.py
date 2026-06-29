import json
import threading
from confluent_kafka import Consumer
from kafka.topics import BOOTSTRAP_SERVERS, GROUP_ID, EVAL_REQUESTS, EVAL_ANSWERS

# Per-session answer queues: session_id -> threading.Queue
_answer_queues: dict[str, "Queue"] = {}
_lock = threading.Lock()

def get_or_create_queue(session_id: str):
    from queue import Queue
    with _lock:
        if session_id not in _answer_queues:
            _answer_queues[session_id] = Queue()
        return _answer_queues[session_id]

def wait_for_answer(session_id: str, timeout: int = 300) -> str:
    """Block until the founder sends an answer for this session."""
    q = get_or_create_queue(session_id)
    answer = q.get(timeout=timeout)   # raises queue.Empty on timeout
    return answer

def start_answer_listener():
    """Background thread: routes incoming answers to the right session queue."""
    def _listen():
        c = Consumer({
            "bootstrap.servers": BOOTSTRAP_SERVERS,
            "group.id": f"{GROUP_ID}-answers",
            "auto.offset.reset": "latest",
        })
        c.subscribe([EVAL_ANSWERS])
        while True:
            msg = c.poll(1.0)
            if msg is None or msg.error():
                continue
            payload = json.loads(msg.value())
            sid = payload.get("session_id")
            answer = payload.get("answer", "")
            if sid:
                q = get_or_create_queue(sid)
                q.put(answer)

    t = threading.Thread(target=_listen, daemon=True)
    t.start()

def start_request_listener(on_new_session):
    """Background thread: triggers pipeline for each new eval request."""
    c = Consumer({
        "bootstrap.servers": BOOTSTRAP_SERVERS,
        "group.id": GROUP_ID,
        "auto.offset.reset": "earliest",
    })
    c.subscribe([EVAL_REQUESTS])
    while True:
        msg = c.poll(1.0)
        if msg is None or msg.error():
            continue
        payload = json.loads(msg.value())
        session_id = payload.get("session_id")
        if session_id:
            # Run each session in its own thread so multiple can run in parallel
            t = threading.Thread(
                target=on_new_session,
                args=(session_id, payload),
                daemon=True,
            )
            t.start()