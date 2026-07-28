import io
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import app as app_module
from face_recognition import FaceDetection, classify_identity, select_onnx_providers
from gallery_db import (
    add_gallery_face_reference,
    connect_db,
    create_face_identity,
    create_face_scan_job,
    decide_face_match,
    get_photo_detail,
    init_db,
    photo_face_cache_valid,
    rematch_photo_faces,
    replace_photo_faces,
    scan_albums,
    set_face_setting,
    update_photo_tags,
)


class FakeFaceEngine:
    model_name = "buffalo_l"
    model_version = "buffalo_l-v1"
    provider = "CPUExecutionProvider"

    def __init__(self):
        self.analyze_calls = 0

    def _load(self):
        return self

    def configuration(self):
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "model_root": "fake",
            "model_directory": "fake/models/buffalo_l",
            "model_present": True,
            "configured": True,
            "provider": self.provider,
        }

    def analyze_path(self, _image_path):
        self.analyze_calls += 1
        return [FaceDetection((2, 2, 22, 22), 0.99, (1.0, 0.0, 0.0))]


def create_png(path, color=(20, 40, 60)):
    Image.new("RGB", (32, 32), color).save(path)


class FaceRecognitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.images_root = self.root / "static" / "images"
        self.thumbnails = self.root / "static" / "thumbnails"
        self.db_path = self.root / "instance" / "gallery.sqlite3"
        self.references = self.root / "instance" / "face_references"
        (self.images_root / "output").mkdir(parents=True)
        self.previous = {
            "DB_PATH": app_module.DB_PATH,
            "IMAGES_ROOT": app_module.IMAGES_ROOT,
            "THUMBNAIL_ROOT": app_module.THUMBNAIL_ROOT,
            "FACE_REFERENCE_ROOT": app_module.FACE_REFERENCE_ROOT,
            "FACE_ENGINE_FACTORY": app_module.FACE_ENGINE_FACTORY,
            "FACE_ENGINE_INSTANCE": app_module.FACE_ENGINE_INSTANCE,
        }
        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        app_module.FACE_REFERENCE_ROOT = self.references
        self.engine = FakeFaceEngine()
        app_module.FACE_ENGINE_FACTORY = FakeFaceEngine
        app_module.FACE_ENGINE_INSTANCE = self.engine
        app_module.FACE_RECOVERED_DATABASES.clear()

    def tearDown(self):
        for name, value in self.previous.items():
            setattr(app_module, name, value)
        app_module.FACE_RECOVERED_DATABASES.clear()
        self.tmp.cleanup()

    def test_classification_uses_best_reference_threshold_and_margin(self):
        identities = [
            {"id": 1, "enabled": True, "review_threshold": 0.4, "automatic_threshold": 0.55, "margin_threshold": 0.08},
            {"id": 2, "enabled": True, "review_threshold": 0.4, "automatic_threshold": 0.55, "margin_threshold": 0.08},
        ]
        references = [
            {"identity_id": 1, "embedding": (1.0, 0.0)},
            {"identity_id": 1, "embedding": (0.8, 0.6)},
            {"identity_id": 2, "embedding": (0.0, 1.0)},
        ]
        result = classify_identity((0.99, 0.05), identities, references)
        self.assertEqual(result["identity_id"], 1)
        self.assertEqual(result["state"], "automatic")

        close_references = references + [{"identity_id": 2, "embedding": (0.98, 0.08)}]
        ambiguous = classify_identity((1.0, 0.0), identities, close_references)
        self.assertEqual(ambiguous["state"], "pending")
        self.assertIsNone(classify_identity((0.0, -1.0), identities, references))
        self.assertEqual(
            select_onnx_providers(["CPUExecutionProvider", "CUDAExecutionProvider"]),
            ["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        self.assertEqual(select_onnx_providers(["CPUExecutionProvider"]), ["CPUExecutionProvider"])

    def test_existing_photo_tags_are_migrated_as_manual(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE photos (id INTEGER PRIMARY KEY, checksum TEXT NOT NULL UNIQUE);
                CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT);
                CREATE TABLE photo_tags (
                    photo_id INTEGER NOT NULL REFERENCES photos(id),
                    tag_id INTEGER NOT NULL REFERENCES tags(id),
                    PRIMARY KEY(photo_id, tag_id)
                );
                INSERT INTO photos(id, checksum) VALUES (1, 'abc');
                INSERT INTO tags(id, name) VALUES (1, 'legacy');
                INSERT INTO photo_tags(photo_id, tag_id) VALUES (1, 1);
                """
            )
            conn.commit()
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            row = conn.execute("SELECT source FROM photo_tags WHERE photo_id=1 AND tag_id=1").fetchone()
            self.assertEqual(row["source"], "manual")

    def test_face_crop_applies_exif_orientation_before_bounding_box(self):
        image_path = self.images_root / "output" / "oriented.jpg"
        image = Image.new("RGB", (80, 40), (220, 20, 20))
        image.paste((20, 20, 220), (40, 0, 80, 40))
        exif = Image.Exif()
        exif[274] = 6
        image.save(image_path, quality=100, subsampling=0, exif=exif)

        with app_module.app.test_request_context():
            response = app_module._cropped_face_response(image_path, (5, 50, 35, 70))
            response.direct_passthrough = False
            cropped = Image.open(io.BytesIO(response.get_data()))
            center = cropped.getpixel((cropped.width // 2, cropped.height // 2))

        self.assertGreater(center[2], 180)
        self.assertLess(center[0], 60)

    def test_manual_tag_survives_rematch_and_rejection_persists(self):
        create_png(self.images_root / "output" / "reference.png")
        create_png(self.images_root / "output" / "target.png", (80, 30, 10))
        scan_albums(self.db_path, self.images_root, self.thumbnails)
        with connect_db(self.db_path) as conn:
            ids = {
                row["filename"]: row["photo_id"]
                for row in conn.execute("SELECT filename, photo_id FROM album_photos").fetchall()
            }
            identity = create_face_identity(conn, "Alice")
            reference_face_id = replace_photo_faces(
                conn,
                ids["reference.png"],
                conn.execute("SELECT checksum FROM photos WHERE id=?", (ids["reference.png"],)).fetchone()["checksum"],
                [FaceDetection((1, 1, 20, 20), 0.99, (1.0, 0.0))],
                "buffalo_l",
                "buffalo_l-v1",
                "CPUExecutionProvider",
            )[0]
            add_gallery_face_reference(conn, identity["id"], reference_face_id)
            target_face_ids = replace_photo_faces(
                conn,
                ids["target.png"],
                conn.execute("SELECT checksum FROM photos WHERE id=?", (ids["target.png"],)).fetchone()["checksum"],
                [
                    FaceDetection((2, 2, 21, 21), 0.98, (0.99, 0.05)),
                    FaceDetection((22, 2, 31, 21), 0.96, (0.98, 0.08)),
                ],
                "buffalo_l",
                "buffalo_l-v1",
                "CPUExecutionProvider",
            )
            target_face_id = target_face_ids[0]
            target_checksum = conn.execute(
                "SELECT checksum FROM photos WHERE id=?", (ids["target.png"],)
            ).fetchone()["checksum"]
            self.assertTrue(
                photo_face_cache_valid(
                    conn, ids["target.png"], target_checksum, "buffalo_l", "buffalo_l-v1"
                )
            )
            self.assertFalse(
                photo_face_cache_valid(
                    conn, ids["target.png"], target_checksum, "buffalo_l", "buffalo_l-v2"
                )
            )
            summary = rematch_photo_faces(conn, ids["target.png"])
            self.assertEqual(summary["recognized"], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS total FROM photo_tags WHERE photo_id=?", (ids["target.png"],)).fetchone()["total"],
                1,
            )
            source = conn.execute(
                "SELECT source FROM photo_tags WHERE photo_id=?", (ids["target.png"],)
            ).fetchone()["source"]
            self.assertEqual(source, "face_auto")

            update_photo_tags(conn, [ids["target.png"]], ["Alice"], "add")
            decide_face_match(conn, target_face_id, identity["id"], "rejected")
            rematch_photo_faces(conn, ids["target.png"])
            tag = conn.execute(
                """
                SELECT pt.source FROM photo_tags pt JOIN tags t ON t.id=pt.tag_id
                WHERE pt.photo_id=? AND t.name='Alice'
                """,
                (ids["target.png"],),
            ).fetchone()
            self.assertEqual(tag["source"], "manual")
            decision = conn.execute(
                "SELECT state FROM face_matches WHERE face_id=? AND identity_id=?",
                (target_face_id, identity["id"]),
            ).fetchone()
            self.assertEqual(decision["state"], "rejected")

    def test_sync_job_api_detects_faces_and_adds_automatic_tag(self):
        create_png(self.images_root / "output" / "reference.png")
        create_png(self.images_root / "output" / "target.png", (60, 30, 90))
        scan_albums(self.db_path, self.images_root, self.thumbnails)
        client = app_module.app.test_client()

        first = client.post("/api/face/jobs", json={"scope": "all", "sync": True})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["job"]["state"], "done")
        with connect_db(self.db_path) as conn:
            identity = create_face_identity(conn, "Alice")
            rows = conn.execute(
                """
                SELECT pf.id AS face_id, pf.photo_id, ap.filename
                FROM photo_faces pf JOIN album_photos ap ON ap.photo_id=pf.photo_id
                ORDER BY ap.filename
                """
            ).fetchall()
            reference = next(row for row in rows if row["filename"] == "reference.png")
            target = next(row for row in rows if row["filename"] == "target.png")
            add_gallery_face_reference(conn, identity["id"], reference["face_id"])

        second = client.post(
            "/api/face/jobs",
            json={"scope": "selection", "photo_ids": [target["photo_id"]], "sync": True},
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["job"]["recognized"], 1)
        detail = client.get(f"/api/photos/{target['photo_id']}").get_json()["photo"]
        self.assertEqual(detail["tags"][0]["name"], "Alice")
        self.assertEqual(detail["tags"][0]["source"], "face_auto")
        self.assertEqual(detail["face_analysis"]["faces"][0]["match"]["state"], "automatic")

    def test_face_job_force_option_bypasses_valid_cache(self):
        create_png(self.images_root / "output" / "cached.png")
        scan_albums(self.db_path, self.images_root, self.thumbnails)
        client = app_module.app.test_client()

        first = client.post("/api/face/jobs", json={"scope": "all", "sync": True})
        cached = client.post("/api/face/jobs", json={"scope": "all", "sync": True})
        forced = client.post("/api/face/jobs", json={"scope": "all", "sync": True, "force": True})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(cached.status_code, 200)
        self.assertEqual(forced.status_code, 200)
        self.assertEqual(self.engine.analyze_calls, 2)

    def test_missing_selection_face_scan_skips_valid_cache_and_retries_obsolete_model(self):
        create_png(self.images_root / "output" / "one.png")
        create_png(self.images_root / "output" / "two.png", (60, 30, 90))
        scan_albums(self.db_path, self.images_root, self.thumbnails)
        with connect_db(self.db_path) as conn:
            photo_ids = {
                row["filename"]: row["photo_id"]
                for row in conn.execute("SELECT filename, photo_id FROM album_photos").fetchall()
            }
        client = app_module.app.test_client()

        first = client.post(
            "/api/scan",
            json={
                "scope": "selection",
                "photo_ids": [photo_ids["one.png"]],
                "scan_mode": "missing",
                "face_recognition": True,
                "sync": True,
            },
        )
        mixed = client.post(
            "/api/scan",
            json={
                "scope": "selection",
                "photo_ids": [photo_ids["one.png"], photo_ids["two.png"]],
                "scan_mode": "missing",
                "face_recognition": True,
                "sync": True,
            },
        )
        self.engine.model_version = "buffalo_l-v2"
        stale = client.post(
            "/api/scan",
            json={
                "scope": "selection",
                "photo_ids": [photo_ids["one.png"]],
                "scan_mode": "missing",
                "face_recognition": True,
                "sync": True,
            },
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(mixed.get_json()["summary"]["faces"]["skipped"], 1)
        self.assertEqual(mixed.get_json()["summary"]["faces"]["processed"], 1)
        self.assertEqual(stale.get_json()["summary"]["faces"]["processed"], 1)
        self.assertEqual(self.engine.analyze_calls, 3)

    def test_cancelling_queued_face_job_finishes_it_immediately(self):
        create_png(self.images_root / "output" / "queued.png")
        scan_albums(self.db_path, self.images_root, self.thumbnails)
        with connect_db(self.db_path) as conn:
            photo_id = conn.execute("SELECT id FROM photos").fetchone()["id"]
            create_face_scan_job(conn, "queued-job", "selection", [photo_id])

        cancelled = app_module.request_face_job_cancel("queued-job")

        self.assertEqual(cancelled["state"], "cancelled")
        self.assertFalse(cancelled["active"])
        with connect_db(self.db_path) as conn:
            create_face_scan_job(conn, "running-job", "selection", [photo_id])
            conn.execute("UPDATE face_scan_jobs SET state='running' WHERE id='running-job'")
        running = app_module.request_face_job_cancel("running-job")
        self.assertEqual(running["state"], "cancel_requested")
        self.assertTrue(running["active"])

    def test_scan_cancel_propagates_to_queued_face_child(self):
        create_png(self.images_root / "output" / "child.png")
        scan_albums(self.db_path, self.images_root, self.thumbnails)
        with connect_db(self.db_path) as conn:
            photo_id = conn.execute("SELECT id FROM photos").fetchone()["id"]
            create_face_scan_job(
                conn,
                "child-job",
                "selection",
                [photo_id],
                params={"parent_scan_job_id": "parent-job"},
            )
        original = app_module.scan_status_snapshot()
        try:
            with app_module.SCAN_LOCK:
                app_module.SCAN_STATUS.update(
                    {
                        "active": True,
                        "job_id": "parent-job",
                        "state": "running",
                        "cancel_requested": False,
                        "face_job_id": "child-job",
                    }
                )
            response = app_module.app.test_client().post("/api/scan/jobs/parent-job/cancel", json={})
            with connect_db(self.db_path) as conn:
                child = conn.execute(
                    "SELECT state FROM face_scan_jobs WHERE id='child-job'"
                ).fetchone()["state"]
            self.assertEqual(response.status_code, 202)
            self.assertEqual(child, "cancelled")
        finally:
            with app_module.SCAN_LOCK:
                app_module.SCAN_STATUS.clear()
                app_module.SCAN_STATUS.update(original)

    def test_explicit_face_scan_does_not_duplicate_automatic_job(self):
        create_png(self.images_root / "output" / "one.png")
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            set_face_setting(conn, "automatic_scan", True)
        client = app_module.app.test_client()

        response = client.post(
            "/api/scan",
            json={
                "sync": True,
                "rescan_existing": False,
                "face_recognition": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        with connect_db(self.db_path) as conn:
            jobs = conn.execute("SELECT COUNT(*) AS total FROM face_scan_jobs").fetchone()["total"]
        self.assertEqual(jobs, 1)
        self.assertEqual(self.engine.analyze_calls, 1)

    def test_automatic_face_setting_still_applies_when_modal_option_is_false(self):
        create_png(self.images_root / "output" / "automatic.png")
        init_db(self.db_path)
        with connect_db(self.db_path) as conn:
            set_face_setting(conn, "automatic_scan", True)
        client = app_module.app.test_client()

        with patch("app.start_face_worker"):
            response = client.post(
                "/api/scan",
                json={
                    "sync": True,
                    "rescan_existing": False,
                    "face_recognition": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        with connect_db(self.db_path) as conn:
            job = conn.execute("SELECT scope, state FROM face_scan_jobs").fetchone()
        self.assertEqual(dict(job), {"scope": "automatic", "state": "queued"})

    def test_reference_upload_requires_selection_and_stays_outside_static(self):
        init_db(self.db_path)
        client = app_module.app.test_client()
        identity = client.post("/api/face/identities", json={"tag_name": "Celine"}).get_json()["identity"]
        buffer = io.BytesIO()
        Image.new("RGB", (32, 32), (20, 30, 40)).save(buffer, format="PNG")
        buffer.seek(0)
        response = client.post(
            "/api/face/imports",
            data={"file": (buffer, "portrait.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        imported = response.get_json()["import"]
        self.assertEqual(len(imported["faces"]), 1)
        self.assertTrue(Path(imported["file_path"]).is_relative_to(self.references))

        added = client.post(
            f"/api/face/identities/{identity['id']}/references/import",
            json={"token": imported["token"], "face_index": 0},
        )
        self.assertEqual(added.status_code, 201)
        self.assertEqual(added.get_json()["identity"]["reference_count"], 1)


if __name__ == "__main__":
    unittest.main()
