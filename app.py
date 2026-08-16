import io
import json
import threading
import time
import traceback
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from flask import Flask, Response, jsonify, render_template, request, send_file
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename

from comfy_generation import (
    CURRENT_WORKFLOW_ID,
    ComfyClient,
    ComfyGenerationCancelled,
    ComfyGenerationError,
    ComfyPromptUnavailable,
    ComfyUnavailable,
    build_edit_options,
    build_registered_edit_options,
    comfy_node_title,
    extract_history_filenames,
    get_registered_workflow,
    list_registered_workflows,
    list_lora_catalog,
    patch_prompt_and_workflow,
)
from gallery_db import (
    ALLOWED_ALBUM_TYPES,
    ALLOWED_LINK_TYPES,
    SENSITIVITY_LEVELS,
    ScanCancelled,
    TagCategoryLocked,
    connect_db,
    create_lora_tag_mapping,
    create_photo_link,
    delete_photo,
    delete_lora_tag_mapping,
    discover_albums,
    ensure_preview,
    ensure_thumbnail,
    find_photo_file,
    find_photo_file_in_album,
    get_photo_detail,
    get_album_by_name,
    import_photo_into_album,
    import_output_photo,
    init_db,
    is_album_path_available,
    list_album_tag_facets,
    list_albums,
    list_gallery_photos,
    list_lora_tag_mappings,
    list_tag_stats,
    list_tags,
    output_paths_from_history,
    rescan_metadata,
    replace_photo_image_analysis,
    scan_albums,
    search_photos,
    set_photo_tags,
    photo_image_analysis_cache_valid,
    update_lora_tag_mapping,
    update_tag_settings,
    update_photo_tags,
    update_album,
    add_gallery_face_reference,
    add_imported_face_reference,
    create_face_identity,
    create_face_import,
    create_face_scan_job,
    decide_face_match,
    delete_face_identity,
    get_current_face_scan_job,
    get_face_identity,
    get_face_import,
    get_face_reference,
    get_face_scan_job,
    get_face_setting,
    list_face_identities,
    photo_face_cache_valid,
    photo_ids_for_face_scope,
    rematch_photo_faces,
    replace_photo_faces,
    set_face_setting,
    update_face_identity,
)
from face_recognition import FaceRecognitionError, FaceRecognitionUnavailable, InsightFaceEngine
from image_analysis import ImageAnalysisError, ImageAnalysisUnavailable, LocalImageAnalysisEngine


BASE_DIR = Path(__file__).resolve().parent
IMAGES_ROOT = BASE_DIR / "static" / "images"
THUMBNAIL_ROOT = BASE_DIR / "static" / "thumbnails"
PREVIEW_ROOT = BASE_DIR / "static" / "previews"
COMFY_WORKFLOW_ROOT = BASE_DIR / "comfyui-workflows"
DB_PATH = BASE_DIR / "instance" / "gallery.sqlite3"
FACE_REFERENCE_ROOT = BASE_DIR / "instance" / "face_references"
PER_PAGE = 100

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
COMFY_CLIENT_FACTORY = ComfyClient
FACE_ENGINE_FACTORY = InsightFaceEngine
IMAGE_ANALYSIS_ENGINE_FACTORY = LocalImageAnalysisEngine
SCAN_LOCK = threading.Lock()
COMFY_JOB_LOCK = threading.Lock()
FACE_WORKER_LOCK = threading.Lock()
FACE_ENGINE_LOCK = threading.Lock()
COMFY_JOBS = {}
SCAN_QUEUE = []
FACE_WORKER_THREAD = None
FACE_ENGINE_INSTANCE = None
IMAGE_ANALYSIS_ENGINE_INSTANCE = None
IMAGE_ANALYSIS_ENGINE_LOCK = threading.Lock()
IMAGE_ANALYSIS_RUN_LOCK = threading.Lock()
PREVIEW_LOCKS_GUARD = threading.Lock()
PREVIEW_LOCKS = {}
FACE_RECOVERED_DATABASES = set()
SCAN_STATUS = {
    "active": False,
    "job_id": None,
    "state": "idle",
    "stage": None,
    "message": "Aucun scan en cours",
    "album": None,
    "file": None,
    "photos": 0,
    "album_photos": 0,
    "processed": 0,
    "skipped": 0,
    "errors": [],
    "options": {},
    "cancel_requested": False,
    "face_job_id": None,
    "face_job": None,
    "metadata_total": 0,
    "metadata_processed": 0,
    "metadata_skipped": 0,
    "metadata_errors": 0,
    "analysis_total": 0,
    "analysis_processed": 0,
    "analysis_skipped": 0,
    "analysis_errors": 0,
    "face_total": 0,
    "face_skipped": 0,
    "started_at": None,
    "updated_at": None,
    "finished_at": None,
    "summary": None,
    "origin": "manual",
}


def ensure_ready():
    init_db(DB_PATH)
    with connect_db(DB_PATH) as conn:
        discover_albums(conn, IMAGES_ROOT)
    recover_face_jobs()


def get_comfy_client():
    return COMFY_CLIENT_FACTORY()


def get_face_engine():
    global FACE_ENGINE_INSTANCE
    with FACE_ENGINE_LOCK:
        if FACE_ENGINE_INSTANCE is None:
            FACE_ENGINE_INSTANCE = FACE_ENGINE_FACTORY()
        return FACE_ENGINE_INSTANCE


def get_image_analysis_engine():
    global IMAGE_ANALYSIS_ENGINE_INSTANCE
    with IMAGE_ANALYSIS_ENGINE_LOCK:
        if IMAGE_ANALYSIS_ENGINE_INSTANCE is None:
            IMAGE_ANALYSIS_ENGINE_INSTANCE = IMAGE_ANALYSIS_ENGINE_FACTORY()
        return IMAGE_ANALYSIS_ENGINE_INSTANCE


def get_preview_lock(checksum):
    with PREVIEW_LOCKS_GUARD:
        return PREVIEW_LOCKS.setdefault(checksum, threading.Lock())


@app.after_request
def apply_gallery_image_cache_headers(response):
    cacheable_status = response.status_code in {200, 206, 304}
    if cacheable_status and request.path.startswith("/static/images/"):
        response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    elif cacheable_status and request.path.startswith("/static/thumbnails/"):
        # Thumbnails can be refreshed manually without changing the source checksum.
        response.headers["Cache-Control"] = "private, max-age=3600"
    return response


def recover_face_jobs():
    database_key = str(Path(DB_PATH).resolve())
    with FACE_WORKER_LOCK:
        if database_key in FACE_RECOVERED_DATABASES:
            return
        with connect_db(DB_PATH) as conn:
            conn.execute(
                "UPDATE face_scan_jobs SET state='queued', message='Reprise apres redemarrage' WHERE state='running'"
            )
            conn.execute(
                "UPDATE face_scan_jobs SET state='cancelled', message='Analyse annulee', finished_at=CURRENT_TIMESTAMP WHERE state='cancel_requested'"
            )
            conn.execute("UPDATE face_scan_items SET state='queued' WHERE state='running'")
            queued = conn.execute("SELECT 1 FROM face_scan_jobs WHERE state='queued' LIMIT 1").fetchone()
        FACE_RECOVERED_DATABASES.add(database_key)
    if queued:
        start_face_worker()


def start_face_worker():
    global FACE_WORKER_THREAD
    with FACE_WORKER_LOCK:
        if FACE_WORKER_THREAD and FACE_WORKER_THREAD.is_alive():
            return FACE_WORKER_THREAD
        FACE_WORKER_THREAD = threading.Thread(target=run_face_job_queue, daemon=True, name="face-recognition")
        FACE_WORKER_THREAD.start()
        return FACE_WORKER_THREAD


def run_face_job_queue():
    global FACE_WORKER_THREAD
    try:
        while True:
            with connect_db(DB_PATH) as conn:
                row = conn.execute(
                    "SELECT id FROM face_scan_jobs WHERE state='queued' ORDER BY created_at LIMIT 1"
                ).fetchone()
            if not row:
                return
            run_face_job(row["id"])
    finally:
        with FACE_WORKER_LOCK:
            if FACE_WORKER_THREAD is threading.current_thread():
                FACE_WORKER_THREAD = None
        with connect_db(DB_PATH) as conn:
            queued = conn.execute("SELECT 1 FROM face_scan_jobs WHERE state='queued' LIMIT 1").fetchone()
        if queued:
            start_face_worker()


def run_face_job(job_id):
    with connect_db(DB_PATH) as conn:
        job = get_face_scan_job(conn, job_id)
        if not job or job["state"] not in {"queued", "running"}:
            return job
        conn.execute(
            """
            UPDATE face_scan_jobs
            SET state='running', message='Chargement du modele', started_at=COALESCE(started_at, CURRENT_TIMESTAMP),
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (job_id,),
        )
    engine = None
    if job["mode"] == "detect":
        try:
            engine = get_face_engine()
            # Force dependency/model validation before consuming job items.
            engine._load()
        except (FaceRecognitionUnavailable, FaceRecognitionError) as exc:
            with connect_db(DB_PATH) as conn:
                conn.execute(
                    """
                    UPDATE face_scan_jobs SET state='error', message=?, error=?, finished_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP WHERE id=?
                    """,
                    (str(exc), str(exc), job_id),
                )
                return get_face_scan_job(conn, job_id)

    while True:
        with connect_db(DB_PATH) as conn:
            job = get_face_scan_job(conn, job_id)
            if not job:
                return None
            if job["state"] == "cancel_requested":
                conn.execute(
                    "UPDATE face_scan_jobs SET state='cancelled', message='Analyse annulee', finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (job_id,),
                )
                return get_face_scan_job(conn, job_id)
            if job["state"] not in {"queued", "running"}:
                return job
            item = conn.execute(
                "SELECT photo_id FROM face_scan_items WHERE job_id=? AND state='queued' ORDER BY photo_id LIMIT 1",
                (job_id,),
            ).fetchone()
            if not item:
                conn.execute(
                    "UPDATE face_scan_jobs SET state='done', message='Analyse terminee', finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (job_id,),
                )
                return get_face_scan_job(conn, job_id)
            photo_id = item["photo_id"]
            conn.execute(
                "UPDATE face_scan_items SET state='running', updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND photo_id=?",
                (job_id, photo_id),
            )
            conn.execute(
                "UPDATE face_scan_jobs SET message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (f"Photo {photo_id}", job_id),
            )

        try:
            with connect_db(DB_PATH) as conn:
                photo = conn.execute("SELECT id, checksum FROM photos WHERE id=?", (photo_id,)).fetchone()
                if not photo:
                    raise ValueError("Photo not found")
                mode = job["mode"]
                image_path = find_photo_file(conn, photo_id) if mode == "detect" else None
                if mode == "detect" and not image_path:
                    raise ValueError("No file found for this photo")
                cached = mode == "match" or (
                    not job["params"].get("force", False)
                    and photo_face_cache_valid(
                        conn, photo_id, photo["checksum"], engine.model_name, engine.model_version
                    )
                )
            if mode == "detect" and not cached:
                detections = engine.analyze_path(image_path)
                with connect_db(DB_PATH) as conn:
                    replace_photo_faces(
                        conn,
                        photo_id,
                        photo["checksum"],
                        detections,
                        engine.model_name,
                        engine.model_version,
                        engine.provider,
                    )
            with connect_db(DB_PATH) as conn:
                summary = rematch_photo_faces(conn, photo_id)
                conn.execute(
                    "UPDATE face_scan_items SET state='done', error=NULL, updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND photo_id=?",
                    (job_id, photo_id),
                )
                conn.execute(
                    """
                    UPDATE face_scan_jobs
                    SET processed=processed+1, recognized=recognized+?, pending=pending+?, updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (summary["recognized"], summary["pending"], job_id),
                )
        except Exception as exc:
            print(f"[face] job {job_id}, photo {photo_id}: {exc}", flush=True)
            with connect_db(DB_PATH) as conn:
                conn.execute(
                    "UPDATE face_scan_items SET state='error', error=?, updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND photo_id=?",
                    (str(exc), job_id, photo_id),
                )
                conn.execute(
                    """
                    UPDATE face_scan_jobs SET processed=processed+1, errors_count=errors_count+1,
                        message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
                    """,
                    (f"Erreur photo {photo_id}: {exc}", job_id),
                )


def enqueue_face_job(scope, photo_ids, mode="detect", params=None, sync=False):
    job_id = str(uuid4())
    with connect_db(DB_PATH) as conn:
        job = create_face_scan_job(conn, job_id, scope, photo_ids, mode=mode, params=params)
    if sync:
        return run_face_job(job_id)
    start_face_worker()
    return job


def request_face_job_cancel(job_id):
    with connect_db(DB_PATH) as conn:
        job = get_face_scan_job(conn, job_id)
        if not job:
            return None
        conn.execute(
            """
            UPDATE face_scan_jobs
            SET state=CASE state
                    WHEN 'queued' THEN 'cancelled'
                    WHEN 'running' THEN 'cancel_requested'
                    ELSE state
                END,
                message=CASE state
                    WHEN 'queued' THEN 'Analyse annulee'
                    WHEN 'running' THEN 'Annulation demandee'
                    ELSE message
                END,
                finished_at=CASE WHEN state='queued' THEN CURRENT_TIMESTAMP ELSE finished_at END,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND state IN ('queued', 'running')
            """,
            (job_id,),
        )
        return get_face_scan_job(conn, job_id)


def enqueue_rematch_all(sync=False):
    with connect_db(DB_PATH) as conn:
        photo_ids = photo_ids_for_face_scope(conn, "rematch", mode="match")
    if not photo_ids:
        return None
    return enqueue_face_job("rematch", photo_ids, mode="match", sync=sync)


def enqueue_automatic_face_scan(params=None):
    with connect_db(DB_PATH) as conn:
        if not get_face_setting(conn, "automatic_scan", False):
            return None
        engine = get_face_engine()
        rows = conn.execute(
            """
            SELECT p.id
            FROM photos p
            JOIN album_photos ap ON ap.photo_id=p.id AND ap.is_missing=0
            LEFT JOIN face_photo_scans fps ON fps.photo_id=p.id
                AND fps.checksum=p.checksum AND fps.model_name=? AND fps.model_version=?
            WHERE fps.photo_id IS NULL
            GROUP BY p.id
            ORDER BY p.id
            """,
            (engine.model_name, engine.model_version),
        ).fetchall()
    photo_ids = [row["id"] for row in rows]
    return enqueue_face_job("automatic", photo_ids, mode="detect", params=params) if photo_ids else None


class ComfyJobAlreadyActive(RuntimeError):
    def __init__(self, job):
        super().__init__("Une generation est deja en cours")
        self.job = job


def _comfy_job_snapshot(job):
    snapshot = {key: value for key, value in job.items() if key != "preview"}
    snapshot["preview_available"] = bool(job.get("preview"))
    return deepcopy(snapshot)


def create_comfy_job(photo_id, payload):
    job_id = str(uuid4())
    job = {
        "id": job_id,
        "photo_id": photo_id,
        "active": True,
        "state": "preparing",
        "message": "Preparation",
        "prompt_id": None,
        "node": None,
        "node_title": None,
        "progress": None,
        "progress_max": None,
        "seed": None,
        "output_filenames": [],
        "workflow_id": str(payload.get("workflow_id") or CURRENT_WORKFLOW_ID),
        "output_kind": None,
        "photo": None,
        "error": None,
        "cancel_requested": False,
        "preview": None,
        "preview_updated_at": None,
        "started_at": time.time(),
        "updated_at": time.time(),
        "finished_at": None,
    }
    with COMFY_JOB_LOCK:
        active_job = next((item for item in COMFY_JOBS.values() if item.get("active")), None)
        if active_job:
            raise ComfyJobAlreadyActive(_comfy_job_snapshot(active_job))
        COMFY_JOBS[job_id] = job
    thread = threading.Thread(target=run_comfy_job, args=(job_id, photo_id, payload), daemon=True)
    thread.start()
    return comfy_job_snapshot(job_id)


def update_comfy_job(job_id, **updates):
    with COMFY_JOB_LOCK:
        job = COMFY_JOBS.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = time.time()


def comfy_job_snapshot(job_id):
    with COMFY_JOB_LOCK:
        job = COMFY_JOBS.get(job_id)
        if not job:
            return None
        return _comfy_job_snapshot(job)


def current_comfy_job_snapshot():
    with COMFY_JOB_LOCK:
        active_jobs = [job for job in COMFY_JOBS.values() if job.get("active")]
        if not active_jobs:
            return None
        job = max(active_jobs, key=lambda item: item.get("started_at") or 0)
        return _comfy_job_snapshot(job)


def comfy_job_cancel_requested(job_id):
    with COMFY_JOB_LOCK:
        job = COMFY_JOBS.get(job_id)
        return bool(job and job.get("cancel_requested"))


def request_comfy_job_cancel(job_id):
    with COMFY_JOB_LOCK:
        job = COMFY_JOBS.get(job_id)
        if not job:
            return None
        if job.get("active") and not job.get("cancel_requested"):
            job.update(
                cancel_requested=True,
                state="cancel_requested",
                message="Annulation demandee",
                updated_at=time.time(),
            )
        return _comfy_job_snapshot(job)


def raise_if_comfy_job_cancelled(job_id):
    if comfy_job_cancel_requested(job_id):
        raise ComfyGenerationCancelled("Generation annulee")


def complete_comfy_job(job_id, **updates):
    with COMFY_JOB_LOCK:
        job = COMFY_JOBS.get(job_id)
        if not job:
            return
        if job.get("cancel_requested"):
            raise ComfyGenerationCancelled("Generation annulee")
        job.update(active=False, state="done", finished_at=time.time(), **updates)
        job["updated_at"] = time.time()


def comfy_job_preview(job_id):
    with COMFY_JOB_LOCK:
        job = COMFY_JOBS.get(job_id)
        if not job:
            return None, None
        preview = job.get("preview")
        updated_at = job.get("preview_updated_at")
    return preview, updated_at


def run_comfy_job(job_id, photo_id, payload):
    client = get_comfy_client()
    try:
        uploaded_images = {}
        patched_payload = deepcopy(payload)
        raise_if_comfy_job_cancelled(job_id)
        update_comfy_job(job_id, state="preparing", message="Preparation du workflow")
        with connect_db(DB_PATH) as conn:
            detail = get_photo_detail(conn, photo_id)
            if not detail:
                raise ValueError("Photo not found")
            workflow_id = str(patched_payload.get("workflow_id") or CURRENT_WORKFLOW_ID)
            registered = get_registered_workflow(workflow_id, COMFY_WORKFLOW_ROOT)
            source_filename = validated_source_filename(detail, patched_payload.get("source_filename"))
            source_image_name = None
            if registered and registered["config"]["mode"] in {"i2i", "i2v"}:
                if detail.get("media_type") != "image":
                    raise ValueError("Le workflow selectionne requiert une image source")
                source_path = find_photo_file(conn, photo_id)
                if not source_path:
                    raise ValueError("Image source introuvable")
                source_image_name = client.upload_image(source_path)
                raise_if_comfy_job_cancelled(job_id)
            for reference in patched_payload.get("references") or []:
                raise_if_comfy_job_cancelled(job_id)
                node_id = str(reference.get("node_id") or "")
                target_photo_id = reference.get("photo_id")
                if not target_photo_id:
                    continue
                image_path = find_photo_file(conn, int(target_photo_id))
                if not image_path:
                    raise ValueError(f"Reference photo {target_photo_id} not found")
                input_name = client.upload_image(image_path)
                raise_if_comfy_job_cancelled(job_id)
                if "reference_id" in reference or "enabled" in reference:
                    reference["input_name"] = input_name
                elif node_id:
                    uploaded_images[node_id] = input_name
            prompt, workflow, patch_info = patch_prompt_and_workflow(
                detail,
                patched_payload,
                uploaded_images=uploaded_images,
                source_image_name=source_image_name,
                source_filename=source_filename,
                workflow_root=COMFY_WORKFLOW_ROOT,
            )

        raise_if_comfy_job_cancelled(job_id)
        update_comfy_job(job_id, state="queued", message="Envoi a ComfyUI", seed=patch_info.get("seed"))
        preview_nodes = {str(node_id) for node_id in patch_info.get("preview_nodes") or []}
        active_node = None

        def on_progress(progress):
            nonlocal active_node
            if comfy_job_cancel_requested(job_id):
                if "prompt_id" in progress:
                    update_comfy_job(job_id, prompt_id=progress.get("prompt_id"))
                return
            if progress.get("preview"):
                if preview_nodes and str(active_node) not in preview_nodes:
                    return
                update_comfy_job(job_id, preview=progress["preview"], preview_updated_at=time.time())
                return
            updates = {}
            if progress.get("state"):
                updates["state"] = progress["state"]
            if "prompt_id" in progress:
                updates["prompt_id"] = progress.get("prompt_id")
            if "node" in progress:
                node_id = progress.get("node")
                active_node = node_id
                updates["node"] = node_id
                updates["node_title"] = comfy_node_title(prompt, workflow, node_id)
            if "value" in progress or "max" in progress:
                updates["progress"] = progress.get("value")
                updates["progress_max"] = progress.get("max")
            if updates:
                message = "Generation en cours" if updates.get("state") == "running" else None
                if message:
                    updates["message"] = message
                update_comfy_job(job_id, **updates)

        prompt_id, history = client.run_prompt(
            prompt,
            workflow,
            job_id,
            progress_callback=on_progress,
            cancel_callback=lambda: comfy_job_cancel_requested(job_id),
        )
        raise_if_comfy_job_cancelled(job_id)
        output_filenames = extract_history_filenames(
            history,
            output_node=patch_info.get("output_node"),
            output_kind=patch_info.get("output_kind"),
        )
        output_label = "video" if patch_info.get("output_kind") == "video" else "image"
        update_comfy_job(
            job_id,
            state="importing",
            message=f"Import de {output_label} generee",
            prompt_id=prompt_id,
            node=None,
            node_title=None,
            progress=None,
            progress_max=None,
            output_filenames=output_filenames,
        )

        generated_photo = None
        generated_photos = []
        raise_if_comfy_job_cancelled(job_id)
        with connect_db(DB_PATH) as conn:
            output_paths = output_paths_from_history(conn, output_filenames)
            if not output_paths:
                raise ComfyGenerationError("Generated output file was not found in the output album")
            for output_path in output_paths:
                raise_if_comfy_job_cancelled(job_id)
                generated_photo_id = import_output_photo(conn, output_path, THUMBNAIL_ROOT)
                if generated_photo_id and generated_photo_id != photo_id:
                    create_photo_link(conn, photo_id, generated_photo_id, "variant")
                    generated_photo = get_photo_detail(conn, generated_photo_id)
                    generated_photos.append(generated_photo)
            raise_if_comfy_job_cancelled(job_id)
            complete_comfy_job(
                job_id,
                message="Video generee" if patch_info.get("output_kind") == "video" else "Image generee",
                prompt_id=prompt_id,
                photo=generated_photo,
                photos=generated_photos,
                workflow_id=patch_info.get("workflow_id"),
                output_kind=patch_info.get("output_kind"),
            )
    except ComfyGenerationCancelled:
        update_comfy_job(
            job_id,
            active=False,
            state="cancelled",
            message="Generation annulee",
            error=None,
            finished_at=time.time(),
        )
    except Exception as exc:
        traceback.print_exc()
        update_comfy_job(
            job_id,
            active=False,
            state="error",
            message=str(exc),
            error=str(exc),
            finished_at=time.time(),
        )


def validated_source_filename(detail, requested_filename=None):
    memberships = detail.get("memberships") or []
    requested_filename = str(requested_filename or "").strip()
    if requested_filename:
        for membership in memberships:
            if membership.get("filename") == requested_filename:
                return requested_filename
    for membership in memberships:
        if membership.get("available") and membership.get("filename"):
            return membership["filename"]
    return next((item.get("filename") for item in memberships if item.get("filename")), "image.png")


def selected_album_name(albums, requested):
    if requested and any(album["name"] == requested for album in albums):
        return requested
    if any(album["name"] == "output" for album in albums):
        return "output"
    return albums[0]["name"] if albums else None


def normalized_query_tag_names(values):
    return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))


def scan_payload_boolean(payload, key, default=False):
    if key not in payload:
        return default
    value = payload[key]
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def normalized_batch_photo_ids(payload):
    values = payload.get("photo_ids")
    if not isinstance(values, list) or not values:
        raise ValueError("photo_ids must be a non-empty list")
    photo_ids = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("photo_ids must contain positive integers")
        if value not in photo_ids:
            photo_ids.append(value)
    if len(photo_ids) > 100:
        raise ValueError("A maximum of 100 photos can be processed at once")
    return photo_ids


def missing_photo_ids(conn, photo_ids):
    if not photo_ids:
        return []
    placeholders = ",".join("?" for _ in photo_ids)
    existing = {
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM photos WHERE id IN ({placeholders})",
            photo_ids,
        ).fetchall()
    }
    return [photo_id for photo_id in photo_ids if photo_id not in existing]


def normalize_scan_options(payload):
    album_name = payload.get("album")
    if album_name is not None and (not isinstance(album_name, str) or not album_name):
        raise ValueError("album must be a non-empty string")

    explicit_scope = payload.get("scope")
    scope = explicit_scope or ("current" if album_name else "all")
    if scope not in {"current", "all", "selection"}:
        raise ValueError("scope must be current, all, or selection")
    if scope == "current" and not album_name:
        raise ValueError("album is required for the current scope")
    if scope in {"all", "selection"} and album_name is not None:
        raise ValueError(f"album is not allowed for the {scope} scope")

    if "scan_mode" in payload:
        scan_mode = payload["scan_mode"]
        if scan_mode not in {"incremental", "missing", "full"}:
            raise ValueError("scan_mode must be incremental, missing, or full")
    else:
        scan_mode = "full" if scan_payload_boolean(payload, "rescan_existing", True) else "incremental"

    photo_ids = normalized_batch_photo_ids(payload) if scope == "selection" else []
    if scope != "selection" and "photo_ids" in payload:
        raise ValueError("photo_ids is only allowed for the selection scope")
    if scope == "selection" and scan_mode == "incremental":
        raise ValueError("selection scope does not support incremental scan mode")

    options = {
        "scope": scope,
        "album": album_name,
        "photo_ids": photo_ids,
        "scan_mode": scan_mode,
        "rescan_existing": scan_mode == "full",
        "metadata": scan_payload_boolean(payload, "metadata", False),
        "face_recognition": scan_payload_boolean(payload, "face_recognition", False),
        "force_face_rescan": scan_payload_boolean(payload, "force_face_rescan", False),
        "image_analysis": scan_payload_boolean(payload, "image_analysis", False),
    }
    if not options["face_recognition"] or scan_mode != "full":
        options["force_face_rescan"] = False
    return options


def validate_scan_selection(options):
    if options["scope"] != "selection":
        return
    init_db(DB_PATH)
    with connect_db(DB_PATH) as conn:
        missing = missing_photo_ids(conn, options["photo_ids"])
    if missing:
        raise ValueError(f"Unknown photo ids: {', '.join(str(photo_id) for photo_id in missing)}")


def photo_membership_summary(conn, photo_id):
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT ap.album_id) AS album_count,
               COUNT(DISTINCT CASE WHEN a.type='user' THEN ap.album_id END) AS user_album_count,
               MAX(CASE WHEN a.type='output' THEN 1 ELSE 0 END) AS has_output,
               MAX(CASE WHEN a.type='user' THEN 1 ELSE 0 END) AS has_user
        FROM album_photos ap
        JOIN albums a ON a.id=ap.album_id
        WHERE ap.photo_id=? AND ap.is_missing=0
        """,
        (photo_id,),
    ).fetchone()
    return {
        "photo_id": photo_id,
        "album_count": row["album_count"],
        "user_album_count": row["user_album_count"],
        "favorite": bool(row["has_output"] and row["has_user"]),
    }


def gallery_page_url(
    album_name,
    page,
    include_tags=None,
    exclude_tags=None,
    max_sensitivity="neutral",
):
    params = [
        ("album", album_name),
        ("page", page),
        ("max_sensitivity", max_sensitivity),
    ]
    params.extend(("include_tag", tag_name) for tag_name in include_tags or [])
    params.extend(("exclude_tag", tag_name) for tag_name in exclude_tags or [])
    return f"?{urlencode(params)}"


def scan_status_snapshot():
    with SCAN_LOCK:
        return _scan_status_snapshot_locked()


def _scan_status_snapshot_locked():
    snapshot = deepcopy(SCAN_STATUS)
    snapshot["queued_count"] = len(SCAN_QUEUE)
    return snapshot


def scan_start_message(options):
    if options["scope"] == "selection":
        return f"Scan de la selection ({len(options['photo_ids'])} images) demarre"
    if options["scope"] == "current":
        return f"Scan de l'album {options['album']} demarre"
    return "Scan de tous les albums demarre"


def activate_scan_job_locked(entry):
    now = time.time()
    options = entry["options"]
    SCAN_STATUS.update(
        {
            "active": True,
            "job_id": entry["job_id"],
            "state": "running",
            "stage": "scan",
            "message": scan_start_message(options),
            "album": options["album"],
            "file": None,
            "photos": 0,
            "album_photos": 0,
            "processed": 0,
            "skipped": 0,
            "errors": [],
            "options": deepcopy(options),
            "cancel_requested": False,
            "face_job_id": None,
            "face_job": None,
            "metadata_total": 0,
            "metadata_processed": 0,
            "metadata_skipped": 0,
            "metadata_errors": 0,
            "analysis_total": 0,
            "analysis_processed": 0,
            "analysis_skipped": 0,
            "analysis_errors": 0,
            "face_total": 0,
            "face_skipped": 0,
            "started_at": now,
            "updated_at": now,
            "finished_at": None,
            "summary": None,
            "origin": entry.get("origin") or "manual",
        }
    )


def start_scan_job_thread(entry):
    thread = threading.Thread(
        target=run_scan_job,
        args=(entry["job_id"], entry["options"]),
        daemon=True,
        name=f"gallery-scan-{entry['job_id'][:8]}",
    )
    thread.start()


def enqueue_scan_job(options, origin="manual", queue_if_busy=False):
    entry = {
        "job_id": str(uuid4()),
        "options": deepcopy(options),
        "origin": origin,
        "queued_at": time.time(),
    }
    start_now = False
    with SCAN_LOCK:
        if SCAN_STATUS["active"]:
            if not queue_if_busy:
                return None, _scan_status_snapshot_locked()
            SCAN_QUEUE.append(entry)
            scheduled = {
                "active": False,
                "job_id": entry["job_id"],
                "state": "queued",
                "stage": "queued",
                "message": "Scan automatique en attente",
                "options": deepcopy(options),
                "origin": origin,
                "queue_position": len(SCAN_QUEUE),
            }
            return scheduled, _scan_status_snapshot_locked()
        activate_scan_job_locked(entry)
        start_now = True
        scheduled = _scan_status_snapshot_locked()
    if start_now:
        start_scan_job_thread(entry)
    return scheduled, scheduled


def finish_scan_job_and_promote(job_id, **updates):
    next_entry = None
    with SCAN_LOCK:
        if SCAN_STATUS.get("job_id") != job_id:
            return _scan_status_snapshot_locked()
        SCAN_STATUS.update(active=False, finished_at=time.time(), **updates)
        SCAN_STATUS["updated_at"] = time.time()
        if SCAN_QUEUE:
            next_entry = SCAN_QUEUE.pop(0)
            activate_scan_job_locked(next_entry)
        snapshot = _scan_status_snapshot_locked()
    if next_entry:
        start_scan_job_thread(next_entry)
    return snapshot


def update_scan_status(**updates):
    with SCAN_LOCK:
        SCAN_STATUS.update(updates)
        SCAN_STATUS["updated_at"] = time.time()


def scan_job_cancel_requested(job_id):
    with SCAN_LOCK:
        return bool(
            SCAN_STATUS.get("active")
            and SCAN_STATUS.get("job_id") == job_id
            and SCAN_STATUS.get("cancel_requested")
        )


def request_scan_job_cancel(job_id):
    face_job_id = None
    with SCAN_LOCK:
        if SCAN_STATUS.get("job_id") != job_id:
            return None
        if SCAN_STATUS.get("active") and not SCAN_STATUS.get("cancel_requested"):
            SCAN_STATUS.update(
                {
                    "cancel_requested": True,
                    "state": "cancel_requested",
                    "message": "Arret demande",
                    "updated_at": time.time(),
                }
            )
        face_job_id = SCAN_STATUS.get("face_job_id")
    if face_job_id:
        request_face_job_cancel(face_job_id)
    return scan_status_snapshot()


def apply_scan_progress(progress):
    with SCAN_LOCK:
        cancel_requested = SCAN_STATUS.get("cancel_requested")
        current_message = SCAN_STATUS.get("message")
    updates = {
        "state": "cancel_requested" if cancel_requested else "running",
        "stage": "scan",
        "message": progress.get("message", current_message),
        "album": progress.get("album"),
        "file": progress.get("file"),
        "updated_at": progress.get("updated_at", time.time()),
    }
    if "photos" in progress:
        updates["photos"] = progress["photos"]
    if "album_photos" in progress:
        updates["album_photos"] = progress["album_photos"]
    if "processed" in progress:
        updates["processed"] = progress["processed"]
    if "skipped" in progress:
        updates["skipped"] = progress["skipped"]
    if progress.get("error"):
        with SCAN_LOCK:
            SCAN_STATUS["errors"].append(
                {
                    "album": progress.get("album"),
                    "file": progress.get("file"),
                    "error": progress.get("error"),
                }
            )
    update_scan_status(**updates)


def wait_for_scan_face_job(scan_job_id, face_job_id):
    while True:
        if scan_job_cancel_requested(scan_job_id):
            request_face_job_cancel(face_job_id)
        with connect_db(DB_PATH) as conn:
            face_job = get_face_scan_job(conn, face_job_id)
        if not face_job:
            raise RuntimeError("Le job de reconnaissance faciale est introuvable")
        update_scan_status(
            stage="faces",
            face_job=face_job,
            state="cancel_requested" if scan_job_cancel_requested(scan_job_id) else "running",
            message=face_job.get("message") or "Reconnaissance faciale",
        )
        if not face_job["active"]:
            if face_job["state"] == "cancelled":
                raise ScanCancelled("Reconnaissance faciale annulee")
            if face_job["state"] == "error":
                raise RuntimeError(face_job.get("error") or face_job.get("message") or "Reconnaissance faciale en erreur")
            return face_job
        time.sleep(0.2)


def scan_scope_photo_ids(options):
    if options["scope"] == "selection":
        return list(options["photo_ids"])
    with connect_db(DB_PATH) as conn:
        return photo_ids_for_face_scope(
            conn,
            "album" if options["scope"] == "current" else "all",
            album_name=options.get("album"),
            mode="detect",
        )


def scan_stage_photo_ids(options, processed_photo_ids):
    if options["scan_mode"] == "incremental":
        return list(dict.fromkeys(processed_photo_ids))
    return scan_scope_photo_ids(options)


def run_metadata_stage(job_id, options, photo_ids, sync=False):
    if not options.get("metadata") or not photo_ids:
        return {"total": 0, "processed": 0, "skipped": 0, "errors": 0}
    photo_ids = list(dict.fromkeys(photo_ids))
    summary = {"total": len(photo_ids), "processed": 0, "skipped": 0, "errors": 0}
    force = options["scan_mode"] == "full"
    for position, photo_id in enumerate(photo_ids, start=1):
        if not sync and scan_job_cancel_requested(job_id):
            raise ScanCancelled("Scan JSON annule")
        try:
            with connect_db(DB_PATH) as conn:
                photo = conn.execute(
                    "SELECT media_type FROM photos WHERE id=?",
                    (photo_id,),
                ).fetchone()
                if not photo or photo["media_type"] != "image":
                    summary["skipped"] += 1
                    continue
                cached = conn.execute(
                    "SELECT 1 FROM photo_metadata WHERE photo_id=?",
                    (photo_id,),
                ).fetchone()
                if cached and not force:
                    summary["skipped"] += 1
                else:
                    image_path = find_photo_file(conn, photo_id)
                    if not image_path:
                        raise ValueError("Fichier photo introuvable")
                    rescan_metadata(conn, photo_id, image_path)
                    summary["processed"] += 1
        except Exception as exc:
            summary["errors"] += 1
            if not sync:
                with SCAN_LOCK:
                    SCAN_STATUS["errors"].append(
                        {"photo_id": photo_id, "stage": "metadata", "error": str(exc)}
                    )
        if not sync:
            update_scan_status(
                stage="metadata",
                message=f"Scan JSON {position}/{len(photo_ids)}",
                metadata_total=len(photo_ids),
                metadata_processed=summary["processed"],
                metadata_skipped=summary["skipped"],
                metadata_errors=summary["errors"],
            )
    return summary


def run_image_analysis_stage(job_id, options, photo_ids, sync=False):
    if not options.get("image_analysis"):
        return {"total": 0, "processed": 0, "skipped": 0, "errors": 0}
    photo_ids = list(dict.fromkeys(photo_ids))
    if not photo_ids:
        return {"total": 0, "processed": 0, "skipped": 0, "errors": 0}
    engine = get_image_analysis_engine()
    summary = {"total": len(photo_ids), "processed": 0, "skipped": 0, "errors": 0}
    force = options["scan_mode"] == "full"
    pending_photo_ids = []
    with connect_db(DB_PATH) as conn:
        for photo_id in photo_ids:
            photo = conn.execute("SELECT checksum, media_type FROM photos WHERE id=?", (photo_id,)).fetchone()
            if not photo or photo["media_type"] != "image":
                summary["skipped"] += 1
                continue
            cached = bool(
                photo
                and not force
                and photo_image_analysis_cache_valid(
                    conn,
                    photo_id,
                    photo["checksum"],
                    engine.analysis_signature,
                )
            )
            if cached:
                summary["skipped"] += 1
            else:
                pending_photo_ids.append(photo_id)
    if not pending_photo_ids:
        return summary

    def model_progress(message):
        if not sync:
            update_scan_status(stage="image_analysis", message=message, analysis_total=len(photo_ids))

    with IMAGE_ANALYSIS_RUN_LOCK:
        engine.preload(progress_callback=model_progress)
    for position, photo_id in enumerate(pending_photo_ids, start=1):
        if not sync and scan_job_cancel_requested(job_id):
            raise ScanCancelled("Analyse d'image annulee")
        try:
            with connect_db(DB_PATH) as conn:
                photo = conn.execute("SELECT id, checksum FROM photos WHERE id=?", (photo_id,)).fetchone()
                if not photo:
                    raise ValueError("Photo introuvable")
                image_path = find_photo_file(conn, photo_id)
                if not image_path:
                    raise ValueError("Fichier photo introuvable")
            with IMAGE_ANALYSIS_RUN_LOCK:
                result = engine.analyze_path(image_path)
            with connect_db(DB_PATH) as conn:
                replace_photo_image_analysis(
                    conn,
                    photo_id,
                    photo["checksum"],
                    engine.analysis_signature,
                    result,
                )
            summary["processed"] += 1
        except ImageAnalysisUnavailable:
            raise
        except Exception as exc:
            summary["errors"] += 1
            if not sync:
                with SCAN_LOCK:
                    SCAN_STATUS["errors"].append(
                        {"photo_id": photo_id, "stage": "image_analysis", "error": str(exc)}
                    )
        if not sync:
            completed = summary["processed"] + summary["errors"] + summary["skipped"]
            update_scan_status(
                stage="image_analysis",
                message=f"Analyse d'image {completed}/{len(photo_ids)}",
                analysis_total=len(photo_ids),
                analysis_processed=summary["processed"],
                analysis_skipped=summary["skipped"],
                analysis_errors=summary["errors"],
            )
    return summary


def face_stage_candidates(options, photo_ids):
    photo_ids = list(dict.fromkeys(photo_ids))
    if not photo_ids:
        return [], 0
    force = options["scan_mode"] == "full" and options.get("force_face_rescan")
    if force:
        return photo_ids, 0
    engine = get_face_engine()
    pending_photo_ids = []
    with connect_db(DB_PATH) as conn:
        for photo_id in photo_ids:
            photo = conn.execute("SELECT checksum FROM photos WHERE id=?", (photo_id,)).fetchone()
            cached = bool(
                photo
                and photo_face_cache_valid(
                    conn,
                    photo_id,
                    photo["checksum"],
                    engine.model_name,
                    engine.model_version,
                )
            )
            if not cached:
                pending_photo_ids.append(photo_id)
    return pending_photo_ids, len(photo_ids) - len(pending_photo_ids)


def enqueue_scan_face_stage(job_id, options, photo_ids, sync=False):
    if not options.get("face_recognition"):
        if options.get("skip_automatic_face_scan"):
            return None, None
        return enqueue_automatic_face_scan(params={"parent_scan_job_id": job_id}), None
    pending_photo_ids, skipped = face_stage_candidates(options, photo_ids)
    summary = {
        "total": len(photo_ids),
        "processed": 0,
        "skipped": skipped,
        "errors": 0,
    }
    if not pending_photo_ids:
        return None, summary
    face_job = enqueue_face_job(
        "selection",
        pending_photo_ids,
        mode="detect",
        params={
            "album_name": options.get("album"),
            "force": bool(options.get("force_face_rescan")),
            "parent_scan_job_id": job_id,
        },
        sync=sync,
    )
    return face_job, summary


def empty_catalog_summary():
    return {
        "albums": 0,
        "photos": 0,
        "processed": 0,
        "skipped": 0,
        "missing": 0,
        "processed_photo_ids": [],
        "errors": [],
    }


def run_scan_pipeline(job_id, options, sync=False):
    if options["scope"] == "selection":
        summary = empty_catalog_summary()
        summary["selected"] = len(options["photo_ids"])
    else:
        summary = scan_albums(
            DB_PATH,
            IMAGES_ROOT,
            THUMBNAIL_ROOT,
            scan_metadata=False,
            rescan_existing=options["scan_mode"] == "full",
            progress_callback=None if sync else apply_scan_progress,
            cancel_callback=None if sync else lambda: scan_job_cancel_requested(job_id),
            commit_interval=25,
            album_name=options.get("album"),
        )
    processed_photo_ids = summary.pop("processed_photo_ids", [])
    target_photo_ids = scan_stage_photo_ids(options, processed_photo_ids)
    summary["targeted"] = len(target_photo_ids)
    if not sync and scan_job_cancel_requested(job_id):
        raise ScanCancelled("Scan annule")
    summary["metadata"] = run_metadata_stage(job_id, options, target_photo_ids, sync=sync)
    if not sync and scan_job_cancel_requested(job_id):
        raise ScanCancelled("Scan annule")
    summary["image_analysis"] = run_image_analysis_stage(
        job_id,
        options,
        target_photo_ids,
        sync=sync,
    )
    if not sync and scan_job_cancel_requested(job_id):
        raise ScanCancelled("Scan annule")
    face_job, face_summary = enqueue_scan_face_stage(
        job_id,
        options,
        target_photo_ids,
        sync=sync,
    )
    if face_job and not sync:
        update_scan_status(
            stage="faces",
            face_job_id=face_job["id"],
            face_job=face_job,
            face_total=face_summary["total"] if face_summary else face_job["total"],
            face_skipped=face_summary["skipped"] if face_summary else 0,
            message="Reconnaissance faciale en attente",
        )
        if scan_job_cancel_requested(job_id):
            request_face_job_cancel(face_job["id"])
        face_job = wait_for_scan_face_job(job_id, face_job["id"])
    if face_summary is not None:
        if face_job:
            face_summary["processed"] = face_job["processed"]
            face_summary["errors"] = face_job["errors_count"]
        summary["faces"] = face_summary
    if not sync and scan_job_cancel_requested(job_id):
        raise ScanCancelled("Scan annule")
    return summary


def run_scan_job(job_id, options):
    try:
        summary = run_scan_pipeline(job_id, options)
        analysis_processed = (
            summary["metadata"]["processed"]
            + summary["image_analysis"]["processed"]
            + summary.get("faces", {}).get("processed", 0)
        )
        finish_scan_job_and_promote(
            job_id,
            state="done",
            stage="done",
            message=(
                f"Scan termine: {summary['processed']} image(s) indexee(s), "
                f"{analysis_processed} analyse(s) executee(s)"
            ),
            summary=summary,
        )
        print(f"[scan] job {job_id} done: {summary}", flush=True)
    except ScanCancelled:
        finish_scan_job_and_promote(
            job_id,
            state="cancelled",
            stage="cancelled",
            message="Scan annule",
            summary=None,
        )
        print(f"[scan] job {job_id} cancelled", flush=True)
    except Exception as exc:
        print(f"[scan] job {job_id} failed: {exc}", flush=True)
        traceback.print_exc()
        finish_scan_job_and_promote(
            job_id,
            state="error",
            stage="error",
            message=str(exc),
            summary=None,
        )


@app.route("/")
def image_gallery():
    ensure_ready()
    page = max(request.args.get("page", 1, type=int), 1)
    requested_album = request.args.get("album")
    max_sensitivity = (
        request.args.get("max_sensitivity")
        or request.cookies.get("gallery_max_sensitivity")
        or "neutral"
    )
    if max_sensitivity not in SENSITIVITY_LEVELS:
        max_sensitivity = "neutral"
    include_tags = normalized_query_tag_names(request.args.getlist("include_tag"))
    exclude_tags = normalized_query_tag_names(request.args.getlist("exclude_tag"))
    with connect_db(DB_PATH) as conn:
        albums = list_albums(conn)
        album_name = selected_album_name(albums, requested_album)
        _, photos, total = (
            list_gallery_photos(
                conn,
                album_name,
                page=page,
                per_page=PER_PAGE,
                include_tags=include_tags,
                exclude_tags=exclude_tags,
                max_sensitivity=max_sensitivity,
            )
            if album_name
            else (None, [], 0)
        )
        album = next((item for item in albums if item["name"] == album_name), None)
        album_facets = (
            list_album_tag_facets(
                conn,
                album["id"],
                include_tags=include_tags,
                exclude_tags=exclude_tags,
                max_sensitivity=max_sensitivity,
            )
            if album
            else {"matching_photo_count": 0, "tags": [], "active_tags": []}
        )
        tags = list_tags(conn)
    total_pages = max((total + PER_PAGE - 1) // PER_PAGE, 1)
    pagination_urls = (
        {
            page_number: gallery_page_url(
                album_name,
                page_number,
                include_tags,
                exclude_tags,
                max_sensitivity,
            )
            for page_number in range(1, total_pages + 1)
        }
        if album_name
        else {}
    )
    return render_template(
        "index.html",
        albums=albums,
        selected_album=album,
        photos=photos,
        tags=tags,
        page=page,
        total_pages=total_pages,
        pagination_urls=pagination_urls,
        album_tag_stats=album_facets["tags"],
        active_tag_stats=album_facets["active_tags"],
        filtered_photo_count=total,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        filter_active=bool(include_tags or exclude_tags),
        gallery_filter_active=bool(include_tags or exclude_tags or max_sensitivity != "high"),
        max_sensitivity=max_sensitivity,
        sensitivity_levels=SENSITIVITY_LEVELS,
        allowed_album_types=sorted(ALLOWED_ALBUM_TYPES),
        allowed_link_types=sorted(ALLOWED_LINK_TYPES),
    )


@app.get("/api/albums")
def api_albums():
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        return jsonify({"albums": list_albums(conn)})


@app.get("/api/albums/<int:album_id>/tag-facets")
def api_album_tag_facets(album_id):
    ensure_ready()
    max_sensitivity = request.args.get("max_sensitivity") or "neutral"
    if max_sensitivity not in SENSITIVITY_LEVELS:
        return jsonify({"ok": False, "error": "Invalid sensitivity"}), 400
    include_tags = normalized_query_tag_names(request.args.getlist("include_tag"))
    exclude_tags = normalized_query_tag_names(request.args.getlist("exclude_tag"))
    with connect_db(DB_PATH) as conn:
        album = conn.execute(
            "SELECT id FROM albums WHERE id=?",
            (album_id,),
        ).fetchone()
        if not album:
            return jsonify({"ok": False, "error": "Album not found"}), 404
        facets = list_album_tag_facets(
            conn,
            album_id,
            include_tags=include_tags,
            exclude_tags=exclude_tags,
            max_sensitivity=max_sensitivity,
        )
    return jsonify({"ok": True, **facets})


@app.get("/api/albums/<int:album_id>/slideshow-photos")
def api_album_slideshow_photos(album_id):
    ensure_ready()
    max_sensitivity = request.args.get("max_sensitivity") or "neutral"
    if max_sensitivity not in SENSITIVITY_LEVELS:
        return jsonify({"ok": False, "error": "Invalid sensitivity"}), 400
    include_tags = normalized_query_tag_names(request.args.getlist("include_tag"))
    exclude_tags = normalized_query_tag_names(request.args.getlist("exclude_tag"))
    with connect_db(DB_PATH) as conn:
        album = conn.execute(
            "SELECT name FROM albums WHERE id=?",
            (album_id,),
        ).fetchone()
        if not album:
            return jsonify({"ok": False, "error": "Album not found"}), 404
        _, photos, total = list_gallery_photos(
            conn,
            album["name"],
            per_page=None,
            include_tags=include_tags,
            exclude_tags=exclude_tags,
            max_sensitivity=max_sensitivity,
        )
    photos = [photo for photo in photos if photo.get("media_type") == "image"]
    return jsonify({"ok": True, "photos": photos, "total": len(photos)})


@app.patch("/api/albums/<int:album_id>")
def api_update_album(album_id):
    payload = request.get_json(silent=True) or {}
    try:
        with connect_db(DB_PATH) as conn:
            update_album(
                conn,
                album_id,
                display_name=payload.get("display_name"),
                album_type=payload.get("type"),
                tags=payload.get("tags"),
            )
            albums = list_albums(conn)
        return jsonify({"ok": True, "albums": albums})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/scan")
def api_scan():
    payload = request.get_json(silent=True) or {}
    try:
        options = normalize_scan_options(payload)
        validate_scan_selection(options)
        sync = scan_payload_boolean(payload, "sync", False)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    if sync:
        try:
            summary = run_scan_pipeline("sync", options, sync=True)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        return jsonify({"ok": True, "summary": summary})

    scheduled, job = enqueue_scan_job(options)
    if scheduled is None:
        return jsonify({"ok": True, "job": job, "already_running": True}), 202
    return jsonify({"ok": True, "job": job}), 202


@app.get("/api/scan/status")
def api_scan_status():
    return jsonify({"ok": True, "job": scan_status_snapshot()})


@app.post("/api/scan/jobs/<job_id>/cancel")
def api_cancel_scan_job(job_id):
    job = request_scan_job_cancel(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Scan job not found"}), 404
    return jsonify({"ok": True, "job": job}), 202 if job["active"] else 200


@app.get("/api/comfy/status")
def api_comfy_status():
    available = get_comfy_client().is_available()
    return jsonify({"ok": True, "available": available})


@app.get("/api/comfy/workflows")
def api_comfy_workflows():
    try:
        workflows = list_registered_workflows(COMFY_WORKFLOW_ROOT)
        return jsonify({"ok": True, "workflows": workflows})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/photos/<int:photo_id>")
def api_photo_detail(photo_id):
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        detail = get_photo_detail(conn, photo_id)
    if not detail:
        return jsonify({"ok": False, "error": "Photo not found"}), 404
    return jsonify({"ok": True, "photo": detail})


@app.get("/api/photos/<int:photo_id>/preview")
def api_photo_preview(photo_id):
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        photo = conn.execute(
            "SELECT checksum, media_type FROM photos WHERE id=?",
            (photo_id,),
        ).fetchone()
        image_path = find_photo_file(conn, photo_id) if photo else None
    if not photo:
        return jsonify({"ok": False, "error": "Photo not found"}), 404
    if photo["media_type"] != "image":
        return jsonify({"ok": False, "error": "Aucun apercu image pour une video"}), 400
    if not image_path:
        return jsonify({"ok": False, "error": "No file found for this photo"}), 404

    try:
        with get_preview_lock(photo["checksum"]):
            preview_path = ensure_preview(
                image_path,
                PREVIEW_ROOT,
                photo["checksum"],
            )
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 422

    response = send_file(
        preview_path,
        mimetype="image/jpeg",
        conditional=True,
        etag=photo["checksum"],
        max_age=31536000,
    )
    response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return response


@app.post("/api/photos/<int:photo_id>/image-analysis/rescan")
def api_rescan_photo_image_analysis(photo_id):
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        photo = conn.execute(
            "SELECT id, checksum, media_type FROM photos WHERE id=?",
            (photo_id,),
        ).fetchone()
        image_path = find_photo_file(conn, photo_id) if photo else None
    if not photo:
        return jsonify({"ok": False, "error": "Photo not found"}), 404
    if photo["media_type"] != "image":
        return jsonify({"ok": False, "error": "L'analyse d'image ne prend pas en charge les videos"}), 400
    if not image_path:
        return jsonify({"ok": False, "error": "No file found for this photo"}), 404

    engine = get_image_analysis_engine()
    try:
        with IMAGE_ANALYSIS_RUN_LOCK:
            result = engine.analyze_path(image_path)
    except ImageAnalysisUnavailable as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    except ImageAnalysisError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 422
    except Exception as exc:
        return jsonify(
            {"ok": False, "error": f"Échec de l'analyse d'image : {exc}"}
        ), 500

    with connect_db(DB_PATH) as conn:
        replace_photo_image_analysis(
            conn,
            photo_id,
            photo["checksum"],
            engine.analysis_signature,
            result,
        )
        detail = get_photo_detail(conn, photo_id)
    return jsonify({"ok": True, "photo": detail})


@app.get("/api/photos/<int:photo_id>/comfy/edit-options")
def api_comfy_edit_options(photo_id):
    ensure_ready()
    workflow_id = str(request.args.get("workflow_id") or CURRENT_WORKFLOW_ID)
    try:
        with connect_db(DB_PATH) as conn:
            detail = get_photo_detail(conn, photo_id)
            if not detail:
                return jsonify({"ok": False, "error": "Photo not found"}), 404
            if detail.get("media_type") != "image":
                return jsonify({"ok": False, "error": "La generation ComfyUI requiert une image source"}), 400
            lora_catalog = list_lora_catalog(conn)
            if workflow_id != CURRENT_WORKFLOW_ID:
                registered = get_registered_workflow(workflow_id, COMFY_WORKFLOW_ROOT)
                if registered["config"]["mode"] == "i2v" and not (detail.get("metadata") or {}).get("prompt"):
                    image_path = find_photo_file(conn, photo_id)
                    if image_path:
                        try:
                            rescan_metadata(conn, photo_id, image_path)
                            detail = get_photo_detail(conn, photo_id)
                        except OSError:
                            pass
                options = build_registered_edit_options(
                    detail,
                    lora_catalog,
                    workflow_id,
                    COMFY_WORKFLOW_ROOT,
                )
                return jsonify({"ok": True, "options": options})
            try:
                options = build_edit_options(detail, lora_catalog)
            except ComfyPromptUnavailable:
                image_path = find_photo_file(conn, photo_id)
                if not image_path:
                    return jsonify({"ok": False, "error": "No file found for this photo"}), 404
                try:
                    rescan_metadata(conn, photo_id, image_path)
                except OSError as exc:
                    return jsonify({"ok": False, "error": str(exc)}), 400
                detail = get_photo_detail(conn, photo_id)
                options = build_edit_options(detail, lora_catalog)
        return jsonify({"ok": True, "options": options})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/photos/<int:photo_id>/comfy/generate")
def api_comfy_generate(photo_id):
    ensure_ready()
    payload = request.get_json(silent=True) or {}
    client = get_comfy_client()
    if not client.is_available():
        return jsonify({"ok": False, "error": "ComfyUI is not available"}), 503
    workflow_id = str(payload.get("workflow_id") or CURRENT_WORKFLOW_ID)
    try:
        registered = get_registered_workflow(workflow_id, COMFY_WORKFLOW_ROOT)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    references = payload.get("references") or []
    if workflow_id == CURRENT_WORKFLOW_ID and "references" in payload and not any(
        bool(item.get("enabled", True)) for item in references if isinstance(item, dict)
    ):
        return jsonify({"ok": False, "error": "Au moins une reference active est requise"}), 400
    with connect_db(DB_PATH) as conn:
        detail = get_photo_detail(conn, photo_id)
        if not detail:
            return jsonify({"ok": False, "error": "Photo not found"}), 404
        if detail.get("media_type") != "image":
            return jsonify({"ok": False, "error": "La generation ComfyUI requiert une image source"}), 400
        if registered and registered["config"]["mode"] in {"i2i", "i2v"} and not detail.get("original_url"):
            return jsonify({"ok": False, "error": "Image source indisponible"}), 400
    try:
        job = create_comfy_job(photo_id, payload)
    except ComfyJobAlreadyActive as exc:
        return jsonify({"ok": False, "error": str(exc), "job": exc.job}), 409
    return jsonify({"ok": True, "job": job}), 202


@app.post("/api/comfy/references/upload")
def api_comfy_reference_upload():
    ensure_ready()
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"ok": False, "error": "Aucun fichier fourni"}), 400
    filename = secure_filename(uploaded.filename)
    suffix = Path(filename).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        return jsonify({"ok": False, "error": "Format de reference non supporte"}), 400
    data = uploaded.read()
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except Exception:
        return jsonify({"ok": False, "error": "Image de reference invalide"}), 400
    client = get_comfy_client()
    if not client.is_available():
        return jsonify({"ok": False, "error": "ComfyUI is not available"}), 503
    try:
        with tempfile.TemporaryDirectory(prefix="gallery-comfy-") as temp_dir:
            temp_path = Path(temp_dir) / filename
            temp_path.write_bytes(data)
            input_name = client.upload_image(temp_path)
        with connect_db(DB_PATH) as conn:
            input_album, input_path = validated_comfy_input_path(conn, input_name)
            photo_id = import_photo_into_album(
                conn,
                input_path,
                input_album,
                THUMBNAIL_ROOT,
                scan_metadata=False,
            )
            photo = get_photo_detail(conn, photo_id)
        scan_options = normalize_scan_options(
            {
                "scope": "selection",
                "photo_ids": [photo_id],
                "scan_mode": "missing",
                "metadata": True,
                "image_analysis": True,
                "face_recognition": False,
            }
        )
        scan_options["skip_automatic_face_scan"] = True
        scan_job, scan_status = enqueue_scan_job(
            scan_options,
            origin="comfy_reference",
            queue_if_busy=True,
        )
        return jsonify(
            {
                "ok": True,
                "input_name": input_name,
                "thumbnail_url": f"/api/comfy/input-preview?filename={urlencode({'name': input_name})[5:]}",
                "photo": photo,
                "scan_job": scan_job,
                "scan_status": scan_status,
            }
        ), 201
    except ComfyUnavailable as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


def validated_comfy_input_path(conn, input_name):
    normalized = str(input_name or "").replace("\\", "/").strip("/")
    parts = normalized.split("/") if normalized else []
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Chemin d'image ComfyUI input invalide")
    album = get_album_by_name(conn, "input")
    if not album or album["type"] != "input":
        raise ValueError("La galerie input n'est pas configuree")
    album_root = Path(album["path"]).resolve()
    input_path = Path(album["path"]).joinpath(*parts)
    resolved_input_path = input_path.resolve()
    try:
        resolved_input_path.relative_to(album_root)
    except ValueError as exc:
        raise ValueError("Chemin d'image ComfyUI input invalide") from exc
    if not resolved_input_path.is_file():
        raise ValueError(f"Image ComfyUI input introuvable: {normalized}")
    return album, input_path


@app.get("/api/comfy/input-preview")
def api_comfy_input_preview():
    image_name = request.args.get("filename", "")
    try:
        data, content_type = get_comfy_client().get_input_image(image_name)
        response = Response(data, mimetype=content_type or "application/octet-stream")
        response.headers["Cache-Control"] = "private, max-age=60"
        return response
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except ComfyUnavailable as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503


@app.get("/api/comfy/jobs/current")
def api_current_comfy_job():
    return jsonify({"ok": True, "job": current_comfy_job_snapshot()})


@app.get("/api/comfy/jobs/<job_id>")
def api_comfy_job(job_id):
    job = comfy_job_snapshot(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found"}), 404
    return jsonify({"ok": True, "job": job})


@app.post("/api/comfy/jobs/<job_id>/cancel")
def api_cancel_comfy_job(job_id):
    job = request_comfy_job_cancel(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found"}), 404
    return jsonify({"ok": True, "job": job}), 202 if job["active"] else 200


@app.get("/api/comfy/jobs/<job_id>/preview")
def api_comfy_job_preview(job_id):
    preview, updated_at = comfy_job_preview(job_id)
    if preview is None:
        return Response(status=404)
    content_type = "image/png" if preview.startswith(b"\x89PNG") else "image/jpeg"
    response = Response(preview, mimetype=content_type)
    response.headers["Cache-Control"] = "no-store"
    if updated_at:
        response.headers["X-Preview-Updated-At"] = str(updated_at)
    return response


@app.post("/api/photos/<int:photo_id>/metadata/rescan")
def api_rescan_metadata(photo_id):
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        photo = conn.execute("SELECT media_type FROM photos WHERE id=?", (photo_id,)).fetchone()
        if photo and photo["media_type"] != "image":
            return jsonify({"ok": False, "error": "Les videos ne contiennent pas de metadonnees image"}), 400
        image_path = find_photo_file(conn, photo_id)
        if not image_path:
            return jsonify({"ok": False, "error": "No file found for this photo"}), 404
        try:
            rescan_metadata(conn, photo_id, image_path)
        except OSError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        detail = get_photo_detail(conn, photo_id)
    return jsonify({"ok": True, "photo": detail})


@app.post("/api/photos/<int:photo_id>/thumbnail/refresh")
def api_refresh_thumbnail(photo_id):
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        image_path = find_photo_file(conn, photo_id)
        if not image_path:
            return jsonify({"ok": False, "error": "No file found for this photo"}), 404
        photo = conn.execute("SELECT checksum FROM photos WHERE id=?", (photo_id,)).fetchone()
        if not photo:
            return jsonify({"ok": False, "error": "Photo not found"}), 404
        try:
            ensure_thumbnail(image_path, THUMBNAIL_ROOT, photo["checksum"], force=True)
        except OSError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        detail = get_photo_detail(conn, photo_id)
    detail["thumbnail_url"] = f'{detail["thumbnail_url"]}?v={time.time_ns()}'
    return jsonify({"ok": True, "photo": detail})


@app.post("/api/photos/batch/metadata/rescan")
def api_batch_rescan_metadata():
    ensure_ready()
    payload = request.get_json(silent=True) or {}
    try:
        photo_ids = normalized_batch_photo_ids(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    results = []
    with connect_db(DB_PATH) as conn:
        missing = missing_photo_ids(conn, photo_ids)
        if missing:
            return jsonify({"ok": False, "error": f"Photos not found: {', '.join(map(str, missing))}"}), 404
        for photo_id in photo_ids:
            photo = conn.execute("SELECT media_type FROM photos WHERE id=?", (photo_id,)).fetchone()
            if photo and photo["media_type"] != "image":
                results.append({"photo_id": photo_id, "status": "skipped", "error": "Media video"})
                continue
            image_path = find_photo_file(conn, photo_id)
            if not image_path:
                results.append({"photo_id": photo_id, "status": "failed", "error": "No file found for this photo"})
                continue
            try:
                rescan_metadata(conn, photo_id, image_path)
                results.append({"photo_id": photo_id, "status": "scanned"})
            except OSError as exc:
                results.append({"photo_id": photo_id, "status": "failed", "error": str(exc)})

    scanned = sum(result["status"] == "scanned" for result in results)
    skipped = sum(result["status"] == "skipped" for result in results)
    failed = len(results) - scanned - skipped
    summary = {"requested": len(photo_ids), "scanned": scanned, "failed": failed}
    if skipped:
        summary["skipped"] = skipped
    return jsonify(
        {
            "ok": True,
            "summary": summary,
            "results": results,
        }
    )


def unique_destination_path(destination_dir, filename):
    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    candidate = destination_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 1
    while True:
        candidate = destination_dir / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


@app.post("/api/photos/<int:photo_id>/album-action")
def api_photo_album_action(photo_id):
    ensure_ready()
    payload = request.get_json(silent=True) or {}
    action = payload.get("action")
    destination_album_name = payload.get("destination_album_name")
    source_album_name = payload.get("source_album_name")
    if action not in {"copy", "move"}:
        return jsonify({"ok": False, "error": "Invalid action"}), 400
    if not destination_album_name:
        return jsonify({"ok": False, "error": "Destination album is required"}), 400

    with connect_db(DB_PATH) as conn:
        source = find_photo_file_in_album(conn, photo_id, source_album_name) or find_photo_file_in_album(conn, photo_id)
        if not source or not source["path"].exists():
            return jsonify({"ok": False, "error": "Source photo file not found"}), 404
        destination_album = get_album_by_name(conn, destination_album_name)
        if not destination_album:
            return jsonify({"ok": False, "error": "Destination album not found"}), 404
        if action == "move" and destination_album["name"] == source["album_name"]:
            return jsonify({"ok": False, "error": "Source and destination albums are identical"}), 400

        try:
            destination_path = unique_destination_path(destination_album["path"], source["path"].name)
            if action == "copy":
                shutil.copy2(source["path"], destination_path)
            else:
                shutil.move(str(source["path"]), str(destination_path))
        except OSError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        imported_photo_id = import_photo_into_album(conn, destination_path, destination_album, THUMBNAIL_ROOT)
        if action == "move":
            conn.execute(
                """
                UPDATE album_photos
                SET is_missing=1, updated_at=CURRENT_TIMESTAMP
                WHERE album_id=? AND photo_id=? AND relative_path=?
                """,
                (source["album_id"], photo_id, Path(source["relative_path"]).as_posix()),
            )
        detail = get_photo_detail(conn, imported_photo_id)
        albums = list_albums(conn)
    return jsonify({"ok": True, "photo": detail, "albums": albums, "action": action})


@app.post("/api/photos/batch/album-copy")
def api_batch_album_copy():
    ensure_ready()
    payload = request.get_json(silent=True) or {}
    try:
        photo_ids = normalized_batch_photo_ids(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    source_album_name = payload.get("source_album_name")
    destination_album_name = payload.get("destination_album_name")
    if not source_album_name or not destination_album_name:
        return jsonify({"ok": False, "error": "Source and destination albums are required"}), 400
    if source_album_name == destination_album_name:
        return jsonify({"ok": False, "error": "Source and destination albums are identical"}), 400

    results = []
    with connect_db(DB_PATH) as conn:
        missing = missing_photo_ids(conn, photo_ids)
        if missing:
            return jsonify({"ok": False, "error": f"Photos not found: {', '.join(map(str, missing))}"}), 404
        if not get_album_by_name(conn, source_album_name):
            return jsonify({"ok": False, "error": "Source album not found"}), 404
        destination_album = get_album_by_name(conn, destination_album_name)
        if not destination_album:
            return jsonify({"ok": False, "error": "Destination album not found"}), 404
        if not is_album_path_available(destination_album["path"]):
            return jsonify({"ok": False, "error": "Destination album is unavailable"}), 400

        for photo_id in photo_ids:
            if find_photo_file_in_album(conn, photo_id, destination_album_name):
                results.append(photo_membership_summary(conn, photo_id) | {"status": "skipped"})
                continue
            source = find_photo_file_in_album(conn, photo_id, source_album_name) or find_photo_file_in_album(conn, photo_id)
            if not source or not source["path"].exists():
                results.append({"photo_id": photo_id, "status": "failed", "error": "Source photo file not found"})
                continue
            try:
                destination_path = unique_destination_path(destination_album["path"], source["path"].name)
                shutil.copy2(source["path"], destination_path)
                import_photo_into_album(conn, destination_path, destination_album, THUMBNAIL_ROOT)
                results.append(photo_membership_summary(conn, photo_id) | {"status": "copied"})
            except (OSError, ValueError) as exc:
                results.append({"photo_id": photo_id, "status": "failed", "error": str(exc)})

        albums = list_albums(conn)

    copied = sum(result["status"] == "copied" for result in results)
    skipped = sum(result["status"] == "skipped" for result in results)
    failed = sum(result["status"] == "failed" for result in results)
    return jsonify(
        {
            "ok": True,
            "summary": {"requested": len(photo_ids), "copied": copied, "skipped": skipped, "failed": failed},
            "results": results,
            "albums": albums,
        }
    )


@app.delete("/api/photos/<int:photo_id>")
def api_delete_photo(photo_id):
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        try:
            result = delete_photo(conn, photo_id, THUMBNAIL_ROOT, PREVIEW_ROOT)
        except OSError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not result:
            return jsonify({"ok": False, "error": "Photo not found"}), 404
        albums = list_albums(conn)
    return jsonify({"ok": True, "deleted": result, "albums": albums})


@app.delete("/api/photos/batch")
def api_delete_photos_batch():
    ensure_ready()
    payload = request.get_json(silent=True) or {}
    try:
        photo_ids = normalized_batch_photo_ids(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    results = []
    with connect_db(DB_PATH) as conn:
        missing = missing_photo_ids(conn, photo_ids)
        if missing:
            return jsonify({"ok": False, "error": f"Photos not found: {', '.join(map(str, missing))}"}), 404
        for photo_id in photo_ids:
            try:
                deleted = delete_photo(
                    conn,
                    photo_id,
                    THUMBNAIL_ROOT,
                    PREVIEW_ROOT,
                )
                results.append({"photo_id": photo_id, "status": "deleted", "deleted_files": deleted["deleted_files"]})
            except OSError as exc:
                results.append({"photo_id": photo_id, "status": "failed", "error": str(exc)})
        albums = list_albums(conn)

    deleted = sum(result["status"] == "deleted" for result in results)
    return jsonify(
        {
            "ok": True,
            "summary": {"requested": len(photo_ids), "deleted": deleted, "failed": len(results) - deleted},
            "results": results,
            "albums": albums,
        }
    )


@app.put("/api/photos/<int:photo_id>/tags")
def api_set_photo_tags(photo_id):
    payload = request.get_json(silent=True) or {}
    tags = payload.get("tags") or []
    with connect_db(DB_PATH) as conn:
        set_photo_tags(conn, photo_id, tags)
        detail = get_photo_detail(conn, photo_id)
    return jsonify({"ok": True, "photo": detail})


@app.patch("/api/photos/batch/tags")
def api_batch_photo_tags():
    ensure_ready()
    payload = request.get_json(silent=True) or {}
    try:
        photo_ids = normalized_batch_photo_ids(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    operation = payload.get("operation")
    raw_tags = payload.get("tags")
    if operation not in {"add", "remove"}:
        return jsonify({"ok": False, "error": "operation must be 'add' or 'remove'"}), 400
    if not isinstance(raw_tags, list) or any(not isinstance(tag, str) for tag in raw_tags):
        return jsonify({"ok": False, "error": "tags must be a list of strings"}), 400
    tags = list(dict.fromkeys(tag.strip() for tag in raw_tags if tag.strip()))
    if not tags:
        return jsonify({"ok": False, "error": "At least one tag is required"}), 400

    with connect_db(DB_PATH) as conn:
        missing = missing_photo_ids(conn, photo_ids)
        if missing:
            return jsonify({"ok": False, "error": f"Photos not found: {', '.join(map(str, missing))}"}), 404
        update_photo_tags(conn, photo_ids, tags, operation)
    return jsonify(
        {
            "ok": True,
            "summary": {"requested": len(photo_ids), "updated": len(photo_ids), "operation": operation, "tags": tags},
        }
    )


@app.get("/api/photos/search")
def api_photo_search():
    ensure_ready()
    query = request.args.get("q", "")
    with connect_db(DB_PATH) as conn:
        results = search_photos(conn, query)
    return jsonify({"ok": True, "photos": results})


@app.post("/api/photos/<int:photo_id>/links")
def api_create_link(photo_id):
    payload = request.get_json(silent=True) or {}
    try:
        with connect_db(DB_PATH) as conn:
            create_photo_link(conn, photo_id, int(payload.get("target_photo_id")), payload.get("type"))
            detail = get_photo_detail(conn, photo_id)
        return jsonify({"ok": True, "photo": detail})
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.delete("/api/photo-links/<int:link_id>")
def api_delete_link(link_id):
    with connect_db(DB_PATH) as conn:
        conn.execute("DELETE FROM photo_links WHERE id=?", (link_id,))
    return jsonify({"ok": True})


@app.get("/api/tags")
def api_tags():
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        return jsonify({"ok": True, "tags": list_tag_stats(conn)})


@app.patch("/api/tags/<int:tag_id>")
def api_update_tag(tag_id):
    ensure_ready()
    payload = request.get_json(silent=True) or {}
    allowed_keys = {"sensitivity", "category"}
    if (
        not isinstance(payload, dict)
        or not payload
        or not set(payload).issubset(allowed_keys)
    ):
        return jsonify(
            {
                "ok": False,
                "error": "Provide sensitivity and/or category only",
            }
        ), 400
    try:
        with connect_db(DB_PATH) as conn:
            tag = update_tag_settings(conn, tag_id, **payload)
    except TagCategoryLocked as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except KeyError:
        return jsonify({"ok": False, "error": "Tag not found"}), 404
    return jsonify({"ok": True, "tag": tag})


@app.get("/api/lora-tag-mappings")
def api_lora_tag_mappings():
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        mappings = list_lora_tag_mappings(conn)
        loras = list_lora_catalog(conn)
    return jsonify({"ok": True, "mappings": mappings, "loras": loras})


@app.post("/api/lora-tag-mappings")
def api_create_lora_tag_mapping():
    ensure_ready()
    payload = request.get_json(silent=True) or {}
    lora_name = payload.get("lora_name")
    tag_names = payload.get("tag_names")
    if not isinstance(lora_name, str) or not lora_name.strip():
        return jsonify({"ok": False, "error": "lora_name is required"}), 400
    if not isinstance(tag_names, list):
        return jsonify({"ok": False, "error": "tag_names must be a list"}), 400

    with connect_db(DB_PATH) as conn:
        catalog_names = {item["lora_name"] for item in list_lora_catalog(conn)}
        if lora_name.strip() not in catalog_names:
            return jsonify({"ok": False, "error": "Unknown LoRA"}), 400
        try:
            mapping = create_lora_tag_mapping(conn, lora_name, tag_names)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except KeyError as exc:
            return jsonify({"ok": False, "error": exc.args[0]}), 409
    return jsonify({"ok": True, "mapping": mapping}), 201


@app.delete("/api/lora-tag-mappings/<int:mapping_id>")
def api_delete_lora_tag_mapping(mapping_id):
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        if not delete_lora_tag_mapping(conn, mapping_id):
            return jsonify({"ok": False, "error": "LoRA tag mapping not found"}), 404
    return jsonify({"ok": True})


@app.patch("/api/lora-tag-mappings/<int:mapping_id>")
def api_update_lora_tag_mapping(mapping_id):
    ensure_ready()
    payload = request.get_json(silent=True) or {}
    tag_names = payload.get("tag_names")
    if not isinstance(tag_names, list):
        return jsonify({"ok": False, "error": "tag_names must be a list"}), 400

    with connect_db(DB_PATH) as conn:
        try:
            mapping = update_lora_tag_mapping(conn, mapping_id, tag_names)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except KeyError as exc:
            return jsonify({"ok": False, "error": exc.args[0]}), 404
    return jsonify({"ok": True, "mapping": mapping})


@app.get("/api/face/status")
def api_face_status():
    ensure_ready()
    engine = get_face_engine()
    with connect_db(DB_PATH) as conn:
        job = get_current_face_scan_job(conn)
        automatic_scan = bool(get_face_setting(conn, "automatic_scan", False))
    return jsonify(
        {
            "ok": True,
            "engine": engine.configuration(),
            "automatic_scan": automatic_scan,
            "job": job,
        }
    )


@app.patch("/api/face/settings")
def api_face_settings():
    ensure_ready()
    payload = request.get_json(silent=True) or {}
    if "automatic_scan" not in payload:
        return jsonify({"ok": False, "error": "automatic_scan is required"}), 400
    with connect_db(DB_PATH) as conn:
        set_face_setting(conn, "automatic_scan", bool(payload["automatic_scan"]))
    return jsonify({"ok": True, "automatic_scan": bool(payload["automatic_scan"])})


@app.get("/api/face/identities")
def api_face_identities():
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        identities = list_face_identities(conn)
    return jsonify({"ok": True, "identities": identities})


@app.post("/api/face/identities")
def api_create_face_identity():
    ensure_ready()
    payload = request.get_json(silent=True) or {}
    try:
        with connect_db(DB_PATH) as conn:
            identity = create_face_identity(
                conn,
                payload.get("tag_name", ""),
                payload.get("review_threshold", 0.40),
                payload.get("automatic_threshold", 0.55),
                payload.get("margin_threshold", 0.08),
                payload.get("enabled", True),
                payload.get("sex", "ND"),
            )
        return jsonify({"ok": True, "identity": identity}), 201
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.patch("/api/face/identities/<int:identity_id>")
def api_update_face_identity(identity_id):
    ensure_ready()
    payload = request.get_json(silent=True) or {}
    allowed = {"tag_name", "sex", "review_threshold", "automatic_threshold", "margin_threshold", "enabled"}
    try:
        with connect_db(DB_PATH) as conn:
            identity = update_face_identity(conn, identity_id, **{key: value for key, value in payload.items() if key in allowed})
        if not identity:
            return jsonify({"ok": False, "error": "Face identity not found"}), 404
        job = enqueue_rematch_all()
        return jsonify({"ok": True, "identity": identity, "job": job})
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.delete("/api/face/identities/<int:identity_id>")
def api_delete_face_identity(identity_id):
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        deleted = delete_face_identity(conn, identity_id)
    if not deleted:
        return jsonify({"ok": False, "error": "Face identity not found"}), 404
    job = enqueue_rematch_all()
    return jsonify({"ok": True, "job": job})


@app.post("/api/face/identities/<int:identity_id>/references/gallery")
def api_add_gallery_face_reference(identity_id):
    ensure_ready()
    payload = request.get_json(silent=True) or {}
    try:
        face_id = int(payload.get("face_id"))
        with connect_db(DB_PATH) as conn:
            reference_id = add_gallery_face_reference(conn, identity_id, face_id)
            identity = get_face_identity(conn, identity_id)
        job = enqueue_rematch_all()
        return jsonify({"ok": True, "reference_id": reference_id, "identity": identity, "job": job}), 201
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/face/imports")
def api_create_face_import():
    ensure_ready()
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"ok": False, "error": "Reference image is required"}), 400
    suffix = Path(secure_filename(uploaded.filename)).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        return jsonify({"ok": False, "error": "Unsupported reference image format"}), 400
    FACE_REFERENCE_ROOT.mkdir(parents=True, exist_ok=True)
    token = str(uuid4())
    image_path = FACE_REFERENCE_ROOT / f"{token}{suffix}"
    uploaded.save(image_path)
    try:
        engine = get_face_engine()
        detections = engine.analyze_path(image_path)
        if not detections:
            image_path.unlink(missing_ok=True)
            return jsonify({"ok": False, "error": "No face detected in this reference"}), 400
        with connect_db(DB_PATH) as conn:
            create_face_import(
                conn,
                token,
                image_path,
                secure_filename(uploaded.filename),
                detections,
                engine.model_name,
                engine.model_version,
            )
            imported = get_face_import(conn, token)
        return jsonify({"ok": True, "import": imported}), 201
    except (FaceRecognitionUnavailable, FaceRecognitionError, OSError, ValueError) as exc:
        image_path.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": str(exc)}), 503 if isinstance(exc, FaceRecognitionUnavailable) else 400


@app.post("/api/face/identities/<int:identity_id>/references/import")
def api_add_imported_face_reference(identity_id):
    ensure_ready()
    payload = request.get_json(silent=True) or {}
    try:
        token = str(payload.get("token") or "")
        face_index = int(payload.get("face_index"))
        with connect_db(DB_PATH) as conn:
            reference_id = add_imported_face_reference(conn, identity_id, token, face_index)
            identity = get_face_identity(conn, identity_id)
        job = enqueue_rematch_all()
        return jsonify({"ok": True, "reference_id": reference_id, "identity": identity, "job": job}), 201
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.delete("/api/face/references/<int:reference_id>")
def api_delete_face_reference(reference_id):
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        reference = get_face_reference(conn, reference_id)
        if not reference:
            return jsonify({"ok": False, "error": "Face reference not found"}), 404
        file_path = reference["file_path"]
        conn.execute("DELETE FROM face_references WHERE id=?", (reference_id,))
        still_used = (
            conn.execute("SELECT 1 FROM face_references WHERE file_path=? LIMIT 1", (file_path,)).fetchone()
            if file_path
            else None
        )
    if file_path and not still_used:
        candidate = Path(file_path).resolve()
        root = FACE_REFERENCE_ROOT.resolve()
        if root in candidate.parents:
            candidate.unlink(missing_ok=True)
    job = enqueue_rematch_all()
    return jsonify({"ok": True, "job": job})


@app.post("/api/face/jobs")
def api_create_face_job():
    ensure_ready()
    payload = request.get_json(silent=True) or {}
    scope = payload.get("scope", "all")
    mode = payload.get("mode", "detect")
    try:
        force = scan_payload_boolean(payload, "force", False)
        with connect_db(DB_PATH) as conn:
            photo_ids = photo_ids_for_face_scope(
                conn,
                scope,
                photo_ids=payload.get("photo_ids"),
                album_name=payload.get("album_name"),
                mode=mode,
            )
            missing = missing_photo_ids(conn, photo_ids)
            if missing:
                return jsonify({"ok": False, "error": f"Photos not found: {', '.join(map(str, missing))}"}), 404
        job = enqueue_face_job(
            scope,
            photo_ids,
            mode=mode,
            params={"album_name": payload.get("album_name"), "force": force},
            sync=bool(payload.get("sync")),
        )
        return jsonify({"ok": True, "job": job}), 200 if payload.get("sync") else 202
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/api/face/jobs/current")
def api_current_face_job():
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        return jsonify({"ok": True, "job": get_current_face_scan_job(conn)})


@app.get("/api/face/jobs/<job_id>")
def api_face_job(job_id):
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        job = get_face_scan_job(conn, job_id)
    if not job:
        return jsonify({"ok": False, "error": "Face scan job not found"}), 404
    return jsonify({"ok": True, "job": job})


@app.post("/api/face/jobs/<job_id>/cancel")
def api_cancel_face_job(job_id):
    ensure_ready()
    job = request_face_job_cancel(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Face scan job not found"}), 404
    return jsonify({"ok": True, "job": job})


@app.post("/api/face/jobs/<job_id>/resume")
def api_resume_face_job(job_id):
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        job = get_face_scan_job(conn, job_id)
        if not job:
            return jsonify({"ok": False, "error": "Face scan job not found"}), 404
        conn.execute(
            "UPDATE face_scan_items SET state='queued', error=NULL WHERE job_id=? AND state='error'", (job_id,)
        )
        conn.execute(
            """
            UPDATE face_scan_jobs SET state='queued', total=(SELECT COUNT(*) FROM face_scan_items WHERE job_id=?),
                processed=(SELECT COUNT(*) FROM face_scan_items WHERE job_id=? AND state='done'),
                errors_count=0, error=NULL, message='Reprise en attente', finished_at=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (job_id, job_id, job_id),
        )
        job = get_face_scan_job(conn, job_id)
    start_face_worker()
    return jsonify({"ok": True, "job": job}), 202


@app.post("/api/photo-faces/<int:face_id>/decision")
def api_decide_face_match(face_id):
    ensure_ready()
    payload = request.get_json(silent=True) or {}
    try:
        with connect_db(DB_PATH) as conn:
            photo_id = decide_face_match(conn, face_id, int(payload.get("identity_id")), payload.get("decision"))
            photo = get_photo_detail(conn, photo_id)
        return jsonify({"ok": True, "photo": photo})
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


def _cropped_face_response(image_path, bbox):
    with Image.open(image_path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        left, top, right, bottom = (float(value) for value in bbox)
        padding = max(right - left, bottom - top) * 0.20
        crop_box = (
            max(0, int(left - padding)),
            max(0, int(top - padding)),
            min(image.width, int(right + padding)),
            min(image.height, int(bottom + padding)),
        )
        if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
            raise ValueError("Invalid face bounding box")
        cropped = image.crop(crop_box)
        cropped.thumbnail((320, 320), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        cropped.save(buffer, format="JPEG", quality=90)
    buffer.seek(0)
    return send_file(buffer, mimetype="image/jpeg", max_age=0)


@app.get("/api/photo-faces/<int:face_id>/crop")
def api_photo_face_crop(face_id):
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        face = conn.execute("SELECT photo_id, bbox_json FROM photo_faces WHERE id=?", (face_id,)).fetchone()
        if not face:
            return jsonify({"ok": False, "error": "Detected face not found"}), 404
        image_path = find_photo_file(conn, face["photo_id"])
    if not image_path:
        return jsonify({"ok": False, "error": "Face source image not found"}), 404
    return _cropped_face_response(image_path, json.loads(face["bbox_json"]))


@app.get("/api/face-references/<int:reference_id>/crop")
def api_face_reference_crop(reference_id):
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        reference = get_face_reference(conn, reference_id)
        if not reference:
            return jsonify({"ok": False, "error": "Face reference not found"}), 404
        image_path = (
            find_photo_file(conn, reference["source_photo_id"])
            if reference["source_photo_id"]
            else Path(reference["file_path"])
        )
    if not image_path or not Path(image_path).is_file():
        return jsonify({"ok": False, "error": "Reference image not found"}), 404
    return _cropped_face_response(image_path, json.loads(reference["bbox_json"]))


@app.get("/api/face-imports/<token>/faces/<int:face_index>/crop")
def api_face_import_crop(token, face_index):
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        imported = conn.execute("SELECT file_path FROM face_imports WHERE token=?", (token,)).fetchone()
        face = conn.execute(
            "SELECT bbox_json FROM face_import_faces WHERE import_token=? AND face_index=?", (token, face_index)
        ).fetchone()
    if not imported or not face or not Path(imported["file_path"]).is_file():
        return jsonify({"ok": False, "error": "Imported face not found"}), 404
    return _cropped_face_response(imported["file_path"], json.loads(face["bbox_json"]))


@app.errorhandler(404)
def not_found(_error):
    return jsonify({"ok": False, "error": "Not found"}), 404


@app.errorhandler(413)
def upload_too_large(_error):
    return jsonify({"ok": False, "error": "Reference image is too large (25 MB maximum)"}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9999)
