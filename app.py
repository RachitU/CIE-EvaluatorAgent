"""
Flask Chat UI — Opportunity Validation System
=============================================
Endpoints:
  GET  /                        → serves the chat interface
  POST /api/start               → creates a new session, starts flow thread
  GET  /api/stream/<session_id> → SSE stream of events from the flow
  POST /api/message/<session_id>→ sends user message into the flow
  POST /api/reset               → tears down old session, creates fresh one
"""

import json
import queue
import threading
import time
import uuid

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from chat_flow import run_chat_flow

app = Flask(__name__)
app.secret_key = "opp-validator-2025-secret"

# session_id → { input_queue, event_queue, thread, buffer, done, created_at }
sessions: dict = {}


# ═══════════════════════════════════════════════════════════════════════════
# Session helpers
# ═══════════════════════════════════════════════════════════════════════════

def _create_session() -> str:
    session_id  = str(uuid.uuid4())
    input_queue = queue.Queue()
    event_queue = queue.Queue()

    thread = threading.Thread(
        target=run_chat_flow,
        args=(input_queue, event_queue),
        daemon=True,
        name=f"flow-{session_id[:8]}",
    )

    sessions[session_id] = {
        "input_queue": input_queue,
        "event_queue": event_queue,
        "thread":      thread,
        "buffer":      [],          # replay buffer for reconnects
        "done":        False,
        "created_at":  time.time(),
    }

    thread.start()
    return session_id


def _cleanup_old_sessions(max_age_seconds: int = 3600):
    now = time.time()
    stale = [sid for sid, s in sessions.items()
             if now - s["created_at"] > max_age_seconds]
    for sid in stale:
        try:
            sessions[sid]["input_queue"].put("exit")
        except Exception:
            pass
        sessions.pop(sid, None)


# ═══════════════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def api_start():
    _cleanup_old_sessions()
    session_id = _create_session()
    return jsonify({"session_id": session_id})


@app.route("/api/stream/<session_id>")
def api_stream(session_id):
    session = sessions.get(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    def generate():
        # Replay buffered events first (supports page refresh / reconnect)
        for event in list(session["buffer"]):
            yield f"data: {json.dumps(event)}\n\n"

        while not session["done"]:
            try:
                event = session["event_queue"].get(timeout=20)
            except queue.Empty:
                # Heartbeat keeps the connection alive through proxies/firewalls
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                continue

            if event is None:           # sentinel — flow is finished
                session["done"] = True
                break

            session["buffer"].append(event)
            yield f"data: {json.dumps(event)}\n\n"

            if event.get("type") in ("complete", "error", "exit"):
                session["done"] = True
                break

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/message/<session_id>", methods=["POST"])
def api_message(session_id):
    session = sessions.get(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    if session["done"]:
        return jsonify({"error": "Session is already complete"}), 400

    data    = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "Empty message"}), 400

    session["input_queue"].put(message)
    return jsonify({"status": "ok"})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    data           = request.get_json(silent=True) or {}
    old_session_id = data.get("session_id")

    # Gracefully unblock any waiting flow thread
    if old_session_id and old_session_id in sessions:
        old = sessions.pop(old_session_id)
        try:
            old["input_queue"].put("exit")
            old["done"] = True
        except Exception:
            pass

    _cleanup_old_sessions()
    session_id = _create_session()
    return jsonify({"session_id": session_id})


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n  Opportunity Validator Chat UI")
    print("  Open: http://localhost:5000\n")
    app.run(debug=False, host="0.0.0.0", port=5000, threaded=True)
