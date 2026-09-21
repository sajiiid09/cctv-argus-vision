"""The ONNX wrappers, against synthetic sessions.

None of these artefacts is resolved (ADR-0030: no pinned url or sha256 yet), so
these tests prove the decoding arithmetic and the refusals -- not accuracy, and
not that the real graphs look like this. When an artefact does land, its
observed layout must be checked against these assumptions and the parity
tolerances re-measured (AGENTS.md §7).
"""

from __future__ import annotations

import numpy as np
import pytest
from argus.backends.arcface import ArcFaceEmbedder
from argus.backends.onnx_common import ModelArtefactError
from argus.backends.scrfd import ScrfdFaceDetector
from argus.backends.yolo import YoloOnnxDetector
from argus.backends.yolo_pose import ROW_WIDTH, YoloPoseOnnxEstimator


class _Spec:
    def __init__(self, name: str, shape: list) -> None:
        self.name = name
        self.shape = shape


class _Session:
    """Minimal stand-in for onnxruntime.InferenceSession."""

    def __init__(self, inputs: list[_Spec], outputs: list[_Spec], results: list) -> None:
        self._inputs = inputs
        self._outputs = outputs
        self._results = results
        self.last_tensor: np.ndarray | None = None

    def get_inputs(self) -> list[_Spec]:
        return self._inputs

    def get_outputs(self) -> list[_Spec]:
        return self._outputs

    def get_providers(self) -> list[str]:
        # The wrappers record what the session GOT, so the stand-in answers too.
        return ["CPUExecutionProvider"]

    def run(self, names, feed):  # mirrors the ort signature
        self.last_tensor = next(iter(feed.values()))
        return self._results


@pytest.fixture
def fake_session(monkeypatch):
    """Install a synthetic session in place of a hash-verified artefact."""
    holder: dict[str, _Session] = {}

    def install(session: _Session, model_ref: str = "fake@000000000000"):
        class _Ref:
            def __str__(self) -> str:
                return model_ref

        def loader(name: str, providers: list[str]):
            holder["session"] = session
            return session, _Ref()

        monkeypatch.setattr("argus.backends.onnx_model.load_onnx_session", loader)
        return session

    return install


def _frame(width: int = 320, height: int = 180) -> np.ndarray:
    return np.full((height, width, 3), 100, dtype=np.uint8)


# --- yolo -------------------------------------------------------------------


def test_yolo_end_to_end_layout_maps_boxes_back_to_source_pixels(fake_session) -> None:
    # One box covering the middle of a letterboxed 640x640 input.
    rows = np.array([[[160.0, 160.0, 480.0, 480.0, 0.9, 0.0]]], dtype=np.float32)
    fake_session(
        _Session(
            [_Spec("images", [1, 3, 640, 640])],
            [_Spec("output0", [1, 1, 6])],
            [rows],
        )
    )
    detector = YoloOnnxDetector(name="onnx-cpu", providers=["CPUExecutionProvider"])
    frame = _frame(640, 640)
    dets = detector.detect(frame, score_threshold=0.5)
    assert len(dets) == 1
    det = dets[0]
    assert det.label == "person" and det.score == pytest.approx(0.9)
    # square source, no padding, scale 1: coordinates pass through unchanged
    assert (det.box.x1, det.box.y1, det.box.x2, det.box.y2) == (160.0, 160.0, 480.0, 480.0)


def test_yolo_undoes_letterbox_padding(fake_session) -> None:
    """A box un-letterboxed with the wrong padding looks like a mediocre
    detector rather than like a bug, so the arithmetic is pinned here."""
    rows = np.array([[[320.0, 320.0, 480.0, 400.0, 0.8, 0.0]]], dtype=np.float32)
    fake_session(
        _Session([_Spec("images", [1, 3, 640, 640])], [_Spec("output0", [1, 1, 6])], [rows])
    )
    detector = YoloOnnxDetector(name="onnx-cpu", providers=["CPUExecutionProvider"])
    frame = _frame(640, 360)  # scale 1.0, pad_y = (640 - 360) / 2 = 140
    det = detector.detect(frame, score_threshold=0.5)[0]
    assert det.box.x1 == pytest.approx(320.0)
    assert det.box.y1 == pytest.approx(180.0)
    assert det.box.y2 == pytest.approx(260.0)


def test_yolo_raw_layout_applies_nms_and_thresholds(fake_session) -> None:
    # (1, 84, N): two near-identical boxes and one distant, all class 0
    table = np.zeros((84, 3), dtype=np.float32)
    table[:4, 0] = [100.0, 100.0, 40.0, 80.0]
    table[4, 0] = 0.9
    table[:4, 1] = [102.0, 101.0, 40.0, 80.0]
    table[4, 1] = 0.7
    table[:4, 2] = [400.0, 300.0, 40.0, 80.0]
    table[4, 2] = 0.6
    fake_session(
        _Session(
            [_Spec("images", [1, 3, 640, 640])],
            [_Spec("output0", [1, 84, 3])],
            [table[None]],
        )
    )
    detector = YoloOnnxDetector(name="onnx-cpu", providers=["CPUExecutionProvider"])
    dets = detector.detect(_frame(640, 640), score_threshold=0.5)
    # the 0.7 box overlaps the 0.9 one and is suppressed; the distant 0.6 survives
    assert [round(d.score, 2) for d in dets] == [0.9, 0.6]


def test_yolo_refuses_an_unexpected_graph(fake_session) -> None:
    fake_session(
        _Session(
            [_Spec("images", [1, 3, 640, 640])],
            [_Spec("output0", [1, 4])],
            [np.zeros((1, 4), dtype=np.float32)],
        )
    )
    with pytest.raises(ModelArtefactError, match="expected 3 dimensions"):
        YoloOnnxDetector(name="onnx-cpu", providers=["CPUExecutionProvider"])


def test_yolo_detections_are_sorted_deterministically(fake_session) -> None:
    rows = np.array(
        [
            [
                [10.0, 10.0, 20.0, 20.0, 0.7, 0.0],
                [30.0, 5.0, 40.0, 15.0, 0.7, 0.0],
                [50.0, 50.0, 60.0, 60.0, 0.95, 0.0],
            ]
        ],
        dtype=np.float32,
    )
    fake_session(
        _Session([_Spec("images", [1, 3, 640, 640])], [_Spec("output0", [1, 3, 6])], [rows])
    )
    detector = YoloOnnxDetector(name="onnx-cpu", providers=["CPUExecutionProvider"])
    dets = detector.detect(_frame(640, 640), score_threshold=0.5)
    # highest score first, then by y1 then x1 -- never "whatever the model emitted"
    assert [(d.score, d.box.y1) for d in dets] == [(0.95, 50.0), (0.7, 5.0), (0.7, 10.0)]


# --- pose -------------------------------------------------------------------


def test_pose_returns_keypoints_in_source_pixels(fake_session) -> None:
    table = np.zeros((1, ROW_WIDTH), dtype=np.float32)
    table[0, :5] = [320.0, 320.0, 100.0, 200.0, 0.9]
    table[0, 5::3] = 320.0  # every keypoint x
    table[0, 6::3] = 320.0  # every keypoint y
    table[0, 7::3] = 0.8
    fake_session(
        _Session(
            [_Spec("images", [1, 3, 640, 640])],
            [_Spec("output0", [1, 1, ROW_WIDTH])],
            [table[None]],
        )
    )
    pose = YoloPoseOnnxEstimator(name="onnx-cpu", providers=["CPUExecutionProvider"])
    people = pose.estimate(_frame(640, 360))
    assert len(people) == 1
    kpts = people[0]
    assert kpts.shape == (17, 3)
    assert kpts[0, 0] == pytest.approx(320.0)
    assert kpts[0, 1] == pytest.approx(180.0)  # 320 - pad_y 140


def test_pose_refuses_a_graph_without_seventeen_keypoints(fake_session) -> None:
    fake_session(
        _Session(
            [_Spec("images", [1, 3, 640, 640])],
            [_Spec("output0", [1, 40, 10])],
            [np.zeros((1, 40, 10), dtype=np.float32)],
        )
    )
    with pytest.raises(ModelArtefactError, match="expected 56"):
        YoloPoseOnnxEstimator(name="onnx-cpu", providers=["CPUExecutionProvider"])


# --- scrfd ------------------------------------------------------------------


def _scrfd_outputs(height: int = 640, width: int = 640) -> list[np.ndarray]:
    outs: list[np.ndarray] = []
    anchors = {}
    for stride in (8, 16, 32):
        anchors[stride] = (height // stride) * (width // stride) * 2
    for stride in (8, 16, 32):
        outs.append(np.zeros((anchors[stride], 1), dtype=np.float32))
    for stride in (8, 16, 32):
        outs.append(np.zeros((anchors[stride], 4), dtype=np.float32))
    for stride in (8, 16, 32):
        outs.append(np.zeros((anchors[stride], 10), dtype=np.float32))
    return outs


def test_scrfd_decodes_a_face_with_landmarks(fake_session) -> None:
    outs = _scrfd_outputs()
    # one hit on the stride-8 map, anchor index 0 (centre 0, 0)
    outs[0][0, 0] = 0.9
    outs[3][0] = [1.0, 1.0, 1.0, 1.0]  # distances, scaled by stride 8
    outs[6][0] = [1.0, 1.0, 2.0, 1.0, 1.5, 1.5, 1.0, 2.0, 2.0, 2.0]
    fake_session(
        _Session(
            [_Spec("input.1", [1, 3, 640, 640])],
            [_Spec(f"out{i}", list(o.shape)) for i, o in enumerate(outs)],
            outs,
        )
    )
    detector = ScrfdFaceDetector(name="onnx-cpu", providers=["CPUExecutionProvider"])
    faces = detector.detect_faces(_frame(640, 640), score_threshold=0.5)
    assert len(faces) == 1
    face = faces[0]
    assert face.landmarks.shape == (5, 2)
    assert (face.box.x1, face.box.y1, face.box.x2, face.box.y2) == (-8.0, -8.0, 8.0, 8.0)
    assert face.landmarks[0].tolist() == [8.0, 8.0]


def test_scrfd_refuses_an_export_without_landmarks(fake_session) -> None:
    outs = _scrfd_outputs()[:6]
    fake_session(
        _Session(
            [_Spec("input.1", [1, 3, 640, 640])],
            [_Spec(f"out{i}", list(o.shape)) for i, o in enumerate(outs)],
            outs,
        )
    )
    with pytest.raises(ModelArtefactError, match="no landmarks"):
        ScrfdFaceDetector(name="onnx-cpu", providers=["CPUExecutionProvider"])


# --- arcface ----------------------------------------------------------------


def test_arcface_normalises_and_refuses_an_unaligned_crop(fake_session) -> None:
    embedding = np.arange(512, dtype=np.float32)[None] + 1.0
    fake_session(
        _Session(
            [_Spec("data", [1, 3, 112, 112])],
            [_Spec("fc1", [1, 512])],
            [embedding],
        )
    )
    embedder = ArcFaceEmbedder(name="onnx-cpu", providers=["CPUExecutionProvider"])
    assert embedder.dim == 512
    vector = embedder.embed(np.full((112, 112, 3), 120, dtype=np.uint8))
    assert vector.shape == (512,)
    assert float(np.linalg.norm(vector)) == pytest.approx(1.0, abs=1e-6)
    with pytest.raises(ValueError, match="align_face"):
        embedder.embed(np.full((96, 96, 3), 120, dtype=np.uint8))


def test_arcface_refuses_a_graph_with_the_wrong_input_size(fake_session) -> None:
    fake_session(
        _Session(
            [_Spec("data", [1, 3, 224, 224])],
            [_Spec("fc1", [1, 512])],
            [np.zeros((1, 512), dtype=np.float32)],
        )
    )
    with pytest.raises(ModelArtefactError, match="expected 112x112"):
        ArcFaceEmbedder(name="onnx-cpu", providers=["CPUExecutionProvider"])
