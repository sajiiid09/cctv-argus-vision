# PROJECT_NAME

CCTV workplace analytics for a garments factory in Bangladesh.

**Status: documentation only. No code exists.** Every technology choice is open;
see `DECISIONS.md`. `PROJECT_NAME` is a placeholder — the product has no name
(ADR-0024). The directory is called `argus-vision`; that is a directory name, not
a decision.

## What it does

Four pipelines:

1. **Gate attendance** — badge/RFID tap, with face recognition used to *verify*
   the badge holder. Anti-buddy-punching, not primary identification.
2. **Canteen time** — face recognition at canteen doors (no badge: 500+ people in
   minutes, and a tap queue would be disabled by supervisors within days). Dwell
   beyond a one-hour allowance becomes a payroll deduction.
3. **Workstation occupancy** — anonymous per-seat occupied/empty. A management
   report. **Never connected to pay.**
4. **Violence detection** — cheap pose trigger → clip → classifier → **human
   review queue**. Never an automated verdict.

Out of scope, deliberately: theft detection, floor-wide tracking, cross-camera
re-identification. Identity is resolved **only at doorways** (ADR-0002).

## The rules that decide arguments

- Two separate numbers: canteen overage touches pay; occupancy never does.
- Fail open: any unpaired, implausible or missing event → zero deduction + a
  review flag.
- Auditability: every payroll-affecting record resolves to a clip in minutes.
- Shadow mode by default: computed and reported, not written to payroll, until
  legal and buyer-compliance review and explicit sign-off.

`SOUL.md` argues for each of these rather than asserting them.

## Where to start reading

| If you are | Read |
|---|---|
| New to the project | `SOUL.md`, then this file's table, then `ARCHITECTURE.md` |
| Building something | `AGENTS.md`, `ARCHITECTURE.md`, `DATA_MODEL.md` |
| An AI coding agent | `AGENTS.md` first, `SOUL.md` second. Both, before code |
| Wondering why X was chosen | `DECISIONS.md` |
| Touching payroll logic | `DATA_MODEL.md` §4, `TESTING.md` §2, `AGENTS.md` §1–2 |
| Setting up a machine | `DEV_SETUP.md` (all of it unverified) |
| Worried about privacy or audits | `PRIVACY_AND_COMPLIANCE.md`, `THREAT_MODEL.md` |
| Planning the next few weeks | `PLAN.md` |
| Confused by a word | `GLOSSARY.md` |

## Documents

- `SOUL.md` — why this exists, who it can harm, the standard of care. Short.
- `ARCHITECTURE.md` — system shape, the doorway-only-identity decision, the
  macOS↔Linux cross-platform strategy, the virtual camera rig, failure modes.
- `PLAN.md` — milestones to demo and pilot; what is blocked on hardware.
- `AGENTS.md` — how to work here; what needs human sign-off.
- `DECISIONS.md` — ADR log: what is decided, what is open, and why.
- `DATA_MODEL.md` — entities and the door-event pairing state machine.
- `PRIVACY_AND_COMPLIANCE.md` — biometrics, retention, access, buyer audits, open
  legal questions.
- `DEV_SETUP.md` — macOS and Ubuntu paths. Unverified.
- `TESTING.md` — golden-frame parity, and the stricter bar for payroll code.
- `THREAT_MODEL.md` — how this gets gamed, by workers and by staff.
- `FOOTAGE.md` — what video we need, and what cannot be validated without real
  cameras.
- `GLOSSARY.md` — shared vocabulary.

## Environment, in one line

Dev is macOS/arm64 where Docker cannot reach the GPU, so inference runs natively.
Production is Ubuntu/x86_64 with NVIDIA where everything is containerised. The
same source tree runs on both, via backend abstraction and a golden-frame parity
suite. `ARCHITECTURE.md` §5 is the substantial part of this.

## No cameras exist yet

Everything through the first several milestones runs on synthetic and recorded
video served over RTSP, indistinguishable from a camera at the pipeline boundary.
The rig is infrastructure, not a fixture: CI video source and demo-day fallback.
`ARCHITECTURE.md` §6, `FOOTAGE.md`.
