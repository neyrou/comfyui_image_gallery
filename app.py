import threading
import time
import traceback
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from flask import Flask, Response, jsonify, render_template, request

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
    discover_albums,
    find_photo_file,
    get_photo_detail,
    import_output_photo,
    init_db,
    list_albums,
    list_gallery_photos,
    list_tags,
    output_paths_from_history,
    rescan_metadata,
    scan_albums,
    search_photos,
    set_photo_tags,
    update_album,
)


BASE_DIR = Path(__file__).resolve().parent
IMAGES_ROOT = BASE_DIR / "static" / "images"
THUMBNAIL_ROOT = BASE_DIR / "static" / "thumbnails"
DB_PATH = BASE_DIR / "instance" / "gallery.sqlite3"
PER_PAGE = 100

app = Flask(__name__)
COMFY_CLIENT_FACTORY = ComfyClient
SCAN_LOCK = threading.Lock()
COMFY_JOB_LOCK = threading.Lock()
COMFY_JOBS = {}
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


def get_comfy_client():
    return COMFY_CLIENT_FACTORY()


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
        update_comfy_job(job_id, state="preparing", message="Upload des references")
        with connect_db(DB_PATH) as conn:
            detail = get_photo_detail(conn, photo_id)
            if not detail:
                raise ValueError("Photo not found")
            for reference in payload.get("references") or []:
                node_id = str(reference.get("node_id"))
                target_photo_id = reference.get("photo_id")
                if not node_id or not target_photo_id:
                    continue
                image_path = find_photo_file(conn, int(target_photo_id))
                if not image_path:
                    raise ValueError(f"Reference photo {target_photo_id} not found")
                uploaded_images[node_id] = client.upload_image(image_path)
            prompt, workflow, patch_info = patch_prompt_and_workflow(detail, payload, uploaded_images=uploaded_images)

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
    with connect_db(DB_PATH) as conn:
        albums = list_albums(conn)
        album_name = selected_album_name(albums, requested_album)
        album, photos, total = list_gallery_photos(conn, album_name, page=page, per_page=PER_PAGE) if album_name else (None, [], 0)
        tags = list_tags(conn)
    total_pages = max((total + PER_PAGE - 1) // PER_PAGE, 1)
    return render_template(
        "index.html",
        albums=albums,
        selected_album=album,
        photos=photos,
        tags=tags,
        page=page,
        total_pages=total_pages,
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
    with connect_db(DB_PATH) as conn:
        if not get_photo_detail(conn, photo_id):
            return jsonify({"ok": False, "error": "Photo not found"}), 404
    job = create_comfy_job(photo_id, payload)
    return jsonify({"ok": True, "job": job}), 202


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


@app.put("/api/photos/<int:photo_id>/tags")
def api_set_photo_tags(photo_id):
    payload = request.get_json(silent=True) or {}
    tags = payload.get("tags") or []
    with connect_db(DB_PATH) as conn:
        set_photo_tags(conn, photo_id, tags)
        detail = get_photo_detail(conn, photo_id)
    return jsonify({"ok": True, "photo": detail})


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


@app.errorhandler(404)
def not_found(_error):
    return jsonify({"ok": False, "error": "Not found"}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9999)
