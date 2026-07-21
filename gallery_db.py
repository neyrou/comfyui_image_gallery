import hashlib
import json
import os
import sqlite3
import struct
import time
from pathlib import Path
from urllib.parse import quote

from PIL import Image

from metadata_extractor import extract_from_image


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
ALLOWED_ALBUM_TYPES = {"input", "output", "user"}
ALLOWED_LINK_TYPES = {"variant", "original"}


class GalleryConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect_db(db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, factory=GalleryConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path):
    with connect_db(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS albums (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('input', 'output', 'user')),
                path TEXT NOT NULL,
                scan_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checksum TEXT NOT NULL UNIQUE,
                width INTEGER,
                height INTEGER,
                file_size INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS album_photos (
                album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
                photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                relative_path TEXT NOT NULL,
                filename TEXT NOT NULL,
                mtime REAL NOT NULL,
                file_size INTEGER NOT NULL,
                is_missing INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(album_id, photo_id, relative_path)
            );

            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS photo_tags (
                photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                source TEXT NOT NULL DEFAULT 'manual',
                PRIMARY KEY(photo_id, tag_id)
            );

            CREATE TABLE IF NOT EXISTS album_tags (
                album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY(album_id, tag_id)
            );

            CREATE TABLE IF NOT EXISTS photo_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                target_photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                type TEXT NOT NULL CHECK(type IN ('variant', 'original')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_photo_id, target_photo_id, type)
            );

            CREATE TABLE IF NOT EXISTS photo_metadata (
                photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
                prompt TEXT,
                seed_noise TEXT,
                seed TEXT,
                raw_prompt_json TEXT,
                raw_workflow_json TEXT,
                scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS photo_loras (
                photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                node_id TEXT,
                lora_name TEXT NOT NULL,
                strength_model REAL,
                PRIMARY KEY(photo_id, node_id, lora_name)
            );

            CREATE TABLE IF NOT EXISTS photo_used_images (
                photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                image_name TEXT NOT NULL,
                PRIMARY KEY(photo_id, image_name)
            );

            CREATE TABLE IF NOT EXISTS face_identities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag_id INTEGER NOT NULL UNIQUE REFERENCES tags(id) ON DELETE CASCADE,
                review_threshold REAL NOT NULL DEFAULT 0.40,
                automatic_threshold REAL NOT NULL DEFAULT 0.55,
                margin_threshold REAL NOT NULL DEFAULT 0.08,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS face_photo_scans (
                photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
                checksum TEXT NOT NULL,
                model_name TEXT NOT NULL,
                model_version TEXT NOT NULL,
                provider TEXT,
                faces_count INTEGER NOT NULL DEFAULT 0,
                scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS photo_faces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                face_index INTEGER NOT NULL,
                bbox_json TEXT NOT NULL,
                detection_score REAL NOT NULL,
                embedding BLOB NOT NULL,
                embedding_dimensions INTEGER NOT NULL,
                model_name TEXT NOT NULL,
                model_version TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(photo_id, model_name, model_version, face_index)
            );

            CREATE TABLE IF NOT EXISTS face_references (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identity_id INTEGER NOT NULL REFERENCES face_identities(id) ON DELETE CASCADE,
                source_type TEXT NOT NULL CHECK(source_type IN ('gallery', 'upload')),
                source_photo_id INTEGER REFERENCES photos(id) ON DELETE SET NULL,
                source_face_id INTEGER REFERENCES photo_faces(id) ON DELETE SET NULL,
                file_path TEXT,
                bbox_json TEXT NOT NULL,
                detection_score REAL NOT NULL,
                embedding BLOB NOT NULL,
                embedding_dimensions INTEGER NOT NULL,
                model_name TEXT NOT NULL,
                model_version TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS face_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                face_id INTEGER NOT NULL REFERENCES photo_faces(id) ON DELETE CASCADE,
                identity_id INTEGER NOT NULL REFERENCES face_identities(id) ON DELETE CASCADE,
                score REAL NOT NULL,
                second_best_score REAL,
                state TEXT NOT NULL CHECK(state IN ('automatic', 'pending', 'confirmed', 'rejected')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                decided_at TEXT,
                UNIQUE(face_id, identity_id)
            );

            CREATE TABLE IF NOT EXISTS face_imports (
                token TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                original_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS face_import_faces (
                import_token TEXT NOT NULL REFERENCES face_imports(token) ON DELETE CASCADE,
                face_index INTEGER NOT NULL,
                bbox_json TEXT NOT NULL,
                detection_score REAL NOT NULL,
                embedding BLOB NOT NULL,
                embedding_dimensions INTEGER NOT NULL,
                model_name TEXT NOT NULL,
                model_version TEXT NOT NULL,
                PRIMARY KEY(import_token, face_index)
            );

            CREATE TABLE IF NOT EXISTS face_scan_jobs (
                id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'detect',
                state TEXT NOT NULL CHECK(state IN ('queued', 'running', 'cancel_requested', 'cancelled', 'done', 'error')),
                total INTEGER NOT NULL DEFAULT 0,
                processed INTEGER NOT NULL DEFAULT 0,
                recognized INTEGER NOT NULL DEFAULT 0,
                pending INTEGER NOT NULL DEFAULT 0,
                errors_count INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                error TEXT,
                params_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS face_scan_items (
                job_id TEXT NOT NULL REFERENCES face_scan_jobs(id) ON DELETE CASCADE,
                photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued', 'running', 'done', 'error')),
                error TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(job_id, photo_id)
            );

            CREATE TABLE IF NOT EXISTS face_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_album_photos_album ON album_photos(album_id, mtime DESC);
            CREATE INDEX IF NOT EXISTS idx_album_photos_photo ON album_photos(photo_id);
            CREATE INDEX IF NOT EXISTS idx_photo_tags_tag_photo ON photo_tags(tag_id, photo_id);
            CREATE INDEX IF NOT EXISTS idx_photo_links_source ON photo_links(source_photo_id);
            CREATE INDEX IF NOT EXISTS idx_photo_links_target ON photo_links(target_photo_id);
            CREATE INDEX IF NOT EXISTS idx_photo_faces_photo ON photo_faces(photo_id);
            CREATE INDEX IF NOT EXISTS idx_face_references_identity ON face_references(identity_id);
            CREATE INDEX IF NOT EXISTS idx_face_matches_face_state ON face_matches(face_id, state);
            CREATE INDEX IF NOT EXISTS idx_face_scan_items_state ON face_scan_items(job_id, state);
            """
        )
        _ensure_column(conn, "photo_tags", "source", "TEXT NOT NULL DEFAULT 'manual'")


def _ensure_column(conn, table_name, column_name, definition):
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def album_type_from_name(name):
    lowered = name.lower()
    if lowered == "output":
        return "output"
    if lowered == "input":
        return "input"
    return "user"


def discover_albums(conn, images_root):
    images_root = Path(images_root)
    images_root.mkdir(parents=True, exist_ok=True)
    for child in sorted(images_root.iterdir(), key=lambda item: item.name.lower()):
        try:
            is_album_dir = child.is_dir()
        except OSError:
            is_album_dir = True
        if not is_album_dir:
            continue
        conn.execute(
            """
            INSERT INTO albums(name, display_name, type, path)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                path=excluded.path,
                updated_at=CURRENT_TIMESTAMP
            """,
            (child.name, child.name, album_type_from_name(child.name), str(child)),
        )


def scan_albums(db_path, images_root, thumbnail_root, scan_metadata=False, progress_callback=None, commit_interval=25):
    init_db(db_path)
    summary = {"albums": 0, "photos": 0, "errors": []}
    with connect_db(db_path) as conn:
        discover_albums(conn, images_root)
        conn.commit()
        albums = conn.execute("SELECT * FROM albums ORDER BY name COLLATE NOCASE").fetchall()
        for album in albums:
            _report_scan_progress(
                progress_callback,
                event="album_start",
                album=album["name"],
                message=f"Scan album {album['name']}",
                photos=summary["photos"],
            )
            print(f"[scan] album '{album['name']}' start: {album['path']}", flush=True)
            result = scan_album(
                conn,
                album,
                thumbnail_root,
                scan_metadata=scan_metadata,
                progress_callback=progress_callback,
                commit_interval=commit_interval,
            )
            summary["albums"] += 1
            summary["photos"] += result["photos"]
            if result["error"]:
                summary["errors"].append({"album": album["name"], "error": result["error"]})
            conn.commit()
            print(
                f"[scan] album '{album['name']}' done: {result['photos']} photos"
                + (f", error: {result['error']}" if result["error"] else ""),
                flush=True,
            )
            _report_scan_progress(
                progress_callback,
                event="album_done",
                album=album["name"],
                message=f"Album {album['name']} termine",
                album_photos=result["photos"],
                photos=summary["photos"],
                error=result["error"],
            )
    return summary


def scan_album(conn, album, thumbnail_root, scan_metadata=False, progress_callback=None, commit_interval=25):
    album_path = Path(album["path"])
    seen_keys = set()
    count = 0
    error = None
    try:
        files = _iter_image_files(album_path)
        conn.execute("UPDATE albums SET scan_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (album["id"],))
    except OSError as exc:
        conn.execute(
            "UPDATE albums SET scan_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (str(exc), album["id"]),
        )
        conn.commit()
        return {"photos": 0, "error": str(exc)}

    try:
        for image_path in files:
            try:
                relative_path = image_path.relative_to(album_path).as_posix()
                stat = image_path.stat()
                checksum = checksum_file(image_path)
                width, height = image_size(image_path)
                photo_id = upsert_photo(conn, checksum, width, height, stat.st_size)
                conn.execute(
                    """
                    INSERT INTO album_photos(album_id, photo_id, relative_path, filename, mtime, file_size, is_missing)
                    VALUES (?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(album_id, photo_id, relative_path) DO UPDATE SET
                        filename=excluded.filename,
                        mtime=excluded.mtime,
                        file_size=excluded.file_size,
                        is_missing=0,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (album["id"], photo_id, relative_path, image_path.name, stat.st_mtime, stat.st_size),
                )
                seen_keys.add((photo_id, relative_path))
                ensure_thumbnail(image_path, thumbnail_root, checksum)
                if scan_metadata:
                    rescan_metadata(conn, photo_id, image_path)
                count += 1
                if count == 1 or count % commit_interval == 0:
                    conn.commit()
                    print(f"[scan] {album['name']}: {count} photos, current: {relative_path}", flush=True)
                    _report_scan_progress(
                        progress_callback,
                        event="file",
                        album=album["name"],
                        file=relative_path,
                        album_photos=count,
                        message=f"{album['name']}: {count} photos",
                    )
            except (OSError, ValueError) as exc:
                error = str(exc)
                print(f"[scan] {album['name']} error on {image_path}: {error}", flush=True)
                _report_scan_progress(
                    progress_callback,
                    event="file_error",
                    album=album["name"],
                    file=str(image_path),
                    error=error,
                    album_photos=count,
                )
    except OSError as exc:
        error = str(exc)
        conn.execute(
            "UPDATE albums SET scan_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (error, album["id"]),
        )
        conn.commit()
        return {"photos": count, "error": error}

    conn.commit()
    rows = conn.execute("SELECT photo_id, relative_path FROM album_photos WHERE album_id=?", (album["id"],)).fetchall()
    missing_count = 0
    for row in rows:
        if (row["photo_id"], row["relative_path"]) not in seen_keys:
            conn.execute(
                "UPDATE album_photos SET is_missing=1, updated_at=CURRENT_TIMESTAMP WHERE album_id=? AND photo_id=? AND relative_path=?",
                (album["id"], row["photo_id"], row["relative_path"]),
            )
            missing_count += 1
            if missing_count % commit_interval == 0:
                conn.commit()
    conn.commit()
    return {"photos": count, "error": error}


def _report_scan_progress(progress_callback, **payload):
    if progress_callback:
        try:
            progress_callback({"updated_at": time.time(), **payload})
        except Exception as exc:
            print(f"[scan] progress callback error: {exc}", flush=True)


def _iter_image_files(root):
    if not root.exists():
        return
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() in IMAGE_EXTENSIONS:
                yield path


def checksum_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_size(path):
    try:
        with Image.open(path) as image:
            return image.size
    except OSError:
        return None, None


def upsert_photo(conn, checksum, width, height, file_size):
    conn.execute(
        """
        INSERT INTO photos(checksum, width, height, file_size)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(checksum) DO UPDATE SET
            width=COALESCE(excluded.width, photos.width),
            height=COALESCE(excluded.height, photos.height),
            file_size=excluded.file_size,
            updated_at=CURRENT_TIMESTAMP
        """,
        (checksum, width, height, file_size),
    )
    return conn.execute("SELECT id FROM photos WHERE checksum=?", (checksum,)).fetchone()["id"]


def ensure_thumbnail(image_path, thumbnail_root, checksum):
    thumbnail_root = Path(thumbnail_root)
    thumbnail_root.mkdir(parents=True, exist_ok=True)
    thumbnail_path = thumbnail_root / f"{checksum}.jpg"
    if thumbnail_path.exists():
        return thumbnail_path
    with Image.open(image_path) as image:
        image.thumbnail((260, 260), Image.LANCZOS)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.save(thumbnail_path, "JPEG", quality=88)
    return thumbnail_path


def rescan_metadata(conn, photo_id, image_path):
    extracted = extract_from_image(image_path)
    conn.execute("DELETE FROM photo_loras WHERE photo_id=?", (photo_id,))
    conn.execute("DELETE FROM photo_used_images WHERE photo_id=?", (photo_id,))
    conn.execute(
        """
        INSERT INTO photo_metadata(photo_id, prompt, seed_noise, seed, raw_prompt_json, raw_workflow_json, scanned_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(photo_id) DO UPDATE SET
            prompt=excluded.prompt,
            seed_noise=excluded.seed_noise,
            seed=excluded.seed,
            raw_prompt_json=excluded.raw_prompt_json,
            raw_workflow_json=excluded.raw_workflow_json,
            scanned_at=CURRENT_TIMESTAMP
        """,
        (
            photo_id,
            extracted.prompt,
            _string_or_none(extracted.seed_noise),
            _string_or_none(extracted.seed),
            _json_or_none(extracted.raw_prompt),
            _json_or_none(extracted.raw_workflow),
        ),
    )
    for lora in extracted.loras:
        conn.execute(
            """
            INSERT OR IGNORE INTO photo_loras(photo_id, node_id, lora_name, strength_model)
            VALUES (?, ?, ?, ?)
            """,
            (photo_id, lora.get("node_id"), lora["lora_name"], lora.get("strength_model")),
        )
    for image_name in extracted.used_images:
        conn.execute(
            "INSERT OR IGNORE INTO photo_used_images(photo_id, image_name) VALUES (?, ?)",
            (photo_id, image_name),
        )
        for source_photo_id in find_source_photo_ids_for_used_image(conn, photo_id, image_name):
            conn.execute(
                """
                INSERT OR IGNORE INTO photo_links(source_photo_id, target_photo_id, type)
                VALUES (?, ?, 'original')
                """,
                (photo_id, source_photo_id),
            )
    return extracted


def find_source_photo_ids_for_used_image(conn, current_photo_id, image_name):
    normalized = _normalize_relative_image_name(image_name)
    basename = Path(normalized).name
    exact_rows = conn.execute(
        """
        SELECT DISTINCT photo_id
        FROM album_photos
        WHERE is_missing=0
          AND photo_id != ?
          AND REPLACE(relative_path, '\\', '/') = ?
        """,
        (current_photo_id, normalized),
    ).fetchall()
    rows = exact_rows
    if not rows and basename:
        rows = conn.execute(
            """
            SELECT DISTINCT photo_id
            FROM album_photos
            WHERE is_missing=0
              AND photo_id != ?
              AND filename = ?
            """,
            (current_photo_id, basename),
        ).fetchall()
    return [row["photo_id"] for row in rows]


def _normalize_relative_image_name(image_name):
    return str(image_name).replace("\\", "/").lstrip("/")


def _string_or_none(value):
    if value is None:
        return None
    return str(value)


def _json_or_none(value):
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def list_albums(conn):
    rows = conn.execute(
        """
        SELECT a.*,
               COUNT(ap.photo_id) FILTER (WHERE ap.is_missing = 0) AS photo_count,
               GROUP_CONCAT(t.name, ',') AS tags
        FROM albums a
        LEFT JOIN album_photos ap ON ap.album_id = a.id
        LEFT JOIN album_tags at ON at.album_id = a.id
        LEFT JOIN tags t ON t.id = at.tag_id
        GROUP BY a.id
        ORDER BY a.name COLLATE NOCASE
        """
    ).fetchall()
    return [dict(row) | {"tags": _split_tags(row["tags"])} for row in rows]


def get_album_by_name(conn, name):
    return conn.execute("SELECT * FROM albums WHERE name=?", (name,)).fetchone()


def _normalized_tag_names(tag_names):
    return list(dict.fromkeys(name.strip() for name in (tag_names or []) if name and name.strip()))


def _photo_tag_filter_sql(include_tags, exclude_tags, photo_id_expression="ap.photo_id"):
    clauses = []
    params = []
    for tag_name in _normalized_tag_names(include_tags):
        clauses.append(
            f"""
            EXISTS (
                SELECT 1
                FROM photo_tags filter_pt
                JOIN tags filter_t ON filter_t.id = filter_pt.tag_id
                WHERE filter_pt.photo_id = {photo_id_expression} AND filter_t.name = ?
            )
            """
        )
        params.append(tag_name)

    excluded = _normalized_tag_names(exclude_tags)
    if excluded:
        placeholders = ", ".join("?" for _ in excluded)
        clauses.append(
            f"""
            NOT EXISTS (
                SELECT 1
                FROM photo_tags excluded_pt
                JOIN tags excluded_t ON excluded_t.id = excluded_pt.tag_id
                WHERE excluded_pt.photo_id = {photo_id_expression}
                  AND excluded_t.name IN ({placeholders})
            )
            """
        )
        params.extend(excluded)

    return " AND ".join(clauses), params


def list_gallery_photos(conn, album_name, page=1, per_page=100, include_tags=None, exclude_tags=None):
    album = get_album_by_name(conn, album_name)
    if not album:
        return None, [], 0
    filter_sql, filter_params = _photo_tag_filter_sql(include_tags, exclude_tags)
    filter_clause = f" AND {filter_sql}" if filter_sql else ""
    total = conn.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM album_photos ap
        WHERE ap.album_id=? AND ap.is_missing=0{filter_clause}
        """,
        (album["id"], *filter_params),
    ).fetchone()["total"]
    offset = max(page - 1, 0) * per_page
    rows = conn.execute(
        f"""
        SELECT p.*, ap.relative_path, ap.filename, ap.mtime, ap.file_size AS album_file_size,
               a.name AS album_name,
               fav.has_output, fav.has_user, fav.album_count, fav.user_album_count,
               GROUP_CONCAT(DISTINCT t.name) AS tags
        FROM album_photos ap
        JOIN photos p ON p.id = ap.photo_id
        JOIN albums a ON a.id = ap.album_id
        LEFT JOIN photo_tags pt ON pt.photo_id = p.id
        LEFT JOIN tags t ON t.id = pt.tag_id
        JOIN (
            SELECT ap2.photo_id,
                   MAX(CASE WHEN a2.type='output' THEN 1 ELSE 0 END) AS has_output,
                   MAX(CASE WHEN a2.type='user' THEN 1 ELSE 0 END) AS has_user,
                   COUNT(DISTINCT ap2.album_id) AS album_count,
                   COUNT(DISTINCT CASE WHEN a2.type='user' THEN ap2.album_id END) AS user_album_count
            FROM album_photos ap2
            JOIN albums a2 ON a2.id = ap2.album_id
            WHERE ap2.is_missing = 0
            GROUP BY ap2.photo_id
        ) fav ON fav.photo_id = p.id
        WHERE ap.album_id=? AND ap.is_missing=0{filter_clause}
        GROUP BY p.id, ap.relative_path
        ORDER BY ap.mtime DESC
        LIMIT ? OFFSET ?
        """,
        (album["id"], *filter_params, per_page, offset),
    ).fetchall()
    return dict(album), [serialize_gallery_photo(row) for row in rows], total


def list_album_tag_stats(conn, album_name):
    album = get_album_by_name(conn, album_name)
    if not album:
        return []
    rows = conn.execute(
        """
        SELECT t.id, t.name, COUNT(*) AS occurrence_count
        FROM album_photos ap
        JOIN photo_tags pt ON pt.photo_id = ap.photo_id
        JOIN tags t ON t.id = pt.tag_id
        WHERE ap.album_id=? AND ap.is_missing=0
        GROUP BY t.id, t.name
        ORDER BY occurrence_count DESC, t.name COLLATE NOCASE
        """,
        (album["id"],),
    ).fetchall()
    return [dict(row) for row in rows]


def serialize_gallery_photo(row):
    data = dict(row)
    album_name = data["album_name"]
    relative_path = data["relative_path"]
    checksum = data["checksum"]
    return {
        "id": data["id"],
        "checksum": checksum,
        "filename": data["filename"],
        "relative_path": relative_path,
        "album_name": album_name,
        "width": data["width"],
        "height": data["height"],
        "mtime": data["mtime"],
        "tags": _split_tags(data.get("tags")),
        "favorite": bool(data["has_output"] and data["has_user"]),
        "album_count": data["album_count"],
        "user_album_count": data["user_album_count"],
        "original_url": f"/static/images/{quote(album_name)}/{quote(relative_path)}",
        "thumbnail_url": f"/static/thumbnails/{checksum}.jpg",
    }


def get_photo_detail(conn, photo_id):
    photo = conn.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    if not photo:
        return None
    memberships = conn.execute(
        """
        SELECT a.id AS album_id, a.name AS album_name, a.type, a.path AS album_path,
               a.scan_error, ap.relative_path, ap.filename
        FROM album_photos ap
        JOIN albums a ON a.id = ap.album_id
        WHERE ap.photo_id=? AND ap.is_missing=0
        ORDER BY a.name COLLATE NOCASE
        """,
        (photo_id,),
    ).fetchall()
    metadata = conn.execute("SELECT * FROM photo_metadata WHERE photo_id=?", (photo_id,)).fetchone()
    loras = conn.execute("SELECT node_id, lora_name, strength_model FROM photo_loras WHERE photo_id=? ORDER BY lora_name", (photo_id,)).fetchall()
    used_images = conn.execute("SELECT image_name FROM photo_used_images WHERE photo_id=? ORDER BY image_name", (photo_id,)).fetchall()
    links = list_photo_links(conn, photo_id)
    tags = conn.execute(
        """
        SELECT t.id, t.name, pt.source FROM tags t
        JOIN photo_tags pt ON pt.tag_id=t.id
        WHERE pt.photo_id=?
        ORDER BY t.name COLLATE NOCASE
        """,
        (photo_id,),
    ).fetchall()
    serialized_memberships = []
    first_available = None
    for row in memberships:
        membership = dict(row)
        file_path = Path(membership.pop("album_path")) / membership["relative_path"]
        try:
            available = file_path.is_file()
        except OSError:
            available = False
        membership["available"] = available
        membership["original_url"] = (
            f"/static/images/{quote(membership['album_name'])}/{quote(membership['relative_path'])}"
            if available
            else None
        )
        serialized_memberships.append(membership)
        if available and first_available is None:
            first_available = membership
    return {
        "id": photo["id"],
        "checksum": photo["checksum"],
        "width": photo["width"],
        "height": photo["height"],
        "thumbnail_url": f"/static/thumbnails/{photo['checksum']}.jpg",
        "original_url": first_available["original_url"] if first_available else None,
        "memberships": serialized_memberships,
        "tags": [dict(row) for row in tags],
        "metadata": dict(metadata) if metadata else None,
        "loras": [dict(row) for row in loras],
        "used_images": [row["image_name"] for row in used_images],
        "links": links,
        "face_analysis": list_photo_faces(conn, photo_id),
    }


def list_photo_links(conn, photo_id):
    rows = conn.execute(
        """
        SELECT pl.id, pl.type, pl.source_photo_id, pl.target_photo_id,
               p.id AS linked_photo_id, p.checksum,
               ap.filename, ap.relative_path, a.name AS album_name
        FROM photo_links pl
        JOIN photos p ON p.id = CASE WHEN pl.source_photo_id=? THEN pl.target_photo_id ELSE pl.source_photo_id END
        LEFT JOIN album_photos ap ON ap.photo_id = p.id AND ap.is_missing=0
        LEFT JOIN albums a ON a.id = ap.album_id
        WHERE pl.source_photo_id=? OR pl.target_photo_id=?
        GROUP BY pl.id
        ORDER BY pl.type, ap.mtime DESC
        """,
        (photo_id, photo_id, photo_id),
    ).fetchall()
    links = []
    for row in rows:
        data = dict(row)
        links.append(
            {
                "id": data["id"],
                "type": data["type"],
                "source_photo_id": data["source_photo_id"],
                "target_photo_id": data["target_photo_id"],
                "linked_photo_id": data["linked_photo_id"],
                "filename": data["filename"] or data["checksum"][:12],
                "thumbnail_url": f"/static/thumbnails/{data['checksum']}.jpg",
                "original_url": f"/static/images/{quote(data['album_name'])}/{quote(data['relative_path'])}" if data["album_name"] else None,
            }
        )
    return links


def find_photo_file(conn, photo_id):
    rows = conn.execute(
        """
        SELECT a.path, ap.relative_path
        FROM album_photos ap
        JOIN albums a ON a.id=ap.album_id
        WHERE ap.photo_id=? AND ap.is_missing=0
        ORDER BY CASE a.type WHEN 'output' THEN 0 WHEN 'user' THEN 1 ELSE 2 END
        """,
        (photo_id,),
    ).fetchall()
    for row in rows:
        candidate = Path(row["path"]) / row["relative_path"]
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def find_photo_file_in_album(conn, photo_id, album_name=None):
    params = [photo_id]
    album_filter = ""
    if album_name:
        album_filter = "AND a.name=?"
        params.append(album_name)
    rows = conn.execute(
        f"""
        SELECT a.id AS album_id, a.name AS album_name, a.path, ap.relative_path
        FROM album_photos ap
        JOIN albums a ON a.id=ap.album_id
        WHERE ap.photo_id=? AND ap.is_missing=0 {album_filter}
        ORDER BY CASE a.type WHEN 'output' THEN 0 WHEN 'user' THEN 1 ELSE 2 END, a.name COLLATE NOCASE
        """,
        params,
    ).fetchall()
    for row in rows:
        candidate = Path(row["path"]) / row["relative_path"]
        try:
            if candidate.is_file():
                return dict(row) | {"path": candidate}
        except OSError:
            continue
    return None


def delete_photo(conn, photo_id, thumbnail_root):
    photo = conn.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    if not photo:
        return None
    memberships = conn.execute(
        """
        SELECT a.path, ap.relative_path
        FROM album_photos ap
        JOIN albums a ON a.id=ap.album_id
        WHERE ap.photo_id=? AND ap.is_missing=0
        """,
        (photo_id,),
    ).fetchall()
    deleted_files = []
    seen_paths = set()
    for membership in memberships:
        image_path = (Path(membership["path"]) / membership["relative_path"]).resolve()
        if image_path in seen_paths:
            continue
        seen_paths.add(image_path)
        if image_path.exists():
            if not image_path.is_file():
                raise OSError(f"Not a file: {image_path}")
            image_path.unlink()
            deleted_files.append(str(image_path))
    thumbnail_path = Path(thumbnail_root) / f"{photo['checksum']}.jpg"
    if thumbnail_path.exists():
        thumbnail_path.unlink()
        deleted_files.append(str(thumbnail_path))
    conn.execute("DELETE FROM photos WHERE id=?", (photo_id,))
    return {"photo_id": photo_id, "checksum": photo["checksum"], "deleted_files": deleted_files}


def find_output_photo_by_name(conn, names):
    normalized_names = [_normalize_relative_image_name(name) for name in names if name]
    if not normalized_names:
        return None
    basenames = [Path(name).name for name in normalized_names]
    placeholders = ",".join("?" for _ in normalized_names)
    basename_placeholders = ",".join("?" for _ in basenames)
    row = conn.execute(
        f"""
        SELECT p.id
        FROM photos p
        JOIN album_photos ap ON ap.photo_id=p.id AND ap.is_missing=0
        JOIN albums a ON a.id=ap.album_id
        WHERE a.type='output'
          AND (
            REPLACE(ap.relative_path, '\\', '/') IN ({placeholders})
            OR ap.filename IN ({basename_placeholders})
          )
        ORDER BY ap.mtime DESC
        LIMIT 1
        """,
        (*normalized_names, *basenames),
    ).fetchone()
    return row["id"] if row else None


def find_latest_output_photo_after(conn, since_timestamp):
    row = conn.execute(
        """
        SELECT p.id
        FROM photos p
        JOIN album_photos ap ON ap.photo_id=p.id AND ap.is_missing=0
        JOIN albums a ON a.id=ap.album_id
        WHERE a.type='output' AND ap.mtime >= ?
        ORDER BY ap.mtime DESC
        LIMIT 1
        """,
        (since_timestamp,),
    ).fetchone()
    return row["id"] if row else None


def import_output_photo(conn, image_path, thumbnail_root):
    image_path = Path(image_path)
    album = find_output_album_for_path(conn, image_path)
    if not album:
        raise ValueError("No output album contains this generated image")
    return import_photo_into_album(conn, image_path, album, thumbnail_root)


def import_photo_into_album(conn, image_path, album, thumbnail_root):
    image_path = Path(image_path)
    relative_path = image_path.relative_to(Path(album["path"])).as_posix()
    stat = image_path.stat()
    checksum = checksum_file(image_path)
    width, height = image_size(image_path)
    photo_id = upsert_photo(conn, checksum, width, height, stat.st_size)
    conn.execute(
        """
        INSERT INTO album_photos(album_id, photo_id, relative_path, filename, mtime, file_size, is_missing)
        VALUES (?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(album_id, photo_id, relative_path) DO UPDATE SET
            filename=excluded.filename,
            mtime=excluded.mtime,
            file_size=excluded.file_size,
            is_missing=0,
            updated_at=CURRENT_TIMESTAMP
        """,
        (album["id"], photo_id, relative_path, image_path.name, stat.st_mtime, stat.st_size),
    )
    ensure_thumbnail(image_path, thumbnail_root, checksum)
    rescan_metadata(conn, photo_id, image_path)
    return photo_id


def find_output_album_for_path(conn, image_path):
    image_path = Path(image_path).resolve()
    rows = conn.execute(
        """
        SELECT *
        FROM albums
        WHERE type='output'
        ORDER BY CASE name WHEN 'output' THEN 0 ELSE 1 END, name COLLATE NOCASE
        """
    ).fetchall()
    for row in rows:
        try:
            image_path.relative_to(Path(row["path"]).resolve())
            return row
        except ValueError:
            continue
    return None


def output_paths_from_history(conn, filenames):
    rows = conn.execute(
        """
        SELECT *
        FROM albums
        WHERE type='output'
        ORDER BY CASE name WHEN 'output' THEN 0 ELSE 1 END, name COLLATE NOCASE
        """
    ).fetchall()
    paths = []
    for filename in filenames:
        normalized = _normalize_relative_image_name(filename)
        for row in rows:
            candidate = Path(row["path"]) / normalized
            if candidate.exists():
                paths.append(candidate)
                break
            basename_candidate = Path(row["path"]) / Path(normalized).name
            if basename_candidate.exists():
                paths.append(basename_candidate)
                break
    return paths


def upsert_tag(conn, name):
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("Tag name is required")
    conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (cleaned,))
    return conn.execute("SELECT * FROM tags WHERE name=?", (cleaned,)).fetchone()


def set_photo_tags(conn, photo_id, tag_names):
    conn.execute("DELETE FROM photo_tags WHERE photo_id=?", (photo_id,))
    for name in tag_names:
        tag = upsert_tag(conn, name)
        conn.execute(
            "INSERT OR REPLACE INTO photo_tags(photo_id, tag_id, source) VALUES (?, ?, 'manual')",
            (photo_id, tag["id"]),
        )


def update_photo_tags(conn, photo_ids, tag_names, operation):
    photo_ids = list(dict.fromkeys(photo_ids))
    tag_names = _normalized_tag_names(tag_names)
    if operation == "add":
        tag_ids = [upsert_tag(conn, name)["id"] for name in tag_names]
        conn.executemany(
            """
            INSERT INTO photo_tags(photo_id, tag_id, source) VALUES (?, ?, 'manual')
            ON CONFLICT(photo_id, tag_id) DO UPDATE SET source='manual'
            """,
            ((photo_id, tag_id) for photo_id in photo_ids for tag_id in tag_ids),
        )
        return

    placeholders = ",".join("?" for _ in tag_names)
    tag_ids = [
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM tags WHERE name IN ({placeholders})",
            tag_names,
        ).fetchall()
    ]
    if not tag_ids:
        return
    photo_placeholders = ",".join("?" for _ in photo_ids)
    tag_placeholders = ",".join("?" for _ in tag_ids)
    conn.execute(
        f"DELETE FROM photo_tags WHERE photo_id IN ({photo_placeholders}) AND tag_id IN ({tag_placeholders})",
        (*photo_ids, *tag_ids),
    )


def set_album_tags(conn, album_id, tag_names):
    conn.execute("DELETE FROM album_tags WHERE album_id=?", (album_id,))
    for name in tag_names:
        tag = upsert_tag(conn, name)
        conn.execute("INSERT OR IGNORE INTO album_tags(album_id, tag_id) VALUES (?, ?)", (album_id, tag["id"]))


def list_tags(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM tags ORDER BY name COLLATE NOCASE").fetchall()]


def embedding_to_blob(embedding):
    values = tuple(float(value) for value in embedding)
    if not values:
        raise ValueError("Face embedding is empty")
    return struct.pack(f"<{len(values)}f", *values), len(values)


def embedding_from_blob(blob, dimensions):
    dimensions = int(dimensions)
    if dimensions <= 0 or len(blob) != dimensions * 4:
        raise ValueError("Invalid face embedding payload")
    return struct.unpack(f"<{dimensions}f", blob)


def _validate_face_thresholds(review_threshold, automatic_threshold, margin_threshold):
    review = float(review_threshold)
    automatic = float(automatic_threshold)
    margin = float(margin_threshold)
    if not 0 <= review <= automatic <= 1:
        raise ValueError("Thresholds must satisfy 0 <= review <= automatic <= 1")
    if not 0 <= margin <= 1:
        raise ValueError("Margin threshold must be between 0 and 1")
    return review, automatic, margin


def create_face_identity(
    conn,
    tag_name,
    review_threshold=0.40,
    automatic_threshold=0.55,
    margin_threshold=0.08,
    enabled=True,
):
    review, automatic, margin = _validate_face_thresholds(
        review_threshold, automatic_threshold, margin_threshold
    )
    tag = upsert_tag(conn, tag_name)
    existing = conn.execute("SELECT id FROM face_identities WHERE tag_id=?", (tag["id"],)).fetchone()
    if existing:
        raise ValueError("This tag already has a face identity")
    cursor = conn.execute(
        """
        INSERT INTO face_identities(tag_id, review_threshold, automatic_threshold, margin_threshold, enabled)
        VALUES (?, ?, ?, ?, ?)
        """,
        (tag["id"], review, automatic, margin, int(bool(enabled))),
    )
    return get_face_identity(conn, cursor.lastrowid)


def get_face_identity(conn, identity_id):
    row = conn.execute(
        """
        SELECT fi.*, t.name AS tag_name,
               COUNT(fr.id) AS reference_count
        FROM face_identities fi
        JOIN tags t ON t.id=fi.tag_id
        LEFT JOIN face_references fr ON fr.identity_id=fi.id
        WHERE fi.id=?
        GROUP BY fi.id
        """,
        (identity_id,),
    ).fetchone()
    return _serialize_face_identity(conn, row) if row else None


def list_face_identities(conn, include_references=True):
    rows = conn.execute(
        """
        SELECT fi.*, t.name AS tag_name,
               COUNT(fr.id) AS reference_count
        FROM face_identities fi
        JOIN tags t ON t.id=fi.tag_id
        LEFT JOIN face_references fr ON fr.identity_id=fi.id
        GROUP BY fi.id
        ORDER BY t.name COLLATE NOCASE
        """
    ).fetchall()
    identities = []
    for row in rows:
        data = _serialize_face_identity(conn, row)
        if not include_references:
            data.pop("references", None)
        identities.append(data)
    return identities


def _serialize_face_identity(conn, row):
    data = dict(row)
    data["enabled"] = bool(data["enabled"])
    references = conn.execute(
        """
        SELECT id, identity_id, source_type, source_photo_id, source_face_id, file_path,
               bbox_json, detection_score, model_name, model_version, created_at
        FROM face_references WHERE identity_id=? ORDER BY id
        """,
        (data["id"],),
    ).fetchall()
    data["references"] = [
        dict(reference)
        | {
            "bbox": json.loads(reference["bbox_json"]),
            "crop_url": f"/api/face-references/{reference['id']}/crop",
        }
        for reference in references
    ]
    return data


def update_face_identity(conn, identity_id, **updates):
    current = get_face_identity(conn, identity_id)
    if not current:
        return None
    review, automatic, margin = _validate_face_thresholds(
        updates.get("review_threshold", current["review_threshold"]),
        updates.get("automatic_threshold", current["automatic_threshold"]),
        updates.get("margin_threshold", current["margin_threshold"]),
    )
    tag_id = current["tag_id"]
    if updates.get("tag_name") is not None:
        tag = upsert_tag(conn, updates["tag_name"])
        conflict = conn.execute(
            "SELECT id FROM face_identities WHERE tag_id=? AND id!=?", (tag["id"], identity_id)
        ).fetchone()
        if conflict:
            raise ValueError("This tag already has a face identity")
        tag_id = tag["id"]
    enabled = int(bool(updates.get("enabled", current["enabled"])))
    conn.execute(
        """
        UPDATE face_identities
        SET tag_id=?, review_threshold=?, automatic_threshold=?, margin_threshold=?, enabled=?,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (tag_id, review, automatic, margin, enabled, identity_id),
    )
    return get_face_identity(conn, identity_id)


def delete_face_identity(conn, identity_id):
    row = conn.execute("SELECT tag_id FROM face_identities WHERE id=?", (identity_id,)).fetchone()
    if not row:
        return False
    conn.execute(
        "DELETE FROM photo_tags WHERE tag_id=? AND source IN ('face_auto', 'face_confirmed')",
        (row["tag_id"],),
    )
    conn.execute("DELETE FROM face_identities WHERE id=?", (identity_id,))
    return True


def replace_photo_faces(conn, photo_id, checksum, detections, model_name, model_version, provider=None):
    conn.execute(
        "DELETE FROM photo_tags WHERE photo_id=? AND source IN ('face_auto', 'face_confirmed')",
        (photo_id,),
    )
    conn.execute("DELETE FROM photo_faces WHERE photo_id=?", (photo_id,))
    face_ids = []
    for face_index, detection in enumerate(detections):
        blob, dimensions = embedding_to_blob(detection.embedding)
        cursor = conn.execute(
            """
            INSERT INTO photo_faces(
                photo_id, face_index, bbox_json, detection_score, embedding, embedding_dimensions,
                model_name, model_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                photo_id,
                face_index,
                json.dumps(list(detection.bbox)),
                float(detection.detection_score),
                blob,
                dimensions,
                model_name,
                model_version,
            ),
        )
        face_ids.append(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO face_photo_scans(photo_id, checksum, model_name, model_version, provider, faces_count, scanned_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(photo_id) DO UPDATE SET
            checksum=excluded.checksum,
            model_name=excluded.model_name,
            model_version=excluded.model_version,
            provider=excluded.provider,
            faces_count=excluded.faces_count,
            scanned_at=CURRENT_TIMESTAMP
        """,
        (photo_id, checksum, model_name, model_version, provider, len(face_ids)),
    )
    return face_ids


def photo_face_cache_valid(conn, photo_id, checksum, model_name, model_version):
    row = conn.execute(
        "SELECT checksum, model_name, model_version FROM face_photo_scans WHERE photo_id=?", (photo_id,)
    ).fetchone()
    return bool(
        row
        and row["checksum"] == checksum
        and row["model_name"] == model_name
        and row["model_version"] == model_version
    )


def _reference_embeddings(conn, model_name, model_version):
    rows = conn.execute(
        """
        SELECT fr.identity_id, fr.embedding, fr.embedding_dimensions
        FROM face_references fr
        JOIN face_identities fi ON fi.id=fr.identity_id
        WHERE fi.enabled=1 AND fr.model_name=? AND fr.model_version=?
        """,
        (model_name, model_version),
    ).fetchall()
    return [
        {"identity_id": row["identity_id"], "embedding": embedding_from_blob(row["embedding"], row["embedding_dimensions"])}
        for row in rows
    ]


def rematch_photo_faces(conn, photo_id):
    from face_recognition import classify_identity

    faces = conn.execute("SELECT * FROM photo_faces WHERE photo_id=? ORDER BY face_index", (photo_id,)).fetchall()
    identities = list_face_identities(conn, include_references=False)
    references_by_model = {}
    recognized_identity_ids = set()
    confirmed_identity_ids = set()
    pending_count = 0
    for face in faces:
        existing_decisions = conn.execute(
            "SELECT identity_id, state FROM face_matches WHERE face_id=? AND state IN ('confirmed', 'rejected')",
            (face["id"],),
        ).fetchall()
        rejected_ids = {row["identity_id"] for row in existing_decisions if row["state"] == "rejected"}
        confirmed_ids = {row["identity_id"] for row in existing_decisions if row["state"] == "confirmed"}
        conn.execute(
            "DELETE FROM face_matches WHERE face_id=? AND state IN ('automatic', 'pending')", (face["id"],)
        )
        if confirmed_ids:
            confirmed_identity_ids.update(confirmed_ids)
            continue
        model_key = (face["model_name"], face["model_version"])
        if model_key not in references_by_model:
            references_by_model[model_key] = _reference_embeddings(conn, *model_key)
        references = references_by_model[model_key]
        embedding = embedding_from_blob(face["embedding"], face["embedding_dimensions"])
        result = classify_identity(embedding, identities, references, rejected_ids)
        if not result:
            continue
        conn.execute(
            """
            INSERT INTO face_matches(face_id, identity_id, score, second_best_score, state)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(face_id, identity_id) DO UPDATE SET
                score=excluded.score,
                second_best_score=excluded.second_best_score,
                state=CASE WHEN face_matches.state IN ('confirmed', 'rejected') THEN face_matches.state ELSE excluded.state END,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                face["id"],
                result["identity_id"],
                result["score"],
                result["second_best_score"],
                result["state"],
            ),
        )
        if result["state"] == "automatic":
            recognized_identity_ids.add(result["identity_id"])
        else:
            pending_count += 1
    _synchronize_face_tags(conn, photo_id, recognized_identity_ids, confirmed_identity_ids)
    return {"recognized": len(recognized_identity_ids | confirmed_identity_ids), "pending": pending_count}


def _synchronize_face_tags(conn, photo_id, automatic_identity_ids, confirmed_identity_ids):
    desired = set(automatic_identity_ids) | set(confirmed_identity_ids)
    desired_tag_ids = {
        row["id"]: row["tag_id"]
        for row in conn.execute(
            "SELECT id, tag_id FROM face_identities WHERE id IN ({})".format(
                ",".join("?" for _ in desired) or "NULL"
            ),
            tuple(desired),
        ).fetchall()
    }
    if desired_tag_ids:
        placeholders = ",".join("?" for _ in desired_tag_ids.values())
        conn.execute(
            f"""
            DELETE FROM photo_tags
            WHERE photo_id=? AND source IN ('face_auto', 'face_confirmed')
              AND tag_id NOT IN ({placeholders})
            """,
            (photo_id, *desired_tag_ids.values()),
        )
    else:
        conn.execute(
            "DELETE FROM photo_tags WHERE photo_id=? AND source IN ('face_auto', 'face_confirmed')", (photo_id,)
        )
    for identity_id, tag_id in desired_tag_ids.items():
        source = "face_confirmed" if identity_id in confirmed_identity_ids else "face_auto"
        conn.execute(
            """
            INSERT INTO photo_tags(photo_id, tag_id, source) VALUES (?, ?, ?)
            ON CONFLICT(photo_id, tag_id) DO UPDATE SET
                source=CASE WHEN photo_tags.source='manual' THEN 'manual' ELSE excluded.source END
            """,
            (photo_id, tag_id, source),
        )


def list_photo_faces(conn, photo_id):
    faces = conn.execute("SELECT * FROM photo_faces WHERE photo_id=? ORDER BY face_index", (photo_id,)).fetchall()
    results = []
    for face in faces:
        match = conn.execute(
            """
            SELECT fm.*, t.name AS tag_name
            FROM face_matches fm
            JOIN face_identities fi ON fi.id=fm.identity_id
            JOIN tags t ON t.id=fi.tag_id
            WHERE fm.face_id=?
            ORDER BY CASE fm.state WHEN 'confirmed' THEN 0 WHEN 'automatic' THEN 1 WHEN 'pending' THEN 2 ELSE 3 END,
                     fm.score DESC
            LIMIT 1
            """,
            (face["id"],),
        ).fetchone()
        results.append(
            {
                "id": face["id"],
                "face_index": face["face_index"],
                "bbox": json.loads(face["bbox_json"]),
                "detection_score": face["detection_score"],
                "model_name": face["model_name"],
                "model_version": face["model_version"],
                "crop_url": f"/api/photo-faces/{face['id']}/crop",
                "match": dict(match) if match else None,
            }
        )
    scan = conn.execute("SELECT * FROM face_photo_scans WHERE photo_id=?", (photo_id,)).fetchone()
    return {"scanned": bool(scan), "scan": dict(scan) if scan else None, "faces": results}


def add_gallery_face_reference(conn, identity_id, face_id):
    identity = get_face_identity(conn, identity_id)
    face = conn.execute("SELECT * FROM photo_faces WHERE id=?", (face_id,)).fetchone()
    if not identity:
        raise ValueError("Face identity not found")
    if not face:
        raise ValueError("Detected face not found")
    existing = conn.execute(
        "SELECT id FROM face_references WHERE identity_id=? AND source_face_id=?", (identity_id, face_id)
    ).fetchone()
    if existing:
        return existing["id"]
    cursor = conn.execute(
        """
        INSERT INTO face_references(
            identity_id, source_type, source_photo_id, source_face_id, bbox_json, detection_score,
            embedding, embedding_dimensions, model_name, model_version
        ) VALUES (?, 'gallery', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            identity_id,
            face["photo_id"],
            face_id,
            face["bbox_json"],
            face["detection_score"],
            face["embedding"],
            face["embedding_dimensions"],
            face["model_name"],
            face["model_version"],
        ),
    )
    return cursor.lastrowid


def create_face_import(conn, token, file_path, original_name, detections, model_name, model_version):
    conn.execute(
        "INSERT INTO face_imports(token, file_path, original_name) VALUES (?, ?, ?)",
        (token, str(file_path), original_name),
    )
    for face_index, detection in enumerate(detections):
        blob, dimensions = embedding_to_blob(detection.embedding)
        conn.execute(
            """
            INSERT INTO face_import_faces(
                import_token, face_index, bbox_json, detection_score, embedding, embedding_dimensions,
                model_name, model_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                face_index,
                json.dumps(list(detection.bbox)),
                detection.detection_score,
                blob,
                dimensions,
                model_name,
                model_version,
            ),
        )


def get_face_import(conn, token):
    imported = conn.execute("SELECT * FROM face_imports WHERE token=?", (token,)).fetchone()
    if not imported:
        return None
    faces = conn.execute(
        "SELECT face_index, bbox_json, detection_score, model_name, model_version FROM face_import_faces WHERE import_token=? ORDER BY face_index",
        (token,),
    ).fetchall()
    return dict(imported) | {
        "faces": [
            dict(face)
            | {
                "bbox": json.loads(face["bbox_json"]),
                "crop_url": f"/api/face-imports/{token}/faces/{face['face_index']}/crop",
            }
            for face in faces
        ]
    }


def add_imported_face_reference(conn, identity_id, token, face_index):
    identity = get_face_identity(conn, identity_id)
    imported = conn.execute("SELECT * FROM face_imports WHERE token=?", (token,)).fetchone()
    face = conn.execute(
        "SELECT * FROM face_import_faces WHERE import_token=? AND face_index=?", (token, face_index)
    ).fetchone()
    if not identity:
        raise ValueError("Face identity not found")
    if not imported or not face:
        raise ValueError("Imported face not found")
    cursor = conn.execute(
        """
        INSERT INTO face_references(
            identity_id, source_type, file_path, bbox_json, detection_score, embedding,
            embedding_dimensions, model_name, model_version
        ) VALUES (?, 'upload', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            identity_id,
            imported["file_path"],
            face["bbox_json"],
            face["detection_score"],
            face["embedding"],
            face["embedding_dimensions"],
            face["model_name"],
            face["model_version"],
        ),
    )
    conn.execute("DELETE FROM face_imports WHERE token=?", (token,))
    return cursor.lastrowid


def get_face_reference(conn, reference_id):
    return conn.execute("SELECT * FROM face_references WHERE id=?", (reference_id,)).fetchone()


def decide_face_match(conn, face_id, identity_id, decision):
    if decision not in {"confirmed", "rejected"}:
        raise ValueError("Decision must be confirmed or rejected")
    face = conn.execute("SELECT * FROM photo_faces WHERE id=?", (face_id,)).fetchone()
    identity = conn.execute("SELECT * FROM face_identities WHERE id=?", (identity_id,)).fetchone()
    if not face or not identity:
        raise ValueError("Face or identity not found")
    match = conn.execute(
        "SELECT * FROM face_matches WHERE face_id=? AND identity_id=?", (face_id, identity_id)
    ).fetchone()
    score = match["score"] if match else 1.0
    second_score = match["second_best_score"] if match else None
    conn.execute(
        """
        INSERT INTO face_matches(face_id, identity_id, score, second_best_score, state, decided_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(face_id, identity_id) DO UPDATE SET
            state=excluded.state, decided_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
        """,
        (face_id, identity_id, score, second_score, decision),
    )
    rematch_photo_faces(conn, face["photo_id"])
    return face["photo_id"]


def get_face_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM face_settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return row["value"]


def set_face_setting(conn, key, value):
    conn.execute(
        """
        INSERT INTO face_settings(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
        """,
        (key, json.dumps(value)),
    )


def create_face_scan_job(conn, job_id, scope, photo_ids, mode="detect", params=None):
    if scope not in {"selection", "album", "all", "automatic", "rematch", "photo"}:
        raise ValueError("Invalid face scan scope")
    if mode not in {"detect", "match"}:
        raise ValueError("Invalid face scan mode")
    photo_ids = list(dict.fromkeys(int(photo_id) for photo_id in photo_ids))
    if not photo_ids:
        raise ValueError("No photos to analyze")
    conn.execute(
        """
        INSERT INTO face_scan_jobs(id, scope, mode, state, total, message, params_json)
        VALUES (?, ?, ?, 'queued', ?, 'En attente', ?)
        """,
        (job_id, scope, mode, len(photo_ids), json.dumps(params or {})),
    )
    conn.executemany(
        "INSERT INTO face_scan_items(job_id, photo_id) VALUES (?, ?)",
        ((job_id, photo_id) for photo_id in photo_ids),
    )
    return get_face_scan_job(conn, job_id)


def get_face_scan_job(conn, job_id):
    row = conn.execute("SELECT * FROM face_scan_jobs WHERE id=?", (job_id,)).fetchone()
    return _serialize_face_scan_job(row) if row else None


def get_current_face_scan_job(conn):
    row = conn.execute(
        """
        SELECT * FROM face_scan_jobs
        ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'cancel_requested' THEN 1 WHEN 'queued' THEN 2 ELSE 3 END,
                 created_at DESC
        LIMIT 1
        """
    ).fetchone()
    return _serialize_face_scan_job(row) if row else None


def _serialize_face_scan_job(row):
    data = dict(row)
    data["active"] = data["state"] in {"queued", "running", "cancel_requested"}
    data["params"] = json.loads(data.pop("params_json") or "{}")
    return data


def photo_ids_for_face_scope(conn, scope, photo_ids=None, album_name=None, mode="detect"):
    if scope in {"selection", "photo"}:
        return list(dict.fromkeys(int(photo_id) for photo_id in (photo_ids or [])))
    if scope == "album":
        rows = conn.execute(
            """
            SELECT DISTINCT ap.photo_id FROM album_photos ap
            JOIN albums a ON a.id=ap.album_id
            WHERE a.name=? AND ap.is_missing=0
            """,
            (album_name,),
        ).fetchall()
    elif mode == "match" or scope == "rematch":
        rows = conn.execute("SELECT photo_id FROM face_photo_scans ORDER BY photo_id").fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT photo_id FROM album_photos WHERE is_missing=0 ORDER BY photo_id"
        ).fetchall()
    return [row["photo_id"] for row in rows]


def search_photos(conn, query, limit=30):
    like = f"%{query.strip()}%"
    rows = conn.execute(
        """
        SELECT p.id, p.checksum, ap.filename, ap.relative_path, a.name AS album_name
        FROM photos p
        JOIN album_photos ap ON ap.photo_id=p.id AND ap.is_missing=0
        JOIN albums a ON a.id=ap.album_id
        WHERE ap.filename LIKE ? OR ap.relative_path LIKE ? OR p.checksum LIKE ?
        GROUP BY p.id
        ORDER BY ap.mtime DESC
        LIMIT ?
        """,
        (like, like, like, limit),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "checksum": row["checksum"],
            "filename": row["filename"],
            "album_name": row["album_name"],
            "relative_path": row["relative_path"],
            "thumbnail_url": f"/static/thumbnails/{row['checksum']}.jpg",
        }
        for row in rows
    ]


def create_photo_link(conn, source_photo_id, target_photo_id, link_type):
    if link_type not in ALLOWED_LINK_TYPES:
        raise ValueError("Invalid link type")
    if int(source_photo_id) == int(target_photo_id):
        raise ValueError("A photo cannot link to itself")
    conn.execute(
        """
        INSERT OR IGNORE INTO photo_links(source_photo_id, target_photo_id, type)
        VALUES (?, ?, ?)
        """,
        (source_photo_id, target_photo_id, link_type),
    )


def update_album(conn, album_id, display_name=None, album_type=None, tags=None):
    if album_type and album_type not in ALLOWED_ALBUM_TYPES:
        raise ValueError("Invalid album type")
    if display_name is not None:
        conn.execute("UPDATE albums SET display_name=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (display_name.strip(), album_id))
    if album_type is not None:
        conn.execute("UPDATE albums SET type=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (album_type, album_id))
    if tags is not None:
        set_album_tags(conn, album_id, tags)


def _split_tags(value):
    if not value:
        return []
    return sorted({tag for tag in value.split(",") if tag})
