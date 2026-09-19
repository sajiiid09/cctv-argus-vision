-- 0002_doorway_dedup.sql — deduplication points backwards (ADR-0032).
--
-- PAYROLL-AFFECTING. 0001 gave doorway_event a `superseded_by` column, which
-- reads forwards and is unwritable anyway: the append-only trigger rejects every
-- UPDATE, so the earlier row can never be marked after the fact. ARCHITECTURE.md
-- §7.3 wants the opposite direction -- the LATER detection carries a pointer at
-- the earlier one, which is the row that survives -- and argus.payroll was
-- already written that way (DoorEvent.duplicate_of).
--
-- Inverting this is not cosmetic: keeping the later detection means keeping the
-- worse estimate of when the crossing happened, which moves a dwell interval and
-- therefore a deduction. Hence the trigger below rather than a convention.

alter table doorway_event
    add column duplicate_of uuid references doorway_event (event_id);

alter table doorway_event
    add constraint doorway_event_duplicate_not_self
    check (duplicate_of is null or duplicate_of <> event_id);

create index doorway_event_duplicate_of_idx
    on doorway_event (duplicate_of) where duplicate_of is not null;

create or replace function doorway_event_duplicate_points_back() returns trigger as $$
declare
    target_ts timestamptz;
begin
    if new.duplicate_of is null then
        return new;
    end if;
    if new.duplicate_of = new.event_id then
        raise exception 'duplicate_of must point at another event, not itself';
    end if;
    select ts_utc into target_ts from doorway_event where event_id = new.duplicate_of;
    if target_ts is null then
        raise exception 'duplicate_of % is not an existing event', new.duplicate_of;
    end if;
    if target_ts > new.ts_utc then
        raise exception 'duplicate_of must point at an EARLIER event (ADR-0032): % is later than %',
            target_ts, new.ts_utc;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger doorway_event_duplicate_points_back_trigger
    before insert on doorway_event
    for each row execute function doorway_event_duplicate_points_back();

alter table doorway_event drop column superseded_by;

-- Gap vocabulary grows by two causes, both of which mean "we stopped measuring"
-- rather than "the camera went away", and both of which payroll reads as a
-- reason to zero the day:
--   aim_changed  the camera moved off its preset (ADR-0029)
--   overload     the analyser could not keep up (ADR-0033)
alter table stream_gap drop constraint stream_gap_cause_check;
alter table stream_gap add constraint stream_gap_cause_check
    check (cause in ('offline', 'decode_error', 'crash', 'clock_anomaly', 'stall',
                     'refused', 'aim_changed', 'overload'));
