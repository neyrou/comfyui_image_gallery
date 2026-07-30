import hashlib
import json
import os
import re
import sqlite3
import struct
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageOps

from face_recognition import FACE_ATTRIBUTES_VERSION
from metadata_extractor import extract_from_image


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
ALLOWED_ALBUM_TYPES = {"input", "output", "user"}
ALLOWED_LINK_TYPES = {"variant", "original"}
SENSITIVITY_LEVELS = ("neutral", "low", "medium", "high")
SENSITIVITY_RANKS = {level: index for index, level in enumerate(SENSITIVITY_LEVELS)}
TAG_CATEGORIES = ("clothing", "person", "constraint")
CROSSDRESSING_TAG_NAME = "crossdressing"
CROSSDRESSING_MACHINE_KEY = "rule:crossdressing"
LEGACY_CROSSDRESS_TAG_NAME = "crossdress"
LEGACY_CROSSDRESS_MACHINE_KEY = "rule:crossdress"
FEMININE_TAG_FAMILY_PATTERNS = {
    "breast": re.compile(r"\bbreasts?\b"),
    "cleavage": re.compile(r"\bcleavage\b"),
    "bra": re.compile(r"\b(?:bras?|braless)\b"),
    "lingerie": re.compile(r"\blingerie\b"),
    "dress": re.compile(r"\b(?:dress|dresses|sundress|sundresses)\b"),
    "skirt": re.compile(r"\b(?:skirts?|mini ?skirts?|micro ?skirts?)\b"),
    "pantyhose": re.compile(r"\bpantyhose\b"),
    "stockings": re.compile(r"\bstockings?\b"),
    "thighhighs": re.compile(r"\b(?:thigh ?highs?|thighhighs?)\b"),
    "high heels": re.compile(r"\b(?:high ?heels?|stilettos?)\b"),
}
ALBUM_PATH_RETRY_DELAYS = (0.15, 0.35)
ALBUM_PATH_UNAVAILABLE_PREFIX = "Album path is unavailable:"


class ScanCancelled(RuntimeError):
    pass


class TagCategoryLocked(ValueError):
    pass


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
                machine_key TEXT,
                sensitivity TEXT NOT NULL DEFAULT 'neutral'
                    CHECK(sensitivity IN ('neutral', 'low', 'medium', 'high')),
                category TEXT
                    CHECK(category IS NULL OR category IN ('clothing', 'person', 'constraint')),
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
                unet_name TEXT,
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

            CREATE TABLE IF NOT EXISTS photo_image_analyses (
                photo_id INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
                checksum TEXT NOT NULL,
                analysis_signature TEXT NOT NULL,
                analysis_level TEXT NOT NULL
                    CHECK(analysis_level IN ('neutral', 'low', 'medium', 'high')),
                freepik_level TEXT NOT NULL
                    CHECK(freepik_level IN ('neutral', 'low', 'medium', 'high')),
                freepik_scores_json TEXT NOT NULL,
                nudenet_detections_json TEXT NOT NULL,
                automatic_tags_json TEXT NOT NULL,
                models_json TEXT NOT NULL,
                provider TEXT,
                analyzed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS face_identities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag_id INTEGER NOT NULL UNIQUE REFERENCES tags(id) ON DELETE CASCADE,
                sex TEXT NOT NULL DEFAULT 'ND' CHECK(sex IN ('ND', 'M', 'F')),
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
                attributes_version INTEGER NOT NULL DEFAULT 1,
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
                detected_sex TEXT CHECK(detected_sex IS NULL OR detected_sex IN ('ND', 'M', 'F')),
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
            CREATE INDEX IF NOT EXISTS idx_photo_image_analyses_level
                ON photo_image_analyses(analysis_level, photo_id);
            CREATE INDEX IF NOT EXISTS idx_photo_links_source ON photo_links(source_photo_id);
            CREATE INDEX IF NOT EXISTS idx_photo_links_target ON photo_links(target_photo_id);
            CREATE INDEX IF NOT EXISTS idx_photo_faces_photo ON photo_faces(photo_id);
            CREATE INDEX IF NOT EXISTS idx_face_references_identity ON face_references(identity_id);
            CREATE INDEX IF NOT EXISTS idx_face_matches_face_state ON face_matches(face_id, state);
            CREATE INDEX IF NOT EXISTS idx_face_scan_items_state ON face_scan_items(job_id, state);
            """
        )
        _ensure_column(conn, "photo_tags", "source", "TEXT NOT NULL DEFAULT 'manual'")
        _ensure_column(conn, "photo_metadata", "unet_name", "TEXT")
        _ensure_column(
            conn,
            "face_identities",
            "sex",
            "TEXT NOT NULL DEFAULT 'ND' CHECK(sex IN ('ND', 'M', 'F'))",
        )
        _ensure_column(
            conn,
            "photo_faces",
            "detected_sex",
            "TEXT CHECK(detected_sex IS NULL OR detected_sex IN ('ND', 'M', 'F'))",
        )
        _ensure_column(
            conn,
            "face_photo_scans",
            "attributes_version",
            "INTEGER NOT NULL DEFAULT 1",
        )
        _ensure_column(
            conn,
            "tags",
            "sensitivity",
            "TEXT NOT NULL DEFAULT 'neutral' CHECK(sensitivity IN ('neutral', 'low', 'medium', 'high'))",
        )
        _ensure_column(conn, "tags", "machine_key", "TEXT")
        _ensure_column(
            conn,
            "tags",
            "category",
            "TEXT CHECK(category IS NULL OR category IN ('clothing', 'person', 'constraint'))",
        )
        conn.execute(
            """
            UPDATE tags
            SET category='person'
            WHERE id IN (SELECT tag_id FROM face_identities)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_machine_key
            ON tags(machine_key)
            WHERE machine_key IS NOT NULL
            """
        )
        _init_lora_tag_mapping_schema(conn)


def _ensure_column(conn, table_name, column_name, definition):
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def _init_lora_tag_mapping_schema(conn):
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(lora_tag_mappings)").fetchall()
    }
    if "tag_id" in columns:
        conn.execute(
            "ALTER TABLE lora_tag_mappings RENAME TO lora_tag_mappings_legacy"
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lora_tag_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lora_name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lora_tag_mapping_tags (
            mapping_id INTEGER NOT NULL REFERENCES lora_tag_mappings(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY(mapping_id, tag_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lora_tag_mapping_tags_tag
        ON lora_tag_mapping_tags(tag_id)
        """
    )

    if "tag_id" in columns:
        conn.execute(
            """
            INSERT INTO lora_tag_mappings(id, lora_name, created_at)
            SELECT id, lora_name, created_at
            FROM lora_tag_mappings_legacy
            """
        )
        conn.execute(
            """
            INSERT INTO lora_tag_mapping_tags(mapping_id, tag_id)
            SELECT id, tag_id
            FROM lora_tag_mappings_legacy
            """
        )
        conn.execute("DROP TABLE lora_tag_mappings_legacy")


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


def scan_albums(
    db_path,
    images_root,
    thumbnail_root,
    scan_metadata=False,
    rescan_existing=True,
    progress_callback=None,
    cancel_callback=None,
    commit_interval=25,
    album_name=None,
):
    init_db(db_path)
    summary = {
        "albums": 0,
        "photos": 0,
        "processed": 0,
        "skipped": 0,
        "missing": 0,
        "processed_photo_ids": [],
        "errors": [],
    }
    with connect_db(db_path) as conn:
        discover_albums(conn, images_root)
        conn.commit()
        if album_name is None:
            albums = conn.execute("SELECT * FROM albums ORDER BY name COLLATE NOCASE").fetchall()
        else:
            album = get_album_by_name(conn, album_name)
            if not album:
                raise ValueError(f"Album introuvable: {album_name}")
            albums = [album]
        for album in albums:
            _raise_if_scan_cancelled(cancel_callback)
            _report_scan_progress(
                progress_callback,
                event="album_start",
                album=album["name"],
                message=f"Scan album {album['name']}",
                photos=summary["photos"],
            )
            print(f"[scan] album '{album['name']}' start: {album['path']}", flush=True)

            def report_album_progress(progress):
                payload = dict(progress)
                if "processed" in payload:
                    payload["processed"] += summary["processed"]
                if "skipped" in payload:
                    payload["skipped"] += summary["skipped"]
                if progress_callback:
                    progress_callback(payload)

            result = scan_album(
                conn,
                album,
                thumbnail_root,
                scan_metadata=scan_metadata,
                rescan_existing=rescan_existing,
                progress_callback=report_album_progress,
                cancel_callback=cancel_callback,
                commit_interval=commit_interval,
            )
            summary["albums"] += 1
            summary["photos"] += result["photos"]
            summary["processed"] += result["processed"]
            summary["skipped"] += result["skipped"]
            summary["missing"] += result["missing"]
            summary["processed_photo_ids"].extend(result["processed_photo_ids"])
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
                processed=summary["processed"],
                skipped=summary["skipped"],
                error=result["error"],
            )
    summary["processed_photo_ids"] = list(dict.fromkeys(summary["processed_photo_ids"]))
    return summary


def scan_album(
    conn,
    album,
    thumbnail_root,
    scan_metadata=False,
    rescan_existing=True,
    progress_callback=None,
    cancel_callback=None,
    commit_interval=25,
):
    album_path = Path(album["path"])
    seen_keys = set()
    protected_keys = set()
    processed_photo_ids = []
    count = 0
    processed = 0
    skipped = 0
    error = None

    try:
        for image_path in _iter_image_files(album_path):
            _raise_if_scan_cancelled(cancel_callback)
            relative_path = None
            try:
                relative_path = image_path.relative_to(album_path).as_posix()
                stat = image_path.stat()
                existing_rows = conn.execute(
                    """
                    SELECT photo_id, relative_path, mtime, file_size
                    FROM album_photos
                    WHERE album_id=? AND relative_path=?
                    """,
                    (album["id"], relative_path),
                ).fetchall()
                unchanged = next(
                    (
                        row
                        for row in existing_rows
                        if row["file_size"] == stat.st_size and row["mtime"] == stat.st_mtime
                    ),
                    None,
                )
                if not rescan_existing and unchanged:
                    conn.execute(
                        """
                        UPDATE album_photos
                        SET filename=?, is_missing=0, updated_at=CURRENT_TIMESTAMP
                        WHERE album_id=? AND photo_id=? AND relative_path=?
                        """,
                        (image_path.name, album["id"], unchanged["photo_id"], relative_path),
                    )
                    seen_keys.add((unchanged["photo_id"], relative_path))
                    count += 1
                    skipped += 1
                    if count == 1 or count % commit_interval == 0:
                        conn.commit()
                        _report_scan_progress(
                            progress_callback,
                            event="file",
                            album=album["name"],
                            file=relative_path,
                            album_photos=count,
                            processed=processed,
                            skipped=skipped,
                            message=f"{album['name']}: {count} photos",
                        )
                    continue
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
                processed += 1
                processed_photo_ids.append(photo_id)
                if count == 1 or count % commit_interval == 0:
                    conn.commit()
                    print(f"[scan] {album['name']}: {count} photos, current: {relative_path}", flush=True)
                    _report_scan_progress(
                        progress_callback,
                        event="file",
                        album=album["name"],
                        file=relative_path,
                        album_photos=count,
                        processed=processed,
                        skipped=skipped,
                        message=f"{album['name']}: {count} photos",
                    )
            except (OSError, ValueError) as exc:
                error = str(exc)
                if relative_path is not None:
                    rows = conn.execute(
                        "SELECT photo_id FROM album_photos WHERE album_id=? AND relative_path=?",
                        (album["id"], relative_path),
                    ).fetchall()
                    protected_keys.update((row["photo_id"], relative_path) for row in rows)
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
        return {
            "photos": count,
            "processed": processed,
            "skipped": skipped,
            "missing": 0,
            "processed_photo_ids": list(dict.fromkeys(processed_photo_ids)),
            "error": error,
        }

    _raise_if_scan_cancelled(cancel_callback)
    conn.commit()
    rows = conn.execute("SELECT photo_id, relative_path FROM album_photos WHERE album_id=?", (album["id"],)).fetchall()
    missing_count = 0
    for row in rows:
        _raise_if_scan_cancelled(cancel_callback)
        key = (row["photo_id"], row["relative_path"])
        if key not in seen_keys and key not in protected_keys:
            conn.execute(
                "UPDATE album_photos SET is_missing=1, updated_at=CURRENT_TIMESTAMP WHERE album_id=? AND photo_id=? AND relative_path=?",
                (album["id"], row["photo_id"], row["relative_path"]),
            )
            missing_count += 1
            if missing_count % commit_interval == 0:
                conn.commit()
    conn.execute(
        "UPDATE albums SET scan_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (error, album["id"]),
    )
    conn.commit()
    return {
        "photos": count,
        "processed": processed,
        "skipped": skipped,
        "missing": missing_count,
        "processed_photo_ids": list(dict.fromkeys(processed_photo_ids)),
        "error": error,
    }


def _report_scan_progress(progress_callback, **payload):
    if progress_callback:
        try:
            progress_callback({"updated_at": time.time(), **payload})
        except Exception as exc:
            print(f"[scan] progress callback error: {exc}", flush=True)


def _raise_if_scan_cancelled(cancel_callback):
    if cancel_callback and cancel_callback():
        raise ScanCancelled("Scan annule")


def _iter_image_files(root):
    last_error = None
    for retry_delay in (0, *ALBUM_PATH_RETRY_DELAYS):
        if retry_delay:
            time.sleep(retry_delay)
        try:
            with os.scandir(root):
                pass
            last_error = None
            break
        except OSError as exc:
            last_error = exc
    if last_error is not None:
        raise OSError(f"{ALBUM_PATH_UNAVAILABLE_PREFIX} {root}: {last_error}") from last_error

    def raise_walk_error(exc):
        raise exc

    for dirpath, _, filenames in os.walk(root, onerror=raise_walk_error):
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


def ensure_thumbnail(image_path, thumbnail_root, checksum, force=False):
    thumbnail_root = Path(thumbnail_root)
    thumbnail_root.mkdir(parents=True, exist_ok=True)
    thumbnail_path = thumbnail_root / f"{checksum}.jpg"
    if thumbnail_path.exists() and not force:
        return thumbnail_path
    with Image.open(image_path) as image:
        image = ImageOps.exif_transpose(image)
        image.thumbnail((260, 260), Image.LANCZOS)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.save(thumbnail_path, "JPEG", quality=88)
    return thumbnail_path


def ensure_preview(
    image_path,
    preview_root,
    checksum,
    force=False,
    max_size=(1600, 1600),
    quality=82,
):
    preview_root = Path(preview_root)
    preview_root.mkdir(parents=True, exist_ok=True)
    preview_path = preview_root / f"{checksum}.jpg"
    if preview_path.exists() and not force:
        return preview_path

    with tempfile.NamedTemporaryFile(
        dir=preview_root,
        prefix=f".{checksum}.",
        suffix=".jpg",
        delete=False,
    ) as temporary_file:
        temporary_path = Path(temporary_file.name)

    try:
        with Image.open(image_path) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail(max_size, Image.LANCZOS)
            if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (5, 5, 5))
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            image.save(
                temporary_path,
                "JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
        os.replace(temporary_path, preview_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return preview_path


def rescan_metadata(conn, photo_id, image_path):
    extracted = extract_from_image(image_path)
    conn.execute("DELETE FROM photo_loras WHERE photo_id=?", (photo_id,))
    conn.execute("DELETE FROM photo_used_images WHERE photo_id=?", (photo_id,))
    conn.execute(
        """
        INSERT INTO photo_metadata(photo_id, prompt, unet_name, seed_noise, seed, raw_prompt_json, raw_workflow_json, scanned_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(photo_id) DO UPDATE SET
            prompt=excluded.prompt,
            unet_name=excluded.unet_name,
            seed_noise=excluded.seed_noise,
            seed=excluded.seed,
            raw_prompt_json=excluded.raw_prompt_json,
            raw_workflow_json=excluded.raw_workflow_json,
            scanned_at=CURRENT_TIMESTAMP
        """,
        (
            photo_id,
            extracted.prompt,
            extracted.unet_name,
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
    sync_lora_tags(conn, photo_id, extracted.loras)
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
    sync_crossdress_tag(conn, photo_id)
    return extracted


def sync_lora_tags(conn, photo_id, loras):
    conn.execute(
        "DELETE FROM photo_tags WHERE photo_id=? AND source='lora_auto'",
        (photo_id,),
    )
    active_lora_names = []
    for lora in loras:
        try:
            is_active = float(lora.get("strength_model")) > 0
        except (TypeError, ValueError):
            is_active = False
        if is_active and lora.get("lora_name"):
            active_lora_names.append(lora["lora_name"])

    if not active_lora_names:
        return

    placeholders = ",".join("?" for _ in active_lora_names)
    rows = conn.execute(
        f"""
        SELECT DISTINCT lmtt.tag_id
        FROM lora_tag_mappings ltm
        JOIN lora_tag_mapping_tags lmtt ON lmtt.mapping_id=ltm.id
        WHERE ltm.lora_name IN ({placeholders})
        """,
        tuple(active_lora_names),
    ).fetchall()
    conn.executemany(
        """
        INSERT INTO photo_tags(photo_id, tag_id, source)
        VALUES (?, ?, 'lora_auto')
        ON CONFLICT(photo_id, tag_id) DO UPDATE SET
            source=CASE
                WHEN photo_tags.source='lora_auto' THEN excluded.source
                ELSE photo_tags.source
            END
        """,
        ((photo_id, row["tag_id"]) for row in rows),
    )


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
    albums = []
    for row in rows:
        available = is_album_path_available(row["path"])
        scan_error = row["scan_error"]
        if available and scan_error and scan_error.startswith(ALBUM_PATH_UNAVAILABLE_PREFIX):
            scan_error = None
        albums.append(
            dict(row)
            | {
                "tags": _split_tags(row["tags"]),
                "available": available,
                "scan_error": scan_error,
            }
        )
    return albums


def is_album_path_available(path):
    try:
        with os.scandir(path):
            return True
    except OSError:
        return False


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


def normalize_sensitivity(value, default="neutral"):
    value = str(value or default).strip().lower()
    if value not in SENSITIVITY_RANKS:
        raise ValueError("Invalid sensitivity")
    return value


def normalize_tag_category(value):
    if value is None:
        return None
    value = str(value).strip().lower()
    if value not in TAG_CATEGORIES:
        raise ValueError("Invalid tag category")
    return value


def _sensitivity_rank_sql(value_expression):
    return (
        f"CASE {value_expression} "
        "WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END"
    )


def _effective_sensitivity_rank_sql(photo_id_expression="p.id", analysis_alias="pia"):
    analysis_rank = _sensitivity_rank_sql(f"COALESCE({analysis_alias}.analysis_level, 'neutral')")
    tag_rank = _sensitivity_rank_sql("t_sensitivity.sensitivity")
    return f"""
        MAX(
            {analysis_rank},
            COALESCE((
                SELECT MAX({tag_rank})
                FROM photo_tags pt_sensitivity
                JOIN tags t_sensitivity ON t_sensitivity.id=pt_sensitivity.tag_id
                WHERE pt_sensitivity.photo_id={photo_id_expression}
            ), 0)
        )
    """


def sensitivity_from_rank(rank):
    try:
        return SENSITIVITY_LEVELS[max(0, min(int(rank), len(SENSITIVITY_LEVELS) - 1))]
    except (TypeError, ValueError):
        return "neutral"


def list_gallery_photos(
    conn,
    album_name,
    page=1,
    per_page=100,
    include_tags=None,
    exclude_tags=None,
    max_sensitivity="neutral",
):
    album = get_album_by_name(conn, album_name)
    if not album:
        return None, [], 0
    max_sensitivity = normalize_sensitivity(max_sensitivity)
    max_sensitivity_rank = SENSITIVITY_RANKS[max_sensitivity]
    filter_sql, filter_params = _photo_tag_filter_sql(include_tags, exclude_tags)
    filter_clause = f" AND {filter_sql}" if filter_sql else ""
    effective_rank_sql = _effective_sensitivity_rank_sql()
    sensitivity_clause = f" AND ({effective_rank_sql}) <= ?"
    total = conn.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM album_photos ap
        JOIN photos p ON p.id=ap.photo_id
        LEFT JOIN photo_image_analyses pia ON pia.photo_id=p.id
        WHERE ap.album_id=? AND ap.is_missing=0{filter_clause}{sensitivity_clause}
        """,
        (album["id"], *filter_params, max_sensitivity_rank),
    ).fetchone()["total"]
    if per_page is None:
        pagination_sql = ""
        pagination_params = ()
    else:
        offset = max(page - 1, 0) * per_page
        pagination_sql = "LIMIT ? OFFSET ?"
        pagination_params = (per_page, offset)
    rows = conn.execute(
        f"""
        SELECT p.*, ap.relative_path, ap.filename, ap.mtime, ap.file_size AS album_file_size,
               a.name AS album_name,
               fav.has_output, fav.has_user, fav.album_count, fav.user_album_count,
               GROUP_CONCAT(DISTINCT t.name) AS tags,
               pia.analysis_level,
               ({effective_rank_sql}) AS effective_sensitivity_rank
        FROM album_photos ap
        JOIN photos p ON p.id = ap.photo_id
        JOIN albums a ON a.id = ap.album_id
        LEFT JOIN photo_image_analyses pia ON pia.photo_id=p.id
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
        WHERE ap.album_id=? AND ap.is_missing=0{filter_clause}{sensitivity_clause}
        GROUP BY p.id, ap.relative_path
        ORDER BY ap.mtime DESC
        {pagination_sql}
        """,
        (album["id"], *filter_params, max_sensitivity_rank, *pagination_params),
    ).fetchall()
    return dict(album), [serialize_gallery_photo(row) for row in rows], total


def list_album_tag_facets(
    conn,
    album_id,
    include_tags=None,
    exclude_tags=None,
    max_sensitivity="neutral",
):
    include_tags = _normalized_tag_names(include_tags)
    exclude_tags = _normalized_tag_names(exclude_tags)
    max_sensitivity = normalize_sensitivity(max_sensitivity)
    max_sensitivity_rank = SENSITIVITY_RANKS[max_sensitivity]
    filter_sql, filter_params = _photo_tag_filter_sql(include_tags, exclude_tags)
    filter_clause = f" AND {filter_sql}" if filter_sql else ""
    effective_rank_sql = _effective_sensitivity_rank_sql()
    cte_sql = f"""
        WITH filtered_photos AS (
            SELECT DISTINCT ap.photo_id
            FROM album_photos ap
            JOIN photos p ON p.id=ap.photo_id
            LEFT JOIN photo_image_analyses pia ON pia.photo_id=p.id
            WHERE ap.album_id=?
              AND ap.is_missing=0
              {filter_clause}
              AND ({effective_rank_sql}) <= ?
        )
    """
    params = (int(album_id), *filter_params, max_sensitivity_rank)
    matching_photo_count = conn.execute(
        cte_sql + "SELECT COUNT(*) AS total FROM filtered_photos",
        params,
    ).fetchone()["total"]
    rows = conn.execute(
        cte_sql
        + """
        SELECT t.id, t.name, t.category,
               COUNT(DISTINCT fp.photo_id) AS occurrence_count
        FROM filtered_photos fp
        JOIN photo_tags pt ON pt.photo_id=fp.photo_id
        JOIN tags t ON t.id=pt.tag_id
        GROUP BY t.id, t.name, t.category
        HAVING COUNT(DISTINCT fp.photo_id) > 0
        ORDER BY occurrence_count DESC, t.name COLLATE NOCASE
        """,
        params,
    ).fetchall()

    active_names = list(include_tags)
    active_names.extend(name for name in exclude_tags if name not in include_tags)
    active_by_name = {}
    if active_names:
        placeholders = ",".join("?" for _ in active_names)
        active_by_name = {
            row["name"]: dict(row)
            for row in conn.execute(
                f"""
                SELECT id, name, category
                FROM tags
                WHERE name IN ({placeholders})
                """,
                tuple(active_names),
            ).fetchall()
        }
    active_tags = []
    for name in active_names:
        item = active_by_name.get(
            name,
            {"id": None, "name": name, "category": None},
        )
        active_tags.append(
            {
                **item,
                "filter_state": "include" if name in include_tags else "exclude",
            }
        )
    return {
        "matching_photo_count": matching_photo_count,
        "tags": [dict(row) for row in rows],
        "active_tags": active_tags,
    }


def list_album_tag_stats(
    conn,
    album_name,
    include_tags=None,
    exclude_tags=None,
    max_sensitivity="high",
):
    album = get_album_by_name(conn, album_name)
    if not album:
        return []
    return list_album_tag_facets(
        conn,
        album["id"],
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        max_sensitivity=max_sensitivity,
    )["tags"]


def original_photo_url(album_name, relative_path, checksum):
    return (
        f"/static/images/{quote(album_name)}/{quote(relative_path)}"
        f"?v={quote(checksum)}"
    )


def preview_photo_url(photo_id, checksum):
    return f"/api/photos/{photo_id}/preview?v={quote(checksum)}"


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
        "analysis_level": data.get("analysis_level"),
        "effective_sensitivity": sensitivity_from_rank(data.get("effective_sensitivity_rank")),
        "favorite": bool(data["has_output"] and data["has_user"]),
        "album_count": data["album_count"],
        "user_album_count": data["user_album_count"],
        "original_url": original_photo_url(album_name, relative_path, checksum),
        "preview_url": preview_photo_url(data["id"], checksum),
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
        SELECT t.id, t.name, t.category, pt.source FROM tags t
        JOIN photo_tags pt ON pt.tag_id=t.id
        WHERE pt.photo_id=?
        ORDER BY t.name COLLATE NOCASE
        """,
        (photo_id,),
    ).fetchall()
    image_analysis = conn.execute(
        "SELECT * FROM photo_image_analyses WHERE photo_id=?",
        (photo_id,),
    ).fetchone()
    effective_row = conn.execute(
        f"""
        SELECT ({_effective_sensitivity_rank_sql('p.id', 'pia')}) AS effective_sensitivity_rank
        FROM photos p
        LEFT JOIN photo_image_analyses pia ON pia.photo_id=p.id
        WHERE p.id=?
        """,
        (photo_id,),
    ).fetchone()
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
            original_photo_url(
                membership["album_name"],
                membership["relative_path"],
                photo["checksum"],
            )
            if available
            else None
        )
        serialized_memberships.append(membership)
        if available and first_available is None:
            first_available = membership
    serialized_analysis = None
    if image_analysis:
        serialized_analysis = dict(image_analysis)
        for source_key, target_key in (
            ("freepik_scores_json", "freepik_scores"),
            ("nudenet_detections_json", "nudenet_detections"),
            ("automatic_tags_json", "automatic_tags"),
            ("models_json", "models"),
        ):
            serialized_analysis[target_key] = json.loads(serialized_analysis.pop(source_key) or "null")
        serialized_analysis["scanned"] = True
    return {
        "id": photo["id"],
        "checksum": photo["checksum"],
        "width": photo["width"],
        "height": photo["height"],
        "thumbnail_url": f"/static/thumbnails/{photo['checksum']}.jpg",
        "preview_url": preview_photo_url(photo["id"], photo["checksum"]),
        "original_url": first_available["original_url"] if first_available else None,
        "memberships": serialized_memberships,
        "tags": [dict(row) for row in tags],
        "metadata": dict(metadata) if metadata else None,
        "loras": [dict(row) for row in loras],
        "used_images": [row["image_name"] for row in used_images],
        "links": links,
        "face_analysis": list_photo_faces(conn, photo_id),
        "image_analysis": serialized_analysis,
        "effective_sensitivity": sensitivity_from_rank(
            effective_row["effective_sensitivity_rank"] if effective_row else 0
        ),
    }


def list_photo_links(conn, photo_id):
    rows = conn.execute(
        """
        SELECT pl.id, pl.type, pl.source_photo_id, pl.target_photo_id,
               p.id AS linked_photo_id, p.checksum,
               ap.filename, ap.relative_path, a.name AS album_name,
               MAX(ap.mtime) AS linked_mtime,
               p.created_at AS linked_created_at
        FROM photo_links pl
        JOIN photos p ON p.id = CASE WHEN pl.source_photo_id=? THEN pl.target_photo_id ELSE pl.source_photo_id END
        LEFT JOIN album_photos ap ON ap.photo_id = p.id AND ap.is_missing=0
        LEFT JOIN albums a ON a.id = ap.album_id
        WHERE pl.source_photo_id=? OR pl.target_photo_id=?
        GROUP BY pl.id
        ORDER BY CASE pl.type WHEN 'original' THEN 0 ELSE 1 END,
                 linked_mtime IS NULL,
                 linked_mtime DESC,
                 linked_created_at DESC,
                 p.id DESC
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
                "checksum": data["checksum"],
                "filename": data["filename"] or data["checksum"][:12],
                "thumbnail_url": f"/static/thumbnails/{data['checksum']}.jpg",
                "preview_url": preview_photo_url(
                    data["linked_photo_id"],
                    data["checksum"],
                ),
                "original_url": (
                    original_photo_url(
                        data["album_name"],
                        data["relative_path"],
                        data["checksum"],
                    )
                    if data["album_name"]
                    else None
                ),
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


def delete_photo(conn, photo_id, thumbnail_root, preview_root=None):
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
    if preview_root is not None:
        preview_path = Path(preview_root) / f"{photo['checksum']}.jpg"
        if preview_path.exists():
            preview_path.unlink()
            deleted_files.append(str(preview_path))
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


def upsert_machine_tag(conn, machine_key, display_name):
    machine_key = str(machine_key or "").strip()
    display_name = str(display_name or "").strip()
    if not machine_key or not display_name:
        raise ValueError("Machine tag key and display name are required")
    row = conn.execute("SELECT * FROM tags WHERE machine_key=?", (machine_key,)).fetchone()
    if row:
        return row
    row = conn.execute("SELECT * FROM tags WHERE name=?", (display_name,)).fetchone()
    if row:
        if not row["machine_key"]:
            conn.execute("UPDATE tags SET machine_key=? WHERE id=?", (machine_key, row["id"]))
        return conn.execute("SELECT * FROM tags WHERE id=?", (row["id"],)).fetchone()
    conn.execute(
        "INSERT INTO tags(name, machine_key) VALUES (?, ?)",
        (display_name, machine_key),
    )
    return conn.execute("SELECT * FROM tags WHERE machine_key=?", (machine_key,)).fetchone()


def feminine_tag_families(tag_names):
    families = set()
    for tag_name in tag_names:
        normalized = re.sub(r"[^a-z0-9]+", " ", str(tag_name or "").lower()).strip()
        if not normalized:
            continue
        for family, pattern in FEMININE_TAG_FAMILY_PATTERNS.items():
            if pattern.search(normalized):
                families.add(family)
    return families


def resolved_single_face_sex(conn, photo_id):
    faces = conn.execute(
        "SELECT id, detected_sex FROM photo_faces WHERE photo_id=? ORDER BY face_index",
        (photo_id,),
    ).fetchall()
    if len(faces) != 1:
        return "ND"
    face = faces[0]
    match = conn.execute(
        """
        SELECT fi.sex
        FROM face_matches fm
        JOIN face_identities fi ON fi.id=fm.identity_id
        WHERE fm.face_id=? AND fm.state IN ('confirmed', 'automatic')
        ORDER BY CASE fm.state WHEN 'confirmed' THEN 0 ELSE 1 END, fm.score DESC
        LIMIT 1
        """,
        (face["id"],),
    ).fetchone()
    if match and match["sex"] in {"M", "F"}:
        return match["sex"]
    return face["detected_sex"] if face["detected_sex"] in {"M", "F"} else "ND"


def _get_crossdressing_tag(conn):
    target = conn.execute(
        """
        SELECT * FROM tags
        WHERE machine_key=? OR name=?
        ORDER BY machine_key=? DESC
        LIMIT 1
        """,
        (
            CROSSDRESSING_MACHINE_KEY,
            CROSSDRESSING_TAG_NAME,
            CROSSDRESSING_MACHINE_KEY,
        ),
    ).fetchone()
    legacy = conn.execute(
        """
        SELECT * FROM tags
        WHERE machine_key=? OR name=?
        ORDER BY machine_key=? DESC
        LIMIT 1
        """,
        (
            LEGACY_CROSSDRESS_MACHINE_KEY,
            LEGACY_CROSSDRESS_TAG_NAME,
            LEGACY_CROSSDRESS_MACHINE_KEY,
        ),
    ).fetchone()
    if not legacy:
        return target
    if not target:
        conn.execute(
            "UPDATE tags SET name=?, machine_key=? WHERE id=?",
            (CROSSDRESSING_TAG_NAME, CROSSDRESSING_MACHINE_KEY, legacy["id"]),
        )
        return conn.execute("SELECT * FROM tags WHERE id=?", (legacy["id"],)).fetchone()
    if legacy["id"] != target["id"]:
        conn.execute(
            """
            INSERT OR IGNORE INTO photo_tags(photo_id, tag_id, source)
            SELECT photo_id, ?, source FROM photo_tags WHERE tag_id=?
            """,
            (target["id"], legacy["id"]),
        )
        conn.execute("DELETE FROM photo_tags WHERE tag_id=?", (legacy["id"],))
        if legacy["machine_key"] == LEGACY_CROSSDRESS_MACHINE_KEY:
            conn.execute("UPDATE tags SET machine_key=NULL WHERE id=?", (legacy["id"],))
        if not target["machine_key"]:
            conn.execute(
                "UPDATE tags SET machine_key=? WHERE id=?",
                (CROSSDRESSING_MACHINE_KEY, target["id"]),
            )
            target = conn.execute("SELECT * FROM tags WHERE id=?", (target["id"],)).fetchone()
    return target


def sync_crossdress_tag(conn, photo_id):
    should_apply = False
    if resolved_single_face_sex(conn, photo_id) == "M":
        tag_names = [
            row["name"]
            for row in conn.execute(
                """
                SELECT t.name
                FROM photo_tags pt
                JOIN tags t ON t.id=pt.tag_id
                WHERE pt.photo_id=?
                  AND COALESCE(t.machine_key, '') NOT IN (?, ?)
                  AND t.name NOT IN (?, ?)
                """,
                (
                    photo_id,
                    CROSSDRESSING_MACHINE_KEY,
                    LEGACY_CROSSDRESS_MACHINE_KEY,
                    CROSSDRESSING_TAG_NAME,
                    LEGACY_CROSSDRESS_TAG_NAME,
                ),
            ).fetchall()
        ]
        should_apply = len(feminine_tag_families(tag_names)) >= 2

    crossdress = _get_crossdressing_tag(conn)
    if should_apply:
        crossdress = crossdress or upsert_machine_tag(
            conn,
            CROSSDRESSING_MACHINE_KEY,
            CROSSDRESSING_TAG_NAME,
        )
        conn.execute(
            """
            INSERT INTO photo_tags(photo_id, tag_id, source)
            VALUES (?, ?, 'rule_auto')
            ON CONFLICT(photo_id, tag_id) DO UPDATE SET
                source=CASE
                    WHEN photo_tags.source='rule_auto' THEN excluded.source
                    ELSE photo_tags.source
                END
            """,
            (photo_id, crossdress["id"]),
        )
        return True

    if crossdress:
        conn.execute(
            "DELETE FROM photo_tags WHERE photo_id=? AND tag_id=? AND source='rule_auto'",
            (photo_id, crossdress["id"]),
        )
    return False


def set_photo_tags(conn, photo_id, tag_names):
    conn.execute("DELETE FROM photo_tags WHERE photo_id=? AND source='manual'", (photo_id,))
    for name in tag_names:
        tag = upsert_tag(conn, name)
        conn.execute(
            """
            INSERT INTO photo_tags(photo_id, tag_id, source) VALUES (?, ?, 'manual')
            ON CONFLICT(photo_id, tag_id) DO UPDATE SET source='manual'
            """,
            (photo_id, tag["id"]),
        )
    sync_crossdress_tag(conn, photo_id)


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
        for photo_id in photo_ids:
            sync_crossdress_tag(conn, photo_id)
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
    for photo_id in photo_ids:
        sync_crossdress_tag(conn, photo_id)


def set_album_tags(conn, album_id, tag_names):
    conn.execute("DELETE FROM album_tags WHERE album_id=?", (album_id,))
    for name in tag_names:
        tag = upsert_tag(conn, name)
        conn.execute("INSERT OR IGNORE INTO album_tags(album_id, tag_id) VALUES (?, ?)", (album_id, tag["id"]))


def list_tags(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM tags ORDER BY name COLLATE NOCASE").fetchall()]


def list_tag_stats(conn):
    rows = conn.execute(
        """
        SELECT t.id, t.name, t.machine_key, t.sensitivity, t.category,
               CASE WHEN fi.id IS NULL THEN 0 ELSE 1 END AS is_face_tag,
               COUNT(DISTINCT pt.photo_id) AS occurrence_count
        FROM tags t
        JOIN photo_tags pt ON pt.tag_id=t.id
        JOIN album_photos ap ON ap.photo_id=pt.photo_id AND ap.is_missing=0
        LEFT JOIN face_identities fi ON fi.tag_id=t.id
        GROUP BY t.id
        ORDER BY occurrence_count DESC, t.name COLLATE NOCASE
        """
    ).fetchall()
    return [
        {
            **dict(row),
            "is_face_tag": bool(row["is_face_tag"]),
        }
        for row in rows
    ]


def set_tag_sensitivity(conn, tag_id, sensitivity):
    return update_tag_settings(conn, tag_id, sensitivity=sensitivity)


_TAG_SETTING_UNSET = object()


def update_tag_settings(
    conn,
    tag_id,
    sensitivity=_TAG_SETTING_UNSET,
    category=_TAG_SETTING_UNSET,
):
    tag_id = int(tag_id)
    current = conn.execute(
        """
        SELECT t.*,
               CASE WHEN fi.id IS NULL THEN 0 ELSE 1 END AS is_face_tag
        FROM tags t
        LEFT JOIN face_identities fi ON fi.tag_id=t.id
        WHERE t.id=?
        """,
        (tag_id,),
    ).fetchone()
    if not current:
        raise KeyError("Tag not found")
    updates = {}
    if sensitivity is not _TAG_SETTING_UNSET:
        updates["sensitivity"] = normalize_sensitivity(sensitivity)
    if category is not _TAG_SETTING_UNSET:
        normalized_category = normalize_tag_category(category)
        if current["is_face_tag"] and normalized_category != "person":
            raise TagCategoryLocked("Face tags must keep the person category")
        updates["category"] = normalized_category
    if updates:
        assignments = ", ".join(f"{column}=?" for column in updates)
        conn.execute(
            f"UPDATE tags SET {assignments} WHERE id=?",
            (*updates.values(), tag_id),
        )
    row = conn.execute(
        """
        SELECT t.*,
               CASE WHEN fi.id IS NULL THEN 0 ELSE 1 END AS is_face_tag
        FROM tags t
        LEFT JOIN face_identities fi ON fi.tag_id=t.id
        WHERE t.id=?
        """,
        (tag_id,),
    ).fetchone()
    return {
        **dict(row),
        "is_face_tag": bool(row["is_face_tag"]),
    }


def photo_image_analysis_cache_valid(conn, photo_id, checksum, analysis_signature):
    row = conn.execute(
        """
        SELECT 1 FROM photo_image_analyses
        WHERE photo_id=? AND checksum=? AND analysis_signature=?
        """,
        (photo_id, checksum, analysis_signature),
    ).fetchone()
    return bool(row)


def replace_photo_image_analysis(conn, photo_id, checksum, analysis_signature, result):
    automatic_tags = list(result.get("automatic_tags") or [])
    conn.execute("DELETE FROM photo_tags WHERE photo_id=? AND source='image_auto'", (photo_id,))
    for automatic_tag in automatic_tags:
        raw_name = str(automatic_tag.get("name") or "").strip()
        display_name = str(automatic_tag.get("display_name") or raw_name.replace("_", " ")).strip()
        if not raw_name or not display_name:
            continue
        tag = upsert_machine_tag(conn, f"wd:{raw_name}", display_name)
        conn.execute(
            """
            INSERT INTO photo_tags(photo_id, tag_id, source) VALUES (?, ?, 'image_auto')
            ON CONFLICT(photo_id, tag_id) DO UPDATE SET
                source=CASE
                    WHEN photo_tags.source='image_auto' THEN excluded.source
                    ELSE photo_tags.source
                END
            """,
            (photo_id, tag["id"]),
        )
    conn.execute(
        """
        INSERT INTO photo_image_analyses(
            photo_id, checksum, analysis_signature, analysis_level, freepik_level,
            freepik_scores_json, nudenet_detections_json, automatic_tags_json,
            models_json, provider, analyzed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(photo_id) DO UPDATE SET
            checksum=excluded.checksum,
            analysis_signature=excluded.analysis_signature,
            analysis_level=excluded.analysis_level,
            freepik_level=excluded.freepik_level,
            freepik_scores_json=excluded.freepik_scores_json,
            nudenet_detections_json=excluded.nudenet_detections_json,
            automatic_tags_json=excluded.automatic_tags_json,
            models_json=excluded.models_json,
            provider=excluded.provider,
            analyzed_at=CURRENT_TIMESTAMP
        """,
        (
            photo_id,
            checksum,
            analysis_signature,
            normalize_sensitivity(result.get("analysis_level")),
            normalize_sensitivity(result.get("freepik_level")),
            json.dumps(result.get("freepik_scores") or {}, ensure_ascii=False),
            json.dumps(result.get("nudenet_detections") or [], ensure_ascii=False),
            json.dumps(automatic_tags, ensure_ascii=False),
            json.dumps(result.get("models") or {}, ensure_ascii=False),
            result.get("provider"),
        ),
    )
    sync_crossdress_tag(conn, photo_id)


def list_lora_tag_mappings(conn):
    rows = conn.execute(
        """
        SELECT
            ltm.id,
            ltm.lora_name,
            ltm.created_at,
            t.id AS tag_id,
            t.name AS tag_name,
            t.category AS tag_category
        FROM lora_tag_mappings ltm
        JOIN lora_tag_mapping_tags lmtt ON lmtt.mapping_id=ltm.id
        JOIN tags t ON t.id=lmtt.tag_id
        ORDER BY ltm.lora_name COLLATE NOCASE, t.name COLLATE NOCASE
        """
    ).fetchall()
    mappings = []
    by_id = {}
    for row in rows:
        mapping = by_id.get(row["id"])
        if mapping is None:
            mapping = {
                "id": row["id"],
                "lora_name": row["lora_name"],
                "created_at": row["created_at"],
                "tags": [],
            }
            by_id[row["id"]] = mapping
            mappings.append(mapping)
        mapping["tags"].append(
            {
                "id": row["tag_id"],
                "name": row["tag_name"],
                "category": row["tag_category"],
            }
        )
    return mappings


def _clean_lora_tag_names(tag_names):
    if isinstance(tag_names, str):
        tag_names = tag_names.split(",")
    cleaned_tag_names = []
    seen_tag_names = set()
    for tag_name in tag_names or []:
        if not isinstance(tag_name, str):
            raise ValueError("Tag names must be strings")
        cleaned_tag_name = tag_name.strip()
        if cleaned_tag_name and cleaned_tag_name not in seen_tag_names:
            seen_tag_names.add(cleaned_tag_name)
            cleaned_tag_names.append(cleaned_tag_name)
    if not cleaned_tag_names:
        raise ValueError("At least one tag name is required")
    return cleaned_tag_names


def _replace_lora_mapping_tags(conn, mapping_id, tag_names):
    conn.execute(
        "DELETE FROM lora_tag_mapping_tags WHERE mapping_id=?",
        (mapping_id,),
    )
    for tag_name in tag_names:
        tag = upsert_tag(conn, tag_name)
        conn.execute(
            """
            INSERT INTO lora_tag_mapping_tags(mapping_id, tag_id)
            VALUES (?, ?)
            """,
            (mapping_id, tag["id"]),
        )


def create_lora_tag_mapping(conn, lora_name, tag_names):
    cleaned_lora_name = lora_name.strip()
    cleaned_tag_names = _clean_lora_tag_names(tag_names)
    if not cleaned_lora_name:
        raise ValueError("LoRA name is required")
    if conn.execute(
        "SELECT 1 FROM lora_tag_mappings WHERE lora_name=?",
        (cleaned_lora_name,),
    ).fetchone():
        raise KeyError("This LoRA already has a tag mapping")
    cursor = conn.execute(
        "INSERT INTO lora_tag_mappings(lora_name) VALUES (?)",
        (cleaned_lora_name,),
    )
    mapping_id = cursor.lastrowid
    _replace_lora_mapping_tags(conn, mapping_id, cleaned_tag_names)
    return next(
        mapping
        for mapping in list_lora_tag_mappings(conn)
        if mapping["id"] == mapping_id
    )


def update_lora_tag_mapping(conn, mapping_id, tag_names):
    cleaned_tag_names = _clean_lora_tag_names(tag_names)
    if not conn.execute(
        "SELECT 1 FROM lora_tag_mappings WHERE id=?",
        (mapping_id,),
    ).fetchone():
        raise KeyError("LoRA tag mapping not found")
    _replace_lora_mapping_tags(conn, mapping_id, cleaned_tag_names)
    return next(
        mapping
        for mapping in list_lora_tag_mappings(conn)
        if mapping["id"] == mapping_id
    )


def delete_lora_tag_mapping(conn, mapping_id):
    cursor = conn.execute("DELETE FROM lora_tag_mappings WHERE id=?", (mapping_id,))
    return cursor.rowcount > 0


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


def normalize_face_sex(value):
    normalized = str(value or "ND").strip().upper()
    if normalized not in {"ND", "M", "F"}:
        raise ValueError("Face sex must be ND, M or F")
    return normalized


def create_face_identity(
    conn,
    tag_name,
    review_threshold=0.40,
    automatic_threshold=0.55,
    margin_threshold=0.08,
    enabled=True,
    sex="ND",
):
    review, automatic, margin = _validate_face_thresholds(
        review_threshold, automatic_threshold, margin_threshold
    )
    sex = normalize_face_sex(sex)
    tag = upsert_tag(conn, tag_name)
    conn.execute(
        "UPDATE tags SET category='person' WHERE id=?",
        (tag["id"],),
    )
    existing = conn.execute("SELECT id FROM face_identities WHERE tag_id=?", (tag["id"],)).fetchone()
    if existing:
        raise ValueError("This tag already has a face identity")
    cursor = conn.execute(
        """
        INSERT INTO face_identities(tag_id, sex, review_threshold, automatic_threshold, margin_threshold, enabled)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (tag["id"], sex, review, automatic, margin, int(bool(enabled))),
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
    conn.execute(
        "UPDATE tags SET category='person' WHERE id=?",
        (tag_id,),
    )
    enabled = int(bool(updates.get("enabled", current["enabled"])))
    sex = normalize_face_sex(updates.get("sex", current["sex"]))
    conn.execute(
        """
        UPDATE face_identities
        SET tag_id=?, sex=?, review_threshold=?, automatic_threshold=?, margin_threshold=?, enabled=?,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (tag_id, sex, review, automatic, margin, enabled, identity_id),
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
                model_name, model_version, detected_sex
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                normalize_face_sex(getattr(detection, "detected_sex", "ND")),
            ),
        )
        face_ids.append(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO face_photo_scans(
            photo_id, checksum, model_name, model_version, provider, faces_count,
            attributes_version, scanned_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(photo_id) DO UPDATE SET
            checksum=excluded.checksum,
            model_name=excluded.model_name,
            model_version=excluded.model_version,
            provider=excluded.provider,
            faces_count=excluded.faces_count,
            attributes_version=excluded.attributes_version,
            scanned_at=CURRENT_TIMESTAMP
        """,
        (
            photo_id,
            checksum,
            model_name,
            model_version,
            provider,
            len(face_ids),
            FACE_ATTRIBUTES_VERSION,
        ),
    )
    return face_ids


def photo_face_cache_valid(conn, photo_id, checksum, model_name, model_version):
    row = conn.execute(
        """
        SELECT checksum, model_name, model_version, attributes_version
        FROM face_photo_scans WHERE photo_id=?
        """,
        (photo_id,),
    ).fetchone()
    return bool(
        row
        and row["checksum"] == checksum
        and row["model_name"] == model_name
        and row["model_version"] == model_version
        and row["attributes_version"] == FACE_ATTRIBUTES_VERSION
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
    sync_crossdress_tag(conn, photo_id)
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
                "detected_sex": face["detected_sex"] or "ND",
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
