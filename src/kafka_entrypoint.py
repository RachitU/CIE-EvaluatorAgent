"""Start the Kafka-driven pipeline. Run instead of main.py."""
from kafka.consumer import start_answer_listener, start_request_listener
from kafka.session_runner import run_session
import time

if __name__ == "__main__":
    print("Starting CIE Evaluator Agent (Kafka mode)...")
    start_answer_listener()           # thread: routes answers to session queues
    start_request_listener(run_session)  # blocking: waits for new eval requests