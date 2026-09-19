-- 0005_gate.sql — badge taps and the four-way verification outcome.
--
-- Not payroll-affecting: the gate measures attendance and never produces a
-- deduction. It is identity-adjacent, so it carries person_id.
--
-- The tap is written BEFORE verification is attempted (ARCHITECTURE.md §7.2), so
-- a crash loses a verification result and never the attendance evidence.

create table gate_tap (
    tap_id             uuid primary key,
    reader_id          text not null,
    badge_id           text not null,
    -- Server clock at receipt: authoritative.
    ts_utc             timestamptz not null,
    -- What the reader said. Audit only, never arithmetic.
    ts_reader_reported timestamptz,
    source             text not null check (source in ('zkt_live', 'zkt_replay', 'simulated')),
    -- The reader's own log index, when it has one. Makes a replayed copy of a
    -- live tap identifiable as the same tap rather than a second one.
    reader_seq         bigint,
    ingest_run_id      uuid references ingest_run (ingest_run_id),
    received_at        timestamptz not null default now()
);

create unique index gate_tap_reader_seq_once
    on gate_tap (reader_id, reader_seq) where reader_seq is not null;
create index gate_tap_reader_ts_idx on gate_tap (reader_id, ts_utc);
create index gate_tap_badge_ts_idx on gate_tap (badge_id, ts_utc);

create trigger gate_tap_append_only_trigger
    before update or delete on gate_tap
    for each row execute function argus_append_only();

create table gate_event (
    gate_event_id uuid primary key,
    tap_id        uuid not null unique references gate_tap (tap_id),
    -- The badge holder at tap time, resolved through badge_assignment. Null
    -- means the badge was unassigned, which is itself worth seeing.
    person_id     text references person (person_id),
    camera_id     text references camera (camera_id),
    -- Four values, and they are four for a reason (ARCHITECTURE.md §7.2):
    --   true          the face matched the badge holder
    --   false         a different face -- a buddy-punching signal
    --   no_face       we looked and saw no usable face -- a camera problem
    --   not_attempted we never compared: replayed tap, or no template
    -- Collapsing no_face into false destroys the only distinction this pipeline
    -- exists to make.
    face_verified text not null check (face_verified in
                    ('true', 'false', 'no_face', 'not_attempted')),
    match_score   real,
    face_quality  real,
    threshold     real,
    source        text not null check (source in ('zkt_live', 'zkt_replay', 'simulated')),
    clip_ref      text,
    decided_at    timestamptz not null default now(),
    review_state  text not null default 'pending'
                    check (review_state in ('pending', 'dismissed', 'escalated')),
    reviewed_by   text,
    reviewed_at   timestamptz,
    review_reason text,
    -- A tap recovered from the reader's log was never compared against
    -- anything, so recording it as no_face would claim we looked.
    constraint gate_event_replay_not_attempted
        check (source <> 'zkt_replay' or face_verified = 'not_attempted'),
    -- A score without a comparison, or a comparison without a score, is a bug
    -- in whichever writer produced it.
    constraint gate_event_score_iff_compared
        check ((face_verified in ('true', 'false')) = (match_score is not null))
);

create index gate_event_review_idx on gate_event (review_state, decided_at desc);
create index gate_event_person_idx on gate_event (person_id, decided_at desc);

-- Everything but the review columns is evidence. Review is a human decision
-- recorded once: pending -> dismissed|escalated, never back, never re-decided.
create or replace function gate_event_guard() returns trigger as $$
begin
    if tg_op = 'DELETE' then
        raise exception 'gate_event is evidence (ARCHITECTURE.md §7.1); delete is rejected';
    end if;
    if row(new.gate_event_id, new.tap_id, new.person_id, new.camera_id, new.face_verified,
           new.match_score, new.face_quality, new.threshold, new.source, new.clip_ref,
           new.decided_at)
       is distinct from
       row(old.gate_event_id, old.tap_id, old.person_id, old.camera_id, old.face_verified,
           old.match_score, old.face_quality, old.threshold, old.source, old.clip_ref,
           old.decided_at) then
        raise exception 'only the review columns of gate_event may be updated';
    end if;
    if old.review_state <> 'pending' then
        raise exception 'gate_event % was already reviewed by % at %',
            old.gate_event_id, old.reviewed_by, old.reviewed_at;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger gate_event_guard_trigger
    before update or delete on gate_event
    for each row execute function gate_event_guard();

-- "Nobody tapped" and "we were not listening" are different facts, and only
-- this table distinguishes them. Taps recovered by replay land inside a gap.
create table reader_gap (
    gap_id      uuid primary key,
    reader_id   text not null,
    from_utc    timestamptz not null,
    to_utc      timestamptz,
    cause       text not null check (cause in
                  ('offline', 'refused', 'replay_window', 'clock_anomaly', 'crash')),
    detected_at timestamptz not null default now(),
    constraint reader_gap_window check (to_utc is null or to_utc >= from_utc)
);

create index reader_gap_reader_from_idx on reader_gap (reader_id, from_utc);
