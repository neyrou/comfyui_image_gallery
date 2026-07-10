import threading
import time
import traceback
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from flask import Flask, jsonify, render_template, request

from comfy_generation import (
    ComfyClient,
    ComfyGenerationError,
    ComfyUnavailable,
    build_edit_options,
    extract_history_filenames,
    list_lora_catalog,
    patch_prompt,
)
from gallery_db import (
    ALLOWED_ALBUM_TYPES,
    ALLOWED_LINK_TYPES,
    connect_db,
    create_photo_link,
    discover_albums,
    find_photo_file,
    find_latest_output_photo_after,
    find_output_photo_by_name,
    get_photo_detail,
    init_db,
    list_albums,
    list_gallery_photos,
    list_tags,
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
    started_at = time.time()
    client = get_comfy_client()
    if not client.is_available():
        return jsonify({"ok": False, "error": "ComfyUI is not available"}), 503

    try:
        uploaded_images = {}
        with connect_db(DB_PATH) as conn:
            detail = get_photo_detail(conn, photo_id)
            if not detail:
                return jsonify({"ok": False, "error": "Photo not found"}), 404
            for reference in payload.get("references") or []:
                node_id = str(reference.get("node_id"))
                target_photo_id = reference.get("photo_id")
                if not node_id or not target_photo_id:
                    continue
                image_path = find_photo_file(conn, int(target_photo_id))
                if not image_path:
                    return jsonify({"ok": False, "error": f"Reference photo {target_photo_id} not found"}), 400
                uploaded_images[node_id] = client.upload_image(image_path)
            prompt, patch_info = patch_prompt(detail, payload, uploaded_images=uploaded_images)

        queued = client.queue_prompt(prompt)
        prompt_id = queued.get("prompt_id")
        if not prompt_id:
            raise ComfyGenerationError("ComfyUI did not return a prompt_id")
        history = client.wait_for_history(prompt_id)
        output_filenames = extract_history_filenames(history)

        scan_albums(DB_PATH, IMAGES_ROOT, THUMBNAIL_ROOT, scan_metadata=True)
        with connect_db(DB_PATH) as conn:
            generated_photo_id = find_output_photo_by_name(conn, output_filenames)
            if generated_photo_id is None:
                generated_photo_id = find_latest_output_photo_after(conn, started_at - 1)
            generated_photo = None
            if generated_photo_id and generated_photo_id != photo_id:
                create_photo_link(conn, photo_id, generated_photo_id, "variant")
                generated_photo = get_photo_detail(conn, generated_photo_id)
        return jsonify(
            {
                "ok": True,
                "prompt_id": prompt_id,
                "seed": patch_info.get("seed"),
                "output_filenames": output_filenames,
                "photo": generated_photo,
            }
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except ComfyUnavailable as exc:
        return jsonify({"ok": False, "error": f"ComfyUI is not available: {exc}"}), 503
    except ComfyGenerationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


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
