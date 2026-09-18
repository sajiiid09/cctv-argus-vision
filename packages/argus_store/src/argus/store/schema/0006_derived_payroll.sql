-- 0006_derived_payroll.sql — the derived rows a wage would be read from.
--
-- PAYROLL-AFFECTING, and the highest-care file in the schema. Every constraint
-- here restates a decision that is already enforced in argus.payroll. The
-- duplication is deliberate: a rule enforced in one place is enforced in one
-- place, and these numbers outlive the process that computed them.
--
-- Nothing in here is ever UPDATEd. Recomputation writes a new pairing_run
-- (ARCHITECTURE.md §7.3), so a disputed line from months ago is reproducible
-- rather than arguable.

create table pairing_run (
    pairing_run_id     uuid primary key,
    computed_at        timestamptz not null,
    logic_version      text not null,
    policy_fingerprint text not null,
    policy_json        jsonb not null,
    -- The human sentence, written at run time. The console may not import
    -- argus.payroll (ADR-0015), so it renders this string rather than
    -- recomputing a description -- which also means the page describes the run
    -- that produced the numbers on it, not whatever config says today.
    policy_description text not null,
    window_from_utc    timestamptz not null,
    window_to_utc      timestamptz not null,
    lead_in_s          int not null,
    space_ids          text[] not null,
    event_count        int not null,
    gap_count          int not null,
    code_version       text,
    config_hash        text,
    actor              text,
    reason             text,
    -- ADR-0005/0006: there is no export. A run that claims otherwise cannot be
    -- stored (AGENTS.md §2.1 is a human sign-off, not a column value).
    shadow_mode        boolean not null default true,
    constraint pairing_run_is_shadow check (shadow_mode),
    constraint pairing_run_window check (window_to_utc >= window_from_utc),
    constraint pairing_run_lead_in_non_negative check (lead_in_s >= 0)
);

create index pairing_run_computed_at_idx on pairing_run (computed_at desc);

create trigger pairing_run_append_only_trigger
    before update or delete on pairing_run
    for each row execute function argus_append_only();

create table dwell_interval (
    interval_id     uuid primary key,
    pairing_run_id  uuid not null references pairing_run (pairing_run_id),
    person_id       text not null references person (person_id),
    space_id        text not null,
    local_day       date not null,
    enter_event_id  uuid references doorway_event (event_id),
    exit_event_id   uuid references doorway_event (event_id),
    start_utc       timestamptz,
    end_utc         timestamptz,
    duration_s      int,
    -- Values match argus.payroll.types.PairingState. A structural test compares
    -- the two lists in both directions.
    state           text not null check (state in
                      ('resolved', 'unpaired_enter', 'unpaired_exit', 'ambiguous',
                       'implausible', 'gap_affected')),
    flags           text[] not null default '{}',
    -- AGENTS.md §7 property 6, as a database fact.
    constraint dwell_interval_ordered check (end_utc is null or start_utc is null
                                             or end_utc >= start_utc),
    constraint dwell_interval_duration_non_negative check (duration_s is null or duration_s >= 0),
    -- A resolved interval is, by definition, a matched pair with both ends.
    constraint dwell_interval_resolved_is_complete
        check (state <> 'resolved' or (enter_event_id is not null and exit_event_id is not null
                                       and start_utc is not null and end_utc is not null
                                       and duration_s is not null))
);

create index dwell_interval_run_idx on dwell_interval (pairing_run_id);
create index dwell_interval_person_day_idx on dwell_interval (person_id, local_day);

create trigger dwell_interval_append_only_trigger
    before update or delete on dwell_interval
    for each row execute function argus_append_only();

create table dwell_day (
    day_id         uuid primary key,
    pairing_run_id uuid not null references pairing_run (pairing_run_id),
    person_id      text not null references person (person_id),
    space_id       text not null,
    local_day      date not null,
    total_dwell_s  int not null,
    allowance_s    int not null,
    overage_s      int not null,
    day_state      text not null check (day_state in ('clean', 'flagged')),
    flags          text[] not null default '{}',
    interval_ids   uuid[] not null default '{}',
    -- AGENTS.md §7 property 2.
    constraint dwell_day_overage_bounded
        check (overage_s >= 0 and overage_s <= total_dwell_s),
    -- ADR-0023, which asks for exactly this: any non-resolved interval in a
    -- local day zeroes that day. The strictest reading of fail-open, on purpose.
    constraint dwell_day_flagged_charges_nothing
        check (day_state = 'clean' or overage_s = 0),
    constraint dwell_day_totals_non_negative check (total_dwell_s >= 0 and allowance_s >= 0),
    unique (pairing_run_id, person_id, space_id, local_day)
);

create index dwell_day_person_day_idx on dwell_day (person_id, local_day);

create trigger dwell_day_append_only_trigger
    before update or delete on dwell_day
    for each row execute function argus_append_only();

-- Written for EVERY (person, local day) in the run, including flagged days with
-- zero overage: a flagged day that produces no row is indistinguishable from a
-- person who did not eat, and fail-open is only visible if it is on the page.
create table payroll_line (
    line_id        uuid primary key,
    pairing_run_id uuid not null references pairing_run (pairing_run_id),
    person_id      text not null references person (person_id),
    space_id       text not null,
    local_day      date not null,
    overage_s      int not null,
    day_state      text not null check (day_state in ('clean', 'flagged')),
    shadow_mode    boolean not null default true,
    exported_at    timestamptz,
    created_at     timestamptz not null default now(),
    constraint payroll_line_is_shadow check (shadow_mode),
    constraint payroll_line_overage_non_negative check (overage_s >= 0),
    unique (pairing_run_id, person_id, space_id, local_day)
);

create index payroll_line_person_day_idx on payroll_line (person_id, local_day);

-- RISKS.md §11. A trigger rather than a CHECK, so that enabling export later is
-- a named migration somebody has to write, not a constraint quietly dropped.
create or replace function payroll_line_no_export() returns trigger as $$
begin
    if new.exported_at is not null then
        raise exception 'payroll export is off: turning it on is a human sign-off (AGENTS.md §2.1) and an ADR, not a column write';
    end if;
    if tg_op = 'UPDATE' then
        raise exception 'payroll_line is append-only; recomputation writes a new pairing_run';
    end if;
    return new;
end;
$$ language plpgsql;

create trigger payroll_line_no_export_trigger
    before insert or update on payroll_line
    for each row execute function payroll_line_no_export();

create or replace function payroll_line_no_delete() returns trigger as $$
begin
    raise exception 'payroll_line is append-only (ARCHITECTURE.md §7.1); delete is rejected';
end;
$$ language plpgsql;

create trigger payroll_line_no_delete_trigger
    before delete on payroll_line
    for each row execute function payroll_line_no_delete();

-- Evidence that belongs to nobody chargeable: duplicates, unknown faces,
-- low-confidence matches. Retained, counted, never charged. The counts are a
-- RISKS.md §8 metric, so they have to be stored rather than logged.
create table pairing_unattributable (
    pairing_run_id uuid not null references pairing_run (pairing_run_id),
    event_id       uuid not null references doorway_event (event_id),
    flag           text not null,
    primary key (pairing_run_id, event_id, flag)
);

create trigger pairing_unattributable_append_only_trigger
    before update or delete on pairing_unattributable
    for each row execute function argus_append_only();

-- Enters with no exit yet, at the moment the run was computed. Not an interval
-- and not a charge; the review queue's "still inside" list.
create table pairing_open_enter (
    pairing_run_id uuid not null references pairing_run (pairing_run_id),
    event_id       uuid not null references doorway_event (event_id),
    primary key (pairing_run_id, event_id)
);

create trigger pairing_open_enter_append_only_trigger
    before update or delete on pairing_open_enter
    for each row execute function argus_append_only();
