# Lemtik Inventory Service

FastAPI service for internal inventory tracking, thresholds, alerts, and readiness queries.

## Endpoints

- `POST /query`
- `POST /update/officer`
- `POST /update/vehicle`
- `POST /update/weapon`
- `POST /update/equipment`
- `POST /update/fuel-reserve`
- `POST /update/cadence`
- `POST /update/ammunition`
- `POST /update/threshold`
- `POST /perf/check`
- `GET /alerts/active`
- `POST /alerts/resolve`
- `GET /health`

## Auth

Set `X-Internal-Key` to `INTERNAL_API_KEY`.

## Notes

- The service seeds a default org: `org_abc123`.
- Alert evaluation runs on startup and every 5 minutes through APScheduler.
- If `RELATIONSHIP_API_URL` and `RELATIONSHIP_API_KEY` are set, alerts are pushed externally.
- If `GROQ_API_KEY` is set, Groq acts as a structured inventory review layer for summaries, readiness, and alert payloads.
- Officer shift windows can automatically transition `off_duty` and `on_duty` when `shift_start` and `shift_end` are present.
- Officer and vehicle updates can link an `incident_id` and auto-transition to `on_duty` or `deployed` when assigned.
- Vehicle updates can include route distance so the service can estimate remaining fuel before deployment.
- Query responses include soft timing metadata so the Relationship API can see whether the inventory service stayed inside its 500ms target.
- `POST /perf/check` measures the core inventory query paths and returns per-request timing metrics.
