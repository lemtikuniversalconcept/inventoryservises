from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:
    import psycopg2
    import psycopg2.extras
except Exception:  # pragma: no cover - optional dependency in local dev
    psycopg2 = None


TZ = timezone(timedelta(hours=1))
APP_STARTED_AT = datetime.now(timezone.utc)
DEFAULT_ORG_ID = "org_abc123"
RELATIONSHIP_API_URL = os.getenv("RELATIONSHIP_API_URL", "").rstrip("/")
RELATIONSHIP_API_KEY = os.getenv("RELATIONSHIP_API_KEY", "")
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "").strip()
if ENVIRONMENT == "production" and not INTERNAL_API_KEY:
    raise RuntimeError("INTERNAL_API_KEY is required in production.")
if not INTERNAL_API_KEY:
    INTERNAL_API_KEY = "dev-internal-key"
DEFAULT_MIN_OFFICERS = int(os.getenv("DEFAULT_MIN_OFFICERS", "9"))
DEFAULT_MIN_ARMED_OFFICERS = int(os.getenv("DEFAULT_MIN_ARMED_OFFICERS", "3"))
DEFAULT_MIN_VEHICLES = int(os.getenv("DEFAULT_MIN_VEHICLES", "2"))
DEFAULT_MIN_FUELLED_VEHICLES = int(os.getenv("DEFAULT_MIN_FUELLED_VEHICLES", "3"))
DEFAULT_VEHICLE_FUEL_THRESHOLD = int(os.getenv("DEFAULT_VEHICLE_FUEL_THRESHOLD", "30"))
DEFAULT_FUEL_RESERVE_LITRES = float(os.getenv("DEFAULT_FUEL_RESERVE_LITRES", "200"))
DEFAULT_OFFICER_PING_SECONDS = int(os.getenv("DEFAULT_OFFICER_PING_SECONDS", "60"))
DEFAULT_VEHICLE_GPS_RUNNING_SECONDS = int(os.getenv("DEFAULT_VEHICLE_GPS_RUNNING_SECONDS", "30"))
DEFAULT_VEHICLE_GPS_PARKED_SECONDS = int(os.getenv("DEFAULT_VEHICLE_GPS_PARKED_SECONDS", "300"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").strip().rstrip("/")
GROQ_SYSTEM_PROMPT = (
    "You are the internal inventory verification layer for Lemtik Security. "
    "You do not access databases or secrets. "
    "You only inspect provided inventory JSON and ensure it is complete, aligned, and structurally correct. "
    "Return JSON only, with no markdown or extra prose. "
    "Do not invent facts. "
    "If a field is missing or inconsistent, report it explicitly. "
    "Prefer short operational text. "
    "Never behave like a general chatbot."
)
SCHEDULER_TIMEZONE = ZoneInfo("Africa/Lagos")


class AlertLevel(str, Enum):
    advisory = "ADVISORY"
    warning = "WARNING"
    critical = "CRITICAL"
    emergency = "EMERGENCY"


ALERT_REPEAT_MINUTES = {
    AlertLevel.advisory: None,
    AlertLevel.warning: 120,
    AlertLevel.critical: 30,
    AlertLevel.emergency: 10,
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or now_utc()).astimezone(timezone.utc).isoformat()


def parse_iso(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def deep_copy(obj: Any) -> Any:
    return json.loads(json.dumps(obj))


def detect_fuel_anomaly(vehicle_id: str, expected_fuel: float, actual_fuel: float) -> dict[str, Any]:
    deviation = abs(expected_fuel - actual_fuel)
    if deviation > 15:
        return {
            "anomaly": True,
            "severity": "high" if deviation > 25 else "medium",
            "message": f"Fuel anomaly on {vehicle_id}: expected {expected_fuel:.1f}%, actual {actual_fuel:.1f}%",
        }
    return {"anomaly": False}


def estimate_route_fuel(current_percentage: float, route_distance_km: float, status: str) -> dict[str, Any]:
    if route_distance_km <= 0:
        return {"estimated": current_percentage, "route_distance_km": route_distance_km, "insufficient": False}
    burn_rate = 0.75 if status == "deployed" else 0.45
    estimated_drop = route_distance_km * burn_rate
    estimated_remaining = max(current_percentage - estimated_drop, 0.0)
    return {
        "estimated": round(estimated_remaining, 2),
        "route_distance_km": route_distance_km,
        "burn_rate_per_km": burn_rate,
        "insufficient": estimated_remaining < 20,
    }


def groq_inventory_review(task: str, proposed: dict[str, Any], evidence: dict[str, Any], schema_hint: dict[str, Any]) -> dict[str, Any] | None:
    if not GROQ_API_KEY:
        return None
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": GROQ_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Task: {task}\n"
                    f"Evidence JSON: {json.dumps(evidence, default=str)}\n"
                    f"Proposed JSON: {json.dumps(proposed, default=str)}\n"
                    f"Return JSON matching this shape: {json.dumps(schema_hint, default=str)}"
                ),
            },
        ],
        "temperature": 0.1,
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        response = httpx.post(f"{GROQ_BASE_URL}/chat/completions", headers=headers, json=payload, timeout=8.0)
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str) and content.strip():
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    return None
    except Exception:
        return None
    return None


def groq_inventory_analysis(task: str, context: dict[str, Any], schema_hint: dict[str, Any]) -> dict[str, Any] | None:
    review = groq_inventory_review(task, proposed=context, evidence=context, schema_hint=schema_hint)
    if review:
        return review
    return None


scheduler = BackgroundScheduler(timezone=SCHEDULER_TIMEZONE)
scheduler_lock = threading.Lock()
scheduler_started = False


class OfficerLocation(BaseModel):
    lat: float
    lng: float
    last_updated: str | None = None


class OfficerUpdate(BaseModel):
    org_id: str = DEFAULT_ORG_ID
    officer_id: str
    incident_id: str | None = None
    status: str | None = None
    armed: bool | None = None
    weapon: str | None = None
    rank: str | None = None
    certifications: list[str] | None = None
    contact: str | None = None
    assigned_zone: str | None = None
    shift_start: str | None = None
    shift_end: str | None = None
    location: OfficerLocation | None = None


class VehicleLocation(BaseModel):
    lat: float
    lng: float
    last_updated: str | None = None


class VehicleUpdate(BaseModel):
    org_id: str = DEFAULT_ORG_ID
    vehicle_id: str
    incident_id: str | None = None
    status: str | None = None
    location: VehicleLocation | None = None
    fuel_percentage: int | None = None
    fuel_litres: float | None = None
    condition: str | None = None
    assigned_driver_id: str | None = None
    capacity: int | None = None
    last_service_date: str | None = None
    next_service_due: str | None = None
    odometer_km: int | None = None
    route_distance_km: float | None = None
    special_equipment: list[str] | None = None
    expected_fuel_percentage: float | None = None


class WeaponUpdate(BaseModel):
    org_id: str = DEFAULT_ORG_ID
    weapon_id: str
    status: str | None = None
    assigned_to: str | None = None
    condition: str | None = None
    last_inspection_date: str | None = None


class EquipmentUpdate(BaseModel):
    org_id: str = DEFAULT_ORG_ID
    category: str
    total_quantity: int
    available_quantity: int
    in_use_quantity: int = 0
    threshold: int = 0
    condition_breakdown: dict[str, int] = Field(default_factory=dict)


class FuelReserveUpdate(BaseModel):
    org_id: str = DEFAULT_ORG_ID
    current_litres: float
    capacity_litres: float
    threshold_litres: float | None = None
    last_restocked: str | None = None
    resupply_contact: str | None = None


class CadenceUpdate(BaseModel):
    org_id: str = DEFAULT_ORG_ID
    officer_ping_seconds: int | None = None
    vehicle_running_seconds: int | None = None
    vehicle_parked_seconds: int | None = None


class AmmunitionUpdate(BaseModel):
    org_id: str = DEFAULT_ORG_ID
    type: str
    quantity: int
    threshold: int = 200
    last_restocked: str | None = None


class ThresholdUpdate(BaseModel):
    org_id: str = DEFAULT_ORG_ID
    resource_type: str
    metric: str
    threshold_value: float
    advisory_value: float | None = None
    alert_repeat_minutes: int = 30


class QueryRequest(BaseModel):
    request_type: str
    request_id: str
    org_id: str = DEFAULT_ORG_ID
    filters: dict[str, Any] = Field(default_factory=dict)
    operation_requirements: dict[str, Any] = Field(default_factory=dict)


class PerfCheckRequest(BaseModel):
    org_id: str = DEFAULT_ORG_ID
    request_types: list[str] = Field(
        default_factory=lambda: ["inventory_summary", "available_officers", "available_vehicles", "readiness_check"]
    )
    iterations: int = 3
    include_llm: bool = False


class ResolveAlertRequest(BaseModel):
    alert_id: str
    resolved_by: str | None = None
    notes: str | None = None


class InventoryStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.use_db = bool(DATABASE_URL and psycopg2 is not None)
        self.orgs: set[str] = {DEFAULT_ORG_ID}
        self.officers: list[dict[str, Any]] = []
        self.vehicles: list[dict[str, Any]] = []
        self.weapons: list[dict[str, Any]] = []
        self.ammunition: list[dict[str, Any]] = []
        self.tactical_equipment: list[dict[str, Any]] = []
        self.fuel_reserves: dict[str, dict[str, Any]] = {}
        self.thresholds: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.cadence_rules: dict[str, dict[str, int]] = {}
        self.alerts: list[dict[str, Any]] = []
        self.transactions: list[dict[str, Any]] = []
        self.last_updated_at: datetime = now_utc()
        if self.use_db:
            try:
                self._init_db()
                if self._db_has_rows("officers"):
                    self._load_from_db()
                else:
                    self.seed()
                    self._persist_seed()
            except Exception:
                self.use_db = False
                self.seed()
        else:
            self.seed()

    @contextmanager
    def _db_conn(self):
        conn = psycopg2.connect(  # type: ignore[union-attr]
            DATABASE_URL,
            connect_timeout=5,
            sslmode="require",
        )
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        if not self.use_db:
            return
        schema = """
        create table if not exists officers (
            id uuid primary key,
            org_id text not null,
            officer_id text unique,
            name text not null,
            badge_number text not null unique,
            status text default 'off_duty',
            armed boolean default false,
            weapon_id uuid,
            rank text,
            certifications jsonb default '[]'::jsonb,
            contact text,
            assigned_zone text,
            current_lat numeric(10,8),
            current_lng numeric(11,8),
            location_updated_at timestamptz,
            shift_start timestamptz,
            shift_end timestamptz,
            created_at timestamptz default now(),
            updated_at timestamptz default now()
        );
        create table if not exists vehicles (
            id uuid primary key,
            org_id text not null,
            vehicle_id text not null unique,
            plate_number text,
            type text,
            status text default 'available',
            fuel_percentage integer default 0,
            fuel_litres numeric(8,2) default 0,
            fuel_last_updated timestamptz,
            condition text default 'good',
            capacity integer default 4,
            assigned_driver_id uuid,
            current_lat numeric(10,8),
            current_lng numeric(11,8),
            location_updated_at timestamptz,
            last_service_date date,
            next_service_due date,
            odometer_km integer default 0,
            special_equipment jsonb default '[]'::jsonb,
            created_at timestamptz default now(),
            updated_at timestamptz default now()
        );
        create table if not exists weapons (
            id uuid primary key,
            org_id text not null,
            serial_number text not null unique,
            type text not null,
            status text default 'in_armoury',
            assigned_to uuid,
            condition text default 'good',
            last_inspection_date date,
            created_at timestamptz default now(),
            updated_at timestamptz default now()
        );
        create table if not exists ammunition (
            id uuid primary key,
            org_id text not null,
            type text not null,
            quantity integer default 0,
            threshold integer default 200,
            last_restocked timestamptz,
            created_at timestamptz default now(),
            updated_at timestamptz default now()
        );
        create table if not exists tactical_equipment (
            id uuid primary key,
            org_id text not null,
            category text not null,
            total_quantity integer default 0,
            available_quantity integer default 0,
            in_use_quantity integer default 0,
            threshold integer default 0,
            condition_breakdown jsonb default '{}'::jsonb,
            created_at timestamptz default now(),
            updated_at timestamptz default now()
        );
        create table if not exists fuel_reserves (
            id uuid primary key,
            org_id text not null unique,
            current_litres numeric(10,2) default 0,
            capacity_litres numeric(10,2) default 0,
            threshold_litres numeric(10,2) default 200,
            last_restocked timestamptz,
            resupply_contact text,
            updated_at timestamptz default now()
        );
        create table if not exists inventory_cadence_rules (
            id uuid primary key,
            org_id text not null unique,
            officer_ping_seconds integer default 60,
            vehicle_running_seconds integer default 30,
            vehicle_parked_seconds integer default 300,
            created_at timestamptz default now(),
            updated_at timestamptz default now()
        );
        create table if not exists inventory_alerts (
            id uuid primary key,
            alert_id text unique,
            org_id text not null,
            alert_level text not null,
            resource_type text not null,
            metric text not null,
            current_value numeric(10,2),
            threshold_value numeric(10,2),
            message text not null,
            affected_resources jsonb default '[]'::jsonb,
            recommended_action text,
            resolved boolean default false,
            resolved_at timestamptz,
            first_alerted_at timestamptz default now(),
            last_alerted_at timestamptz default now(),
            next_alert_at timestamptz,
            alert_count integer default 1
        );
        create table if not exists inventory_transactions (
            id uuid primary key,
            org_id text not null,
            resource_type text not null,
            resource_id uuid not null,
            action text not null,
            old_value jsonb,
            new_value jsonb,
            performed_by uuid,
            incident_id uuid,
            notes text,
            created_at timestamptz default now()
        );
        create table if not exists inventory_thresholds (
            id uuid primary key,
            org_id text not null,
            resource_type text not null,
            metric text not null,
            threshold_value numeric(10,2) not null,
            advisory_value numeric(10,2),
            alert_repeat_minutes integer default 30,
            created_at timestamptz default now(),
            updated_at timestamptz default now(),
            unique (org_id, resource_type, metric)
        );
        alter table if exists officers add column if not exists officer_id text;
        alter table if exists inventory_alerts add column if not exists alert_id text;
        create unique index if not exists inventory_alerts_alert_id_uindex on inventory_alerts(alert_id);
        """
        with self._db_conn() as conn, conn.cursor() as cur:
            cur.execute(schema)

    def _db_has_rows(self, table: str) -> bool:
        if not self.use_db:
            return False
        with self._db_conn() as conn, conn.cursor() as cur:
            cur.execute(f"select exists(select 1 from {table} limit 1)")
            row = cur.fetchone()
            return bool(row and row[0])

    def _fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if not self.use_db:
            return []
        with self._db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:  # type: ignore[union-attr]
            cur.execute(query, params)
            return list(cur.fetchall())

    def _execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        if not self.use_db:
            return
        with self._db_conn() as conn, conn.cursor() as cur:
            cur.execute(query, params)

    def _persist_seed(self) -> None:
        for officer in self.officers:
            self._upsert_officer_db(officer)
        for vehicle in self.vehicles:
            self._upsert_vehicle_db(vehicle)
        for weapon in self.weapons:
            self._upsert_weapon_db(weapon)
        for ammo in self.ammunition:
            self._upsert_ammunition_db(ammo)
        for equipment in self.tactical_equipment:
            self._upsert_equipment_db(equipment)
        for reserve in self.fuel_reserves.values():
            self._upsert_fuel_reserve_db(reserve)
        for org_id, cadence in self.cadence_rules.items():
            self._upsert_cadence_db(org_id, cadence)
        for key, threshold in self.thresholds.items():
            org_id, resource_type, metric = key
            self._upsert_threshold_db(
                {
                    "org_id": org_id,
                    "resource_type": resource_type,
                    "metric": metric,
                    "threshold_value": threshold["threshold_value"],
                    "advisory_value": threshold.get("advisory_value"),
                    "alert_repeat_minutes": threshold.get("alert_repeat_minutes", 30),
                }
            )

    def _load_from_db(self) -> None:
        self.officers = self._fetchall("select * from officers order by created_at")
        for index, officer in enumerate(self.officers, start=1):
            officer.setdefault("officer_id", f"OFF-{index:03d}")
        self.vehicles = self._fetchall("select * from vehicles order by created_at")
        self.weapons = self._fetchall("select * from weapons order by created_at")
        self.ammunition = self._fetchall("select * from ammunition order by created_at")
        self.tactical_equipment = self._fetchall("select * from tactical_equipment order by created_at")
        fuel = self._fetchall("select * from fuel_reserves")
        self.fuel_reserves = {row["org_id"]: row for row in fuel}
        cadence = self._fetchall("select org_id, officer_ping_seconds, vehicle_running_seconds, vehicle_parked_seconds from inventory_cadence_rules")
        self.cadence_rules = {
            row["org_id"]: {
                "officer_ping_seconds": int(row["officer_ping_seconds"] or DEFAULT_OFFICER_PING_SECONDS),
                "vehicle_running_seconds": int(row["vehicle_running_seconds"] or DEFAULT_VEHICLE_GPS_RUNNING_SECONDS),
                "vehicle_parked_seconds": int(row["vehicle_parked_seconds"] or DEFAULT_VEHICLE_GPS_PARKED_SECONDS),
            }
            for row in cadence
        }
        thresholds = self._fetchall("select org_id, resource_type, metric, threshold_value, advisory_value, alert_repeat_minutes from inventory_thresholds")
        self.thresholds = {
            (row["org_id"], row["resource_type"], row["metric"]): {
                "threshold_value": float(row["threshold_value"]),
                "advisory_value": float(row["advisory_value"]) if row["advisory_value"] is not None else None,
                "alert_repeat_minutes": row["alert_repeat_minutes"] or 30,
            }
            for row in thresholds
        }
        alerts = self._fetchall("select * from inventory_alerts order by last_alerted_at")
        for alert in alerts:
            alert.setdefault("alert_id", str(alert.get("id")))
        self.alerts = alerts
        self.orgs = {DEFAULT_ORG_ID}
        self.orgs.update(row["org_id"] for row in self.officers)
        self.orgs.update(row["org_id"] for row in self.vehicles)
        self.orgs.update(self.fuel_reserves.keys())
        self.orgs.update(self.cadence_rules.keys())

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, default=str)

    def _sync_org(self, org_id: str) -> None:
        self.orgs.add(org_id)

    def _upsert_officer_db(self, officer: dict[str, Any]) -> None:
        if not self.use_db:
            return
        sql = """
        insert into officers (
            id, org_id, officer_id, name, badge_number, status, armed, weapon_id, rank, certifications,
            contact, assigned_zone, current_lat, current_lng, location_updated_at,
            shift_start, shift_end, created_at, updated_at
        ) values (
            %(id)s, %(org_id)s, %(officer_id)s, %(name)s, %(badge_number)s, %(status)s, %(armed)s, %(weapon_id)s, %(rank)s,
            %(certifications)s::jsonb, %(contact)s, %(assigned_zone)s, %(current_lat)s, %(current_lng)s,
            %(location_updated_at)s, %(shift_start)s, %(shift_end)s, %(created_at)s, %(updated_at)s
        )
        on conflict (id) do update set
            org_id=excluded.org_id,
            officer_id=excluded.officer_id,
            name=excluded.name,
            badge_number=excluded.badge_number,
            status=excluded.status,
            armed=excluded.armed,
            weapon_id=excluded.weapon_id,
            rank=excluded.rank,
            certifications=excluded.certifications,
            contact=excluded.contact,
            assigned_zone=excluded.assigned_zone,
            current_lat=excluded.current_lat,
            current_lng=excluded.current_lng,
            location_updated_at=excluded.location_updated_at,
            shift_start=excluded.shift_start,
            shift_end=excluded.shift_end,
            updated_at=excluded.updated_at
        """
        row = dict(officer)
        row.setdefault("weapon_id", None)
        row["certifications"] = self._json(row.get("certifications", []))
        self._execute(sql, row)

    def _upsert_vehicle_db(self, vehicle: dict[str, Any]) -> None:
        if not self.use_db:
            return
        sql = """
        insert into vehicles (
            id, org_id, vehicle_id, plate_number, type, status, fuel_percentage, fuel_litres,
            fuel_last_updated, condition, capacity, assigned_driver_id, current_lat, current_lng,
            location_updated_at, last_service_date, next_service_due, odometer_km, special_equipment,
            created_at, updated_at
        ) values (
            %(id)s, %(org_id)s, %(vehicle_id)s, %(plate_number)s, %(type)s, %(status)s, %(fuel_percentage)s, %(fuel_litres)s,
            %(fuel_last_updated)s, %(condition)s, %(capacity)s, %(assigned_driver_id)s, %(current_lat)s, %(current_lng)s,
            %(location_updated_at)s, %(last_service_date)s, %(next_service_due)s, %(odometer_km)s, %(special_equipment)s::jsonb,
            %(created_at)s, %(updated_at)s
        )
        on conflict (id) do update set
            org_id=excluded.org_id,
            vehicle_id=excluded.vehicle_id,
            plate_number=excluded.plate_number,
            type=excluded.type,
            status=excluded.status,
            fuel_percentage=excluded.fuel_percentage,
            fuel_litres=excluded.fuel_litres,
            fuel_last_updated=excluded.fuel_last_updated,
            condition=excluded.condition,
            capacity=excluded.capacity,
            assigned_driver_id=excluded.assigned_driver_id,
            current_lat=excluded.current_lat,
            current_lng=excluded.current_lng,
            location_updated_at=excluded.location_updated_at,
            last_service_date=excluded.last_service_date,
            next_service_due=excluded.next_service_due,
            odometer_km=excluded.odometer_km,
            special_equipment=excluded.special_equipment,
            updated_at=excluded.updated_at
        """
        row = dict(vehicle)
        row["special_equipment"] = self._json(row.get("special_equipment", []))
        self._execute(sql, row)

    def _upsert_weapon_db(self, weapon: dict[str, Any]) -> None:
        if not self.use_db:
            return
        sql = """
        insert into weapons (
            id, org_id, serial_number, type, status, assigned_to, condition, last_inspection_date, created_at, updated_at
        ) values (
            %(id)s, %(org_id)s, %(serial_number)s, %(type)s, %(status)s, %(assigned_to)s, %(condition)s, %(last_inspection_date)s, %(created_at)s, %(updated_at)s
        )
        on conflict (id) do update set
            org_id=excluded.org_id,
            serial_number=excluded.serial_number,
            type=excluded.type,
            status=excluded.status,
            assigned_to=excluded.assigned_to,
            condition=excluded.condition,
            last_inspection_date=excluded.last_inspection_date,
            updated_at=excluded.updated_at
        """
        self._execute(sql, weapon)

    def _upsert_ammunition_db(self, ammo: dict[str, Any]) -> None:
        if not self.use_db:
            return
        sql = """
        insert into ammunition (
            id, org_id, type, quantity, threshold, last_restocked, created_at, updated_at
        ) values (
            %(id)s, %(org_id)s, %(type)s, %(quantity)s, %(threshold)s, %(last_restocked)s, %(created_at)s, %(updated_at)s
        )
        on conflict (id) do update set
            org_id=excluded.org_id,
            type=excluded.type,
            quantity=excluded.quantity,
            threshold=excluded.threshold,
            last_restocked=excluded.last_restocked,
            updated_at=excluded.updated_at
        """
        self._execute(sql, ammo)

    def _upsert_equipment_db(self, equipment: dict[str, Any]) -> None:
        if not self.use_db:
            return
        sql = """
        insert into tactical_equipment (
            id, org_id, category, total_quantity, available_quantity, in_use_quantity, threshold, condition_breakdown, created_at, updated_at
        ) values (
            %(id)s, %(org_id)s, %(category)s, %(total_quantity)s, %(available_quantity)s, %(in_use_quantity)s, %(threshold)s, %(condition_breakdown)s::jsonb, %(created_at)s, %(updated_at)s
        )
        on conflict (id) do update set
            org_id=excluded.org_id,
            category=excluded.category,
            total_quantity=excluded.total_quantity,
            available_quantity=excluded.available_quantity,
            in_use_quantity=excluded.in_use_quantity,
            threshold=excluded.threshold,
            condition_breakdown=excluded.condition_breakdown,
            updated_at=excluded.updated_at
        """
        row = dict(equipment)
        row["condition_breakdown"] = self._json(row.get("condition_breakdown", {}))
        self._execute(sql, row)

    def _upsert_fuel_reserve_db(self, reserve: dict[str, Any]) -> None:
        if not self.use_db:
            return
        sql = """
        insert into fuel_reserves (
            id, org_id, current_litres, capacity_litres, threshold_litres, last_restocked, resupply_contact, updated_at
        ) values (
            %(id)s, %(org_id)s, %(current_litres)s, %(capacity_litres)s, %(threshold_litres)s, %(last_restocked)s, %(resupply_contact)s, %(updated_at)s
        )
        on conflict (org_id) do update set
            current_litres=excluded.current_litres,
            capacity_litres=excluded.capacity_litres,
            threshold_litres=excluded.threshold_litres,
            last_restocked=excluded.last_restocked,
            resupply_contact=excluded.resupply_contact,
            updated_at=excluded.updated_at
        """
        self._execute(sql, reserve)

    def _upsert_cadence_db(self, org_id: str, cadence: dict[str, int]) -> None:
        if not self.use_db:
            return
        sql = """
        insert into inventory_cadence_rules (
            id, org_id, officer_ping_seconds, vehicle_running_seconds, vehicle_parked_seconds, created_at, updated_at
        ) values (
            %(id)s, %(org_id)s, %(officer_ping_seconds)s, %(vehicle_running_seconds)s, %(vehicle_parked_seconds)s, now(), now()
        )
        on conflict (org_id) do update set
            officer_ping_seconds=excluded.officer_ping_seconds,
            vehicle_running_seconds=excluded.vehicle_running_seconds,
            vehicle_parked_seconds=excluded.vehicle_parked_seconds,
            updated_at=now()
        """
        row = {
            "id": str(uuid4()),
            "org_id": org_id,
            "officer_ping_seconds": cadence["officer_ping_seconds"],
            "vehicle_running_seconds": cadence["vehicle_running_seconds"],
            "vehicle_parked_seconds": cadence["vehicle_parked_seconds"],
        }
        self._execute(sql, row)

    def _upsert_threshold_db(self, threshold: dict[str, Any]) -> None:
        if not self.use_db:
            return
        sql = """
        insert into inventory_thresholds (
            id, org_id, resource_type, metric, threshold_value, advisory_value, alert_repeat_minutes, created_at, updated_at
        ) values (
            %(id)s, %(org_id)s, %(resource_type)s, %(metric)s, %(threshold_value)s, %(advisory_value)s, %(alert_repeat_minutes)s, now(), now()
        )
        on conflict (org_id, resource_type, metric) do update set
            threshold_value=excluded.threshold_value,
            advisory_value=excluded.advisory_value,
            alert_repeat_minutes=excluded.alert_repeat_minutes,
            updated_at=now()
        """
        row = dict(threshold)
        row["id"] = row.get("id") or str(uuid4())
        self._execute(sql, row)

    def _upsert_alert_db(self, alert: dict[str, Any]) -> None:
        if not self.use_db:
            return
        sql = """
        insert into inventory_alerts (
            id, alert_id, org_id, alert_level, resource_type, metric, current_value, threshold_value, message,
            affected_resources, recommended_action, resolved, resolved_at, first_alerted_at, last_alerted_at,
            next_alert_at, alert_count
        ) values (
            %(id)s, %(alert_id)s, %(org_id)s, %(alert_level)s, %(resource_type)s, %(metric)s, %(current_value)s, %(threshold_value)s, %(message)s,
            %(affected_resources)s::jsonb, %(recommended_action)s, %(resolved)s, %(resolved_at)s, %(first_alerted_at)s, %(last_alerted_at)s,
            %(next_alert_at)s, %(alert_count)s
        )
        on conflict (alert_id) do update set
            alert_level=excluded.alert_level,
            resource_type=excluded.resource_type,
            metric=excluded.metric,
            current_value=excluded.current_value,
            threshold_value=excluded.threshold_value,
            message=excluded.message,
            affected_resources=excluded.affected_resources,
            recommended_action=excluded.recommended_action,
            resolved=excluded.resolved,
            resolved_at=excluded.resolved_at,
            first_alerted_at=excluded.first_alerted_at,
            last_alerted_at=excluded.last_alerted_at,
            next_alert_at=excluded.next_alert_at,
            alert_count=excluded.alert_count
        """
        row = dict(alert)
        row["id"] = row.get("id") or str(uuid4())
        row["affected_resources"] = self._json(row.get("affected_resources", []))
        self._execute(sql, row)

    def _insert_transaction_db(self, txn: dict[str, Any]) -> None:
        if not self.use_db:
            return
        sql = """
        insert into inventory_transactions (
            id, org_id, resource_type, resource_id, action, old_value, new_value, performed_by, incident_id, notes, created_at
        ) values (
            %(id)s, %(org_id)s, %(resource_type)s, %(resource_id)s, %(action)s, %(old_value)s::jsonb, %(new_value)s::jsonb, %(performed_by)s, %(incident_id)s, %(notes)s, %(created_at)s
        )
        """
        row = dict(txn)
        row["old_value"] = self._json(row.get("old_value"))
        row["new_value"] = self._json(row.get("new_value"))
        self._execute(sql, row)

    def seed(self) -> None:
        with self._lock:
            self.officers = []
            statuses = (
                ["available"] * 9
                + ["on_duty"] * 8
                + ["off_duty"] * 5
                + ["on_leave"] * 2
            )
            for index in range(24):
                status = statuses[index]
                armed = index < 4 if status == "available" else index < 7 if status == "on_duty" else False
                self.officers.append(
                    {
                        "id": str(uuid4()),
                        "org_id": DEFAULT_ORG_ID,
                        "officer_id": f"OFF-{index + 1:03d}",
                        "name": [
                            "Ahmed Bello",
                            "Bisi Adeyemi",
                            "Chinedu Okafor",
                            "Damilola Sanni",
                            "Emeka Uche",
                            "Favour Ibe",
                            "Grace Amina",
                            "Hassan Musa",
                            "Ifeoma Nwosu",
                            "Jide Kareem",
                            "Khalid Ibrahim",
                            "Laila Yusuf",
                            "Musa Danladi",
                            "Ngozi Eze",
                            "Olawale Sodiq",
                            "Precious Okoye",
                            "Sani Yusuf",
                            "Tunde Ajayi",
                            "Uchechukwu Obi",
                            "Victoria Akin",
                            "Wale Ade",
                            "Yemi Peters",
                            "Zainab Bello",
                            "Aisha Umar",
                        ][index],
                        "badge_number": f"LG-{42 + index:04d}",
                        "status": status,
                        "armed": armed,
                        "weapon": "pistol" if armed else None,
                        "rank": "Officer" if index < 18 else "Sergeant",
                        "certifications": ["armed_response", "first_aid"] if armed else ["first_aid"],
                        "contact": f"+23480000{index:04d}",
                        "assigned_zone": "Zone A" if index % 2 == 0 else "Zone B",
                        "current_lat": 6.4281 + index * 0.001,
                        "current_lng": 3.4219 + index * 0.001,
                        "location_updated_at": iso(),
                        "shift_start": None,
                        "shift_end": None,
                        "created_at": iso(),
                        "updated_at": iso(),
                    }
                )

            self.vehicles = [
                {
                    "id": str(uuid4()),
                    "org_id": DEFAULT_ORG_ID,
                    "vehicle_id": "V001",
                    "plate_number": "LG-XYZ-001",
                    "type": "patrol_car",
                    "status": "available",
                    "fuel_percentage": 85,
                    "fuel_litres": 42.5,
                    "fuel_last_updated": iso(),
                    "condition": "good",
                    "capacity": 4,
                    "assigned_driver_id": None,
                    "current_lat": 6.4300,
                    "current_lng": 3.4250,
                    "location_updated_at": iso(),
                    "last_service_date": "2026-05-01",
                    "next_service_due": "2026-08-01",
                    "odometer_km": 1420,
                    "special_equipment": ["light_bar", "radio"],
                    "created_at": iso(),
                    "updated_at": iso(),
                },
                {
                    "id": str(uuid4()),
                    "org_id": DEFAULT_ORG_ID,
                    "vehicle_id": "V002",
                    "plate_number": "LG-XYZ-002",
                    "type": "patrol_car",
                    "status": "available",
                    "fuel_percentage": 28,
                    "fuel_litres": 12.2,
                    "fuel_last_updated": iso(),
                    "condition": "needs_service",
                    "capacity": 4,
                    "assigned_driver_id": None,
                    "current_lat": 6.4310,
                    "current_lng": 3.4260,
                    "location_updated_at": iso(),
                    "last_service_date": "2026-05-12",
                    "next_service_due": "2026-06-01",
                    "odometer_km": 2180,
                    "special_equipment": ["light_bar", "radio", "weapon_rack"],
                    "created_at": iso(),
                    "updated_at": iso(),
                },
                {
                    "id": str(uuid4()),
                    "org_id": DEFAULT_ORG_ID,
                    "vehicle_id": "V003",
                    "plate_number": "LG-XYZ-003",
                    "type": "motorcycle",
                    "status": "deployed",
                    "fuel_percentage": 18,
                    "fuel_litres": 6.5,
                    "fuel_last_updated": iso(),
                    "condition": "good",
                    "capacity": 1,
                    "assigned_driver_id": None,
                    "current_lat": 6.4330,
                    "current_lng": 3.4270,
                    "location_updated_at": iso(),
                    "last_service_date": "2026-05-20",
                    "next_service_due": "2026-07-20",
                    "odometer_km": 640,
                    "special_equipment": ["radio"],
                    "created_at": iso(),
                    "updated_at": iso(),
                },
                {
                    "id": str(uuid4()),
                    "org_id": DEFAULT_ORG_ID,
                    "vehicle_id": "V004",
                    "plate_number": "LG-XYZ-004",
                    "type": "armoured",
                    "status": "deployed",
                    "fuel_percentage": 12,
                    "fuel_litres": 20.0,
                    "fuel_last_updated": iso(),
                    "condition": "critical",
                    "capacity": 6,
                    "assigned_driver_id": None,
                    "current_lat": 6.4340,
                    "current_lng": 3.4280,
                    "location_updated_at": iso(),
                    "last_service_date": "2026-04-10",
                    "next_service_due": "2026-05-25",
                    "odometer_km": 5400,
                    "special_equipment": ["weapon_rack", "radio"],
                    "created_at": iso(),
                    "updated_at": iso(),
                },
                {
                    "id": str(uuid4()),
                    "org_id": DEFAULT_ORG_ID,
                    "vehicle_id": "V005",
                    "plate_number": "LG-XYZ-005",
                    "type": "truck",
                    "status": "available",
                    "fuel_percentage": 35,
                    "fuel_litres": 30.0,
                    "fuel_last_updated": iso(),
                    "condition": "good",
                    "capacity": 8,
                    "assigned_driver_id": None,
                    "current_lat": 6.4350,
                    "current_lng": 3.4290,
                    "location_updated_at": iso(),
                    "last_service_date": "2026-05-30",
                    "next_service_due": "2026-08-30",
                    "odometer_km": 1820,
                    "special_equipment": ["light_bar", "radio"],
                    "created_at": iso(),
                    "updated_at": iso(),
                },
            ]

            self.weapons = [
                *[
                    {
                        "id": str(uuid4()),
                        "org_id": DEFAULT_ORG_ID,
                        "serial_number": f"PISTOL-{n:03d}",
                        "type": "pistol",
                        "status": "in_armoury",
                        "assigned_to": None,
                        "condition": "good",
                        "last_inspection_date": "2026-05-01",
                        "created_at": iso(),
                        "updated_at": iso(),
                    }
                    for n in range(1, 7)
                ],
                *[
                    {
                        "id": str(uuid4()),
                        "org_id": DEFAULT_ORG_ID,
                        "serial_number": f"RIFLE-{n:03d}",
                        "type": "rifle",
                        "status": "in_armoury",
                        "assigned_to": None,
                        "condition": "good",
                        "last_inspection_date": "2026-05-03",
                        "created_at": iso(),
                        "updated_at": iso(),
                    }
                    for n in range(1, 3)
                ],
                *[
                    {
                        "id": str(uuid4()),
                        "org_id": DEFAULT_ORG_ID,
                        "serial_number": f"TASER-{n:03d}",
                        "type": "taser",
                        "status": "in_armoury",
                        "assigned_to": None,
                        "condition": "good",
                        "last_inspection_date": "2026-05-05",
                        "created_at": iso(),
                        "updated_at": iso(),
                    }
                    for n in range(1, 4)
                ],
            ]

            self.ammunition = [
                {
                    "id": str(uuid4()),
                    "org_id": DEFAULT_ORG_ID,
                    "type": "pistol_rounds",
                    "quantity": 450,
                    "threshold": 200,
                    "last_restocked": "2026-05-28T10:00:00+00:00",
                    "created_at": iso(),
                    "updated_at": iso(),
                },
                {
                    "id": str(uuid4()),
                    "org_id": DEFAULT_ORG_ID,
                    "type": "rifle_rounds",
                    "quantity": 200,
                    "threshold": 150,
                    "last_restocked": "2026-05-28T10:00:00+00:00",
                    "created_at": iso(),
                    "updated_at": iso(),
                },
            ]

            self.tactical_equipment = [
                {
                    "id": str(uuid4()),
                    "org_id": DEFAULT_ORG_ID,
                    "category": "body_armour",
                    "total_quantity": 8,
                    "available_quantity": 8,
                    "in_use_quantity": 0,
                    "threshold": 6,
                    "condition_breakdown": {"good": 7, "needs_service": 1, "decommissioned": 0},
                    "created_at": iso(),
                    "updated_at": iso(),
                },
                {
                    "id": str(uuid4()),
                    "org_id": DEFAULT_ORG_ID,
                    "category": "radios",
                    "total_quantity": 12,
                    "available_quantity": 12,
                    "in_use_quantity": 0,
                    "threshold": 10,
                    "condition_breakdown": {"good": 11, "needs_service": 1, "decommissioned": 0},
                    "created_at": iso(),
                    "updated_at": iso(),
                },
                {
                    "id": str(uuid4()),
                    "org_id": DEFAULT_ORG_ID,
                    "category": "first_aid_kits",
                    "total_quantity": 4,
                    "available_quantity": 4,
                    "in_use_quantity": 0,
                    "threshold": 3,
                    "condition_breakdown": {"good": 4, "needs_service": 0, "decommissioned": 0},
                    "created_at": iso(),
                    "updated_at": iso(),
                },
            ]

            self.fuel_reserves = {
                DEFAULT_ORG_ID: {
                    "id": str(uuid4()),
                    "org_id": DEFAULT_ORG_ID,
                    "current_litres": 180.0,
                    "capacity_litres": 400.0,
                    "threshold_litres": DEFAULT_FUEL_RESERVE_LITRES,
                    "last_restocked": "2026-05-30T08:00:00+00:00",
                    "resupply_contact": "+2348000000000",
                    "updated_at": iso(),
                }
            }

            self.thresholds = {
                (DEFAULT_ORG_ID, "officers", "available"): {
                    "threshold_value": float(DEFAULT_MIN_OFFICERS),
                    "advisory_value": max(float(DEFAULT_MIN_OFFICERS) * 0.8, 0),
                    "alert_repeat_minutes": 30,
                },
                (DEFAULT_ORG_ID, "officers", "armed"): {
                    "threshold_value": float(DEFAULT_MIN_ARMED_OFFICERS),
                    "advisory_value": max(float(DEFAULT_MIN_ARMED_OFFICERS) * 0.8, 0),
                    "alert_repeat_minutes": 30,
                },
                (DEFAULT_ORG_ID, "vehicles", "available"): {
                    "threshold_value": float(DEFAULT_MIN_VEHICLES),
                    "advisory_value": max(float(DEFAULT_MIN_VEHICLES) * 0.8, 0),
                    "alert_repeat_minutes": 30,
                },
                (DEFAULT_ORG_ID, "vehicles", "fuelled_count"): {
                    "threshold_value": float(DEFAULT_MIN_FUELLED_VEHICLES),
                    "advisory_value": max(float(DEFAULT_MIN_FUELLED_VEHICLES) * 0.8, 0),
                    "alert_repeat_minutes": 30,
                },
                (DEFAULT_ORG_ID, "vehicles", "fuel_percentage"): {
                    "threshold_value": float(DEFAULT_VEHICLE_FUEL_THRESHOLD),
                    "advisory_value": 24.0,
                    "alert_repeat_minutes": 30,
                },
                (DEFAULT_ORG_ID, "fuel_reserve", "litres"): {
                    "threshold_value": float(DEFAULT_FUEL_RESERVE_LITRES),
                    "advisory_value": float(DEFAULT_FUEL_RESERVE_LITRES) * 1.2,
                    "alert_repeat_minutes": 30,
                },
            }
            self.last_updated_at = now_utc()
            self.orgs = {DEFAULT_ORG_ID}

    def touch(self) -> None:
        self.last_updated_at = now_utc()

    def record_transaction(
        self,
        org_id: str,
        resource_type: str,
        resource_id: str,
        action: str,
        old_value: Any,
        new_value: Any,
        performed_by: str | None = None,
        notes: str | None = None,
        incident_id: str | None = None,
    ) -> None:
        self.transactions.append(
            {
                "id": str(uuid4()),
                "org_id": org_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "action": action,
                "old_value": deep_copy(old_value),
                "new_value": deep_copy(new_value),
                "performed_by": performed_by,
                "incident_id": incident_id,
                "notes": notes,
                "created_at": iso(),
            }
        )
        self._insert_transaction_db(self.transactions[-1])

    def find_alert(self, org_id: str, resource_type: str, metric: str) -> dict[str, Any] | None:
        for alert in self.alerts:
            if (
                alert["org_id"] == org_id
                and alert["resource_type"] == resource_type
                and alert["metric"] == metric
                and not alert["resolved"]
            ):
                return alert
        return None

    def upsert_alert(self, alert: dict[str, Any]) -> dict[str, Any]:
        existing = self.find_alert(alert["org_id"], alert["resource_type"], alert["metric"])
        if existing is None:
            self.alerts.append(alert)
            self._upsert_alert_db(alert)
            return alert
        existing.update(alert)
        self._upsert_alert_db(existing)
        return existing

    def resolve_alert(self, alert_id: str) -> dict[str, Any] | None:
        for alert in self.alerts:
            if alert["alert_id"] == alert_id:
                alert["resolved"] = True
                alert["resolved_at"] = iso()
                self._upsert_alert_db(alert)
                return alert
        return None

    def threshold_for(self, org_id: str, resource_type: str, metric: str) -> dict[str, Any] | None:
        return self.thresholds.get((org_id, resource_type, metric))

    def cadence_for(self, org_id: str) -> dict[str, int]:
        cadence = self.cadence_rules.get(org_id)
        if cadence:
            return cadence
        return {
            "officer_ping_seconds": DEFAULT_OFFICER_PING_SECONDS,
            "vehicle_running_seconds": DEFAULT_VEHICLE_GPS_RUNNING_SECONDS,
            "vehicle_parked_seconds": DEFAULT_VEHICLE_GPS_PARKED_SECONDS,
        }

    def set_cadence(self, payload: CadenceUpdate) -> dict[str, int]:
        with self._lock:
            current = self.cadence_for(payload.org_id)
            updated = {
                "officer_ping_seconds": payload.officer_ping_seconds or current["officer_ping_seconds"],
                "vehicle_running_seconds": payload.vehicle_running_seconds or current["vehicle_running_seconds"],
                "vehicle_parked_seconds": payload.vehicle_parked_seconds or current["vehicle_parked_seconds"],
            }
            self.cadence_rules[payload.org_id] = updated
            self._upsert_cadence_db(payload.org_id, updated)
            self._sync_org(payload.org_id)
            self.touch()
            return updated

    def officer_ping_staleness(self, org_id: str) -> dict[str, Any]:
        cadence = self.cadence_for(org_id)
        max_age = timedelta(seconds=cadence["officer_ping_seconds"])
        stale = []
        for officer in self.officer_rows(org_id):
            updated_at = parse_iso(officer.get("location_updated_at"))
            if updated_at is None or now_utc() - updated_at > max_age:
                stale.append(officer["officer_id"])
        return {
            "cadence_seconds": cadence["officer_ping_seconds"],
            "stale_count": len(stale),
            "stale_officers": stale,
        }

    def vehicle_gps_staleness(self, org_id: str) -> dict[str, Any]:
        cadence = self.cadence_for(org_id)
        stale = []
        running_limit = timedelta(seconds=cadence["vehicle_running_seconds"])
        parked_limit = timedelta(seconds=cadence["vehicle_parked_seconds"])
        for vehicle in self.vehicle_rows(org_id):
            updated_at = parse_iso(vehicle.get("location_updated_at"))
            if updated_at is None:
                stale.append(vehicle["vehicle_id"])
                continue
            limit = running_limit if vehicle.get("status") == "deployed" else parked_limit
            if now_utc() - updated_at > limit:
                stale.append(vehicle["vehicle_id"])
        return {
            "running_cadence_seconds": cadence["vehicle_running_seconds"],
            "parked_cadence_seconds": cadence["vehicle_parked_seconds"],
            "stale_count": len(stale),
            "stale_vehicles": stale,
        }

    def set_threshold(self, payload: ThresholdUpdate) -> dict[str, Any]:
        with self._lock:
            value = {
                "threshold_value": float(payload.threshold_value),
                "advisory_value": payload.advisory_value,
                "alert_repeat_minutes": payload.alert_repeat_minutes,
            }
            self.thresholds[(payload.org_id, payload.resource_type, payload.metric)] = value
            self._upsert_threshold_db(
                {
                    "org_id": payload.org_id,
                    "resource_type": payload.resource_type,
                    "metric": payload.metric,
                    "threshold_value": value["threshold_value"],
                    "advisory_value": value["advisory_value"],
                    "alert_repeat_minutes": value["alert_repeat_minutes"],
                }
            )
            self.touch()
            return value

    def officer_rows(self, org_id: str) -> list[dict[str, Any]]:
        return [row for row in self.officers if row["org_id"] == org_id]

    def vehicle_rows(self, org_id: str) -> list[dict[str, Any]]:
        return [row for row in self.vehicles if row["org_id"] == org_id]

    def weapon_rows(self, org_id: str) -> list[dict[str, Any]]:
        return [row for row in self.weapons if row["org_id"] == org_id]

    def ammunition_rows(self, org_id: str) -> list[dict[str, Any]]:
        return [row for row in self.ammunition if row["org_id"] == org_id]

    def tactical_rows(self, org_id: str) -> list[dict[str, Any]]:
        return [row for row in self.tactical_equipment if row["org_id"] == org_id]

    def active_alerts(self, org_id: str | None = None) -> list[dict[str, Any]]:
        return [a for a in self.alerts if not a["resolved"] and (org_id is None or a["org_id"] == org_id)]

    def summary(self, org_id: str) -> dict[str, Any]:
        self.apply_shift_transitions(org_id)
        officers = self.officer_rows(org_id)
        vehicles = self.vehicle_rows(org_id)
        weapons = self.weapon_rows(org_id)
        ammunition = self.ammunition_rows(org_id)
        tactical = self.tactical_rows(org_id)
        fuel_reserve = self.fuel_reserves.get(org_id)
        officer_available = [o for o in officers if o["status"] == "available"]
        armed_available = [o for o in officer_available if o.get("armed")]
        vehicle_available = [v for v in vehicles if v["status"] == "available"]
        fuelled = [v for v in vehicles if v.get("fuel_percentage", 0) >= DEFAULT_VEHICLE_FUEL_THRESHOLD]
        thresholds = self.evaluate_thresholds(org_id, persist=False)
        fuel_threshold = self.threshold_for(org_id, "fuel_reserve", "litres")
        fuel_below = False
        fuel_level = 0
        fuel_pct = 0
        if fuel_reserve:
            fuel_level = as_float(fuel_reserve.get("current_litres"))
            cap = as_float(fuel_reserve.get("capacity_litres"))
            fuel_pct = int((fuel_level / cap) * 100) if cap else 0
            fuel_below = fuel_level < as_float(fuel_threshold["threshold_value"]) if fuel_threshold else False
        officer_cadence = self.officer_ping_staleness(org_id)
        vehicle_cadence = self.vehicle_gps_staleness(org_id)
        return {
            "officers": {
                "total": len(officers),
                "available": len(officer_available),
                "on_duty": sum(1 for o in officers if o["status"] == "on_duty"),
                "off_duty": sum(1 for o in officers if o["status"] == "off_duty"),
                "on_leave": sum(1 for o in officers if o["status"] == "on_leave"),
                "armed_available": len(armed_available),
                "below_threshold": len(officer_available) < DEFAULT_MIN_OFFICERS,
            },
            "vehicles": {
                "total": len(vehicles),
                "available": len(vehicle_available),
                "deployed": sum(1 for v in vehicles if v["status"] == "deployed"),
                "fuelled": len(fuelled),
                "below_threshold": len(fuelled) < DEFAULT_MIN_FUELLED_VEHICLES or len(vehicle_available) < DEFAULT_MIN_VEHICLES,
                "threshold_alert_level": self.alert_level_for(len(fuelled), DEFAULT_MIN_FUELLED_VEHICLES),
            },
            "weapons": {
                "pistols_available": sum(1 for w in weapons if w["type"] == "pistol" and w["status"] == "in_armoury"),
                "rifles_available": sum(1 for w in weapons if w["type"] == "rifle" and w["status"] == "in_armoury"),
                "tasers_available": sum(1 for w in weapons if w["type"] == "taser" and w["status"] == "in_armoury"),
                "below_threshold": False,
            },
            "ammunition": {
                "pistol_rounds": sum(a["quantity"] for a in ammunition if a["type"] == "pistol_rounds"),
                "rifle_rounds": sum(a["quantity"] for a in ammunition if a["type"] == "rifle_rounds"),
                "below_threshold": any(a["quantity"] < a["threshold"] for a in ammunition),
            },
            "tactical": {
                "body_armour_available": next((t["available_quantity"] for t in tactical if t["category"] == "body_armour"), 0),
                "radios_available": next((t["available_quantity"] for t in tactical if t["category"] == "radios"), 0),
                "first_aid_kits": next((t["available_quantity"] for t in tactical if t["category"] == "first_aid_kits"), 0),
                "below_threshold": any(t["available_quantity"] < t["threshold"] for t in tactical),
            },
            "fuel_reserve": {
                "litres": fuel_level,
                "percentage": fuel_pct,
                "below_threshold": fuel_below,
                "threshold_alert_level": self.alert_level_for(fuel_level, as_float(fuel_threshold["threshold_value"]) if fuel_threshold else DEFAULT_FUEL_RESERVE_LITRES),
            },
            "cadence": {
                "officers": officer_cadence,
                "vehicles": vehicle_cadence,
            },
            "active_alerts": len(self.active_alerts(org_id)),
            "last_updated": iso(self.last_updated_at),
            "_threshold_state": thresholds,
        }

    def alert_level_for(self, current: float, threshold: float) -> str:
        if threshold <= 0:
            return AlertLevel.advisory.value
        ratio = current / threshold
        if current < threshold * 0.5:
            return AlertLevel.emergency.value
        if current < threshold:
            return AlertLevel.critical.value
        if current == threshold:
            return AlertLevel.warning.value
        if ratio <= 1.2:
            return AlertLevel.advisory.value
        return AlertLevel.advisory.value

    def apply_shift_transitions(self, org_id: str) -> list[dict[str, Any]]:
        with self._lock:
            now = now_utc()
            changed: list[dict[str, Any]] = []
            for officer in self.officer_rows(org_id):
                shift_start = parse_iso(officer.get("shift_start"))
                shift_end = parse_iso(officer.get("shift_end"))
                old_status = officer.get("status")
                new_status = old_status
                if shift_start and shift_end:
                    if shift_start <= now <= shift_end:
                        if old_status == "off_duty":
                            new_status = "on_duty"
                    elif now > shift_end and old_status == "on_duty":
                        new_status = "off_duty"
                elif shift_start and now >= shift_start and old_status == "off_duty":
                    new_status = "on_duty"
                elif shift_end and now > shift_end and old_status == "on_duty":
                    new_status = "off_duty"

                if new_status != old_status:
                    officer["status"] = new_status
                    officer["updated_at"] = iso()
                    self.record_transaction(
                        org_id,
                        "officers",
                        officer["id"],
                        "status_changed",
                        {"status": old_status, "shift_start": officer.get("shift_start"), "shift_end": officer.get("shift_end")},
                        {"status": new_status, "shift_start": officer.get("shift_start"), "shift_end": officer.get("shift_end")},
                    )
                    self._upsert_officer_db(officer)
                    changed.append(officer)
            if changed:
                self.touch()
            return changed

    def apply_incident_assignment(self, resource: dict[str, Any], resource_type: str, incident_id: str | None) -> bool:
        if not incident_id:
            return False
        current_status = resource.get("status")
        if resource_type == "officer" and current_status in {None, "off_duty"}:
            resource["status"] = "on_duty"
            return True
        if resource_type == "vehicle" and current_status in {None, "available"}:
            resource["status"] = "deployed"
            return True
        return False

    def evaluate_metric(
        self,
        org_id: str,
        resource_type: str,
        metric: str,
        current_value: float,
        threshold_value: float,
        message: str,
        affected_resources: list[str] | None = None,
        recommended_action: str | None = None,
        advisory_value: float | None = None,
        repeat_minutes: int | None = None,
    ) -> dict[str, Any] | None:
        if threshold_value <= 0:
            return None

        breached = current_value <= threshold_value
        if not breached:
            return None

        if current_value < threshold_value * 0.5:
            level = AlertLevel.emergency.value
        elif current_value < threshold_value:
            level = AlertLevel.critical.value
        elif current_value == threshold_value:
            level = AlertLevel.warning.value
        else:
            level = AlertLevel.advisory.value

        existing = self.find_alert(org_id, resource_type, metric)
        if existing:
            existing_level = existing.get("alert_level", AlertLevel.warning.value)
            level_rank = {
                AlertLevel.advisory.value: 1,
                AlertLevel.warning.value: 2,
                AlertLevel.critical.value: 3,
                AlertLevel.emergency.value: 4,
            }
            existing_next = parse_iso(existing.get("next_alert_at"))
            repeat = repeat_minutes if repeat_minutes is not None else ALERT_REPEAT_MINUTES.get(AlertLevel(existing_level), None)
            is_escalation = level_rank.get(level, 0) > level_rank.get(existing_level, 0)
            if not is_escalation:
                if not repeat:
                    return None
                if existing_next and existing_next > now_utc():
                    return None
            alert_id = existing["alert_id"]
            alert_count = existing["alert_count"] + 1
            first_alerted_at = existing["first_alerted_at"]
        else:
            alert_id = f"INV-ALERT-{uuid4().hex[:8].upper()}"
            repeat = repeat_minutes if repeat_minutes is not None else ALERT_REPEAT_MINUTES[AlertLevel(level)]
            alert_count = 1
            first_alerted_at = iso()

        next_alert_at = iso(now_utc() + timedelta(minutes=repeat)) if repeat else None
        payload = {
            "alert_id": alert_id,
            "org_id": org_id,
            "timestamp": iso(),
            "alert_level": level,
            "resource_type": resource_type,
            "metric": metric,
            "current_value": current_value,
            "threshold_value": threshold_value,
            "message": message,
            "affected_resources": affected_resources or [],
            "recommended_action": recommended_action,
            "repeat_alert": repeat is not None,
            "next_alert_at": next_alert_at,
            "resolved": False,
            "resolved_at": None,
            "first_alerted_at": first_alerted_at,
            "last_alerted_at": iso(),
            "alert_count": alert_count,
        }
        review = groq_inventory_review(
            "inventory_alert",
            proposed=payload,
            evidence={
                "org_id": org_id,
                "resource_type": resource_type,
                "metric": metric,
                "current_value": current_value,
                "threshold_value": threshold_value,
                "affected_resources": affected_resources or [],
                "recommended_action": recommended_action,
            },
            schema_hint={
                "approved": True,
                "issues": ["string"],
                "severity": "low|medium|high|critical",
                "suggested_message": "string",
                "suggested_action": "string",
            },
        )
        if review:
            payload["llm_review"] = review
            if isinstance(review.get("suggested_message"), str) and review["suggested_message"].strip():
                payload["message"] = review["suggested_message"].strip()
            if isinstance(review.get("suggested_action"), str) and review["suggested_action"].strip():
                payload["recommended_action"] = review["suggested_action"].strip()
        return self.upsert_alert(payload)

    def clear_resolved_alerts(self, active_keys: set[tuple[str, str, str]]) -> list[dict[str, Any]]:
        cleared: list[dict[str, Any]] = []
        for alert in self.alerts:
            key = (alert["org_id"], alert["resource_type"], alert["metric"])
            if not alert["resolved"] and key not in active_keys:
                alert["resolved"] = True
                alert["resolved_at"] = iso()
                cleared.append(alert)
        return cleared

    def evaluate_thresholds(self, org_id: str, persist: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            org_id = org_id or DEFAULT_ORG_ID
            self.apply_shift_transitions(org_id)
            alerts: list[dict[str, Any]] = []
            active_keys: set[tuple[str, str, str]] = set()

            officers = self.officer_rows(org_id)
            available = [o for o in officers if o["status"] == "available"]
            armed_available = [o for o in available if o.get("armed")]
            threshold = self.threshold_for(org_id, "officers", "available")
            if threshold:
                breached = len(available) <= threshold["threshold_value"]
                if breached:
                    alerts_generated = self.evaluate_metric(
                        org_id,
                        "officers",
                        "available",
                        len(available),
                        threshold["threshold_value"],
                        f"{len(available)} officers available. Minimum required: {int(threshold['threshold_value'])}.",
                        [o["officer_id"] for o in available],
                        "Return officers to available duty status or bring in reserves.",
                        threshold.get("advisory_value"),
                        threshold.get("alert_repeat_minutes"),
                    )
                    active_keys.add((org_id, "officers", "available"))
                    if alerts_generated:
                        alerts.append(alerts_generated)
            threshold = self.threshold_for(org_id, "officers", "armed")
            if threshold:
                breached = len(armed_available) <= threshold["threshold_value"]
                if breached:
                    alerts_generated = self.evaluate_metric(
                        org_id,
                        "officers",
                        "armed",
                        len(armed_available),
                        threshold["threshold_value"],
                        f"{len(armed_available)} armed officers available. Minimum required: {int(threshold['threshold_value'])}.",
                        [o["officer_id"] for o in armed_available],
                        "Ensure armed officer coverage before next shift.",
                        threshold.get("advisory_value"),
                        threshold.get("alert_repeat_minutes"),
                    )
                    active_keys.add((org_id, "officers", "armed"))
                    if alerts_generated:
                        alerts.append(alerts_generated)

            vehicles = self.vehicle_rows(org_id)
            vehicle_available = [v for v in vehicles if v["status"] == "available"]
            fuelled = [v for v in vehicles if v.get("fuel_percentage", 0) >= DEFAULT_VEHICLE_FUEL_THRESHOLD]
            threshold = self.threshold_for(org_id, "vehicles", "available")
            if threshold:
                breached = len(vehicle_available) <= threshold["threshold_value"]
                if breached:
                    alerts_generated = self.evaluate_metric(
                        org_id,
                        "vehicles",
                        "available",
                        len(vehicle_available),
                        threshold["threshold_value"],
                        f"Only {len(vehicle_available)} vehicles available. Minimum required: {int(threshold['threshold_value'])}.",
                        [v["vehicle_id"] for v in vehicle_available],
                        "Restore vehicle availability or revise deployment plan.",
                        threshold.get("advisory_value"),
                        threshold.get("alert_repeat_minutes"),
                    )
                    active_keys.add((org_id, "vehicles", "available"))
                    if alerts_generated:
                        alerts.append(alerts_generated)
            threshold = self.threshold_for(org_id, "vehicles", "fuelled_count")
            if threshold:
                breached = len(fuelled) <= threshold["threshold_value"]
                if breached:
                    alerts_generated = self.evaluate_metric(
                        org_id,
                        "vehicles",
                        "fuelled_count",
                        len(fuelled),
                        threshold["threshold_value"],
                        f"Only {len(fuelled)} of {len(vehicles)} vehicles has sufficient fuel. Minimum required: {int(threshold['threshold_value'])}.",
                        [v["vehicle_id"] for v in vehicles if v.get("fuel_percentage", 0) < DEFAULT_VEHICLE_FUEL_THRESHOLD],
                        "Refuel low vehicles before next shift.",
                        threshold.get("advisory_value"),
                        threshold.get("alert_repeat_minutes"),
                    )
                    active_keys.add((org_id, "vehicles", "fuelled_count"))
                    if alerts_generated:
                        alerts.append(alerts_generated)
            threshold = self.threshold_for(org_id, "vehicles", "fuel_percentage")
            if threshold:
                for vehicle in vehicles:
                    current = as_float(vehicle.get("fuel_percentage"))
                    if current <= threshold["threshold_value"]:
                        metric_name = f"fuel_percentage:{vehicle['vehicle_id']}"
                        alert = self.evaluate_metric(
                            org_id,
                            "vehicles",
                            metric_name,
                            current,
                            threshold["threshold_value"],
                            f"Vehicle {vehicle['vehicle_id']} fuel below {int(threshold['threshold_value'])}%.",
                            [vehicle["vehicle_id"]],
                            f"Refuel vehicle {vehicle['vehicle_id']} immediately.",
                            threshold.get("advisory_value"),
                            threshold.get("alert_repeat_minutes"),
                        )
                        active_keys.add((org_id, "vehicles", metric_name))
                        if alert:
                            alerts.append(alert)

            fuel_reserve = self.fuel_reserves.get(org_id)
            threshold = self.threshold_for(org_id, "fuel_reserve", "litres")
            if fuel_reserve and threshold:
                current = as_float(fuel_reserve.get("current_litres"))
                breached = current <= threshold["threshold_value"]
                if breached:
                    alert = self.evaluate_metric(
                        org_id,
                        "fuel_reserve",
                        "litres",
                        current,
                        threshold["threshold_value"],
                        f"Fuel reserve at {current:.0f}L. Threshold: {float(threshold['threshold_value']):.0f}L.",
                        [],
                        "Schedule refuel of base fuel reserve.",
                        threshold.get("advisory_value"),
                        threshold.get("alert_repeat_minutes"),
                    )
                    active_keys.add((org_id, "fuel_reserve", "litres"))
                    if alert:
                        alerts.append(alert)

            for ammunition in self.ammunition_rows(org_id):
                if ammunition["quantity"] <= ammunition["threshold"]:
                    alert = self.evaluate_metric(
                        org_id,
                        "ammunition",
                        ammunition["type"],
                        ammunition["quantity"],
                        ammunition["threshold"],
                        f"{ammunition['type']} below threshold.",
                        [ammunition["type"]],
                        "Restock ammunition immediately.",
                        advisory_value=max(ammunition["threshold"] * 0.8, 0),
                    )
                    active_keys.add((org_id, "ammunition", ammunition["type"]))
                    if alert:
                        alerts.append(alert)

            for equipment in self.tactical_rows(org_id):
                if equipment["available_quantity"] <= equipment["threshold"]:
                    alert = self.evaluate_metric(
                        org_id,
                        "tactical",
                        equipment["category"],
                        equipment["available_quantity"],
                        equipment["threshold"],
                        f"{equipment['category']} below threshold.",
                        [equipment["category"]],
                        "Restock tactical equipment.",
                        advisory_value=max(equipment["threshold"] * 0.8, 0),
                    )
                    active_keys.add((org_id, "tactical", equipment["category"]))
                    if alert:
                        alerts.append(alert)

            if persist:
                self.clear_resolved_alerts(active_keys)
                for alert in alerts:
                    self.upsert_alert(alert)
            self.touch()
            return alerts

    def update_officer(self, payload: OfficerUpdate) -> dict[str, Any]:
        with self._lock:
            officer = next((o for o in self.officers if o["org_id"] == payload.org_id and o["officer_id"] == payload.officer_id), None)
            if officer is None:
                officer = {
                    "id": str(uuid4()),
                    "org_id": payload.org_id,
                    "officer_id": payload.officer_id,
                    "name": payload.officer_id,
                    "badge_number": payload.officer_id,
                    "status": "off_duty",
                    "armed": False,
                    "weapon": None,
                    "rank": None,
                    "certifications": [],
                    "contact": None,
                    "assigned_zone": None,
                    "current_lat": None,
                    "current_lng": None,
                    "location_updated_at": None,
                    "shift_start": None,
                    "shift_end": None,
                    "created_at": iso(),
                    "updated_at": iso(),
                }
                self.officers.append(officer)
            old = deep_copy(officer)
            if payload.status is not None:
                officer["status"] = payload.status
            if payload.armed is not None:
                officer["armed"] = payload.armed
            if payload.weapon is not None:
                officer["weapon"] = payload.weapon
            if payload.rank is not None:
                officer["rank"] = payload.rank
            if payload.certifications is not None:
                officer["certifications"] = payload.certifications
            if payload.contact is not None:
                officer["contact"] = payload.contact
            if payload.assigned_zone is not None:
                officer["assigned_zone"] = payload.assigned_zone
            if payload.shift_start is not None:
                officer["shift_start"] = payload.shift_start
            if payload.shift_end is not None:
                officer["shift_end"] = payload.shift_end
            if payload.location is not None:
                officer["current_lat"] = payload.location.lat
                officer["current_lng"] = payload.location.lng
                officer["location_updated_at"] = payload.location.last_updated or iso()
            incident_applied = self.apply_incident_assignment(officer, "officer", payload.incident_id)
            if incident_applied and payload.status is None:
                officer["status"] = "on_duty"
            officer["updated_at"] = iso()
            self.record_transaction(payload.org_id, "officers", officer["id"], "status_changed", old, officer, incident_id=payload.incident_id)
            self._upsert_officer_db(officer)
            self._sync_org(payload.org_id)
            self.apply_shift_transitions(payload.org_id)
            self.touch()
            return officer

    def update_vehicle(self, payload: VehicleUpdate) -> tuple[dict[str, Any], dict[str, Any] | None]:
        with self._lock:
            vehicle = next((v for v in self.vehicles if v["org_id"] == payload.org_id and v["vehicle_id"] == payload.vehicle_id), None)
            if vehicle is None:
                vehicle = {
                    "id": str(uuid4()),
                    "org_id": payload.org_id,
                    "vehicle_id": payload.vehicle_id,
                    "plate_number": payload.vehicle_id,
                    "type": "patrol_car",
                    "status": "available",
                    "fuel_percentage": 0,
                    "fuel_litres": 0.0,
                    "fuel_last_updated": iso(),
                    "condition": "good",
                    "capacity": 4,
                    "assigned_driver_id": None,
                    "current_lat": None,
                    "current_lng": None,
                    "location_updated_at": None,
                    "last_service_date": None,
                    "next_service_due": None,
                    "odometer_km": 0,
                    "special_equipment": [],
                    "created_at": iso(),
                    "updated_at": iso(),
                }
                self.vehicles.append(vehicle)
            old = deep_copy(vehicle)
            if payload.status is not None:
                vehicle["status"] = payload.status
            if payload.location is not None:
                vehicle["current_lat"] = payload.location.lat
                vehicle["current_lng"] = payload.location.lng
                vehicle["location_updated_at"] = payload.location.last_updated or iso()
            if payload.fuel_percentage is not None:
                vehicle["fuel_percentage"] = int(payload.fuel_percentage)
                vehicle["fuel_last_updated"] = iso()
            if payload.fuel_litres is not None:
                vehicle["fuel_litres"] = float(payload.fuel_litres)
            if payload.condition is not None:
                vehicle["condition"] = payload.condition
            if payload.assigned_driver_id is not None:
                vehicle["assigned_driver_id"] = payload.assigned_driver_id
            if payload.capacity is not None:
                vehicle["capacity"] = payload.capacity
            if payload.last_service_date is not None:
                vehicle["last_service_date"] = payload.last_service_date
            if payload.next_service_due is not None:
                vehicle["next_service_due"] = payload.next_service_due
            if payload.odometer_km is not None:
                vehicle["odometer_km"] = payload.odometer_km
            route_estimate = None
            if payload.route_distance_km is not None and payload.fuel_percentage is not None:
                route_estimate = estimate_route_fuel(
                    float(payload.fuel_percentage),
                    float(payload.route_distance_km),
                    str(vehicle.get("status") or payload.status or "available"),
                )
                vehicle["route_distance_km"] = float(payload.route_distance_km)
            if payload.special_equipment is not None:
                vehicle["special_equipment"] = payload.special_equipment
            incident_applied = self.apply_incident_assignment(vehicle, "vehicle", payload.incident_id)
            if incident_applied and payload.status is None:
                vehicle["status"] = "deployed"
            vehicle["updated_at"] = iso()
            anomaly = None
            if payload.expected_fuel_percentage is not None and payload.fuel_percentage is not None:
                anomaly = detect_fuel_anomaly(
                    payload.vehicle_id,
                    float(payload.expected_fuel_percentage),
                    float(payload.fuel_percentage),
                )
                if anomaly.get("anomaly"):
                    review = groq_inventory_review(
                        "vehicle_fuel_anomaly",
                        proposed={
                            "analysis": anomaly,
                            "vehicle_id": payload.vehicle_id,
                        },
                        evidence={
                            "vehicle_id": payload.vehicle_id,
                            "expected_fuel_percentage": float(payload.expected_fuel_percentage),
                            "actual_fuel_percentage": float(payload.fuel_percentage),
                            "severity": anomaly.get("severity", "medium"),
                        },
                        schema_hint={
                            "approved": True,
                            "issues": ["string"],
                            "severity": "low|medium|high|critical",
                            "suggested_message": "string",
                            "suggested_action": "string",
                        },
                    )
                    if review:
                        anomaly["llm_review"] = review
                        if isinstance(review.get("suggested_message"), str) and review["suggested_message"].strip():
                            anomaly["analysis"] = review["suggested_message"].strip()
            if route_estimate:
                vehicle["route_estimate"] = route_estimate
                if route_estimate.get("insufficient"):
                    vehicle["status"] = "deployed" if vehicle.get("status") == "deployed" else vehicle["status"]
            notes = None
            if anomaly and anomaly.get("anomaly"):
                notes = anomaly["message"]
            if route_estimate and route_estimate.get("insufficient"):
                notes = (notes + " " if notes else "") + "Route fuel estimate below safe threshold."
            self.record_transaction(payload.org_id, "vehicles", vehicle["id"], "status_changed", old, vehicle, incident_id=payload.incident_id, notes=notes)
            self._upsert_vehicle_db(vehicle)
            self._sync_org(payload.org_id)
            self.touch()
            return vehicle, {"anomaly": anomaly, "route_estimate": route_estimate} if route_estimate is not None else anomaly

    def update_weapon(self, payload: WeaponUpdate) -> dict[str, Any]:
        with self._lock:
            weapon = next((w for w in self.weapons if w["org_id"] == payload.org_id and w["serial_number"] == payload.weapon_id), None)
            if weapon is None:
                weapon = {
                    "id": str(uuid4()),
                    "org_id": payload.org_id,
                    "serial_number": payload.weapon_id,
                    "type": "pistol",
                    "status": "in_armoury",
                    "assigned_to": None,
                    "condition": "good",
                    "last_inspection_date": None,
                    "created_at": iso(),
                    "updated_at": iso(),
                }
                self.weapons.append(weapon)
            old = deep_copy(weapon)
            if payload.status is not None:
                weapon["status"] = payload.status
            if payload.assigned_to is not None:
                weapon["assigned_to"] = payload.assigned_to
            if payload.condition is not None:
                weapon["condition"] = payload.condition
            if payload.last_inspection_date is not None:
                weapon["last_inspection_date"] = payload.last_inspection_date
            weapon["updated_at"] = iso()
            self.record_transaction(payload.org_id, "weapons", weapon["id"], "status_changed", old, weapon)
            self._upsert_weapon_db(weapon)
            self._sync_org(payload.org_id)
            self.touch()
            return weapon

    def update_equipment(self, payload: EquipmentUpdate) -> dict[str, Any]:
        with self._lock:
            equipment = next((e for e in self.tactical_equipment if e["org_id"] == payload.org_id and e["category"] == payload.category), None)
            if equipment is None:
                equipment = {
                    "id": str(uuid4()),
                    "org_id": payload.org_id,
                    "category": payload.category,
                    "total_quantity": 0,
                    "available_quantity": 0,
                    "in_use_quantity": 0,
                    "threshold": 0,
                    "condition_breakdown": {},
                    "created_at": iso(),
                    "updated_at": iso(),
                }
                self.tactical_equipment.append(equipment)
            old = deep_copy(equipment)
            equipment["total_quantity"] = payload.total_quantity
            equipment["available_quantity"] = payload.available_quantity
            equipment["in_use_quantity"] = payload.in_use_quantity
            equipment["threshold"] = payload.threshold
            equipment["condition_breakdown"] = payload.condition_breakdown
            equipment["updated_at"] = iso()
            self.record_transaction(payload.org_id, "tactical_equipment", equipment["id"], "status_changed", old, equipment)
            self._upsert_equipment_db(equipment)
            self._sync_org(payload.org_id)
            self.touch()
            return equipment

    def update_fuel_reserve(self, payload: FuelReserveUpdate) -> dict[str, Any]:
        with self._lock:
            reserve = self.fuel_reserves.get(payload.org_id)
            if reserve is None:
                reserve = {
                    "id": str(uuid4()),
                    "org_id": payload.org_id,
                    "current_litres": 0.0,
                    "capacity_litres": 0.0,
                    "threshold_litres": DEFAULT_FUEL_RESERVE_LITRES,
                    "last_restocked": None,
                    "resupply_contact": None,
                    "updated_at": iso(),
                }
                self.fuel_reserves[payload.org_id] = reserve
            old = deep_copy(reserve)
            reserve["current_litres"] = float(payload.current_litres)
            reserve["capacity_litres"] = float(payload.capacity_litres)
            reserve["threshold_litres"] = float(payload.threshold_litres) if payload.threshold_litres is not None else reserve["threshold_litres"]
            reserve["last_restocked"] = payload.last_restocked or reserve["last_restocked"]
            reserve["resupply_contact"] = payload.resupply_contact or reserve["resupply_contact"]
            reserve["updated_at"] = iso()
            self.record_transaction(payload.org_id, "fuel_reserves", reserve["id"], "refuelled", old, reserve)
            self._upsert_fuel_reserve_db(reserve)
            self._sync_org(payload.org_id)
            self.touch()
            return reserve

    def update_ammunition(self, payload: AmmunitionUpdate) -> dict[str, Any]:
        with self._lock:
            ammo = next((a for a in self.ammunition if a["org_id"] == payload.org_id and a["type"] == payload.type), None)
            if ammo is None:
                ammo = {
                    "id": str(uuid4()),
                    "org_id": payload.org_id,
                    "type": payload.type,
                    "quantity": 0,
                    "threshold": payload.threshold,
                    "last_restocked": None,
                    "created_at": iso(),
                    "updated_at": iso(),
                }
                self.ammunition.append(ammo)
            old = deep_copy(ammo)
            ammo["quantity"] = payload.quantity
            ammo["threshold"] = payload.threshold
            ammo["last_restocked"] = payload.last_restocked or ammo["last_restocked"]
            ammo["updated_at"] = iso()
            self.record_transaction(payload.org_id, "ammunition", ammo["id"], "status_changed", old, ammo)
            self._upsert_ammunition_db(ammo)
            self._sync_org(payload.org_id)
            self.touch()
            return ammo


store = InventoryStore()
app = FastAPI(title="Lemtik Inventory Service", version="1.0.0")


def require_internal_key(x_internal_key: str | None = Header(default=None)) -> None:
    if x_internal_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing internal key")


def unwrap_payload(data: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {"request_id": request_id, "status": "success", "data": data}


def timed_payload(data: dict[str, Any], request_id: str, started_at: datetime) -> dict[str, Any]:
    payload = unwrap_payload(data, request_id)
    elapsed_ms = round((now_utc() - started_at).total_seconds() * 1000, 2)
    payload["meta"] = {
        "elapsed_ms": elapsed_ms,
        "soft_target_ms": 500,
        "within_soft_target": elapsed_ms <= 500,
    }
    return payload


def build_query_data(
    request_type: str,
    org_id: str,
    filters: dict[str, Any] | None = None,
    operation_requirements: dict[str, Any] | None = None,
    include_llm: bool = True,
) -> dict[str, Any]:
    filters = filters or {}
    operation_requirements = operation_requirements or {}
    if request_type == "inventory_summary":
        data = store.summary(org_id)
        threshold_state = data.pop("_threshold_state", None)
        if include_llm:
            review = groq_inventory_review(
                "inventory_summary",
                proposed={
                    "officers": data.get("officers"),
                    "vehicles": data.get("vehicles"),
                    "weapons": data.get("weapons"),
                    "ammunition": data.get("ammunition"),
                    "tactical": data.get("tactical"),
                    "fuel_reserve": data.get("fuel_reserve"),
                    "active_alerts": data.get("active_alerts"),
                    "last_updated": data.get("last_updated"),
                },
                evidence={
                    "org_id": org_id,
                    "summary": data,
                    "threshold_state": threshold_state,
                },
                schema_hint={
                    "approved": True,
                    "issues": ["string"],
                    "risk_level": "low|medium|high|critical",
                    "missing_fields": ["string"],
                    "recommended_actions": ["string"],
                },
            )
            if review:
                data["llm_review"] = review
        return data
    if request_type == "available_officers":
        return query_officers(org_id, filters)
    if request_type == "available_vehicles":
        return query_vehicles(org_id, filters)
    if request_type == "readiness_check":
        result = readiness_check(org_id, operation_requirements)
        if not include_llm and "llm_review" in result:
            result.pop("llm_review", None)
        return result
    raise HTTPException(status_code=400, detail=f"Unsupported request_type: {request_type}")


def query_officers(org_id: str, filters: dict[str, Any]) -> dict[str, Any]:
    officers = store.officer_rows(org_id)
    if filters.get("available_only"):
        officers = [o for o in officers if o["status"] == "available"]
    if filters.get("armed_only"):
        officers = [o for o in officers if o.get("armed")]
    certifications = filters.get("certified") or []
    if certifications:
        officers = [o for o in officers if all(cert in o.get("certifications", []) for cert in certifications)]
    items = [
        {
            "officer_id": o["officer_id"],
            "name": o["name"],
            "badge": o["badge_number"],
            "status": o["status"],
            "armed": o["armed"],
            "weapon": o["weapon"],
            "location": {
                "lat": o.get("current_lat"),
                "lng": o.get("current_lng"),
                "last_updated": o.get("location_updated_at"),
            },
            "certifications": o.get("certifications", []),
            "contact": o.get("contact"),
        }
        for o in officers
    ]
    return {"officers": items, "total_returned": len(items)}


def query_vehicles(org_id: str, filters: dict[str, Any]) -> dict[str, Any]:
    vehicles = store.vehicle_rows(org_id)
    if filters.get("available_only"):
        vehicles = [v for v in vehicles if v["status"] == "available"]
    if filters.get("fuelled_only"):
        vehicles = [v for v in vehicles if v.get("fuel_percentage", 0) >= as_int(filters.get("min_fuel_percentage"), DEFAULT_VEHICLE_FUEL_THRESHOLD)]
    if filters.get("type"):
        vehicles = [v for v in vehicles if v.get("type") == filters["type"]]
    min_fuel = as_int(filters.get("min_fuel_percentage"), DEFAULT_VEHICLE_FUEL_THRESHOLD)
    items = [
        {
            "vehicle_id": v["vehicle_id"],
            "plate": v["plate_number"],
            "type": v["type"],
            "status": v["status"],
            "fuel_percentage": v.get("fuel_percentage", 0),
            "fuel_litres": v.get("fuel_litres", 0.0),
            "location": {
                "lat": v.get("current_lat"),
                "lng": v.get("current_lng"),
                "last_updated": v.get("location_updated_at"),
            },
            "capacity": v.get("capacity", 0),
            "condition": v.get("condition"),
            "assigned_driver": v.get("assigned_driver_id"),
        }
        for v in vehicles
        if v.get("fuel_percentage", 0) >= min_fuel or not filters.get("fuelled_only")
    ]
    return {"vehicles": items, "total_returned": len(items)}


def readiness_check(org_id: str, requirements: dict[str, Any]) -> dict[str, Any]:
    summary = store.summary(org_id)
    officers_needed = as_int(requirements.get("officers_needed"), 0)
    vehicles_needed = as_int(requirements.get("vehicles_needed"), 0)
    armed_required = bool(requirements.get("armed_required", False))
    equipment = requirements.get("equipment") or []

    available_officers = summary["officers"]["available"]
    armed_officers = summary["officers"]["armed_available"]
    available_vehicles = len([v for v in store.vehicle_rows(org_id) if v["status"] == "available" and v.get("fuel_percentage", 0) >= DEFAULT_VEHICLE_FUEL_THRESHOLD])

    gaps: list[dict[str, Any]] = []
    if available_officers < officers_needed:
        gaps.append(
            {
                "resource": "officers",
                "needed": officers_needed,
                "available": available_officers,
                "gap": officers_needed - available_officers,
                "severity": "critical",
                "message": "Not enough available officers",
            }
        )
    if armed_required and armed_officers < officers_needed:
        gaps.append(
            {
                "resource": "armed_officers",
                "needed": officers_needed,
                "available": armed_officers,
                "gap": officers_needed - armed_officers,
                "severity": "critical",
                "message": "Not enough armed officers",
            }
        )
    if available_vehicles < vehicles_needed:
        gaps.append(
            {
                "resource": "vehicles",
                "needed": vehicles_needed,
                "available": available_vehicles,
                "gap": vehicles_needed - available_vehicles,
                "severity": "critical",
                "message": "No fuelled vehicles available" if available_vehicles == 0 else "Not enough fuelled vehicles available",
            }
        )
    for item in equipment:
        available = next((t["available_quantity"] for t in store.tactical_rows(org_id) if t["category"] == item), 0)
        if available <= 0:
            gaps.append(
                {
                    "resource": item,
                    "needed": 1,
                    "available": available,
                    "gap": 1,
                    "severity": "warning",
                    "message": f"{item} unavailable",
                }
            )

    ready = not gaps
    available_to_deploy = {
        "officers": available_officers,
        "armed_officers": armed_officers,
        "vehicles": available_vehicles,
        "body_armour": next((t["available_quantity"] for t in store.tactical_rows(org_id) if t["category"] == "body_armour"), 0),
        "radios": next((t["available_quantity"] for t in store.tactical_rows(org_id) if t["category"] == "radios"), 0),
    }
    recommendation = "Operation can proceed." if ready else " ".join(
        [
            "Operation can proceed on foot only." if available_vehicles == 0 else "Operation has resource gaps.",
            "Alert management to refuel vehicles immediately." if available_vehicles == 0 else "Resolve the listed gaps before deployment.",
        ]
    )
    review = groq_inventory_review(
        "readiness_check",
        proposed={
            "ready": ready,
            "gaps": gaps,
            "available_to_deploy": available_to_deploy,
            "recommendation": recommendation,
        },
        evidence={
            "org_id": org_id,
            "ready": ready,
            "gaps": gaps,
            "available_to_deploy": available_to_deploy,
            "base_recommendation": recommendation,
        },
        schema_hint={
            "approved": True,
            "issues": ["string"],
            "severity": "low|medium|high|critical",
            "suggested_message": "string",
            "suggested_action": "string",
        },
    )
    response = {"ready": ready, "gaps": gaps, "available_to_deploy": available_to_deploy, "recommendation": recommendation}
    if review:
        response["llm_review"] = review
        if isinstance(review.get("suggested_action"), str) and review["suggested_action"].strip():
            response["recommendation"] = review["suggested_action"].strip()
    return response


def push_alert_to_relationship_api(alert: dict[str, Any]) -> None:
    if not RELATIONSHIP_API_URL or not RELATIONSHIP_API_KEY:
        return
    req = urllib.request.Request(
        f"{RELATIONSHIP_API_URL}/internal/inventory-alert",
        data=json.dumps(alert, default=str).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Internal-Key": RELATIONSHIP_API_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            response.read()
    except Exception:
        return


def run_threshold_cycle() -> None:
    for org_id in list(store.orgs):
        alerts = store.evaluate_thresholds(org_id, persist=True)
        for alert in alerts:
            push_alert_to_relationship_api(alert)


def start_scheduler() -> None:
    global scheduler_started
    with scheduler_lock:
        if scheduler_started:
            return
        scheduler.add_job(
            run_threshold_cycle,
            trigger="interval",
            minutes=5,
            id="threshold_check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        scheduler_started = True


def stop_scheduler() -> None:
    global scheduler_started
    with scheduler_lock:
        if not scheduler_started:
            return
        scheduler.shutdown(wait=False)
        scheduler_started = False


@app.on_event("startup")
def on_startup() -> None:
    start_scheduler()
    run_threshold_cycle()


@app.on_event("shutdown")
def on_shutdown() -> None:
    stop_scheduler()


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "inventory",
        "environment": ENVIRONMENT,
        "started_at": iso(APP_STARTED_AT),
        "last_updated": iso(store.last_updated_at),
        "active_alerts": len(store.active_alerts()),
    }


@app.post("/query")
def query(payload: QueryRequest, _: None = Depends(require_internal_key)) -> JSONResponse:
    started = now_utc()
    data = build_query_data(payload.request_type, payload.org_id, payload.filters, payload.operation_requirements, include_llm=True)
    return JSONResponse(timed_payload(data, payload.request_id, started))


@app.post("/update/officer")
def update_officer(payload: OfficerUpdate, _: None = Depends(require_internal_key)) -> JSONResponse:
    officer = store.update_officer(payload)
    alerts = store.evaluate_thresholds(payload.org_id)
    for alert in alerts:
        push_alert_to_relationship_api(alert)
    return JSONResponse({"status": "success", "data": {"officer": officer, "alerts_generated": len(alerts)}})


@app.post("/update/vehicle")
def update_vehicle(payload: VehicleUpdate, _: None = Depends(require_internal_key)) -> JSONResponse:
    vehicle, anomaly = store.update_vehicle(payload)
    alerts = store.evaluate_thresholds(payload.org_id)
    for alert in alerts:
        push_alert_to_relationship_api(alert)
    data = {"vehicle": vehicle, "alerts_generated": len(alerts)}
    if anomaly:
        data["anomaly"] = anomaly
    return JSONResponse({"status": "success", "data": data})


@app.post("/update/weapon")
def update_weapon(payload: WeaponUpdate, _: None = Depends(require_internal_key)) -> JSONResponse:
    weapon = store.update_weapon(payload)
    alerts = store.evaluate_thresholds(payload.org_id)
    for alert in alerts:
        push_alert_to_relationship_api(alert)
    return JSONResponse({"status": "success", "data": {"weapon": weapon, "alerts_generated": len(alerts)}})


@app.post("/update/equipment")
def update_equipment(payload: EquipmentUpdate, _: None = Depends(require_internal_key)) -> JSONResponse:
    equipment = store.update_equipment(payload)
    alerts = store.evaluate_thresholds(payload.org_id)
    for alert in alerts:
        push_alert_to_relationship_api(alert)
    return JSONResponse({"status": "success", "data": {"equipment": equipment, "alerts_generated": len(alerts)}})


@app.post("/update/fuel-reserve")
def update_fuel_reserve(payload: FuelReserveUpdate, _: None = Depends(require_internal_key)) -> JSONResponse:
    reserve = store.update_fuel_reserve(payload)
    alerts = store.evaluate_thresholds(payload.org_id)
    for alert in alerts:
        push_alert_to_relationship_api(alert)
    return JSONResponse({"status": "success", "data": {"fuel_reserve": reserve, "alerts_generated": len(alerts)}})


@app.post("/update/cadence")
def update_cadence(payload: CadenceUpdate, _: None = Depends(require_internal_key)) -> JSONResponse:
    cadence = store.set_cadence(payload)
    return JSONResponse({"status": "success", "data": {"cadence": cadence}})


@app.post("/update/ammunition")
def update_ammunition(payload: AmmunitionUpdate, _: None = Depends(require_internal_key)) -> JSONResponse:
    ammo = store.update_ammunition(payload)
    alerts = store.evaluate_thresholds(payload.org_id)
    for alert in alerts:
        push_alert_to_relationship_api(alert)
    return JSONResponse({"status": "success", "data": {"ammunition": ammo, "alerts_generated": len(alerts)}})


@app.post("/perf/check")
def perf_check(payload: PerfCheckRequest, _: None = Depends(require_internal_key)) -> dict[str, Any]:
    request_types = payload.request_types or ["inventory_summary"]
    iterations = max(1, min(int(payload.iterations), 10))
    metrics: dict[str, Any] = {}
    overall_elapsed = 0.0
    for request_type in request_types:
        samples: list[float] = []
        for _ in range(iterations):
            started = time.perf_counter()
            build_query_data(
                request_type,
                payload.org_id,
                include_llm=payload.include_llm,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            samples.append(elapsed_ms)
            overall_elapsed += elapsed_ms
        metrics[request_type] = {
            "iterations": iterations,
            "min_ms": round(min(samples), 2),
            "avg_ms": round(sum(samples) / len(samples), 2),
            "max_ms": round(max(samples), 2),
            "within_soft_target": max(samples) <= 500,
            "soft_target_ms": 500,
        }
    return {
        "status": "success",
        "data": {
            "org_id": payload.org_id,
            "include_llm": payload.include_llm,
            "metrics": metrics,
            "overall_avg_ms": round(overall_elapsed / (iterations * len(request_types)), 2),
            "overall_within_soft_target": all(metric["within_soft_target"] for metric in metrics.values()),
        },
    }


@app.post("/update/threshold")
def update_threshold(payload: ThresholdUpdate, _: None = Depends(require_internal_key)) -> JSONResponse:
    threshold = store.set_threshold(payload)
    alerts = store.evaluate_thresholds(payload.org_id)
    for alert in alerts:
        push_alert_to_relationship_api(alert)
    return JSONResponse({"status": "success", "data": {"threshold": threshold, "alerts_generated": len(alerts)}})


@app.get("/alerts/active")
def active_alerts(org_id: str = DEFAULT_ORG_ID, _: None = Depends(require_internal_key)) -> dict[str, Any]:
    return {"status": "success", "data": {"alerts": store.active_alerts(org_id), "total": len(store.active_alerts(org_id))}}


@app.post("/alerts/resolve")
def resolve_alert(payload: ResolveAlertRequest, _: None = Depends(require_internal_key)) -> dict[str, Any]:
    alert = store.resolve_alert(payload.alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"status": "success", "data": {"alert": alert}}


@app.exception_handler(Exception)
def catch_all(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
