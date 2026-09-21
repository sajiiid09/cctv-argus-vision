"""The violence trigger: cheap, recall-biased, and never a verdict (ADR-0012).

What this produces is an item in a queue with a clip attached. It does not
classify, it does not name anybody, and nothing downstream may act on it
automatically -- `SOUL.md` is explicit that a model trained on other people's
fights in other people's lighting has no business producing an accusation about
a specific worker.

The score is a weighted sum of three pose features, each of which is a
*geometric* statement rather than an interpretation:

* **proximity** -- how close two people's torsos are, in shoulder widths, which
  normalises for distance from the camera;
* **extension** -- how far a wrist is from its own shoulder, relative to that
  person's shoulder width: a punch or a grab extends a limb, and so does
  reaching for a bundle, which is why this is one feature of three;
* **energy** -- how much the keypoints moved since the previous sample.

Recall-biased on purpose: the cost of a missed incident is much higher than the
cost of a reviewer watching ten seconds of nothing. The honest false-positive
rate on a real factory floor is unmeasurable before a site pilot and is
expected to be the dominant error source (ARCHITECTURE.md §9.2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

import numpy as np
from argus.common.config import ViolenceConfig

log = logging.getLogger(__name__)

# COCO keypoint indices used here.
LEFT_SHOULDER, RIGHT_SHOULDER = 5, 6
LEFT_WRIST, RIGHT_WRIST = 9, 10
LEFT_HIP, RIGHT_HIP = 11, 12
MIN_CONFIDENCE = 0.2


@dataclass(frozen=True, slots=True)
class Features:
    proximity: float
    extension: float
    energy: float
    people: int

    def as_dict(self) -> dict[str, float]:
        return {
            "proximity": round(self.proximity, 4),
            "extension": round(self.extension, 4),
            "energy": round(self.energy, 4),
            "people": float(self.people),
        }


@dataclass(frozen=True, slots=True)
class Candidate:
    at: datetime
    score: float
    features: Features


def _torso(person: np.ndarray) -> np.ndarray | None:
    points = [
        person[index]
        for index in (LEFT_SHOULDER, RIGHT_SHOULDER, LEFT_HIP, RIGHT_HIP)
        if person[index, 2] >= MIN_CONFIDENCE
    ]
    if not points:
        return None
    return np.mean([point[:2] for point in points], axis=0)


def shoulder_width(person: np.ndarray) -> float:
    left, right = person[LEFT_SHOULDER], person[RIGHT_SHOULDER]
    if left[2] < MIN_CONFIDENCE or right[2] < MIN_CONFIDENCE:
        return 0.0
    return float(np.linalg.norm(left[:2] - right[:2]))


def proximity_feature(people: list[np.ndarray], shoulders: float) -> float:
    """1.0 when two torsos are touching, 0.0 when they are far apart.

    Distance is measured in shoulder widths so that two people close together at
    the back of the frame score the same as two people close together at the
    front. In pixels, everything near the camera would look like a fight.
    """
    if len(people) < 2 or shoulders <= 0:
        return 0.0
    torsos = [torso for torso in (_torso(person) for person in people) if torso is not None]
    if len(torsos) < 2:
        return 0.0
    widths = [shoulder_width(person) for person in people]
    reference = max(w for w in widths if w > 0) if any(w > 0 for w in widths) else 0.0
    if reference <= 0:
        return 0.0
    closest = min(
        float(np.linalg.norm(a - b)) for index, a in enumerate(torsos) for b in torsos[index + 1 :]
    )
    in_shoulders = closest / reference
    return float(max(0.0, min(1.0, 1.0 - in_shoulders / shoulders)))


def extension_feature(people: list[np.ndarray]) -> float:
    """How far the most-extended arm reaches, in shoulder widths.

    Normalised so that 1.0 is an arm at roughly two shoulder widths, which is
    about full extension. Reaching for a bundle looks like this too -- hence one
    feature of three, and hence a human at the end.
    """
    best = 0.0
    for person in people:
        width = shoulder_width(person)
        if width <= 0:
            continue
        for shoulder_index, wrist_index in (
            (LEFT_SHOULDER, LEFT_WRIST),
            (RIGHT_SHOULDER, RIGHT_WRIST),
        ):
            shoulder, wrist = person[shoulder_index], person[wrist_index]
            if shoulder[2] < MIN_CONFIDENCE or wrist[2] < MIN_CONFIDENCE:
                continue
            reach = float(np.linalg.norm(wrist[:2] - shoulder[:2])) / width
            best = max(best, min(1.0, reach / 2.0))
    return best


def energy_feature(
    people: list[np.ndarray], previous: list[np.ndarray] | None, shoulders_px: float
) -> float:
    """Keypoint movement since the previous sample, in shoulder widths.

    Normalised the same way and clipped: a camera that drops frames would
    otherwise report a brawl every time it recovers.
    """
    if not previous or not people or shoulders_px <= 0:
        return 0.0
    count = min(len(people), len(previous))
    movements: list[float] = []
    for index in range(count):
        now, before = people[index], previous[index]
        usable = (now[:, 2] >= MIN_CONFIDENCE) & (before[:, 2] >= MIN_CONFIDENCE)
        if not usable.any():
            continue
        delta = np.linalg.norm(now[usable, :2] - before[usable, :2], axis=1)
        movements.append(float(np.mean(delta)))
    if not movements:
        return 0.0
    return float(min(1.0, max(movements) / shoulders_px))


def score_features(features: Features, cfg: ViolenceConfig) -> float:
    return float(
        min(
            1.0,
            cfg.proximity_weight * features.proximity
            + cfg.extension_weight * features.extension
            + cfg.energy_weight * features.energy,
        )
    )


class ViolenceTrigger:
    """Pose in, queue items out. One candidate per cooldown."""

    def __init__(self, camera_id: str, cfg: ViolenceConfig) -> None:
        self.camera_id = camera_id
        self.cfg = cfg
        self._previous: list[np.ndarray] | None = None
        self._last_candidate_at: datetime | None = None

    def features_for(self, people: list[np.ndarray]) -> Features:
        widths = [shoulder_width(person) for person in people]
        reference = max(widths) if widths else 0.0
        features = Features(
            proximity=proximity_feature(people, self.cfg.proximity_shoulders),
            extension=extension_feature(people),
            energy=energy_feature(people, self._previous, reference),
            people=len(people),
        )
        return features

    def observe(self, at: datetime, people: list[np.ndarray]) -> Candidate | None:
        features = self.features_for(people)
        self._previous = [person.copy() for person in people]
        score = score_features(features, self.cfg)
        if score < self.cfg.trigger_threshold:
            return None
        if self._last_candidate_at is not None and (at - self._last_candidate_at) < timedelta(
            seconds=self.cfg.cooldown_s
        ):
            # A scuffle is one queue item, not forty. The reviewer's attention is
            # the scarce resource here.
            return None
        self._last_candidate_at = at
        log.info(
            "violence trigger at %s score=%.2f features=%s",
            at.isoformat(),
            score,
            features.as_dict(),
        )
        return Candidate(at=at, score=score, features=features)

    def row(self, candidate: Candidate, clip_id: object | None) -> tuple:
        """Parameters for the violence_candidate insert.

        Note what is absent: no person, and no classifier score. There is no
        column for one (ADR-0012), so adding a classifier needs a migration and
        a reopened ADR rather than a quiet write.
        """
        import json

        return (
            uuid4(),
            self.camera_id,
            candidate.at,
            candidate.score,
            json.dumps(candidate.features.as_dict()),
            clip_id,
        )


INSERT_CANDIDATE = (
    "insert into violence_candidate (candidate_id, camera_id, at, trigger_score, features,"
    " clip_id) values (%s,%s,%s,%s,%s::jsonb,%s)"
)
