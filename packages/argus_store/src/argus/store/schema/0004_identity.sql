-- 0004_identity.sql — who a deduction can land on, and the consent that allows
-- us to recognise them at all.
--
-- PAYROLL-AFFECTING: identity decides whose wage an interval touches. Two rules
-- are schema facts here rather than CLI checks, because a CLI can be
-- refactored:
--   * a face template without a consent row is unrepresentable;
--   * a template records the model that produced it, because embeddings are not
--     comparable across models or even versions (ARCHITECTURE.md §7.2).

create table person (
    person_id    text primary key,
    employee_ref text not null unique,
    display_name text,
    line_id      text,
    seat_id      text,
    -- Roster churn is high, so validity windows rather than deletes: a
    -- deduction computed last month must still resolve against who that person
    -- was last month.
    active_from  date not null,
    active_to    date,
    created_at   timestamptz not null default now(),
    constraint person_active_window check (active_to is null or active_to >= active_from)
);

-- A typo'd person_id on a doorway event attributes evidence to nobody and is
-- invisible in a report. Unknown stays representable -- the column is nullable
-- and null means "we did not recognise them", which is the fail-open outcome.
alter table doorway_event
    add constraint doorway_event_person_fk
    foreign key (person_id) references person (person_id);

create table consent_record (
    consent_id  uuid primary key,
    person_id   text not null references person (person_id),
    purpose     text not null,
    granted_on  date not null,
    expires_on  date not null,
    revoked_at  timestamptz,
    recorded_by text not null,
    evidence_note text,
    created_at  timestamptz not null default now(),
    constraint consent_window check (expires_on >= granted_on),
    constraint consent_recorded_by_named check (length(btrim(recorded_by)) > 0)
);

create index consent_record_person_idx on consent_record (person_id);

create table face_template (
    template_id   uuid primary key,
    person_id     text not null references person (person_id),
    -- NOT NULL by design (ADR-0027, RISKS.md §2): no consent row, no template.
    consent_id    uuid not null references consent_record (consent_id),
    -- name@sha256[:12] of the artefact that produced the embedding. A model
    -- upgrade invalidates every template, and storing this makes that an error
    -- at startup instead of silently catastrophic.
    model_ref     text not null,
    embedding     bytea not null,
    dim           int not null,
    quality_score real,
    source        text not null check (source in ('images', 'rtsp')),
    image_sha256  text,
    enrolled_at   timestamptz not null default now(),
    enrolled_by   text not null,
    retired_at    timestamptz,
    constraint face_template_enrolled_by_named check (length(btrim(enrolled_by)) > 0),
    constraint face_template_dim_positive check (dim > 0)
);

create index face_template_person_idx on face_template (person_id) where retired_at is null;
create index face_template_model_ref_idx on face_template (model_ref);

-- Enrolment images are retained unencrypted for the demo roster (ADR-0027) and
-- deleted together with the templates derived from them. The filename is the
-- image hash, so re-adding the same photo is idempotent.
create table enrol_image (
    person_id   text not null references person (person_id),
    image_sha256 text not null,
    rel_path    text not null,
    captured_at timestamptz,
    source      text not null check (source in ('images', 'rtsp')),
    camera_id   text references camera (camera_id),
    added_by    text not null,
    added_at    timestamptz not null default now(),
    primary key (person_id, image_sha256),
    constraint enrol_image_added_by_named check (length(btrim(added_by)) > 0),
    constraint enrol_image_rel_path_is_relative
        check (rel_path not like '/%' and rel_path !~ '\.\.')
);

-- Every enrolment write names a human. An inferred actor ($USER, getlogin) is a
-- fake audit trail, so the CLI requires --by and this table has no default.
create table enrolment_action (
    action_id uuid primary key,
    at        timestamptz not null default now(),
    actor     text not null,
    action    text not null check (action in
                ('person_add', 'consent_record', 'consent_revoke', 'enrol',
                 'badge_assign', 'badge_unassign', 'purge')),
    person_id text references person (person_id),
    detail    jsonb not null default '{}'::jsonb,
    constraint enrolment_action_actor_named check (length(btrim(actor)) > 0)
);

create index enrolment_action_at_idx on enrolment_action (at desc);

create trigger enrolment_action_append_only_trigger
    before update or delete on enrolment_action
    for each row execute function argus_append_only();

-- Badge history, as-of. Overlapping assignments are how a gate verification
-- gets attributed to the wrong person, so overlap is refused rather than
-- documented.
create table badge_assignment (
    assignment_id uuid primary key,
    badge_id      text not null,
    person_id     text not null references person (person_id),
    assigned_from timestamptz not null,
    assigned_to   timestamptz,
    assigned_by   text not null,
    constraint badge_assignment_window check (assigned_to is null or assigned_to > assigned_from),
    constraint badge_assignment_assigned_by_named check (length(btrim(assigned_by)) > 0)
);

create unique index badge_assignment_one_active_per_badge
    on badge_assignment (badge_id) where assigned_to is null;
create unique index badge_assignment_one_active_per_person
    on badge_assignment (person_id) where assigned_to is null;
create index badge_assignment_badge_from_idx on badge_assignment (badge_id, assigned_from desc);

-- The partial indexes above stop two *open* assignments; this stops two
-- overlapping closed ones. A range EXCLUDE constraint would say it in one line
-- but needs btree_gist, which is one more extension to have present on the
-- staging box for no behavioural gain.
create or replace function badge_assignment_no_overlap() returns trigger as $$
begin
    if exists (
        select 1 from badge_assignment b
        where b.badge_id = new.badge_id
          and b.assignment_id <> new.assignment_id
          and tstzrange(b.assigned_from, b.assigned_to, '[)')
              && tstzrange(new.assigned_from, new.assigned_to, '[)')
    ) then
        raise exception 'badge % already has a holder over that window', new.badge_id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger badge_assignment_no_overlap_trigger
    before insert or update on badge_assignment
    for each row execute function badge_assignment_no_overlap();
