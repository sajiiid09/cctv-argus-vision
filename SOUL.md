# SOUL.md

This is the document that settles arguments. It is short on purpose. When two
reasonable engineering choices conflict, resolve the conflict here first, then go
back to the code.

The system is called **Sparrow Vision** (ADR-0024). The code namespace stays
`argus.`, which is a directory and import name, not a product name.

## What this is

A camera system for one garments factory in Bangladesh. It does four things:
verifies that the person tapping a badge at the gate is the badge holder; measures
how long people spend in the canteen; reports which production seats are occupied;
and flags possible violence for a human to look at.

One of those four things affects someone's pay. Only one. That asymmetry is the
whole moral centre of this project, and most of the rules below exist to protect
it.

## Who it serves

Factory management, who currently cannot answer basic questions about their own
floor without walking it, and who are audited by international buyers who ask
those questions anyway.

It also, if built correctly, serves the workers — because the alternative to a
measured deduction is not "no deduction". It is a supervisor's memory, a
supervisor's grudge, and a number written on a paper roster with no way to
challenge it. An auditable system that is wrong 2% of the time and shows its
working is better for a worker than an unauditable system that is wrong 10% of
the time and shows nothing. That is the argument for building this at all, and if
we stop being able to make that argument honestly, the project has failed on its
own terms.

## Who it can harm

Workers. Specifically:

- Someone gets a deduction they did not earn, because a camera missed an exit
  event, and has no way to prove it.
- Someone gets watched more closely than their coworkers because the model
  performs worse on their face, or their skin tone, or their height, or because
  they sit nearest the camera.
- Occupancy data leaks into a disciplinary conversation it was never meant for.
- A violence flag becomes an accusation before a human has looked at the clip.
- The biometric database leaves the building.

Every one of these is a plausible outcome of a system built carelessly, and
several of them are the *default* outcome. They are what the rules below are for.

## What we are not deciding

Whether the factory deploys this, what they tell workers, and how disputes are
handled inside the factory are management's decisions. They have been made. We
were not asked.

So this document does not promise anything on workers' behalf, because we are not
in a position to keep such a promise and a promise we cannot keep is worse than
none. What it does instead is constrain *our* engineering, which is the part we
actually control. Where management's policy and this document collide, we say so
out loud and in writing rather than quietly building the thing anyway — see
`PRIVACY_AND_COMPLIANCE.md` for the questions we escalate rather than answer.

The distinction matters and is easy to blur. "We built a system that makes
disputes resolvable in minutes" is a true thing we can deliver. "Workers can
dispute deductions" is a claim about factory process that we cannot deliver and
must not print on a slide.

## The rules

### Two separate numbers, always

Canteen overage is a payroll number. Seat occupancy is a management indicator.
They are computed by different pipelines, stored separately, and displayed
separately.

The reason is not squeamishness. It is that seat occupancy is a *bad* measure of
work. An operator away from their seat may be at the toilet, collecting bundles,
waiting on a mechanic, being talked at by a supervisor, or covering another line.
Occupancy measures presence at a coordinate. It does not measure effort,
output, or fault, and the gap between those is exactly where an unfair deduction
would live.

Anyone who proposes merging them is proposing to pay people based on a number
that does not mean what it appears to mean. The answer is no, and it does not
require a meeting.

### Fail open

Any event that is unpaired, implausible, or missing produces **zero deduction and
a review flag**. Never a default deduction, never an estimated one, never "the
average".

The reason is that our errors are not symmetric. A missed deduction costs the
factory a few minutes of wage across a large payroll — a rounding error. A false
deduction costs one specific person money they earned, in a context where the
sums are not trivial to them and the power to complain is limited. When the two
error types cost that differently, the system must be biased toward the cheap
one. Deliberately. In the code, not in the documentation.

This has a consequence people find uncomfortable: the system will be gameable.
Someone who learns that walking out of the canteen in a tight group behind
another person breaks the pairing will get free minutes. We accept that. A system
that is occasionally cheated is recoverable; a system that occasionally steals
wages is not.

### Auditability in minutes, not days

Every payroll-affecting record must resolve to a clip and a timestamp, retrievable
by a non-engineer, in minutes.

If a number cannot be traced back to something a human can watch, it should not
be capable of reducing anyone's pay. This is a hard architectural requirement,
not a reporting feature — it constrains retention, storage layout, and clock
discipline, and those constraints are cheaper to accept now than to retrofit. An
unauditable deduction is indistinguishable from an arbitrary one, including to
the people writing this software.

### Shadow mode until someone with authority signs

The deduction is computed and reported from early on, and written to a payroll
export only when a config flag is set. That flag stays off until legal review and
buyer-compliance review (BSCI / SMETA / WRAP) have happened.

The reason is that we will be wrong in ways we cannot anticipate from a desk in
another building, and the cheapest place to discover that is a report nobody is
paid from. Shadow mode is not caution theatre; it is how we find out that the
canteen has a second door nobody mentioned.

### Violence detection never reaches a verdict

The pipeline's output is an item in a review queue, with a clip. It is never a
notification, never a name, never an incident record on its own. A model trained
on other people's fights, in other people's lighting, has no business producing
an accusation about a specific worker.

### Anonymity on the floor is a feature, not an oversight

Identity is resolved at doorways only. There is no floor-wide tracking and no
cross-camera re-identification. This is an architectural decision (see
`ARCHITECTURE.md`) and it is also a boundary: it makes "where was this person all
day" a question the system structurally cannot answer. Anyone proposing re-ID is
proposing to remove that, and owes an argument for why the capability is worth
more than the boundary.

## Standard of care

Assume everything you build will be read back to you in a room with a buyer's
auditor and a worker's representative in it, and that you will have to defend the
default you chose, not the intent behind it. Choose defaults you would defend in
that room.

Concretely, for anyone — human or agent — working in this repo:

- Payroll-affecting code is held to a higher standard than everything else. It
  gets tested harder, changed more slowly, and reviewed by a human. See
  `AGENTS.md`.
- If you cannot tell whether a code path can affect pay, stop and find out. Do
  not guess.
- Uncertainty gets written down, not smoothed over. "We have not decided" is a
  complete and acceptable sentence in this repo.
- Convenience is not a reason. Demo day is not a reason. Neither of those has ever
  been a good explanation for a wrong number on someone's wage slip.
