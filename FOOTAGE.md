# FOOTAGE.md

**Why this document exists.** The virtual camera rig is only as good as what it
plays. Obtaining footage is a procurement task with lead time and licensing
constraints — not an architecture question — and it churns at a different rate
than `ARCHITECTURE.md`. It also carries the honest list of things we cannot
validate without real cameras, which is the list most likely to be quietly
dropped from a demo.

Status: **nothing sourced yet.**

---

## 1. What we need, per pipeline

Ordered by how much it blocks. Each row states the minimum that makes the
pipeline developable, not the ideal.

| Pipeline | Needed | Minimum viable | Blocks |
|---|---|---|---|
| Ingest / rig (M1) | Any video, several streams, long enough to loop without visible seams | Any stock or self-shot footage, 4+ files, ≥ 10 min each | M1 |
| Canteen doorway (M3) | People crossing a threshold toward and away from a fixed camera, faces visible, including two-at-once and partially occluded crossings | **Self-shot**: a doorway, a phone on a tripod, a handful of consenting people walking in and out | M3 — the critical path |
| Face identity (M3) | Enrolment images + crossing footage for the same small set of people | Ourselves and consenting volunteers, written consent, deleted after | M3 |
| Gate verification (M4) | A person presenting at a camera at close range, plus mismatched pairs (person A's badge, person B's face) | Same volunteers, staged mismatches | M4 |
| Occupancy (M5) | A static wide view of seated people at fixed positions, with people leaving and returning | Any office/workshop wide shot; a staged desk row works | M5 |
| Violence (M6) | Pushing, grabbing, sudden aggressive motion, from an overhead-ish CCTV angle — **plus far more non-violent fast motion** (bundle handling, hurrying, celebration, horseplay) | Public datasets if obtainable, otherwise staged + a large negative set | M6 |
| Load testing | The real camera count at real resolution | Duplicated streams at 2304×1296 and 2880×1620 | Hardware sizing (ADR-0020) |

The most useful thing anyone can do this week is **film a doorway**. M3 is the
critical path, and a phone, a tripod and six willing people unblock it entirely.

---

## 2. Sourcing routes

**Self-shot (preferred for doorways, gate and occupancy).** Matches our own
geometry, no licence questions, and consent is straightforward because we know
everyone in it. Requirements: written consent naming what it is used for and when
it is deleted; not committed to git; not uploaded to a cloud staging box
(`PRIVACY_AND_COMPLIANCE.md` §2); deleted when the milestone closes.

**Public CCTV / pedestrian datasets.** Useful for detection and for crowd density.
Licences vary and several forbid commercial use — check each, record the licence
next to the file, and do not assume "research dataset" means "free to use here".

**Violence datasets.** The known ones (RWF-2000, RLVS, Hockey Fight, Movies
Fight, and similar) are **access-restricted**: request forms, institutional
affiliation, research-only terms. Expect weeks of lead time and expect to be
refused for a commercial project. Assume we may get none of them and plan ADR-0012
accordingly — which is exactly why the leaning is "trigger only, no classifier"
for the demo.

Note the domain gap even if we do get them: most are hand-held or movie footage,
not overhead factory CCTV, and a classifier trained on them will report confident
nonsense on our angle. That gap is a reason to distrust any pre-site accuracy
number, including our own.

**Synthetic.** Rendered or composited scenes, useful for geometry and edge cases
(exact door-line crossings, precise timing) and useless for appearance realism.
Good for pairing-logic tests, bad for anything about faces.

**Site footage.** The only footage that settles anything. Available from M8
onward, and subject to the same privacy rules as production data — it is
production data.

---

## 3. Rig requirements the footage must support

- **Loopable** without an obvious jump — a discontinuity every 10 minutes will be
  mistaken for a bug more than once.
- **Time-mappable**: a manifest states that a file begins at, say, 13:58 local, so
  pairing and pay-period boundaries can be exercised.
- **Labelled** where it matters: a sidecar listing known crossings
  (`13:58:02 person_a enter`) to serve as ground truth.
- **Matched to target resolutions**: 2304×1296 (Imou fixed), 2880×1620 (Imou PTZ),
  and whatever the Dahua/DVR channels output — likely 704×576 or 1280×720.
  Downstream behaviour differs by resolution, and the analog channels are the
  weak case that will be tested last and fail first.
- **Codec-matched**: H.264 and H.265, as the cameras produce, not a
  re-encoded-for-convenience variant.

Proposed layout (**UNBUILT**): `rig/footage/` for files (git-ignored),
`rig/manifests/` for committed manifests naming file, start time, resolution,
codec, licence, consent status and ground-truth labels. The manifests are the
part worth committing; the video is not.

---

## 4. What cannot be validated without real cameras

State this list unprompted, especially at the demo. It is the difference between
a working demo and a working system.

1. **Motion blur under uncontrolled auto-exposure.** Factory doorways swing
   between bright daylight and dim interior. Cameras chase exposure, shutter
   slows, walking faces smear. Self-shot footage taken in good light will not
   show this, and it is the single most likely cause of a lower-than-expected
   face capture rate.
2. **Walk-through capture geometry.** Whether a camera at a given height and
   angle actually captures a usable face from someone walking briskly through a
   doorway, not pausing. This is a physical question — mounting height, angle,
   lens, distance, stride — and it is answered by mounting a camera, not by code.
3. **Backlight at doorways.** A doorway is usually the brightest thing in the
   frame with a person silhouetted against it. The worst case in the building,
   and hard to stage convincingly.
4. **Night and low-light behaviour.** IR mode changes appearance enough to break
   face matching. Whether night shifts matter here is itself unknown.
5. **WiFi stream instability at scale.** Two Imou cameras over factory WiFi, with
   machinery, metal and other traffic. Reconnect behaviour under *real*
   instability is different from injected faults, which are always cleaner.
6. **DVR concurrent-client behaviour under sustained load.** Documented limits and
   actual limits differ, and the failure is usually a refused connection at the
   worst moment.
7. **Real crowd density.** 500+ people through canteen doors in minutes.
   Occlusion, grouping and tailgating at that density cannot be staged with six
   volunteers.
8. **Appearance variation across the actual workforce**, including how the face
   stack performs across the people who actually work there. A six-person
   volunteer set says nothing about this, and it is the fairness question that
   matters most.
9. **Long-run drift.** Dust on lenses, knocks from cleaning, seasonal light.
10. **Violence false-positive rate on real floor activity.** Bundle throwing,
    hurrying, horseplay, celebration. Almost certainly the dominant error source,
    and entirely unmeasurable before M9.

Every accuracy number produced before M8/M9 describes the rig. Saying so out loud
costs a sentence and prevents a much worse conversation later.

---

## 5. Consent and handling for footage of real people

Applies to self-shot footage as much as to site footage — a colleague's face is
biometric data too.

- Written consent naming the purpose, the retention period and who can view it.
- Never committed to git; never on a cloud staging box; never attached to an
  issue, a log or an error report.
- Deleted when the milestone closes, unless a fresh reason and fresh consent
  exist.
- Volunteer templates are deleted from the enrolment store at the same time —
  it is easy to remove the video and leave the embeddings behind.
- Site footage is production data and is governed by
  `PRIVACY_AND_COMPLIANCE.md`, with no separate "it's just for testing" category.
