# Lemtik Inventory Service API

This document explains how the Inventory Service is meant to be consumed by the Relationship API or any other trusted internal application.

The service is a private internal backend. It is not designed for public access. All traffic should be authenticated with `X-Internal-Key`.

## What The Service Does

The Inventory Service is responsible for:

- Tracking officers, vehicles, weapons, ammunition, tactical equipment, fuel reserves, cadence rules, and thresholds
- Returning structured inventory snapshots for downstream systems
- Emitting threshold alerts when inventory falls below configured limits
- Pushing those alerts to the Relationship API
- Producing structured review metadata with Groq when `GROQ_API_KEY` is present

It does not make operational decisions beyond the inventory domain. It does not deploy resources by itself. It reports the inventory state and flags gaps.

## Authentication

Every request must include:

```http
X-Internal-Key: <INTERNAL_API_KEY>
```

If the key is missing or incorrect, the service returns `401 Unauthorized`.

The same key is used for:

- Relationship API calls into the Inventory Service
- Inventory Service alert pushes into the Relationship API

## Base Contract

The service is a FastAPI app.

Typical deployment base URL:

```text
https://<your-render-service>.onrender.com
```

Local development base URL:

```text
http://127.0.0.1:8001
```

## Endpoint Overview

### Query

`POST /query`

Supported request types:

- `inventory_summary`
- `available_officers`
- `available_vehicles`
- `readiness_check`

### Updates

- `POST /update/officer`
- `POST /update/vehicle`
- `POST /update/weapon`
- `POST /update/equipment`
- `POST /update/fuel-reserve`
- `POST /update/cadence`
- `POST /update/ammunition`
- `POST /update/threshold`

### Alerts

- `GET /alerts/active`
- `POST /alerts/resolve`

### Ops

- `GET /health`
- `POST /perf/check`

## Query Usage

### 1. Inventory Summary

Use this when the Relationship API wants a full snapshot of inventory state.

Request:

```json
{
  "request_type": "inventory_summary",
  "request_id": "req_xyz",
  "org_id": "org_abc123"
}
```

Response:

```json
{
  "request_id": "req_xyz",
  "status": "success",
  "data": {
    "officers": {
      "total": 24,
      "available": 9,
      "on_duty": 8,
      "off_duty": 5,
      "on_leave": 2,
      "armed_available": 4,
      "below_threshold": false
    },
    "vehicles": {
      "total": 5,
      "available": 3,
      "deployed": 2,
      "fuelled": 2,
      "below_threshold": true,
      "threshold_alert_level": "CRITICAL"
    },
    "weapons": {
      "pistols_available": 6,
      "rifles_available": 2,
      "tasers_available": 3,
      "below_threshold": false
    },
    "ammunition": {
      "pistol_rounds": 450,
      "rifle_rounds": 200,
      "below_threshold": false
    },
    "tactical": {
      "body_armour_available": 8,
      "radios_available": 12,
      "first_aid_kits": 4,
      "below_threshold": false
    },
    "fuel_reserve": {
      "litres": 180,
      "percentage": 45,
      "below_threshold": true,
      "threshold_alert_level": "WARNING"
    },
    "cadence": {
      "officers": {
        "cadence_seconds": 60,
        "stale_count": 0,
        "stale_officers": []
      },
      "vehicles": {
        "running_cadence_seconds": 30,
        "parked_cadence_seconds": 300,
        "stale_count": 0,
        "stale_vehicles": []
      }
    },
    "active_alerts": 2,
    "last_updated": "ISO8601",
    "llm_review": {
      "approved": true,
      "issues": [],
      "risk_level": "medium",
      "missing_fields": [],
      "recommended_actions": ["Refuel vehicles before next shift"]
    }
  },
  "meta": {
    "elapsed_ms": 42.3,
    "soft_target_ms": 500,
    "within_soft_target": true
  }
}
```

### 2. Available Officers

Use this when the Relationship API needs a deployable officer list.

Request:

```json
{
  "request_type": "available_officers",
  "request_id": "req_xyz",
  "org_id": "org_abc123",
  "filters": {
    "armed_only": true,
    "certified": ["armed_response"],
    "available_only": true
  }
}
```

The service returns a filtered list of officers plus `total_returned`.

### 3. Available Vehicles

Use this when the Relationship API needs vehicles that are available and sufficiently fuelled.

Request:

```json
{
  "request_type": "available_vehicles",
  "request_id": "req_xyz",
  "org_id": "org_abc123",
  "filters": {
    "available_only": true,
    "fuelled_only": true,
    "min_fuel_percentage": 30,
    "type": null
  }
}
```

The service returns a filtered list of vehicles plus `total_returned`.

### 4. Readiness Check

Use this to ask whether an operation can proceed with the currently available inventory.

Request:

```json
{
  "request_type": "readiness_check",
  "request_id": "req_xyz",
  "org_id": "org_abc123",
  "operation_requirements": {
    "officers_needed": 4,
    "armed_required": true,
    "vehicles_needed": 1,
    "equipment": ["body_armour", "radios"]
  }
}
```

The service returns:

- `ready`
- `gaps`
- `available_to_deploy`
- `recommendation`
- `llm_review` when Groq is enabled

## Update Usage

### Officer Update

`POST /update/officer`

Use this for:

- officer location pings
- status changes
- shift start / shift end updates
- incident assignment

Important fields:

- `location.lat`
- `location.lng`
- `location.last_updated`
- `shift_start`
- `shift_end`
- `incident_id`

When `shift_start` and `shift_end` are present, the service can auto-transition:

- `off_duty` to `on_duty`
- `on_duty` to `off_duty`

When `incident_id` is present, the service can auto-mark an officer as `on_duty` if they were off duty.

### Vehicle Update

`POST /update/vehicle`

Use this for:

- GPS updates
- fuel updates
- vehicle status changes
- route fuel estimation
- incident assignment

Important fields:

- `location.lat`
- `location.lng`
- `location.last_updated`
- `fuel_percentage`
- `fuel_litres`
- `odometer_km`
- `route_distance_km`
- `incident_id`

When `route_distance_km` is present, the service estimates remaining fuel and can flag the route as unsafe if the estimate drops below the safe threshold.

When `incident_id` is present, the service can auto-mark a vehicle as `deployed` if it was available.

### Cadence Update

`POST /update/cadence`

Use this to set org-specific telemetry timing:

- officer ping cadence
- vehicle running GPS cadence
- vehicle parked GPS cadence

### Threshold Update

`POST /update/threshold`

Use this to change the threshold for any resource metric for a specific org.

### Fuel Reserve, Ammunition, Equipment, Weapon

Use these endpoints when the inventory values change outside normal automatic feeds.

## Alerts

### Active Alerts

`GET /alerts/active`

Returns unresolved alerts for the org.

### Resolve Alert

`POST /alerts/resolve`

Use this when the Relationship API has already handled the alert and wants the inventory record marked resolved.

Request:

```json
{
  "alert_id": "INV-ALERT-001",
  "resolved_by": "relationship_api",
  "notes": "Vehicles refuelled"
}
```

## Alert Push Flow

The Inventory Service pushes alerts to the Relationship API automatically when thresholds are breached.

Push target:

```http
POST {RELATIONSHIP_API_URL}/internal/inventory-alert
```

Push headers:

```http
X-Internal-Key: <RELATIONSHIP_API_KEY>
Content-Type: application/json
```

Example alert payload:

```json
{
  "alert_id": "INV-ALERT-001",
  "org_id": "org_abc123",
  "timestamp": "ISO8601",
  "alert_level": "CRITICAL",
  "resource_type": "vehicles",
  "metric": "fuelled_count",
  "current_value": 1,
  "threshold_value": 3,
  "message": "Only 1 of 5 vehicles has sufficient fuel. Minimum required: 3.",
  "affected_resources": ["V002", "V003", "V004"],
  "recommended_action": "Refuel vehicles before next shift.",
  "repeat_alert": true,
  "next_alert_at": "ISO8601",
  "llm_review": {
    "approved": true,
    "issues": [],
    "severity": "high",
    "suggested_message": "Only 1 of 5 vehicles is fuelled. Minimum required: 3.",
    "suggested_action": "Refuel vehicles before next shift."
  }
}
```

## Groq Review Layer

If `GROQ_API_KEY` is configured, Groq is used as a structured review layer.

It does not:

- query the database directly
- write code
- see secrets
- change business rules

It does:

- inspect inventory summary output
- inspect readiness output
- inspect alert payloads
- return structured JSON review metadata

The deterministic Python code remains the source of truth. Groq only validates and annotates the output before it is returned or pushed.

## Performance Check

`POST /perf/check`

Use this to measure the main inventory query paths.

Request example:

```json
{
  "org_id": "org_abc123",
  "request_types": [
    "inventory_summary",
    "available_officers",
    "available_vehicles",
    "readiness_check"
  ],
  "iterations": 3,
  "include_llm": false
}
```

The endpoint returns:

- per-request min/avg/max timing
- whether each path stayed within the 500ms soft target
- overall average timing

## Relationship API Integration Pattern

The Relationship API should treat Inventory Service as a private internal dependency.

Recommended pattern:

1. Relationship API receives a user request.
2. Relationship API calls `POST /query` on Inventory Service.
3. Inventory Service returns structured inventory data.
4. If thresholds are breached, Inventory Service also pushes alerts back to the Relationship API.
5. Relationship API forwards the alert to the dashboard or operator workflow.

This means the Relationship API does not need to poll for threshold violations.
It can still query on demand for summaries and readiness checks.

## Error Handling

- `401` means the `X-Internal-Key` is missing or invalid
- `400` means the request type or payload is invalid
- `404` is used for alert resolution misses
- `500` means the service hit an unexpected runtime issue

## Environment Variables

Required:

- `DATABASE_URL`
- `INTERNAL_API_KEY`

Optional:

- `RELATIONSHIP_API_URL`
- `RELATIONSHIP_API_KEY`
- `GROQ_API_KEY`
- `GROQ_MODEL`
- `GROQ_BASE_URL`
- `DEFAULT_MIN_OFFICERS`
- `DEFAULT_MIN_ARMED_OFFICERS`
- `DEFAULT_MIN_VEHICLES`
- `DEFAULT_MIN_FUELLED_VEHICLES`
- `DEFAULT_VEHICLE_FUEL_THRESHOLD`
- `DEFAULT_FUEL_RESERVE_LITRES`
- `DEFAULT_OFFICER_PING_SECONDS`
- `DEFAULT_VEHICLE_GPS_RUNNING_SECONDS`
- `DEFAULT_VEHICLE_GPS_PARKED_SECONDS`

## Notes For Integrators

- Keep requests small and structured
- Reuse the same `request_id` from your workflow
- Use the alert endpoint to close the loop after operational action
- Do not send secrets through query payloads
- Treat this service as a trusted internal service only
