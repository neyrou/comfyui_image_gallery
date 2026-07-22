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
from PIL import Image
from werkzeug.utils import secure_filename

from comfy_generation import (
    ComfyClient,
    ComfyGenerationError,
    ComfyUnavailable,
    build_edit_options,
    extract_history_filenames,
    list_lora_catalog,
    patch_prompt_and_workflow,
)
from gallery_db import (
    ALLOWED_ALBUM_TYPES,
    ALLOWED_LINK_TYPES,
    connect_db,
    create_photo_link,
    delete_photo,
    discover_albums,
    ensure_thumbnail,
    find_photo_file,
    find_photo_file_in_album,
    get_photo_detail,
    get_album_by_name,
    import_photo_into_album,
    import_output_photo,
    init_db,
    list_album_tag_stats,
    list_albums,
    list_gallery_photos,
    list_tags,
    output_paths_from_history,
    rescan_metadata,
    scan_albums,
    search_photos,
    set_photo_tags,
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


BASE_DIR = Path(__file__).resolve().parent
IMAGES_ROOT = BASE_DIR / "static" / "images"
THUMBNAIL_ROOT = BASE_DIR / "static" / "thumbnails"
DB_PATH = BASE_DIR / "instance" / "gallery.sqlite3"
FACE_REFERENCE_ROOT = BASE_DIR / "instance" / "face_references"
PER_PAGE = 100

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
COMFY_CLIENT_FACTORY = ComfyClient
FACE_ENGINE_FACTORY = InsightFaceEngine
SCAN_LOCK = threading.Lock()
COMFY_JOB_LOCK = threading.Lock()
FACE_WORKER_LOCK = threading.Lock()
FACE_ENGINE_LOCK = threading.Lock()
COMFY_JOBS = {}
FACE_WORKER_THREAD = None
FACE_ENGINE_INSTANCE = None
FACE_RECOVERED_DATABASES = set()
SCAN_STATUS = {
    "active": False,
    "job_id": None,
    "state": "idle",
    "message": "Aucun scan en cours",
    "album": None,
    "file": None,
    "photos": 0,
    "album_photos": 0,
    "errors": [],
    "started_at": None,
    "updated_at": None,
    "finished_at": None,
    "summary": None,
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
                cached = mode == "match" or photo_face_cache_valid(
                    conn, photo_id, photo["checksum"], engine.model_name, engine.model_version
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


def enqueue_rematch_all(sync=False):
    with connect_db(DB_PATH) as conn:
        photo_ids = photo_ids_for_face_scope(conn, "rematch", mode="match")
    if not photo_ids:
        return None
    return enqueue_face_job("rematch", photo_ids, mode="match", sync=sync)


def enqueue_automatic_face_scan():
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
    return enqueue_face_job("automatic", photo_ids, mode="detect") if photo_ids else None


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
        "progress": None,
        "progress_max": None,
        "seed": None,
        "output_filenames": [],
        "photo": None,
        "error": None,
        "preview": None,
        "preview_updated_at": None,
        "started_at": time.time(),
        "updated_at": time.time(),
        "finished_at": None,
    }
    with COMFY_JOB_LOCK:
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
        snapshot = {key: value for key, value in job.items() if key != "preview"}
        snapshot["preview_available"] = bool(job.get("preview"))
        return deepcopy(snapshot)


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
        update_comfy_job(job_id, state="preparing", message="Upload des references")
        with connect_db(DB_PATH) as conn:
            detail = get_photo_detail(conn, photo_id)
            if not detail:
                raise ValueError("Photo not found")
            for reference in patched_payload.get("references") or []:
                node_id = str(reference.get("node_id") or "")
                target_photo_id = reference.get("photo_id")
                if not target_photo_id:
                    continue
                image_path = find_photo_file(conn, int(target_photo_id))
                if not image_path:
                    raise ValueError(f"Reference photo {target_photo_id} not found")
                input_name = client.upload_image(image_path)
                if "reference_id" in reference or "enabled" in reference:
                    reference["input_name"] = input_name
                elif node_id:
                    uploaded_images[node_id] = input_name
            prompt, workflow, patch_info = patch_prompt_and_workflow(detail, patched_payload, uploaded_images=uploaded_images)

        update_comfy_job(job_id, state="queued", message="Envoi a ComfyUI", seed=patch_info.get("seed"))

        def on_progress(progress):
            if progress.get("preview"):
                update_comfy_job(job_id, preview=progress["preview"], preview_updated_at=time.time())
                return
            updates = {}
            if progress.get("state"):
                updates["state"] = progress["state"]
            if "prompt_id" in progress:
                updates["prompt_id"] = progress.get("prompt_id")
            if "node" in progress:
                updates["node"] = progress.get("node")
            if "value" in progress or "max" in progress:
                updates["progress"] = progress.get("value")
                updates["progress_max"] = progress.get("max")
            if updates:
                message = "Generation en cours" if updates.get("state") == "running" else None
                if message:
                    updates["message"] = message
                update_comfy_job(job_id, **updates)

        prompt_id, history = client.run_prompt(prompt, workflow, job_id, progress_callback=on_progress)
        output_filenames = extract_history_filenames(history)
        update_comfy_job(
            job_id,
            state="importing",
            message="Import de l'image generee",
            prompt_id=prompt_id,
            output_filenames=output_filenames,
        )

        generated_photo = None
        with connect_db(DB_PATH) as conn:
            output_paths = output_paths_from_history(conn, output_filenames)
            if not output_paths:
                raise ComfyGenerationError("Generated output file was not found in the output album")
            generated_photo_id = None
            for output_path in output_paths:
                generated_photo_id = import_output_photo(conn, output_path, THUMBNAIL_ROOT)
            if generated_photo_id and generated_photo_id != photo_id:
                create_photo_link(conn, photo_id, generated_photo_id, "variant")
                generated_photo = get_photo_detail(conn, generated_photo_id)
        update_comfy_job(
            job_id,
            active=False,
            state="done",
            message="Image generee",
            prompt_id=prompt_id,
            photo=generated_photo,
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


def selected_album_name(albums, requested):
    if requested and any(album["name"] == requested for album in albums):
        return requested
    if any(album["name"] == "output" for album in albums):
        return "output"
    return albums[0]["name"] if albums else None


def normalized_query_tag_names(values):
    return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))


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


def gallery_page_url(album_name, page, include_tags=None, exclude_tags=None):
    params = [("album", album_name), ("page", page)]
    params.extend(("include_tag", tag_name) for tag_name in include_tags or [])
    params.extend(("exclude_tag", tag_name) for tag_name in exclude_tags or [])
    return f"?{urlencode(params)}"


def scan_status_snapshot():
    with SCAN_LOCK:
        return deepcopy(SCAN_STATUS)


def update_scan_status(**updates):
    with SCAN_LOCK:
        SCAN_STATUS.update(updates)
        SCAN_STATUS["updated_at"] = time.time()


def apply_scan_progress(progress):
    updates = {
        "state": "running",
        "message": progress.get("message", SCAN_STATUS.get("message")),
        "album": progress.get("album"),
        "file": progress.get("file"),
        "updated_at": progress.get("updated_at", time.time()),
    }
    if "photos" in progress:
        updates["photos"] = progress["photos"]
    if "album_photos" in progress:
        updates["album_photos"] = progress["album_photos"]
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


def run_scan_job(job_id, scan_metadata):
    try:
        summary = scan_albums(
            DB_PATH,
            IMAGES_ROOT,
            THUMBNAIL_ROOT,
            scan_metadata=scan_metadata,
            progress_callback=apply_scan_progress,
            commit_interval=25,
        )
        update_scan_status(
            active=False,
            state="done",
            message=f"Scan termine: {summary['photos']} images",
            finished_at=time.time(),
            summary=summary,
        )
        enqueue_automatic_face_scan()
        print(f"[scan] job {job_id} done: {summary}", flush=True)
    except Exception as exc:
        print(f"[scan] job {job_id} failed: {exc}", flush=True)
        traceback.print_exc()
        update_scan_status(
            active=False,
            state="error",
            message=str(exc),
            finished_at=time.time(),
            summary=None,
        )


@app.route("/")
def image_gallery():
    ensure_ready()
    page = max(request.args.get("page", 1, type=int), 1)
    requested_album = request.args.get("album")
    include_tags = normalized_query_tag_names(request.args.getlist("include_tag"))
    exclude_tags = normalized_query_tag_names(request.args.getlist("exclude_tag"))
    with connect_db(DB_PATH) as conn:
        albums = list_albums(conn)
        album_name = selected_album_name(albums, requested_album)
        album, photos, total = (
            list_gallery_photos(
                conn,
                album_name,
                page=page,
                per_page=PER_PAGE,
                include_tags=include_tags,
                exclude_tags=exclude_tags,
            )
            if album_name
            else (None, [], 0)
        )
        album_tag_stats = list_album_tag_stats(conn, album_name) if album_name else []
        tags = list_tags(conn)
    total_pages = max((total + PER_PAGE - 1) // PER_PAGE, 1)
    pagination_urls = (
        {
            page_number: gallery_page_url(album_name, page_number, include_tags, exclude_tags)
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
        album_tag_stats=album_tag_stats,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        filter_active=bool(include_tags or exclude_tags),
        allowed_album_types=sorted(ALLOWED_ALBUM_TYPES),
        allowed_link_types=sorted(ALLOWED_LINK_TYPES),
    )


@app.get("/api/albums")
def api_albums():
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        return jsonify({"albums": list_albums(conn)})


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
    if payload.get("sync"):
        summary = scan_albums(DB_PATH, IMAGES_ROOT, THUMBNAIL_ROOT, scan_metadata=bool(payload.get("metadata")))
        enqueue_automatic_face_scan()
        return jsonify({"ok": True, "summary": summary})

    with SCAN_LOCK:
        if SCAN_STATUS["active"]:
            return jsonify({"ok": True, "job": deepcopy(SCAN_STATUS), "already_running": True}), 202
        job_id = str(uuid4())
        SCAN_STATUS.update(
            {
                "active": True,
                "job_id": job_id,
                "state": "running",
                "message": "Scan demarre",
                "album": None,
                "file": None,
                "photos": 0,
                "album_photos": 0,
                "errors": [],
                "started_at": time.time(),
                "updated_at": time.time(),
                "finished_at": None,
                "summary": None,
            }
        )
        job = deepcopy(SCAN_STATUS)
    thread = threading.Thread(target=run_scan_job, args=(job_id, bool(payload.get("metadata"))), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job": job}), 202


@app.get("/api/scan/status")
def api_scan_status():
    return jsonify({"ok": True, "job": scan_status_snapshot()})


@app.get("/api/comfy/status")
def api_comfy_status():
    available = get_comfy_client().is_available()
    return jsonify({"ok": True, "available": available})


@app.get("/api/photos/<int:photo_id>")
def api_photo_detail(photo_id):
    ensure_ready()
    with connect_db(DB_PATH) as conn:
        detail = get_photo_detail(conn, photo_id)
    if not detail:
        return jsonify({"ok": False, "error": "Photo not found"}), 404
    return jsonify({"ok": True, "photo": detail})


@app.get("/api/photos/<int:photo_id>/comfy/edit-options")
def api_comfy_edit_options(photo_id):
    ensure_ready()
    try:
        with connect_db(DB_PATH) as conn:
            detail = get_photo_detail(conn, photo_id)
            if not detail:
                return jsonify({"ok": False, "error": "Photo not found"}), 404
            options = build_edit_options(detail, list_lora_catalog(conn))
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
    references = payload.get("references") or []
    if "references" in payload and not any(
        bool(item.get("enabled", True)) for item in references if isinstance(item, dict)
    ):
        return jsonify({"ok": False, "error": "Au moins une reference active est requise"}), 400
    with connect_db(DB_PATH) as conn:
        if not get_photo_detail(conn, photo_id):
            return jsonify({"ok": False, "error": "Photo not found"}), 404
    job = create_comfy_job(photo_id, payload)
    return jsonify({"ok": True, "job": job}), 202


@app.post("/api/comfy/references/upload")
def api_comfy_reference_upload():
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
        return jsonify(
            {
                "ok": True,
                "input_name": input_name,
                "thumbnail_url": f"/api/comfy/input-preview?filename={urlencode({'name': input_name})[5:]}",
            }
        ), 201
    except (ComfyUnavailable, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503


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


@app.get("/api/comfy/jobs/<job_id>")
def api_comfy_job(job_id):
    job = comfy_job_snapshot(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found"}), 404
    return jsonify({"ok": True, "job": job})


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
    failed = len(results) - scanned
    return jsonify(
        {
            "ok": True,
            "summary": {"requested": len(photo_ids), "scanned": scanned, "failed": failed},
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
        if destination_album["scan_error"]:
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
            result = delete_photo(conn, photo_id, THUMBNAIL_ROOT)
        except OSError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not result:
            return jsonify({"ok": False, "error": "Photo not found"}), 404
        albums = list_albums(conn)
    return jsonify({"ok": True, "deleted": result, "albums": albums})


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
        return jsonify({"ok": True, "tags": list_tags(conn)})


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
            )
        return jsonify({"ok": True, "identity": identity}), 201
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.patch("/api/face/identities/<int:identity_id>")
def api_update_face_identity(identity_id):
    ensure_ready()
    payload = request.get_json(silent=True) or {}
    allowed = {"tag_name", "review_threshold", "automatic_threshold", "margin_threshold", "enabled"}
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
            params={"album_name": payload.get("album_name")},
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
    with connect_db(DB_PATH) as conn:
        job = get_face_scan_job(conn, job_id)
        if not job:
            return jsonify({"ok": False, "error": "Face scan job not found"}), 404
        if job["active"]:
            conn.execute(
                "UPDATE face_scan_jobs SET state='cancel_requested', message='Annulation demandee', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (job_id,),
            )
        job = get_face_scan_job(conn, job_id)
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
        image = image.convert("RGB")
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
