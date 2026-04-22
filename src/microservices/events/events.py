"""Events MVP-сервис — Producer + Consumer Kafka для «Кинобездны».

API (по OpenAPI):
    GET  /api/events/health    — health-check
    POST /api/events/movie     — публикация события фильма   (movie-events)
    POST /api/events/user      — публикация события user     (user-events)
    POST /api/events/payment   — публикация события платежа  (payment-events)

В фоне крутится один KafkaConsumer на все три топика — читает опубликованные
сообщения и пишет их в лог сервиса (проверка гипотезы, что сервис видит
собственные события).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, request
from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import KafkaError, NoBrokersAvailable

# --- конфигурация -----------------------------------------------------------

PORT = int(os.getenv("PORT", "8082"))
KAFKA_BROKERS = [b.strip() for b in os.getenv("KAFKA_BROKERS", "kafka:9092").split(",") if b.strip()]
CONSUMER_GROUP = os.getenv("KAFKA_CONSUMER_GROUP", "events-service")

TOPIC_MOVIE = "movie-events"
TOPIC_USER = "user-events"
TOPIC_PAYMENT = "payment-events"
ALL_TOPICS = [TOPIC_MOVIE, TOPIC_USER, TOPIC_PAYMENT]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("events")

app = Flask(__name__)

# --- Kafka producer ---------------------------------------------------------

_producer: KafkaProducer | None = None
_producer_lock = threading.Lock()


def get_producer() -> KafkaProducer:
    """Ленивая инициализация продюсера с ретраями — Kafka может стартовать дольше сервиса."""
    global _producer
    with _producer_lock:
        if _producer is not None:
            return _producer
        last_err: Exception | None = None
        for attempt in range(1, 31):
            try:
                _producer = KafkaProducer(
                    bootstrap_servers=KAFKA_BROKERS,
                    value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
                    retries=3,
                    acks="all",
                    linger_ms=10,
                )
                log.info("Kafka producer connected to %s", KAFKA_BROKERS)
                return _producer
            except NoBrokersAvailable as exc:
                last_err = exc
                log.warning("producer: kafka not ready (attempt %d/30): %s", attempt, exc)
                time.sleep(2)
        assert last_err is not None
        raise last_err


def publish(topic: str, event: dict[str, Any]) -> tuple[int, int]:
    p = get_producer()
    future = p.send(topic, value=event)
    metadata = future.get(timeout=10)
    p.flush()
    log.info(
        "PRODUCED topic=%s partition=%d offset=%d id=%s",
        metadata.topic, metadata.partition, metadata.offset, event.get("id"),
    )
    return metadata.partition, metadata.offset


# --- helpers ----------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def event_response(event: dict[str, Any], partition: int, offset: int):
    """Схема EventResponse из api-specification.yaml."""
    return jsonify({
        "status": "success",
        "partition": partition,
        "offset": offset,
        "event": event,
    }), 201


def require(fields: dict[str, Any], *names: str) -> str | None:
    """Вернуть имя первого отсутствующего/пустого поля или None."""
    for name in names:
        if fields.get(name) in (None, ""):
            return name
    return None


# --- API --------------------------------------------------------------------

@app.get("/api/events/health")
def health():
    return jsonify({"status": True}), 200


@app.post("/api/events/movie")
def create_movie_event():
    payload = request.get_json(force=True, silent=True) or {}
    missing = require(payload, "movie_id", "title", "action")
    if missing:
        return jsonify({"error": f"field '{missing}' is required"}), 400

    event = {
        "id": f"movie-{payload['movie_id']}-{payload['action']}",
        "type": "movie",
        "timestamp": now_iso(),
        "payload": payload,
    }
    try:
        partition, offset = publish(TOPIC_MOVIE, event)
    except KafkaError as exc:
        log.exception("publish failed: %s", exc)
        return jsonify({"error": str(exc)}), 500
    return event_response(event, partition, offset)


@app.post("/api/events/user")
def create_user_event():
    payload = request.get_json(force=True, silent=True) or {}
    missing = require(payload, "user_id", "action", "timestamp")
    if missing:
        return jsonify({"error": f"field '{missing}' is required"}), 400

    event = {
        "id": f"user-{payload['user_id']}-{payload['action']}",
        "type": "user",
        "timestamp": now_iso(),
        "payload": payload,
    }
    try:
        partition, offset = publish(TOPIC_USER, event)
    except KafkaError as exc:
        log.exception("publish failed: %s", exc)
        return jsonify({"error": str(exc)}), 500
    return event_response(event, partition, offset)


@app.post("/api/events/payment")
def create_payment_event():
    payload = request.get_json(force=True, silent=True) or {}
    missing = require(payload, "payment_id", "user_id", "amount", "status", "timestamp")
    if missing:
        return jsonify({"error": f"field '{missing}' is required"}), 400

    event = {
        "id": f"payment-{payload['payment_id']}-{payload['status']}",
        "type": "payment",
        "timestamp": now_iso(),
        "payload": payload,
    }
    try:
        partition, offset = publish(TOPIC_PAYMENT, event)
    except KafkaError as exc:
        log.exception("publish failed: %s", exc)
        return jsonify({"error": str(exc)}), 500
    return event_response(event, partition, offset)


# --- Kafka consumer ---------------------------------------------------------

def consume_forever() -> None:
    """Фоновый консьюмер — читает все три топика и логирует события."""
    consumer: KafkaConsumer | None = None
    for attempt in range(1, 31):
        try:
            consumer = KafkaConsumer(
                *ALL_TOPICS,
                bootstrap_servers=KAFKA_BROKERS,
                value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                auto_offset_reset="earliest",
                enable_auto_commit=True,
                group_id=CONSUMER_GROUP,
                client_id="events-service",
            )
            log.info(
                "Kafka consumer connected to %s, topics=%s, group=%s",
                KAFKA_BROKERS, ALL_TOPICS, CONSUMER_GROUP,
            )
            break
        except NoBrokersAvailable as exc:
            log.warning("consumer: kafka not ready (attempt %d/30): %s", attempt, exc)
            time.sleep(2)
    else:
        log.error("consumer: failed to connect after 30 attempts, giving up")
        return

    assert consumer is not None
    try:
        for msg in consumer:
            log.info(
                "CONSUMED topic=%s partition=%d offset=%d event=%s",
                msg.topic, msg.partition, msg.offset, msg.value,
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("consumer loop crashed: %s", exc)


def start_consumer_thread() -> None:
    t = threading.Thread(target=consume_forever, name="kafka-consumer", daemon=True)
    t.start()
    log.info("Consumer thread started")


# --- точка входа ------------------------------------------------------------

def main() -> None:
    log.info(
        "Events service starting on :%d | kafka=%s | topics=%s | group=%s",
        PORT, KAFKA_BROKERS, ALL_TOPICS, CONSUMER_GROUP,
    )
    start_consumer_thread()
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=8)


if __name__ == "__main__":
    main()
