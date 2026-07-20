import hashlib
import json
import os
import sqlite3
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

            CREATE INDEX IF NOT EXISTS idx_album_photos_album ON album_photos(album_id, mtime DESC);
            CREATE INDEX IF NOT EXISTS idx_album_photos_photo ON album_photos(photo_id);
            CREATE INDEX IF NOT EXISTS idx_photo_links_source ON photo_links(source_photo_id);
            CREATE INDEX IF NOT EXISTS idx_photo_links_target ON photo_links(target_photo_id);
            """
        )


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


def list_gallery_photos(conn, album_name, page=1, per_page=100):
    album = get_album_by_name(conn, album_name)
    if not album:
        return None, [], 0
    total = conn.execute(
        "SELECT COUNT(*) AS total FROM album_photos WHERE album_id=? AND is_missing=0",
        (album["id"],),
    ).fetchone()["total"]
    offset = max(page - 1, 0) * per_page
    rows = conn.execute(
        """
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
        WHERE ap.album_id=? AND ap.is_missing=0
        GROUP BY p.id, ap.relative_path
        ORDER BY ap.mtime DESC
        LIMIT ? OFFSET ?
        """,
        (album["id"], per_page, offset),
    ).fetchall()
    return dict(album), [serialize_gallery_photo(row) for row in rows], total


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
        SELECT t.id, t.name FROM tags t
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
        conn.execute("INSERT OR IGNORE INTO photo_tags(photo_id, tag_id) VALUES (?, ?)", (photo_id, tag["id"]))


def set_album_tags(conn, album_id, tag_names):
    conn.execute("DELETE FROM album_tags WHERE album_id=?", (album_id,))
    for name in tag_names:
        tag = upsert_tag(conn, name)
        conn.execute("INSERT OR IGNORE INTO album_tags(album_id, tag_id) VALUES (?, ?)", (album_id, tag["id"]))


def list_tags(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM tags ORDER BY name COLLATE NOCASE").fetchall()]


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
