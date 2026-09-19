-- 0001_init.sql — initial schema (ARCHITECTURE.md §7, PROVISIONAL field names).
--
-- Doorway events are evidence: append-only, enforced by trigger, not by habit.
-- cause values extend ARCHITECTURE.md §7's illustrative list with 'stall' and
-- 'refused' (AGENTS.md §7: stall is the WiFi failure mode; DVR refusal is
-- called out in ARCHITECTURE.md §6).

create table if not exists camera (
    camera_id      text primary key,
    role           text not null check (role in ('gate', 'canteen_door', 'floor')),
    source_uri     text not null,
    space_id       text,
    door_id        text,
    direction_hint text,
    is_virtual     boolean not null default false,
    created_at     timestamptz not null default now()
);

create table if not exists ingest_run (
    ingest_run_id uuid primary key,
    code_version  text not null,
    config_hash   text not null,
    started_at    timestamptz not null default now()
);

create table if not exists doorway_event (
    event_id           uuid primary key,
    camera_id          text not null references camera (camera_id),
    door_id            text,
    ts_utc             timestamptz not null,
    ts_camera_reported timestamptz,
    direction          text not null check (direction in ('enter', 'exit', 'ambiguous')),
    person_id          text,
    match_confidence   real,
    detection_quality  real,
    clip_ref           text,
    ingest_run_id      uuid references ingest_run (ingest_run_id),
    superseded_by      uuid references doorway_event (event_id)
);

create index if not exists doorway_event_camera_ts_idx
    on doorway_event (camera_id, ts_utc);

create or replace function doorway_event_append_only() returns trigger as $$
begin
    raise exception 'doorway_event is append-only evidence (ARCHITECTURE.md §7.1); update/delete is rejected';
end;
$$ language plpgsql;

drop trigger if exists doorway_event_append_only_trigger on doorway_event;
create trigger doorway_event_append_only_trigger
    before update or delete on doorway_event
    for each row execute function doorway_event_append_only();

create table if not exists stream_gap (
    gap_id     uuid primary key,
    camera_id  text not null references camera (camera_id),
    from_utc   timestamptz not null,
    to_utc     timestamptz,
    cause      text not null check (cause in
                 ('offline', 'decode_error', 'crash', 'clock_anomaly', 'stall', 'refused')),
    detected_at timestamptz not null default now()
);

create index if not exists stream_gap_camera_from_idx
    on stream_gap (camera_id, from_utc);
