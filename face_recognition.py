import os
import importlib.util
import hashlib
from dataclasses import dataclass
from pathlib import Path


MODEL_NAME = "buffalo_l"
MODEL_VERSION = "buffalo_l-v1"
FACE_ATTRIBUTES_VERSION = 2
DEFAULT_DETECTION_SIZE = (640, 640)


class FaceRecognitionError(RuntimeError):
    pass


class FaceRecognitionUnavailable(FaceRecognitionError):
    pass


@dataclass(frozen=True)
class FaceDetection:
    bbox: tuple[float, float, float, float]
    detection_score: float
    embedding: tuple[float, ...]
    detected_sex: str = "ND"


def normalize_detected_sex(value):
    normalized = str(value or "ND").upper()
    return normalized if normalized in {"M", "F"} else "ND"


def normalized_embedding(values):
    values = tuple(float(value) for value in values)
    length = sum(value * value for value in values) ** 0.5
    if not values or length <= 0:
        raise ValueError("Face embedding is empty")
    return tuple(value / length for value in values)


def cosine_similarity(left, right):
    left = normalized_embedding(left)
    right = normalized_embedding(right)
    if len(left) != len(right):
        raise ValueError("Face embeddings have different dimensions")
    return sum(a * b for a, b in zip(left, right))


def rank_identities(embedding, references, rejected_identity_ids=None):
    """Return identity scores using the best individual reference prototype."""
    rejected_identity_ids = set(rejected_identity_ids or [])
    scores = {}
    for reference in references:
        identity_id = int(reference["identity_id"])
        if identity_id in rejected_identity_ids:
            continue
        score = cosine_similarity(embedding, reference["embedding"])
        scores[identity_id] = max(score, scores.get(identity_id, -1.0))
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


def classify_identity(embedding, identities, references, rejected_identity_ids=None):
    identity_by_id = {int(identity["id"]): identity for identity in identities if identity.get("enabled", True)}
    ranked = [
        (identity_id, score)
        for identity_id, score in rank_identities(embedding, references, rejected_identity_ids)
        if identity_id in identity_by_id
    ]
    if not ranked:
        return None
    identity_id, score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else None
    identity = identity_by_id[identity_id]
    review_threshold = float(identity.get("review_threshold", 0.40))
    automatic_threshold = float(identity.get("automatic_threshold", 0.55))
    margin_threshold = float(identity.get("margin_threshold", 0.08))
    margin = score - second_score if second_score is not None else None
    if score < review_threshold:
        return None
    automatic = score >= automatic_threshold and (margin is None or margin >= margin_threshold)
    return {
        "identity_id": identity_id,
        "score": score,
        "second_best_score": second_score,
        "state": "automatic" if automatic else "pending",
    }


def select_onnx_providers(available_providers):
    available = set(available_providers)
    providers = []
    if "CUDAExecutionProvider" in available:
        providers.append("CUDAExecutionProvider")
    if "CPUExecutionProvider" in available:
        providers.append("CPUExecutionProvider")
    if not providers:
        raise FaceRecognitionUnavailable("Aucun provider ONNX Runtime compatible n'est disponible")
    return providers


class InsightFaceEngine:
    """Lazy local InsightFace adapter. It never downloads a model implicitly."""

    model_name = MODEL_NAME

    def __init__(self, model_root=None, detection_size=DEFAULT_DETECTION_SIZE):
        default_root = Path(__file__).resolve().parent / "instance" / "face_models"
        self.model_root = Path(model_root or os.environ.get("FACE_MODEL_ROOT") or default_root)
        self.detection_size = tuple(detection_size)
        self._application = None
        self.provider = None

    @property
    def model_directory(self):
        return self.model_root / "models" / self.model_name

    @property
    def model_version(self):
        model_files = sorted(self.model_directory.glob("*.onnx")) if self.model_directory.is_dir() else []
        if not model_files:
            return MODEL_VERSION
        fingerprint = hashlib.sha256()
        for model_file in model_files:
            stat = model_file.stat()
            fingerprint.update(f"{model_file.name}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8"))
        return f"{self.model_name}-{fingerprint.hexdigest()[:12]}"

    def configuration(self):
        model_directory = self.model_directory
        onnx_files = list(model_directory.glob("*.onnx")) if model_directory.is_dir() else []
        dependencies_present = bool(importlib.util.find_spec("insightface") and importlib.util.find_spec("onnxruntime"))
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "model_root": str(self.model_root),
            "model_directory": str(model_directory),
            "model_present": len(onnx_files) >= 2,
            "gender_model_present": any(path.name.lower() == "genderage.onnx" for path in onnx_files),
            "dependencies_present": dependencies_present,
            "configured": len(onnx_files) >= 2 and dependencies_present,
            "provider": self.provider,
        }

    def _load(self):
        if self._application is not None:
            return self._application
        status = self.configuration()
        if not status["model_present"]:
            raise FaceRecognitionUnavailable(
                f"Modele InsightFace absent: placez buffalo_l dans {status['model_directory']} "
                "ou definissez FACE_MODEL_ROOT"
            )
        try:
            import onnxruntime
            from insightface.app import FaceAnalysis
        except ImportError as exc:
            raise FaceRecognitionUnavailable(
                "InsightFace/ONNX Runtime n'est pas installe. Installez insightface et onnxruntime-gpu "
                "(ou onnxruntime pour le CPU)."
            ) from exc

        providers = select_onnx_providers(onnxruntime.get_available_providers())
        try:
            application = FaceAnalysis(
                name=self.model_name,
                root=str(self.model_root),
                allowed_modules=["detection", "recognition", "genderage"],
                providers=providers,
            )
            application.prepare(ctx_id=0 if providers[0] == "CUDAExecutionProvider" else -1, det_size=self.detection_size)
        except Exception as exc:
            raise FaceRecognitionUnavailable(f"Impossible de charger InsightFace: {exc}") from exc
        self.provider = providers[0]
        self._application = application
        return application

    def analyze_path(self, image_path):
        try:
            import cv2
        except ImportError as exc:
            raise FaceRecognitionUnavailable("OpenCV (cv2) n'est pas installe") from exc
        image = cv2.imread(str(image_path))
        if image is None:
            raise FaceRecognitionError(f"Image illisible: {image_path}")
        return self.analyze_array(image)

    def analyze_array(self, image):
        application = self._load()
        try:
            faces = application.get(image)
        except Exception as exc:
            raise FaceRecognitionError(f"Echec de l'analyse faciale: {exc}") from exc
        detections = []
        for face in sorted(faces, key=lambda item: (float(item.bbox[0]), float(item.bbox[1]))):
            embedding = getattr(face, "normed_embedding", None)
            if embedding is None:
                continue
            bbox = tuple(float(value) for value in face.bbox[:4])
            detections.append(
                FaceDetection(
                    bbox=bbox,
                    detection_score=float(getattr(face, "det_score", 0.0)),
                    embedding=normalized_embedding(embedding),
                    detected_sex=normalize_detected_sex(getattr(face, "sex", None)),
                )
            )
        return detections
