# AGENTS.md

Operating instructions for AI coding agents (and humans) working in this repo.

Read `SOUL.md` first. It is short, and it decides most arguments this document
would otherwise have to have.

**Current state: documentation only. No code, no scaffolding, no configuration
exists.** Sections marked **UNBUILT** describe conventions for code that does not
exist yet; follow them when you create it, and update this file if reality
diverges.

---

## 1. The one distinction that matters

Code in this repo falls into two categories, and they are worked on differently:

**Payroll-affecting paths.** Canteen doorway events, the pairing state machine,
dwell and overage computation, the payroll export, the shadow-mode flag, the
clock/timezone handling they depend on, and the retention rules protecting their
clips.

**Everything else.** Occupancy, violence detection, dashboards, ingest
plumbing, tooling.

If you cannot tell which category you are in, **stop and find out**. Do not
guess. The difference in required care is large:

| | Payroll-affecting | Everything else |
|---|---|---|
| Tests | Every branch; property tests over the state machine; fixed clock | Normal coverage |
| Review | Human reads the diff before merge | Normal |
| Changing behaviour | ADR required | Not required |
| Ambiguity | Must resolve to zero deduction + flag | Use judgement |
| Refactors | Behaviour-preserving only, proven by tests, separate commit | Normal |

---

## 2. Human sign-off — required, no exceptions

Agents do not do these autonomously. Stop, state what you propose, wait for a
human answer. The team is effectively one engineer plus agents, so these gates
are kept few and cheap enough to actually be used.

1. **Turning on the payroll export flag** (leaving shadow mode), or any change
   that makes export possible without the flag.
2. **Changing the pairing state machine's semantics** — which cases resolve to a
   deduction, thresholds, allowance windows, day-boundary attribution.
3. **Anything that connects occupancy data to a person or to pay**, including a
   schema change that creates the join path.
4. **Adding automated action to the violence pipeline** — notification, naming,
   incident creation. The output is a human review queue. That is the design.
5. **Changing retention or deletion** of clips, events or biometric templates.
6. **Moving biometric data or clips off the site**, including to a cloud GPU, an
   error tracker, a log aggregator or a model vendor's API.
7. **Adding cross-camera re-identification or floor-wide tracking** (ADR-0002).
8. **Changing anything asserted in `SOUL.md`.**
9. **Adding a dependency with a licence that restricts commercial deployment**
   (AGPL in particular — ADR-0011 is open on exactly this).

A pattern that looks like an exception but is not: "just for the demo". Demo
pressure is the most common reason these gates get skipped, and a demo that
deducts real pay from real people is the exact failure this project is built to
avoid.

---

## 3. What needs an ADR before changing

An ADR (`DECISIONS.md`) is needed to change any ACCEPTED decision, and to close
any OPEN one. In particular: scope (ADR-0001), doorway-only identity (ADR-0002),
two-separate-numbers (ADR-0003), fail-open (ADR-0004), shadow mode (ADR-0005),
the no-write-to-payroll boundary (ADR-0006), auditability (ADR-0008).

Also ADR-worthy even though nothing is decided yet: choosing the database, the
face stack, the detection model family, the dependency tool, the Python version.
Closing an OPEN ADR is a decision — record it, do not just start using the thing.

Write the ADR *before* the code, not as documentation afterwards. An ADR written
after implementation is a justification, not a decision.

---

## 4. Directory conventions — **UNBUILT**

Proposed layout, to be created with the first code and revised if it does not
fit:

```
docs/            these documents (currently at root; may move)
services/        long-running processes (ingest, analysis workers, api)
packages/        shared libraries
  backends/      inference backends — the ONLY place a backend is imported
  pipelines/     gate, canteen, occupancy, violence
  payroll/       pairing, overage, export — the high-care code, isolated
  store/         event store, clip store
infra/           compose files, systemd units, mediamtx config (Linux-facing)
rig/             virtual camera rig: footage manifests, serving config
tests/
  golden/        golden frames, clips, reference outputs
models/          ONNX artefacts referenced by hash (or a pointer file; see below)
```

Rules:

- **All paths lowercase with underscores.** The dev Mac is case-insensitive and
  the production box is not; `Models/Face.onnx` works on one and not the other.
  This is enforced by a CI check on Linux, not by care.
- **`packages/payroll/` imports no vision code.** Pairing and overage are pure
  functions over stored events. Keeping probabilistic code out of wage arithmetic
  is deliberate — it is what makes the wage arithmetic testable.
- **`packages/backends/` is the only place** `onnxruntime`, `torch`, TensorRT or
  a vendor SDK may be imported. Application code asks a registry for a
  `Detector`. Add an import-check to CI; this is the abstraction that keeps the
  Mac and the Linux box running the same source.
- Large model binaries do not go in git history. Mechanism (LFS, a pointer file
  plus a fetch script, an artefact store) is undecided — pick one before the
  first model lands, not after.

---

## 5. Running things — **UNBUILT**

Detail in `DEV_SETUP.md`, and note that nobody has executed any of it.

**macOS (dev).** Infrastructure in Docker (Postgres, mediamtx, broker); analysis
processes run **natively**, because Docker on macOS cannot reach the GPU. So the
dev machine is a hybrid, permanently. That is not a workaround to be fixed later;
it is the platform.

**Linux (staging/prod).** Everything in containers with the NVIDIA Container
Toolkit. Containers must invoke the *same* entrypoints a developer runs natively
— the container supplies environment, not behaviour. If a container needs a
different command to work, the difference is a bug.

**Both.** Configuration comes from the same file/env mechanism. No ad-hoc
command-line arguments that exist only on someone's laptop.

**The Linux box is the arbiter.** macOS proves logic; Linux proves the system. A
change is not done because it works on the Mac. From M1 onward, "it runs on
staging" is an exit criterion, not a follow-up task.

---

## 6. Testing expectations

Full strategy in `TESTING.md`. The parts agents get wrong:

- **Payroll logic is tested against the case table in `DATA_MODEL.md` §4.** Every
  row needs a test. Adding a row to that table means adding a test.
- **Property test the invariant, not just the examples:** no sequence of doorway
  events may produce non-zero overage in a non-`RESOLVED` state. This is the
  fail-open guarantee, and examples alone will not hold it.
- **Fixed clocks.** Never `now()` in payroll code paths. Time is an input.
  Timezone is configuration, not the server's locale.
- **Golden-frame parity** runs on Linux CI. The macOS leg is open (ADR-0022) —
  do not silently drop it because it is inconvenient.
- **Video tests use recorded footage through the rig over RTSP**, not frames read
  from disk. Reading files bypasses decode, reconnection and timing — which is
  where the bugs are.
- **A test asserts occupancy cannot reach payroll.** If you refactor storage,
  that test must still mean something; do not weaken it into a tautology.

---

## 7. Working style in this repo

- **Write down uncertainty.** "We have not decided" is a complete sentence here.
  A confident invention is worse than an acknowledged gap, because it is
  invisible in review.
- **Do not smooth over gaps in documentation.** `PROVISIONAL`, `UNVERIFIED` and
  `OPEN` are load-bearing markers. Removing one is a claim that you verified
  something — so verify it, or leave it.
- **No synthetic accuracy claims.** Numbers from the virtual rig describe the
  rig. They do not describe the factory, and must never be presented as if they
  do.
- **Do not design around the demo.** The demo is a milestone, not the product.
- **Prefer deleting a feature to weakening a guarantee.** If occupancy is too
  noisy to be useful, ship less occupancy — do not make it useful by tying it to
  something it should not touch.
- **Small diffs in payroll code.** A 600-line refactor of the pairing logic
  cannot be reviewed by a tired human at 11pm, which is when it will be reviewed.
- **When blocked on a decision, propose and stop.** Do not pick a stack, a model
  or a threshold to keep moving. State the options, state a leaning, wait.

## 8. Things that look helpful and are not

- Adding a "best guess" for an unpaired event.
- Adding a confidence-weighted partial deduction.
- Caching a person's last known location "to help with pairing" — that is re-ID
  with a friendlier name.
- Logging clips or face crops to an error tracker for debugging.
- Backfilling missing events from a schedule or an average.
- Making the review queue auto-dismiss low-score violence candidates. The queue
  is the product; an empty queue is not a success metric.
- Renaming `unknown` to something that sorts better next to real identities.
