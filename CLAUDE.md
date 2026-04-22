# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project context

Учебный проект 2-го спринта — декомпозиция Go-монолита «Кинобездна / CinemaAbyss» на микросервисы по паттерну **Strangler Fig**, с переездом в **Kubernetes + Helm**, MVP на **Kafka** и **CI/CD** через GitHub Actions.

Задания описаны в `Project_template.md`. README проекта — `README-правка.md`. Рабочая ветка — `cinema` (не `main`/`master`).

## Current state (статус по заданиям)

- **Задание 1 — TO-BE архитектура:** ✅ готово. C4 Container диаграмма в `docs/architecture/tobe-container.md` (Mermaid, GitHub рендерит).
- **Задание 2 — Proxy (часть 1):** ✅ готово. Python/Flask прокси в `src/microservices/proxy/`.
- **Задание 2 — Kafka events (часть 2):** ✅ готово. Python/Flask + `kafka-python-ng` в `src/microservices/events/`. Producer+Consumer в одном процессе, три топика (`movie-events`/`user-events`/`payment-events`). Postman 22/22, 42/42 assertions. Скриншоты — в `docs/screenshots/task2-*.png`.
- **Задание 3 — K8s + CI/CD:** ✅ готово. CI/CD собирает и пушит 4 образа в `ghcr.io/dmitryvsergienko-ux/cinemanew/*:latest` (все public). K8s манифесты для всех сервисов заполнены; ingress имеет fallback-правило без `host:` для тестов через `kubectl port-forward` без прав на `/etc/hosts`. Postman-тесты в k8s — 42/42. Скриншоты — `docs/screenshots/task3-*.png`.
- **Задание 4 — Helm:** частично. Chart-скелет в `src/kubernetes/helm/`, `values.yaml` и шаблоны `services/{proxy,events}-service.yaml` нужно заполнить.

## Architecture (big picture)

**Периметр (docker-compose):**
- `monolith` (Go, :8080) → PostgreSQL. Роуты `/api/users`, `/api/movies`, `/api/payments`, `/api/subscriptions`.
- `movies-service` (Go, :8081) → PostgreSQL (та же БД, временно). Извлечённый из монолита домен фильмов.
- `proxy-service` (Python/Flask + waitress, :8000) — **API Gateway / Strangler Fig**. Маршрутизирует по префиксу пути, для `/api/movies*` использует фиче-флаг + процентный роутинг.
- `events-service` (:8082) — заглушка; должен стать Kafka producer/consumer.
- `postgres`, `kafka`, `zookeeper`, `kafka-ui` (:8090).

**Роутинг прокси (`src/microservices/proxy/proxy.py`):**
- `/api/movies*`: `GRADUAL_MIGRATION=true` → `MOVIES_MIGRATION_PERCENT`% в `movies-service`, остальное в монолит. `false` → 100% в `movies-service`.
- `/api/events*` → `events-service`.
- Всё остальное → монолит.

**Общая БД.** Сейчас монолит и movies-service делят одну PostgreSQL — это намеренно, для упрощения миграции. В TO-BE — database-per-service.

## Common commands

### Локальный стенд

```bash
# Поднять postgres сначала, подождать, затем остальное (важно — см. Gotchas)
docker compose up -d postgres
sleep 8
docker compose up -d monolith movies-service proxy-service

# Полная сборка + запуск
docker compose up -d --build

# Снести всё с volumes
docker compose down -v
```

UI после запуска: monolith `:8080`, movies `:8081`, events `:8082`, **proxy `:8000`**, kafka-ui `:8090`.

### Тесты (Newman / Postman)

```bash
cd tests/postman
npm install                # один раз
npm run test:local         # против localhost
npm run test:docker        # против имён контейнеров (для запуска изнутри docker network)
npm run test:kubernetes    # против ingress cinemaabyss.example.com
```

Один блок тестов запустить по имени — через `newman run ... --folder "Proxy Service"`.

### Проверка постепенного перехода

```bash
# 10 запросов через прокси
for i in $(seq 1 10); do curl -s http://localhost:8000/api/movies > /dev/null; done

# Куда ушли (монолит vs movies-service)
docker logs cinemaabyss-monolith 2>&1 | grep -c "get movies from monolith"
docker logs cinemaabyss-movies-service 2>&1 | grep -c "get movies from movies"
```

Логи сервисов содержат строки `get movies from monolith` / `get movies from movies` — по ним считается распределение трафика. Менять процент — в `docker-compose.yml` env `MOVIES_MIGRATION_PERCENT`, затем `docker compose up -d --build proxy-service`.

### Kubernetes / Helm (когда дойдёт до заданий 3–4)

Команды — в `Project_template.md` (полный порядок `kubectl apply`). Кратко:
```bash
kubectl apply -f src/kubernetes/namespace.yaml
kubectl apply -f src/kubernetes/{configmap,secret,dockerconfigsecret,postgres-init-configmap}.yaml
kubectl apply -f src/kubernetes/postgres.yaml
kubectl apply -f src/kubernetes/kafka/kafka.yaml
kubectl apply -f src/kubernetes/{monolith,movies-service,events-service,proxy-service}.yaml
kubectl apply -f src/kubernetes/ingress.yaml
minikube tunnel
```

Helm:
```bash
helm install cinemaabyss ./src/kubernetes/helm --namespace cinemaabyss --create-namespace
```

В `src/kubernetes/dockerconfigsecret.yaml` и `helm/values.yaml` нужно подставлять **свои** пути до образов в `ghcr.io/<username>/...` и свой `.dockerconfigjson` в base64.

## Gotchas

- **Монолит падает на холодном старте без retry-логики к БД.** Контейнер не имеет `restart` policy, `initDB()` делает `log.Fatal` при первой ошибке DNS. Решение при локальном запуске — стартовать `postgres` первым и ждать 5–10 сек, потом поднимать остальное. Постоянный фикс (не применён): `depends_on: { postgres: { condition: service_healthy } }` или `restart: on-failure`.
- **`kafka-python` 2.0.x не работает на Python 3.12** (удалённый `six.moves`). В `events-service` используется форк `kafka-python-ng==2.2.3` — при апдейтах зависимостей не откатывать обратно на `kafka-python`.
- **Порт 8080 на хосте.** Конфликтует с любым локальным сервисом на том же порту. При ошибке `port is already allocated` — проверь `docker ps`.
- **Docker Compose `version:` obsolete warning** — безопасно игнорировать, правки compose не требуют.
- **Минikube + ingress без админ-прав.** `minikube tunnel` и правка `C:\Windows\System32\drivers\etc\hosts` требуют админа. Workaround — `kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller 8888:80` + fallback-правило в `ingress.yaml` без `host:`. `kubernetes.environment.json` для postman указывает на `http://127.0.0.1:8888`.

## Conventions

- **Ветки.** Работаем в `cinema` (не `main`). Пуш — `git push origin cinema`.
- **Стиль сервисов.** Монолит и movies-service — Go stdlib (`net/http`, `database/sql`, `lib/pq`). Прокси — Python 3.12 / Flask + requests + waitress (нестандартный выбор для этого репо — сделан сознательно по запросу).
- **Дизайн API** — `api-specification.yaml` (OpenAPI 3.0). Поддерживать в согласии с реальным поведением сервисов.
- **Имена контейнеров** — `cinemaabyss-<service>` (так именно в docker-compose).
- **Сеть** — `cinemaabyss-network`.

## Key files

| Файл | Что |
|---|---|
| `Project_template.md` | Техзадание спринта и отчёты по заданиям |
| `docs/architecture/tobe-container.md` | TO-BE C4 Container (Mermaid) |
| `api-specification.yaml` | OpenAPI 3.0 всех сервисов |
| `docker-compose.yml` | Локальный стенд |
| `src/monolith/main.go` | Монолит (users/movies/payments/subscriptions) |
| `src/microservices/movies/main.go` | Movies-service |
| `src/microservices/proxy/proxy.py` | Strangler Fig прокси |
| `src/kubernetes/` | Манифесты K8s |
| `src/kubernetes/helm/` | Helm chart |
| `tests/postman/` | Postman collection + Newman runner |
| `.github/workflows/docker-build-push.yml` | CI сборка образов (в работе) |
