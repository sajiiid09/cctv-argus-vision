# TESTING.md

Testing strategy and the standard code is held to.

**Status 2026-09-17:** the ingest, store, backend and structural suites are real
and green on Linux/CPU (`uv run pytest`). The payroll tier described in §2 is
being built now, ahead of the pipelines that feed it. The golden parity suite
runs its CPU leg in CI; the CUDA and CoreML legs are selected with
`ARGUS_PARITY_BACKEND` and remain unrun.

The governing idea: **the tests are asymmetric because the consequences are.** A
bug in the occupancy dashboard is embarrassing. A bug in the pairing state
machine takes money from someone who cannot easily contest it. They do not get
the same treatment.

---

## 1. Tiers

| Tier | What | Bar |
|---|---|---|
| **Payroll-affecting** | Pairing, dwell, overage, export, the clock/timezone code they depend on | Every case in `DATA_MODEL.md` §4; property tests; fixed clocks; human review of diffs |
| **Vision** | Detection, pose, embedding, classification | Golden-frame parity within tolerance; no accuracy claims from synthetic footage |
| **Pipeline** | Ingest, clipping, reconnection, fault behaviour | Integration tests over RTSP from the rig, including faults |
| **Everything else** | Dashboards, tooling, review UI | Normal coverage |

---

## 2. Payroll-affecting logic

This code is a pure function of `(events, gaps, policy, code version)`. Keeping
it free of vision code is what makes this tier possible — probabilistic code
cannot be tested this way, and wage arithmetic must be.

**Case coverage.** Every row of the `DATA_MODEL.md` §4 table is a test: clean
pair, enter→enter, exit→exit, unpaired enter, unpaired exit, too short, too long,
stream-gap overlap, clock anomaly, unknown face, low confidence, multi-door,
midnight crossing, duplicate event. Adding a row to that table means adding a
test in the same commit.

**Property tests — the important part.** Examples prove the cases you thought
of; the fail-open guarantee is a claim about cases you did not.

1. For any generated sequence of doorway events, any interval not in state
   `RESOLVED` contributes **exactly zero** overage.
2. Overage is never negative and never exceeds the day's measured dwell.
3. Any `stream_gap` overlapping an interval forces zero for that day.
4. Recomputing over the same inputs yields identical output (determinism).
5. Adding a *later* event never increases a previously computed overage for an
   already-closed day without an explicit recomputation record.
6. No event ordering, including out-of-order arrival, produces an interval with
   `end < start`.

Property 1 is the fail-open invariant in executable form. If it ever fails, stop
and fix it before anything else — it is the one guarantee `SOUL.md` makes.

**Fixed clocks.** `now()` never appears in this code. Time is an input, the
policy timezone is configuration, and the `Asia/Dhaka` boundary cases (pay period
edges, shift edges, midnight-crossing dwell) are tested explicitly. There is no
DST in Bangladesh, which removes one class of bug and tempts people into
shortcuts that break the moment the code is read by someone who assumes UTC.

**Export tests.** The signed export must be reproducible byte-for-byte from the
same inputs, must refuse any line lacking a clip reference, and must refuse to
include anything derived from a virtual camera (`is_virtual`). Demo footage must
never be able to reach a wage.

**Shadow mode.** A test asserts that with the flag off, no export artefact is
produced and no payroll line is marked exported — and that flipping the flag is
recorded with an actor.

---

## 3. Golden-frame parity suite

Purpose: make backend divergence **visible and bounded**, not zero. Different
backends will differ; the suite tells us whether the difference matters.

**Contents.** A small committed set of frames and short clips, with reference
outputs from the designated reference backend (leaning: ONNX Runtime CPU fp32 —
available everywhere including CI, and numerically the most boring). Frames
chosen for what stresses the pipeline: a door crossing at the threshold, two
people overlapping in a doorway, backlit faces, partial faces, an empty scene, a
seated floor view.

**Tolerances.** Starting points in `ARCHITECTURE.md` §5.3 — box IoU ≥ 0.95,
confidence ±0.05, embedding cosine distance ≤ 0.02, identical verification
decisions, keypoints ≤ 2 px, clip scores ±0.10 with identical top-1. All
**PROVISIONAL**. The suite's first job is to tell us what natural divergence
actually is; then we tighten.

**Rules.**

- Tolerances are widened only with a recorded reason in the commit message. A
  widened tolerance is a decision to care less, and it should read like one.
- **Decisions matter more than numbers.** A face verification that flips
  accept↔reject fails the suite regardless of how small the cosine drift was.
- Runs on Linux CI on every change to backends or models. The macOS leg is
  unsolved (ADR-0022) — run it on a developer machine and record results rather
  than quietly dropping the only evidence that dev and prod agree.
- Model files are referenced by hash. Comparing outputs from two different model
  files proves nothing, and it is an easy mistake to make accidentally.

---

## 4. Testing against recorded video, not live cameras

All video tests go **through the rig over RTSP**, never by reading frames from
disk. Reading files bypasses decode, reconnection, timing and backpressure —
which is precisely where the bugs live.

**Determinism.** Fixed file, fixed start offset, fixed frame count. A golden-frame
test that depends on when the loop happened to restart is not a test.

**Wall-clock mapping.** Tests must be able to say "this footage begins at 13:58
local" so pairing, allowance windows and pay-period boundaries can be exercised
against realistic times.

**Fault injection is the valuable part**, and the part most likely to be skipped
under demo pressure:

- Stream drops mid-clip → reconnect, and a `stream_gap` recorded.
- Stream stalls (connection open, no frames) → detected, not hung forever. This
  is the WiFi failure mode and it is nastier than a clean drop.
- Resolution or codec change mid-stream → handled or cleanly failed.
- High latency / jitter.
- DVR refusing a connection because the concurrent-client limit is reached →
  backoff, and a recorded gap rather than a silent loss.

Each of those has a corresponding expected behaviour in `ARCHITECTURE.md` §7, and
each expectation needs a test. The invariant behind all of them: **a gap is
always recorded**, because "we saw nothing" and "nothing happened" must never be
confused by anything downstream.

---

## 5. Structural tests

Tests that enforce architecture rather than behaviour. They exist because these
properties erode quietly:

1. **No backend import outside `packages/backends/`.** An import-graph check.
2. **`packages/payroll/` imports no vision code.**
3. **No path from occupancy storage to a person or to a payroll table.** If a
   refactor makes this test trivially true, the test has been weakened rather
   than satisfied — check that it still means something.
4. **Lowercase paths**, checked on Linux where case actually matters.
5. **No `now()` in payroll code paths.**

---

## 6. What we cannot test yet

State it rather than papering over it:

- **Real-world accuracy.** Synthetic and borrowed footage says nothing about face
  capture at this factory's doorways, in its lighting, with its people. Any
  accuracy number produced before M9 describes the rig.
- **Throughput at the real camera count** on the real production box (ADR-0020
  open).
- **Violence classifier performance.** No site footage; restricted datasets.
- **Long-run stability.** Days of continuous operation cannot be simulated in CI.
- **The macOS GPU leg in CI** (ADR-0022).

`FOOTAGE.md` §4 lists behaviours that cannot be validated without real cameras at
all. When someone asks whether the system works, those two lists are the honest
answer, and offering them unprompted is much better than being asked for them.
