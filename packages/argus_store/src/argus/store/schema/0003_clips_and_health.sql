-- 0003_clips_and_health.sql — the evidence a payroll line points at, and the
-- camera-health facts that say whether we were looking.
--
-- "No clip, no deduction" (ADR-0008) is read from this table, so a clip row is
-- payroll-affecting even though nothing here computes a number.

-- Shared append-only guard. Attached per table below, so the error names the
-- table the caller actually touched.
create or replace function argus_append_only() returns trigger as $$
begin
    raise exception '% is append-only evidence (ARCHITECTURE.md §7.1); update/delete is rejected',
        tg_table_name;
end;
$$ language plpgsql;

create table clip (
    clip_id         uuid primary key,
    camera_id       text not null references camera (camera_id),
    rel_path        text not null unique,
    start_utc       timestamptz not null,
    end_utc         timestamptz not null,
    -- The actual first frame, which is at or BEFORE start_utc: clips snap back
    -- to the previous keyframe (ADR-0031). A deep link computed from the
    -- requested start instead of this one lands in the wrong place.
    keyframe_utc    timestamptz not null,
    bytes           bigint,
    -- Denormalised from camera.is_virtual at write time, on purpose: a clip
    -- derived from rig footage must be refusable without a join, because the
    -- camera row can be edited later and demo footage must never reach a wage.
    is_virtual      boolean not null,
    created_at      timestamptz not null default now(),
    deleted_at      timestamptz,
    -- First line of defence for the UI's path assertion. The route resolves a
    -- clip_id, never a path, but a stored path is still checked twice.
    constraint clip_rel_path_is_relative check (rel_path not like '/%' and rel_path !~ '\.\.'),
    constraint clip_window_ordered check (end_utc >= start_utc and keyframe_utc <= start_utc)
);

create index clip_camera_start_idx on clip (camera_id, start_utc);

-- Retention consults references, not just age (RISKS.md §9): a clip that a
-- doorway event points at cannot be deleted, and nothing but deleted_at may
-- ever change.
create or replace function clip_guard() returns trigger as $$
begin
    if tg_op = 'DELETE' then
        if exists (select 1 from doorway_event where clip_ref = old.rel_path) then
            raise exception 'clip % is referenced by a doorway event (ADR-0008); it is evidence',
                old.rel_path;
        end if;
        return old;
    end if;
    if row(new.clip_id, new.camera_id, new.rel_path, new.start_utc, new.end_utc,
           new.keyframe_utc, new.bytes, new.is_virtual, new.created_at)
       is distinct from
       row(old.clip_id, old.camera_id, old.rel_path, old.start_utc, old.end_utc,
           old.keyframe_utc, old.bytes, old.is_virtual, old.created_at) then
        raise exception 'only clip.deleted_at may be updated (RISKS.md §9)';
    end if;
    return new;
end;
$$ language plpgsql;

create trigger clip_guard_trigger
    before update or delete on clip
    for each row execute function clip_guard();

-- The accepted view of a camera. ADR-0029: drift is detected against a
-- reference frame that is never updated automatically, because an
-- auto-updating reference tracks the drift it exists to detect.
create table camera_reference_frame (
    reference_id uuid primary key,
    camera_id    text not null references camera (camera_id),
    preset_id    text,
    captured_at  timestamptz not null,
    width        int not null,
    height       int not null,
    rel_path     text not null,
    sha256       text not null,
    accepted_by  text not null,
    note         text,
    constraint camera_reference_accepted_by_named check (length(btrim(accepted_by)) > 0)
);

create index camera_reference_frame_camera_idx
    on camera_reference_frame (camera_id, captured_at desc);

create trigger camera_reference_frame_append_only_trigger
    before update or delete on camera_reference_frame
    for each row execute function argus_append_only();

-- Health samples carry no person_id and never will: this is about cameras.
create table camera_health_sample (
    sample_id      uuid primary key,
    camera_id      text not null references camera (camera_id),
    at             timestamptz not null default now(),
    fps_observed   real,
    queue_depth    int,
    frames_dropped bigint,
    reconnects     int,
    decode_errors  int,
    backend        text,
    model_ref      text
);

create index camera_health_sample_camera_at_idx on camera_health_sample (camera_id, at desc);

-- ADR-0031 opens each camera twice; the substream URI belongs next to the main
-- one rather than only in config, so an operator can see what was actually used.
alter table camera add column analysis_uri text;

-- Credentials never reach the database. The config layer stores the
-- un-expanded ${VAR} template; this refuses the expanded form outright, so a
-- future writer that does not know the rule still cannot break it.
alter table camera add constraint camera_source_uri_has_no_credentials
    check (source_uri !~ '://[^/@]*:[^/@]*@');
alter table camera add constraint camera_analysis_uri_has_no_credentials
    check (analysis_uri is null or analysis_uri !~ '://[^/@]*:[^/@]*@');
