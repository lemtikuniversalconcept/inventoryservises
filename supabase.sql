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
