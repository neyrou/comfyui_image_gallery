import csv
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path

from PIL import Image, ImageOps


FREEPIK_MODEL_ID = "Freepik/nsfw_image_detector"
WD_MODEL_ID = "SmilingWolf/wd-swinv2-tagger-v3"
NUDENET_MODEL_VERSION = "nudenet-3.4.2-320n"
TAG_THRESHOLD = 0.40
MAX_AUTOMATIC_TAGS = 500
NUDENET_THRESHOLD = 0.60
LEVELS = ("neutral", "low", "medium", "high")
LEVEL_RANK = {level: index for index, level in enumerate(LEVELS)}
HIGH_NUDENET_CLASSES = {
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
}
MEDIUM_NUDENET_CLASSES = {
    "FEMALE_BREAST_EXPOSED",
    "BUTTOCKS_EXPOSED",
}


class ImageAnalysisError(RuntimeError):
    pass


class ImageAnalysisUnavailable(ImageAnalysisError):
    pass


def freepik_level_from_scores(scores, threshold=0.50):
    normalized = {level: float(scores.get(level, 0.0)) for level in LEVELS}
    if normalized["high"] >= threshold:
        return "high"
    if normalized["medium"] + normalized["high"] >= threshold:
        return "medium"
    if normalized["low"] + normalized["medium"] + normalized["high"] >= threshold:
        return "low"
    return "neutral"


def nudenet_level(detections, threshold=NUDENET_THRESHOLD):
    level = "neutral"
    for detection in detections or []:
        if float(detection.get("score", 0.0)) < threshold:
            continue
        class_name = str(detection.get("class") or "").upper()
        if class_name in HIGH_NUDENET_CLASSES:
            return "high"
        if class_name in MEDIUM_NUDENET_CLASSES:
            level = "medium"
    return level


def maximum_level(*levels):
    return max(
        (level if level in LEVEL_RANK else "neutral" for level in levels),
        key=lambda level: LEVEL_RANK[level],
        default="neutral",
    )


def readable_tag_name(raw_name):
    return " ".join(str(raw_name or "").strip().replace("_", " ").split()).lower()


def filter_wd_tags(rows, probabilities, threshold=TAG_THRESHOLD):
    tags = []
    for row, probability in zip(rows, probabilities):
        try:
            category = int(row.get("category", -1))
            score = float(probability)
        except (TypeError, ValueError):
            continue
        raw_name = str(row.get("name") or "").strip()
        if category != 0 or not raw_name or score < threshold:
            continue
        tags.append(
            {
                "name": raw_name,
                "display_name": readable_tag_name(raw_name),
                "score": score,
            }
        )
    return sorted(tags, key=lambda item: (-item["score"], item["name"]))


def wd_probabilities_from_output(values):
    probabilities = [float(value) for value in values]
    if all(0.0 <= value <= 1.0 for value in probabilities):
        return probabilities
    return [
        1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, value))))
        for value in probabilities
    ]


class LocalImageAnalysisEngine:
    def __init__(self, model_root=None, hf_token=None):
        default_root = Path(__file__).resolve().parent / "instance" / "image_models"
        self.model_root = Path(model_root or os.environ.get("IMAGE_MODEL_ROOT") or default_root)
        self.hf_token = hf_token or os.environ.get("HF_TOKEN")
        self.cache_root = self.model_root / "huggingface"
        self._torch = None
        self._freepik_model = None
        self._freepik_transform = None
        self._nudenet = None
        self._wd_session = None
        self._wd_input_name = None
        self._wd_tags = None
        self.provider = None

    @property
    def analysis_signature(self):
        payload = {
            "schema": 2,
            "freepik": FREEPIK_MODEL_ID,
            "nudenet": NUDENET_MODEL_VERSION,
            "wd": WD_MODEL_ID,
            "tag_threshold": TAG_THRESHOLD,
            "max_automatic_tags": MAX_AUTOMATIC_TAGS,
            "nudenet_threshold": NUDENET_THRESHOLD,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]

    def configuration(self):
        required = ("torch", "transformers", "timm", "huggingface_hub", "onnxruntime", "nudenet")
        missing = [name for name in required if importlib.util.find_spec(name) is None]
        return {
            "configured": not missing,
            "missing_dependencies": missing,
            "model_root": str(self.model_root),
            "provider": self.provider,
            "analysis_signature": self.analysis_signature,
        }

    def _ensure_dependencies(self):
        status = self.configuration()
        if status["missing_dependencies"]:
            raise ImageAnalysisUnavailable(
                "Dépendances d'analyse absentes : "
                + ", ".join(status["missing_dependencies"])
                + ". Installez requirements-analysis.txt."
            )
        self.model_root.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def _load_freepik(self):
        if self._freepik_model is not None:
            return
        self._ensure_dependencies()
        try:
            import torch
            from timm.data import create_transform, resolve_data_config
            from timm.models import get_pretrained_cfg
            from transformers import AutoModelForImageClassification

            device = "cuda" if torch.cuda.is_available() else "cpu"
            dtype = torch.bfloat16 if device == "cuda" else torch.float32
            model = AutoModelForImageClassification.from_pretrained(
                FREEPIK_MODEL_ID,
                cache_dir=str(self.cache_root),
                token=self.hf_token,
                torch_dtype=dtype,
            ).to(device)
            model.eval()
            pretrained_cfg = get_pretrained_cfg("eva02_base_patch14_448.mim_in22k_ft_in22k_in1k")
            transform = create_transform(**resolve_data_config(pretrained_cfg.__dict__))
        except Exception as exc:
            raise ImageAnalysisUnavailable(
                f"Impossible de télécharger ou charger {FREEPIK_MODEL_ID}: {exc}"
            ) from exc
        self._torch = torch
        self._freepik_model = model
        self._freepik_transform = transform
        self.provider = "CUDA" if device == "cuda" else "CPU"

    def _load_nudenet(self):
        if self._nudenet is not None:
            return
        self._ensure_dependencies()
        try:
            from nudenet import NudeDetector

            self._nudenet = NudeDetector()
        except Exception as exc:
            raise ImageAnalysisUnavailable(f"Impossible de charger NudeNet: {exc}") from exc

    def _load_wd(self):
        if self._wd_session is not None:
            return
        self._ensure_dependencies()
        try:
            import onnxruntime
            from huggingface_hub import hf_hub_download

            model_path = hf_hub_download(
                WD_MODEL_ID,
                "model.onnx",
                cache_dir=str(self.cache_root),
                token=self.hf_token,
            )
            tags_path = hf_hub_download(
                WD_MODEL_ID,
                "selected_tags.csv",
                cache_dir=str(self.cache_root),
                token=self.hf_token,
            )
            available = set(onnxruntime.get_available_providers())
            providers = [
                provider
                for provider in ("CUDAExecutionProvider", "CPUExecutionProvider")
                if provider in available
            ]
            if not providers:
                raise RuntimeError("Aucun provider ONNX Runtime compatible")
            session = onnxruntime.InferenceSession(model_path, providers=providers)
            with open(tags_path, "r", encoding="utf-8") as source:
                tags = list(csv.DictReader(source))
            if not tags:
                raise RuntimeError("Le vocabulaire WD est vide")
        except Exception as exc:
            raise ImageAnalysisUnavailable(
                f"Impossible de télécharger ou charger {WD_MODEL_ID}: {exc}"
            ) from exc
        self._wd_session = session
        self._wd_input_name = session.get_inputs()[0].name
        self._wd_tags = tags
        if self.provider is None:
            self.provider = providers[0]

    def preload(self, progress_callback=None):
        loaders = (
            ("Chargement du classifieur Freepik", self._load_freepik),
            ("Chargement de NudeNet", self._load_nudenet),
            ("Chargement du tagger WD SwinV2", self._load_wd),
        )
        for message, loader in loaders:
            if progress_callback:
                progress_callback(message)
            loader()

    def _predict_freepik(self, image):
        self._load_freepik()
        inputs = self._freepik_transform(image.convert("RGB")).unsqueeze(0)
        device = next(self._freepik_model.parameters()).device
        inputs = inputs.to(device=device, dtype=next(self._freepik_model.parameters()).dtype)
        with self._torch.inference_mode():
            logits = self._freepik_model(inputs).logits[0]
            probabilities = self._torch.softmax(logits.float(), dim=-1).detach().cpu().tolist()
        labels = {
            int(index): str(label).lower()
            for index, label in self._freepik_model.config.id2label.items()
        }
        scores = {labels.get(index, str(index)): float(score) for index, score in enumerate(probabilities)}
        return {level: scores.get(level, 0.0) for level in LEVELS}

    def _predict_wd(self, image):
        self._load_wd()
        try:
            import numpy

            rgb = image.convert("RGB")
            size = max(rgb.size)
            padded = ImageOps.pad(rgb, (size, size), color=(255, 255, 255))
            resized = padded.resize((448, 448), Image.Resampling.BICUBIC)
            batch = numpy.asarray(resized, dtype=numpy.float32)[:, :, ::-1][None, ...]
            output = numpy.asarray(
                self._wd_session.run(None, {self._wd_input_name: batch})[0]
            ).reshape(-1)
            if len(output) != len(self._wd_tags):
                raise RuntimeError(
                    f"Sortie WD invalide : {len(output)} scores pour "
                    f"{len(self._wd_tags)} tags"
                )
            probabilities = wd_probabilities_from_output(output)
        except Exception as exc:
            raise ImageAnalysisError(f"Échec du tagger WD: {exc}") from exc
        tags = filter_wd_tags(self._wd_tags, probabilities)
        if len(tags) > MAX_AUTOMATIC_TAGS:
            raise ImageAnalysisError(
                f"Le tagger WD a retourné {len(tags)} tags, au-delà de la "
                f"limite de sécurité ({MAX_AUTOMATIC_TAGS}). "
                "Les résultats précédents sont conservés."
            )
        return tags

    def analyze_path(self, image_path):
        self.preload()
        try:
            with Image.open(image_path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                freepik_scores = self._predict_freepik(image)
                automatic_tags = self._predict_wd(image)
            detections = self._nudenet.detect(str(image_path))
        except ImageAnalysisError:
            raise
        except Exception as exc:
            raise ImageAnalysisError(f"Échec de l'analyse de {Path(image_path).name}: {exc}") from exc
        freepik_level = freepik_level_from_scores(freepik_scores)
        analysis_level = maximum_level(freepik_level, nudenet_level(detections))
        return {
            "analysis_level": analysis_level,
            "freepik_level": freepik_level,
            "freepik_scores": freepik_scores,
            "nudenet_detections": detections,
            "automatic_tags": automatic_tags,
            "models": {
                "freepik": FREEPIK_MODEL_ID,
                "nudenet": NUDENET_MODEL_VERSION,
                "tagger": WD_MODEL_ID,
            },
            "provider": self.provider,
        }
