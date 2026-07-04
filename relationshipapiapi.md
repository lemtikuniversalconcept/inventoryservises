# Relationship API Integration Guide

This document explains how internal Lemtik services and approved external callers connect to the Relationship API, send data into it, and receive results back.

The Relationship API is the orchestration and linkage layer for the platform.
It authenticates callers, enforces organization scope, validates contracts, routes requests to upstream services when needed, assembles responses, and records audit trails.

It does not replace the specialized services it calls.
It connects them.

---

## 1. What This Service Is

The Relationship API is the central integration layer for:

- Incidents
- Relationships and graph data
- OSINT and intelligence lookups
- Inventory queries
- Proximity scoring
- Route calculation
- Main AI / Master AI orchestration
- Autonomous control actions
- Audit logging

It is designed so that callers do not need to know where each downstream service lives.
The Relationship API can:

- Receive a request
- Validate it
- Route it to the correct service
- Fall back to local heuristics if the upstream service is unavailable
- Return a structured response

---

## 2. Core Design

The service follows a simple contract:

1. A caller sends an authenticated request.
2. The Relationship API validates the payload and organization scope.
3. The API either handles the request locally or forwards it to an upstream service.
4. If the upstream service is missing or unhealthy, the API returns a degraded but structured fallback response.
5. The request and result are written to the audit trail.

That means callers can use one endpoint pattern and one auth pattern for the whole platform.

---

## 3. Authentication

Supported authentication modes:

- `Authorization: Bearer <jwt>` for user or service JWTs
- `X-Internal-Key: <key>` for internal service-to-service calls
- `X-API-Key: <key>` for legacy or partner-style service auth
- `X-WebHook-Signature: <signature>` for inbound webhook verification

Typical caller headers:

```http
Authorization: Bearer <token>
Content-Type: application/json
X-Request-Id: <uuid>
X-Org-Id: <org_id>
X-Client-Name: <service-name>
X-Idempotency-Key: <uuid>
```

Optional actor headers:

```http
X-Actor-Id: <user_id>
X-Actor-Role: <role>
```

If no valid auth is present and dev auth is disabled, the request is rejected.

---

## 4. Base URLs

The service exposes both direct and versioned routes.

Primary API prefix:

```text
/api/v1
```

Legacy aliases are also available for compatibility on many routes.

Example deployment:

```text
https://relationship-api.example.com
```

Example local deployment:

```text
http://localhost:3000
```

---

## 5. Service Registry And Fallbacks

The Relationship API knows about the downstream services through environment configuration.

Common env vars:

- `OSINT_BRAIN_URL`
- `AI_ANALYSIS_URL`
- `MAIN_AGENT_URL`
- `AUTONOMOUS_CONTROL_URL`
- `INVENTORY_SERVICE_URL`
- `PROXIMITY_URL`
- `ROUTE_CALCULATOR_URL`

If an upstream service is unavailable or its base URL is missing, the Relationship API returns a local fallback response instead of hard failing where possible.

This keeps the dashboard and calling systems responsive even when one dependency is down.

---

## 6. Request Flow

Normal flow:

1. Caller sends an incident, query, or action request.
2. Relationship API validates org scope and permissions.
3. Relationship API fans out to the required services when needed.
4. The API combines the data into one response.
5. The API stores audit and event records.
6. The caller receives a single structured result.

For example, an incident can trigger:

- OSINT query
- Inventory lookup
- Proximity ranking
- Route calculation
- AI analysis
- Main agent orchestration

That is the preferred seamless integration path.

---

## 7. Key Endpoints

### Health

```http
GET /health
GET /ready
GET /health/:service
```

Use these for readiness and service visibility.

### Graph And Relationship Data

```http
GET    /api/v1/entities
GET    /api/v1/entities/:id
POST   /api/v1/entities
PATCH  /api/v1/entities/:id

GET    /api/v1/relationships
GET    /api/v1/relationships/:id
POST   /api/v1/relationships
PATCH  /api/v1/relationships/:id
DELETE /api/v1/relationships/:id

POST   /api/v1/graph/query
GET    /api/v1/entities/:id/graph
GET    /api/v1/entities/:id/relationships
```

### Intelligence

```http
GET /api/v1/intelligence
GET /api/v1/intelligence/heatmap
GET /api/v1/intelligence/risk-score
GET /api/v1/intelligence/brief/:org
```

### Incidents

```http
POST  /api/v1/incidents
GET   /api/v1/incidents/:id
PATCH /api/v1/incidents/:id/status
POST  /api/v1/incidents/:id/analyse
POST  /api/v1/incidents/:id/dispatch
```

### Agent / Master AI

```http
POST /api/v1/agent/process
POST /api/v1/triage
POST /api/v1/synthesise
POST /api/v1/process
GET  /api/v1/session/:id
GET  /api/v1/agent/jobs/:request_id
POST /api/v1/agent/approve/:request_id
```

### Inventory

```http
GET  /api/v1/inventory/officers
GET  /api/v1/inventory/vehicles
GET  /api/v1/inventory/weapons
GET  /api/v1/inventory/summary
POST /api/v1/query
POST /api/v1/update/officer
POST /api/v1/update/vehicle
POST /api/v1/update/weapon
POST /api/v1/update/equipment
POST /api/v1/update/fuel-reserve
POST /api/v1/update/cadence
POST /api/v1/update/ammunition
POST /api/v1/update/threshold
GET  /api/v1/alerts/active
POST /api/v1/alerts/resolve
POST /api/v1/perf/check
GET  /api/v1/inventory/alerts
```

### Autonomous Control

```http
POST /api/v1/autonomous/action
POST /api/v1/execute
GET  /api/v1/autonomous/status/:id
POST /api/v1/autonomous/revert/:id
GET  /api/v1/autonomous/active
GET  /api/v1/overrides/active
```

### Proximity And Routes

```http
POST /api/v1/find
GET  /api/v1/queries
GET  /api/v1/proximity/health

POST /api/v1/route/calculate
POST /api/v1/route/push
GET  /api/v1/route/active/:id
POST /api/v1/route/update/:id
GET  /api/v1/infrastructure/registry
POST /api/v1/infrastructure/register
GET  /api/v1/route/health
```

### System And Audit

```http
GET /api/v1/system/registry
GET /api/v1/system/overview
GET /api/v1/audit-log
GET /api/v1/log
```

---

## 8. Downstream Service Contracts

The Relationship API already wraps the common downstream calls with consistent request and response shapes.

### OSINT Brain

Request sent to OSINT Brain:

```json
{
  "request_type": "intelligence_query",
  "request_id": "req_abc123",
  "query": {
    "area": "Lekki Phase 1",
    "radius_km": 5,
    "days_back": 30,
    "categories": ["Physical", "Cyber", "Political", "Macro"],
    "severity_min": 1,
    "limit": 50,
    "include_heatmap": true,
    "incident_context": {
      "type": "robbery",
      "keywords": ["armed", "robbery", "bikes"]
    }
  }
}
```

### AI Analysis Engine

Request sent:

```json
{
  "request_type": "incident_analysis",
  "request_id": "req_def456",
  "incident": { },
  "context": {
    "osint_data": { },
    "inventory": { },
    "available_officers": [ ],
    "client_type": "estate"
  }
}
```

### Main AI Agent

Request sent:

```json
{
  "request_type": "agent_task",
  "request_id": "req_ghi789",
  "task_type": "incident_dispatch",
  "raw_input": {
    "source": "distress_call",
    "content": "Robbery at Lekki Phase 1 Gate A",
    "caller_id": "string",
    "location_confirmed": true,
    "location": {
      "name": "string",
      "lat": 0,
      "lng": 0
    }
  },
  "available_services": ["osint_brain", "ai_analysis", "autonomous_control"],
  "constraints": {
    "autonomous_actions_require_approval": true,
    "max_response_time_seconds": 30,
    "approval_officer_id": "string"
  }
}
```

### Autonomous Control

Request sent:

```json
{
  "request_type": "autonomous_action",
  "request_id": "req_jkl012",
  "action": {
    "type": "traffic_light_override",
    "target_id": "TL-LEKKI-042",
    "command": "green_corridor",
    "route_ids": ["ROUTE-001"],
    "duration_seconds": 300,
    "reason": "Emergency response vehicle corridor",
    "incident_id": "INC-2024-001"
  },
  "authorisation": {
    "approved_by": "officer_id",
    "approval_timestamp": "ISO8601",
    "approval_level": "supervisor"
  },
  "constraints": {
    "auto_revert_after_seconds": 300,
    "revert_on_incident_resolved": true,
    "max_override_duration_seconds": 600
  }
}
```

---

## 9. How Callers Should Use It

### For browser apps

- Do not call the Relationship API directly from the browser unless the route is explicitly public
- Use a backend route or server function
- Keep secrets server-side
- Pass the user intent to the backend
- Let the backend call the Relationship API

### For internal services

- Use the internal key or a signed JWT
- Include `X-Request-Id` and `X-Org-Id`
- Send only validated JSON
- Treat non-200 responses as actionable errors

### For external partners

- Use a dedicated credential
- Scope every request to one organization
- Use idempotency keys on writes
- Expect structured errors and partial responses

---

## 10. Failure Handling

If a downstream service is unavailable:

- OSINT fails -> return reduced intelligence
- Inventory fails -> return local fallback inventory data
- Proximity fails -> return local responder ranking
- Route calculator fails -> return a simplified route plan
- AI analysis fails -> return heuristic threat assessment
- Main agent fails -> return direct orchestration output
- Autonomous control fails -> queue or mark action for manual intervention

This service is designed to degrade gracefully rather than break the caller flow.

---

## 11. Audit And Observability

Every request should be traceable.

The Relationship API records:

- Request ID
- Org ID
- Caller identity
- Actor identity and role
- Downstream services called
- Result status
- Response timing
- Audit trail timestamp

Graph events, autonomous actions, incidents, overrides, and inventory alerts are also persisted in the local store and mirrored to optional external backends when configured.

---

## 12. Environment Variables

Minimum useful configuration:

```env
NODE_ENV=production
PORT=3000
JWT_SECRET=
DATABASE_URL=
RELATIONSHIP_API_KEYS=
RELATIONSHIP_ALLOW_DEV_AUTH=false
WEBHOOK_SECRET=
RELATIONSHIP_WEBHOOK_URLS=
AUDIT_LOG_PATH=./data/audit-log.jsonl
OSINT_BRAIN_URL=
AI_ANALYSIS_URL=
MAIN_AGENT_URL=
AUTONOMOUS_CONTROL_URL=
INVENTORY_SERVICE_URL=
PROXIMITY_URL=
ROUTE_CALCULATOR_URL=
SUPABASE_URL=
SUPABASE_SERVICE_KEY=
REDIS_URL=
BLOB_STORAGE_URL=
BLOB_STORAGE_KEY=
BLOB_STORAGE_CONTAINER=
RESEND_API_KEY=
```

---

## 13. Example Integration Pattern

### Incident intake

```text
UI or external system -> Relationship API /api/v1/incidents
Relationship API -> OSINT + Inventory + Proximity + Route + AI + Main Agent
Relationship API -> returns combined incident record
```

### Dispatch approval

```text
Operator approves plan -> Relationship API /api/v1/incidents/:id/dispatch
Relationship API -> Autonomous Control
Relationship API -> audit log + updated incident state
```

### Intelligence query

```text
Caller -> Relationship API /api/v1/intelligence
Relationship API -> OSINT Brain
Relationship API -> structured intelligence response
```

### Route push

```text
Caller -> Relationship API /api/v1/route/push
Relationship API -> marks route pushed
Relationship API -> notifies configured Relationship API push endpoint if present
```

---

## 14. Short Version

Use the Relationship API as the secure front door for the platform.

- Send authenticated JSON requests
- Include org and request IDs
- Let the Relationship API fan out to internal services
- Accept structured responses or graceful fallbacks
- Keep secrets and routing logic out of the browser

That is the intended seamless integration model.
