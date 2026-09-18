-- 0007_anonymous_and_access.sql — the two anonymous pipelines, and the log of
-- who looked at what.
--
-- NOT payroll-affecting, and the tables here must stay that way. occupancy_sample,
-- violence_candidate and violence_review_action carry no person_id and no
-- foreign key that could acquire one. SOUL.md calls this "two separate numbers";
-- a structural test parses this file and enforces it, because a comment is not
-- a control and adding the join would look like a small refactor.

create table occupancy_sample (
    sample_id  uuid primary key,
    camera_id  text not null references camera (camera_id),
    -- A seat, not a person. Seats are assigned, so per-seat data is closer to
    -- identified data than this schema suggests -- which is exactly why
    -- ADR-0019 puts the per-seat view behind the admin tier and logs it.
    seat_id    text not null,
    line_id    text,
    at         timestamptz not null,
    occupied   boolean not null,
    confidence real,
    window_s   int not null,
    constraint occupancy_window_positive check (window_s > 0)
);

create index occupancy_sample_seat_at_idx on occupancy_sample (seat_id, at desc);
create index occupancy_sample_line_at_idx on occupancy_sample (line_id, at desc);

-- ADR-0012: trigger-only. There is deliberately NO classifier_score column --
-- adding one means reopening that ADR and writing a migration, which is the
-- point. The score here is the cheap pose trigger's, and it decides only
-- whether a human is shown a clip.
create table violence_candidate (
    candidate_id  uuid primary key,
    camera_id     text not null references camera (camera_id),
    at            timestamptz not null,
    trigger_score real not null,
    features      jsonb not null default '{}'::jsonb,
    clip_id       uuid references clip (clip_id),
    review_state  text not null default 'pending'
                    check (review_state in ('pending', 'dismissed', 'escalated')),
    reviewed_by   text,
    reviewed_at   timestamptz,
    review_reason text,
    created_at    timestamptz not null default now()
);

create index violence_candidate_review_idx on violence_candidate (review_state, at desc);

-- The queue is the product (AGENTS.md §9). Nothing auto-dismisses, so review
-- columns move pending -> dismissed|escalated exactly once, by a named human;
-- everything else is evidence.
create or replace function violence_candidate_guard() returns trigger as $$
begin
    if tg_op = 'DELETE' then
        raise exception 'violence_candidate is evidence; delete is rejected';
    end if;
    if row(new.candidate_id, new.camera_id, new.at, new.trigger_score, new.features,
           new.clip_id, new.created_at)
       is distinct from
       row(old.candidate_id, old.camera_id, old.at, old.trigger_score, old.features,
           old.clip_id, old.created_at) then
        raise exception 'only the review columns of violence_candidate may be updated';
    end if;
    if old.review_state <> 'pending' then
        raise exception 'violence_candidate % was already reviewed by % at %',
            old.candidate_id, old.reviewed_by, old.reviewed_at;
    end if;
    if new.review_state <> 'pending'
       and (new.reviewed_by is null or length(btrim(new.reviewed_by)) = 0) then
        raise exception 'a review names the human who made it';
    end if;
    return new;
end;
$$ language plpgsql;

create trigger violence_candidate_guard_trigger
    before update or delete on violence_candidate
    for each row execute function violence_candidate_guard();

create table violence_review_action (
    action_id    uuid primary key,
    candidate_id uuid not null references violence_candidate (candidate_id),
    at           timestamptz not null default now(),
    actor        text not null,
    role         text not null,
    action       text not null check (action in ('viewed', 'dismissed', 'escalated')),
    reason       text,
    constraint violence_review_actor_named check (length(btrim(actor)) > 0)
);

create index violence_review_action_candidate_idx on violence_review_action (candidate_id, at);

create trigger violence_review_action_append_only_trigger
    before update or delete on violence_review_action
    for each row execute function argus_append_only();

-- RISKS.md §5 names casual clip browsing as the misuse most likely to happen
-- and least likely to be reported, and RISKS.md §10 says this log is built
-- regardless of how good the authentication turns out to be (ADR-0028).
--
-- `actor` is the staff member doing the looking. There is no subject person_id
-- here, and "it has a person column" is exactly the misreading that would
-- prompt someone to add one.
create table clip_access_log (
    access_id uuid primary key,
    at        timestamptz not null default now(),
    actor     text not null,
    role      text not null,
    clip_id   uuid references clip (clip_id),
    context   text,
    reason    text,
    -- Denied attempts are the interesting signal, so they are logged too --
    -- before the response, never after.
    outcome   text not null check (outcome in ('served', 'denied')),
    constraint clip_access_actor_named check (length(btrim(actor)) > 0)
);

create index clip_access_log_actor_at_idx on clip_access_log (actor, at desc);
create index clip_access_log_clip_at_idx on clip_access_log (clip_id, at desc);

create trigger clip_access_log_append_only_trigger
    before update or delete on clip_access_log
    for each row execute function argus_append_only();
