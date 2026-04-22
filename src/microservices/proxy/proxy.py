"""Strangler Fig Proxy — API Gateway для постепенной миграции монолита
«Кинобездны» на микросервисы.

Роутинг по префиксу пути:
  /api/movies*  — при GRADUAL_MIGRATION=true процент MOVIES_MIGRATION_PERCENT
                  идёт в movies-service, остальное — в монолит.
                  При GRADUAL_MIGRATION=false весь трафик уходит в movies-service
                  (миграция домена завершена).
  /api/events*  — events-service.
  всё остальное — монолит.
"""
from __future__ import annotations

import logging
import os
import random
from urllib.parse import urljoin

import requests
from flask import Flask, Response, request

# --- конфигурация ------------------------------------------------------------

PORT = int(os.getenv("PORT", "8000"))
MONOLITH_URL = os.getenv("MONOLITH_URL", "http://monolith:8080").rstrip("/")
MOVIES_SERVICE_URL = os.getenv("MOVIES_SERVICE_URL", "http://movies-service:8081").rstrip("/")
EVENTS_SERVICE_URL = os.getenv("EVENTS_SERVICE_URL", "http://events-service:8082").rstrip("/")
GRADUAL_MIGRATION = os.getenv("GRADUAL_MIGRATION", "true").strip().lower() == "true"


def _parse_percent(raw: str) -> int:
    try:
        return max(0, min(100, int(raw)))
    except (TypeError, ValueError):
        return 0


MOVIES_MIGRATION_PERCENT = _parse_percent(os.getenv("MOVIES_MIGRATION_PERCENT", "0"))

# Заголовки, которые нельзя просто передавать через прокси (hop-by-hop)
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("proxy")

app = Flask(__name__)


# --- роутинг -----------------------------------------------------------------


def pick_target(path: str) -> str:
    """Выбрать апстрим по пути запроса."""
    if path.startswith("/api/movies"):
        if not GRADUAL_MIGRATION:
            return MOVIES_SERVICE_URL
        if random.randint(0, 99) < MOVIES_MIGRATION_PERCENT:
            return MOVIES_SERVICE_URL
        return MONOLITH_URL
    if path.startswith("/api/events"):
        return EVENTS_SERVICE_URL
    return MONOLITH_URL


@app.get("/health")
def health() -> Response:
    """Health-check по контракту OpenAPI: text/plain, 200."""
    return Response("Strangler Fig Proxy is healthy", status=200, mimetype="text/plain")


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
def dispatch(path: str) -> Response:
    target = pick_target("/" + path)
    upstream = urljoin(target + "/", path)
    if request.query_string:
        upstream = f"{upstream}?{request.query_string.decode('utf-8', 'ignore')}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}

    log.info("%s /%s -> %s", request.method, path, target)

    try:
        resp = requests.request(
            method=request.method,
            url=upstream,
            headers=headers,
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            timeout=30,
        )
    except requests.RequestException as exc:
        log.error("upstream error %s %s: %s", request.method, upstream, exc)
        return Response("upstream unavailable", status=502, mimetype="text/plain")

    response_headers = [
        (k, v) for k, v in resp.raw.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS
    ]
    return Response(resp.content, status=resp.status_code, headers=response_headers)


# --- точка входа -------------------------------------------------------------


def _main() -> None:
    log.info(
        "Strangler Fig Proxy listening on :%d | monolith=%s movies=%s events=%s | gradual=%s movies%%=%d",
        PORT,
        MONOLITH_URL,
        MOVIES_SERVICE_URL,
        EVENTS_SERVICE_URL,
        GRADUAL_MIGRATION,
        MOVIES_MIGRATION_PERCENT,
    )
    from waitress import serve

    serve(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    _main()
