# Lemtik Security — Inventory Service Specification
### Service 1 of 6 — Resource Tracking & Threshold Intelligence
**Classification:** Internal Engineering
**Version:** 1.0
**Status:** Build-Ready

---

## 1. What This Service Is

The Inventory Service is a continuously running backend service
that tracks every resource an organisation owns or manages —
officers, vehicles, weapons, fuel, and tactical equipment.

It does three things:

1. **Tracks** the real-time status of every resource
2. **Flags** the Relationship API when anything falls below
   a defined threshold
3. **Answers queries** from the Relationship API when the
   Master Agent needs to know what is available for an operation

It does not make decisions. It does not deploy anyone.
It just knows what exists, what state everything is in,
and it never stops watching.

---

## 2. What It Tracks

### 2.1 Officers
Every officer registered to an organisation.

```
Per officer:
- Officer ID
- Full name
- Badge number
- Current status: available / on_duty / off_duty / on_leave / injured
- Current location (lat, lng) — updated by their device
- Last location update timestamp
- Armed: yes / no
- Weapon carried (if armed)
- Rank / role
- Certifications (armed response, first aid, tactical, K9)
- Shift schedule (when are they supposed to be on?)
- Contact (phone number for ping)
- Assigned zone or post
```

Threshold rule example:
"This organisation requires minimum 9 officers available at all times.
Current available: 7. FLAG."

### 2.2 Vehicles
Every vehicle owned or assigned to an organisation.

```
Per vehicle:
- Vehicle ID
- Plate number
- Type: patrol_car / truck / motorcycle / armoured / boat
- Current status: available / deployed / maintenance / offline
- Current location (lat, lng) — GPS tracker
- Fuel level: percentage (0–100%)
- Fuel amount: litres
- Last fuelled: timestamp
- Condition: good / needs_service / critical
- Assigned driver (if any)
- Capacity: number of officers it can carry
- Special equipment fitted: light bar, radio, weapon rack
- Last service date
- Next service due date
```

Threshold rules example:
"Less than 3 of 5 vehicles have fuel above 30%. FLAG."
"Vehicle V003 fuel below 20%. FLAG."
"Vehicle V002 overdue for service by 14 days. FLAG."

### 2.3 Weapons & Ammunition
Every weapon in the organisation's armoury.

```
Per weapon:
- Weapon ID
- Type: pistol / rifle / shotgun / taser / baton / pepper_spray
- Serial number
- Current status: in_armoury / issued / lost / maintenance
- Assigned to officer (if issued)
- Condition: good / needs_service / decommissioned
- Last inspection date
- Ammunition type compatible
```

Per ammunition type:
- Ammunition ID
- Type
- Quantity in stock
- Threshold quantity (flag if below this)
- Last restocked

Threshold rule example:
"Pistol ammunition below 200 rounds. FLAG."
"Only 2 tasers available, 4 required. FLAG."

### 2.4 Tactical Equipment
All other operational equipment.

```
Items tracked (expandable — not fixed list):
- Body armour (quantity, condition, size distribution)
- Handcuffs / zip ties
- Radios / communication devices (battery status, availability)
- Flashlights
- First aid kits (quantity, expiry of contents)
- Cameras / body cams
- Drones (battery, availability)
- Riot shields
- Bolt cutters / breaching tools
- Night vision equipment
```

Per item category:
- Total quantity
- Available quantity
- In use quantity
- Condition breakdown (good / needs_service / decommissioned)
- Threshold quantity

### 2.5 Fuel Reserves
Overall fuel reserves at the organisation's base.

```
- Current reserve: litres
- Capacity: litres
- Percentage full
- Last restocked: timestamp
- Resupply contact
- Threshold: flag if below X litres
```

---

## 3. Threshold & Alert System

This is the most important feature. The inventory service
never waits to be asked if something is low. It watches
continuously and pushes alerts to the Relationship API
the moment a threshold is breached.

### 3.1 How Thresholds Work

Every resource category has configurable thresholds set
per organisation. Defaults are provided but each org can
customise them.

```
Default thresholds:

Officers:
  available_minimum: 9          (flag if below)
  armed_minimum: 3              (flag if below)

Vehicles:
  available_minimum: 2          (flag if below)
  fuelled_minimum: 3            (flag if below 30% fuel)
  fuel_percentage_minimum: 30   (flag per vehicle if below)

Weapons:
  set per weapon type per org

Ammunition:
  set per type per org

Tactical Equipment:
  set per item category per org

Fuel Reserve:
  minimum_litres: 200           (flag if below)
```

### 3.2 Alert Levels

```
ADVISORY   — approaching threshold (within 20% of threshold)
             "4 officers available. Minimum is 9. Getting low."

WARNING    — at threshold
             "3 vehicles fuelled. Minimum is 3. At limit."

CRITICAL   — below threshold
             "2 vehicles fuelled. Minimum is 3. Threshold breached."

EMERGENCY  — severely below threshold (below 50% of threshold)
             "1 vehicle fuelled. Minimum is 3. Severely understaffed."
```

### 3.3 Alert Persistence

Alerts do not fire once and stop. They keep alerting
at defined intervals until the issue is corrected.

```
Alert repeat schedule:
  ADVISORY:  once only
  WARNING:   every 2 hours
  CRITICAL:  every 30 minutes
  EMERGENCY: every 10 minutes
```

When the issue is resolved (resource restocked, officer
returns to duty, vehicle fuelled), the alert automatically
clears and a resolution notification is sent.

### 3.4 What the Alert Looks Like

The inventory service sends this to the Relationship API
which forwards it to the dashboard:

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
  "recommended_action": "Refuel vehicles V002, V003, and V004 before next shift.",
  "repeat_alert": true,
  "next_alert_at": "ISO8601"
}
```

---

## 4. Query Interface

When the Master Agent needs inventory data for an operation,
it sends a query through the Relationship API. The inventory
service must respond fast — under 500ms for any query.

### 4.1 Full Inventory Summary Query

**Input from Relationship API:**
```json
{
  "request_type": "inventory_summary",
  "request_id": "req_xyz",
  "org_id": "org_abc123"
}
```

**Output from Inventory Service:**
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
    "active_alerts": 2,
    "last_updated": "ISO8601"
  }
}
```

### 4.2 Available Officers Query

**Input:**
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

**Output:**
```json
{
  "request_id": "req_xyz",
  "status": "success",
  "data": {
    "officers": [
      {
        "officer_id": "OFF-001",
        "name": "Ahmed Bello",
        "badge": "LG-0042",
        "status": "available",
        "armed": true,
        "weapon": "pistol",
        "location": {
          "lat": 6.4281,
          "lng": 3.4219,
          "last_updated": "ISO8601"
        },
        "certifications": ["armed_response", "first_aid"],
        "contact": "+234XXXXXXXXXX"
      }
    ],
    "total_returned": 4
  }
}
```

### 4.3 Available Vehicles Query

**Input:**
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

**Output:**
```json
{
  "request_id": "req_xyz",
  "status": "success",
  "data": {
    "vehicles": [
      {
        "vehicle_id": "V001",
        "plate": "LG-XYZ-001",
        "type": "patrol_car",
        "status": "available",
        "fuel_percentage": 85,
        "fuel_litres": 42.5,
        "location": {
          "lat": 6.4300,
          "lng": 3.4250,
          "last_updated": "ISO8601"
        },
        "capacity": 4,
        "condition": "good",
        "assigned_driver": null
      }
    ],
    "total_returned": 2
  }
}
```

### 4.4 Operational Readiness Check

A quick check the Master Agent runs before recommending
a full response — "are we ready for an operation right now?"

**Input:**
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

**Output:**
```json
{
  "request_id": "req_xyz",
  "status": "success",
  "data": {
    "ready": false,
    "gaps": [
      {
        "resource": "vehicles",
        "needed": 1,
        "available": 0,
        "gap": 1,
        "severity": "critical",
        "message": "No fuelled vehicles available"
      }
    ],
    "available_to_deploy": {
      "officers": 4,
      "armed_officers": 3,
      "vehicles": 0,
      "body_armour": 6,
      "radios": 8
    },
    "recommendation": "Operation can proceed on foot only.
                       No vehicle available. Alert management
                       to refuel vehicles immediately."
  }
}
```

---

## 5. Data Update Mechanisms

How does inventory data stay current?

### 5.1 Officer Location Updates
Officers carry a mobile device running the Lemtik officer app.
The app sends a location ping every 60 seconds while on duty.
The inventory service receives these pings and updates
the officer's location in the database.

```
Officer app → Relationship API → Inventory Service
(ping every 60 seconds while on shift)
```

### 5.2 Officer Status Updates
Status changes come from:
- Officer clocking in / out via the mobile app
- Supervisor manually updating an officer's status
- Shift schedule (auto-sets off_duty when shift ends)
- Incident assignment (auto-sets on_duty when assigned)

### 5.3 Vehicle Location Updates
Each vehicle has a GPS tracker installed.
Tracker pings the inventory service every 30 seconds
when vehicle is running, every 5 minutes when parked.

```
Vehicle GPS tracker → Relationship API → Inventory Service
```

### 5.4 Fuel Level Updates
Two sources:
- Manual update by fleet manager after refuelling
- GPS tracker fuel sensor (if vehicle has one — not all do)
- Estimated consumption based on distance driven (fallback)

When a vehicle is deployed for an operation, the inventory
service estimates fuel consumption based on route distance
and flags if estimated remaining fuel is insufficient for
return journey.

### 5.5 Equipment Updates
Manual only for MVP. Armoury officer logs:
- Weapon issued to officer
- Weapon returned from officer
- Equipment checked out for operation
- Equipment returned after operation
- Equipment flagged as damaged or lost

---

## 6. AI Model Usage

The Inventory Service uses a lightweight AI model for
one specific task — **anomaly detection**.

### What the AI does:
- Detects unusual patterns in inventory data
- Example: "Vehicle V003 is showing 80% fuel but was
  deployed for 3 hours and 45km — expected fuel should
  be around 55%. Possible sensor malfunction or
  unauthorised refuel. Flag for review."
- Example: "Officer Ahmed has been marked available for
  12 hours straight without any activity. Verify status."
- Example: "Ammunition count dropped by 50 rounds outside
  of a logged operation. Flag for investigation."

### Model used:
No heavy LLM needed here. Use a simple statistical
anomaly detection approach for MVP:

```python
# Simple z-score anomaly detection
# No API cost, runs locally in the service

from scipy import stats
import numpy as np

def detect_fuel_anomaly(vehicle_id, expected_fuel, actual_fuel):
    deviation = abs(expected_fuel - actual_fuel)
    if deviation > 15:  # More than 15% unexpected deviation
        return {
            "anomaly": True,
            "severity": "high" if deviation > 25 else "medium",
            "message": f"Fuel anomaly on {vehicle_id}: expected {expected_fuel}%, actual {actual_fuel}%"
        }
    return {"anomaly": False}
```

For Phase 2 — add Groq (Llama 3) to generate natural
language explanations of anomalies and suggest causes.

---

## 7. Database Schema

```sql
-- Officers
CREATE TABLE officers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL,
    name VARCHAR(255) NOT NULL,
    badge_number VARCHAR(50) UNIQUE NOT NULL,
    status VARCHAR(50) DEFAULT 'off_duty',
    -- available / on_duty / off_duty / on_leave / injured
    armed BOOLEAN DEFAULT FALSE,
    weapon_id UUID,
    rank VARCHAR(100),
    certifications JSONB DEFAULT '[]',
    contact VARCHAR(20),
    assigned_zone VARCHAR(255),
    current_lat DECIMAL(10,8),
    current_lng DECIMAL(11,8),
    location_updated_at TIMESTAMPTZ,
    shift_start TIMESTAMPTZ,
    shift_end TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Vehicles
CREATE TABLE vehicles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL,
    vehicle_id VARCHAR(50) UNIQUE NOT NULL,
    plate_number VARCHAR(50),
    type VARCHAR(50),
    -- patrol_car / truck / motorcycle / armoured / boat
    status VARCHAR(50) DEFAULT 'available',
    -- available / deployed / maintenance / offline
    fuel_percentage INTEGER DEFAULT 0,
    fuel_litres DECIMAL(8,2) DEFAULT 0,
    fuel_last_updated TIMESTAMPTZ,
    condition VARCHAR(50) DEFAULT 'good',
    -- good / needs_service / critical
    capacity INTEGER DEFAULT 4,
    assigned_driver_id UUID,
    current_lat DECIMAL(10,8),
    current_lng DECIMAL(11,8),
    location_updated_at TIMESTAMPTZ,
    last_service_date DATE,
    next_service_due DATE,
    odometer_km INTEGER DEFAULT 0,
    special_equipment JSONB DEFAULT '[]',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Weapons
CREATE TABLE weapons (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL,
    serial_number VARCHAR(100) UNIQUE NOT NULL,
    type VARCHAR(100) NOT NULL,
    -- pistol / rifle / shotgun / taser / baton / pepper_spray
    status VARCHAR(50) DEFAULT 'in_armoury',
    -- in_armoury / issued / lost / maintenance
    assigned_to UUID REFERENCES officers(id),
    condition VARCHAR(50) DEFAULT 'good',
    last_inspection_date DATE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Ammunition
CREATE TABLE ammunition (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL,
    type VARCHAR(100) NOT NULL,
    quantity INTEGER DEFAULT 0,
    threshold INTEGER DEFAULT 200,
    last_restocked TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Tactical Equipment
CREATE TABLE tactical_equipment (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL,
    category VARCHAR(100) NOT NULL,
    total_quantity INTEGER DEFAULT 0,
    available_quantity INTEGER DEFAULT 0,
    in_use_quantity INTEGER DEFAULT 0,
    threshold INTEGER DEFAULT 0,
    condition_breakdown JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Fuel Reserves
CREATE TABLE fuel_reserves (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL UNIQUE,
    current_litres DECIMAL(10,2) DEFAULT 0,
    capacity_litres DECIMAL(10,2) DEFAULT 0,
    threshold_litres DECIMAL(10,2) DEFAULT 200,
    last_restocked TIMESTAMPTZ,
    resupply_contact VARCHAR(255),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Inventory Alerts (persistent alert log)
CREATE TABLE inventory_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL,
    alert_level VARCHAR(50) NOT NULL,
    -- ADVISORY / WARNING / CRITICAL / EMERGENCY
    resource_type VARCHAR(100) NOT NULL,
    metric VARCHAR(100) NOT NULL,
    current_value DECIMAL(10,2),
    threshold_value DECIMAL(10,2),
    message TEXT NOT NULL,
    affected_resources JSONB DEFAULT '[]',
    recommended_action TEXT,
    resolved BOOLEAN DEFAULT FALSE,
    resolved_at TIMESTAMPTZ,
    first_alerted_at TIMESTAMPTZ DEFAULT NOW(),
    last_alerted_at TIMESTAMPTZ DEFAULT NOW(),
    next_alert_at TIMESTAMPTZ,
    alert_count INTEGER DEFAULT 1
);

-- Inventory Transactions (audit log of all changes)
CREATE TABLE inventory_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL,
    resource_type VARCHAR(100) NOT NULL,
    resource_id UUID NOT NULL,
    action VARCHAR(100) NOT NULL,
    -- issued / returned / refuelled / serviced / flagged / status_changed
    old_value JSONB,
    new_value JSONB,
    performed_by UUID,
    incident_id UUID,
    -- linked incident if change was operation-related
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Thresholds (per org, configurable)
CREATE TABLE inventory_thresholds (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL,
    resource_type VARCHAR(100) NOT NULL,
    metric VARCHAR(100) NOT NULL,
    threshold_value DECIMAL(10,2) NOT NULL,
    advisory_value DECIMAL(10,2),
    alert_repeat_minutes INTEGER DEFAULT 30,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(org_id, resource_type, metric)
);
```

---

## 8. API Endpoints

The inventory service exposes these endpoints.
Only the Relationship API can call them — no direct
external access.

```
Authentication: X-Internal-Key header (set in environment)

POST /query                  — Main query endpoint (all query types)
POST /update/officer         — Update officer status or location
POST /update/vehicle         — Update vehicle status, location, or fuel
POST /update/weapon          — Update weapon status
POST /update/equipment       — Update equipment quantities
POST /update/fuel-reserve    — Update base fuel reserve
POST /update/ammunition      — Update ammunition stock
POST /update/threshold       — Update threshold for a resource

GET  /alerts/active          — Get all active unresolved alerts
POST /alerts/resolve         — Mark an alert as resolved

GET  /health                 — Service health check
```

---

## 9. Tech Stack

```
Language:     Python 3.11+
Framework:    FastAPI
Database:     Supabase PostgreSQL (shared database)
Scheduler:    APScheduler
              (runs threshold checks every 5 minutes continuously)
Anomaly:      scipy + numpy (local, no API cost)
Hosting:      Render web service
Instance:     free ($0/month)
```

---

## 10. Environment Variables

```env
DATABASE_URL=          # Supabase connection string
INTERNAL_API_KEY=      # Key that Relationship API uses to call this service
RELATIONSHIP_API_URL=  # Where to push alerts
RELATIONSHIP_API_KEY=  # Key to authenticate with Relationship API
ENVIRONMENT=production
PORT=8000

# Default threshold overrides (org-level thresholds stored in DB)
DEFAULT_MIN_OFFICERS=9
DEFAULT_MIN_ARMED_OFFICERS=3
DEFAULT_MIN_VEHICLES=2
DEFAULT_MIN_FUELLED_VEHICLES=3
DEFAULT_VEHICLE_FUEL_THRESHOLD=30
DEFAULT_FUEL_RESERVE_LITRES=200
```

---

## 11. How It Pushes Alerts to the Relationship API

The inventory service does not wait to be asked.
When a threshold is breached, it immediately pushes
an alert to the Relationship API.

```python
import httpx
import asyncio
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler(timezone="Africa/Lagos")

# Run threshold checks every 5 minutes
scheduler.add_job(
    check_all_thresholds,
    'interval',
    minutes=5,
    id='threshold_check'
)

scheduler.start()

async def push_alert_to_relationship_api(alert: dict):
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{RELATIONSHIP_API_URL}/internal/inventory-alert",
            json=alert,
            headers={"X-Internal-Key": RELATIONSHIP_API_KEY},
            timeout=5.0
        )

async def check_all_thresholds():
    orgs = get_all_active_organisations()
    for org in orgs:
        alerts = evaluate_thresholds(org["id"])
        for alert in alerts:
            await push_alert_to_relationship_api(alert)
```

---

## 12. Deployment on Render

```yaml
# render.yaml

services:
  - type: web
    name: lemtik-inventory-service
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn main:app --host 0.0.0.0 --port $PORT
    envVars:
      - key: DATABASE_URL
        sync: false
      - key: INTERNAL_API_KEY
        sync: false
      - key: RELATIONSHIP_API_URL
        sync: false
      - key: RELATIONSHIP_API_KEY
        sync: false
      - key: ENVIRONMENT
        value: production
    plan: free
```

---

## 13. Build Checklist

Before pushing to Render:

- [ ] Database schema created in Supabase
- [ ] All query types tested with mock data
- [ ] Threshold check scheduler tested (fires every 5 min)
- [ ] Alert push to Relationship API tested
- [ ] Alert persistence tested (keeps alerting until resolved)
- [ ] All update endpoints tested
- [ ] Anomaly detection tested with sample fuel data
- [ ] Health endpoint returns accurate status
- [ ] Internal API key validation working
- [ ] Environment variables set on Render
- [ ] At least one org seeded with test data
- [ ] Full query response under 500ms verified

---

## 14. What the Dashboard Will See

When the Relationship API forwards inventory data
to the C4I dashboard, the operator sees cards like these:

```
┌─────────────────────────────────┐
│ 🔴 CRITICAL — Vehicles          │
│ Only 1 of 5 vehicles fuelled    │
│ Minimum required: 3             │
│ Affected: V002, V003, V004      │
│ Action: Refuel before next shift│
└─────────────────────────────────┘

┌─────────────────────────────────┐
│ 🟡 WARNING — Fuel Reserve       │
│ 180L remaining (45% capacity)   │
│ Threshold: 200L                 │
│ Action: Schedule resupply       │
└─────────────────────────────────┘

┌─────────────────────────────────┐
│ ✅ Officers — All Good          │
│ 9 available / 24 total          │
│ 4 armed and ready               │
└─────────────────────────────────┘
```

---

*Version 1.0 — Lemtik Security Engineering*
*Build this first. Every other service depends on knowing*
*what resources are available. Get this live on Render first.*