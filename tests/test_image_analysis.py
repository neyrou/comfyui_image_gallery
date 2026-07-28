import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import app as app_module
from gallery_db import (
    TagCategoryLocked,
    connect_db,
    create_face_identity,
    delete_face_identity,
    init_db,
    list_gallery_photos,
    list_tag_stats,
    replace_photo_image_analysis,
    scan_albums,
    set_photo_tags,
    set_tag_sensitivity,
    update_face_identity,
    update_tag_settings,
    upsert_tag,
)
from image_analysis import (
    ImageAnalysisError,
    filter_wd_tags,
    freepik_level_from_scores,
    nudenet_level,
    readable_tag_name,
    wd_probabilities_from_output,
)


def create_png(path, color=(20, 40, 60)):
    Image.new("RGB", (24, 24), color).save(path)


def analysis_result(level="neutral", tags=None):
    return {
        "analysis_level": level,
        "freepik_level": level,
        "freepik_scores": {
            "neutral": 1.0 if level == "neutral" else 0.0,
            "low": 1.0 if level == "low" else 0.0,
            "medium": 1.0 if level == "medium" else 0.0,
            "high": 1.0 if level == "high" else 0.0,
        },
        "nudenet_detections": [],
        "automatic_tags": tags or [],
        "models": {"test": "v1"},
        "provider": "test",
    }


class FakeImageAnalysisEngine:
    analysis_signature = "fake-analysis-v2"

    def __init__(self):
        self.preload_calls = 0
        self.analyze_calls = 0

    def preload(self, progress_callback=None):
        self.preload_calls += 1
        if progress_callback:
            progress_callback("ready")

    def analyze_path(self, _image_path):
        self.analyze_calls += 1
        return analysis_result()


class ImageAnalysisRulesTests(unittest.TestCase):
    def test_freepik_uses_ordered_cumulative_probabilities(self):
        self.assertEqual(
            freepik_level_from_scores(
                {"neutral": 0.30, "low": 0.20, "medium": 0.40, "high": 0.10}
            ),
            "medium",
        )
        self.assertEqual(
            freepik_level_from_scores(
                {"neutral": 0.51, "low": 0.20, "medium": 0.20, "high": 0.09}
            ),
            "neutral",
        )
        self.assertEqual(
            freepik_level_from_scores(
                {"neutral": 0.10, "low": 0.10, "medium": 0.20, "high": 0.60}
            ),
            "high",
        )

    def test_nudenet_only_escalates_selected_exposed_classes(self):
        self.assertEqual(
            nudenet_level([{"class": "FEMALE_BREAST_EXPOSED", "score": 0.80}]),
            "medium",
        )
        self.assertEqual(
            nudenet_level([{"class": "MALE_GENITALIA_EXPOSED", "score": 0.70}]),
            "high",
        )
        self.assertEqual(
            nudenet_level(
                [
                    {"class": "BUTTOCKS_EXPOSED", "score": 0.59},
                    {"class": "BELLY_EXPOSED", "score": 0.99},
                ]
            ),
            "neutral",
        )

    def test_wd_keeps_only_general_tags_above_threshold(self):
        rows = [
            {"name": "pleated_skirt", "category": "0"},
            {"name": "some_character", "category": "4"},
            {"name": "questionable", "category": "9"},
            {"name": "android", "category": "0"},
        ]
        tags = filter_wd_tags(rows, [0.91, 0.99, 0.99, 0.39])
        self.assertEqual([tag["name"] for tag in tags], ["pleated_skirt"])
        self.assertEqual(tags[0]["display_name"], "pleated skirt")
        self.assertEqual(readable_tag_name("  mechanical_arm  "), "mechanical arm")

    def test_wd_does_not_apply_sigmoid_twice_to_probability_output(self):
        probabilities = wd_probabilities_from_output([0.01, 0.39, 0.40, 0.91])
        self.assertEqual(probabilities, [0.01, 0.39, 0.40, 0.91])
        logits = wd_probabilities_from_output([-2.0, 2.0])
        self.assertLess(logits[0], 0.40)
        self.assertGreater(logits[1], 0.40)


class ImageAnalysisDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.images = self.root / "images"
        self.thumbnails = self.root / "thumbnails"
        self.db_path = self.root / "gallery.sqlite3"
        (self.images / "output").mkdir(parents=True)
        create_png(self.images / "output" / "one.png")
        create_png(self.images / "output" / "two.png", color=(50, 60, 70))
        scan_albums(self.db_path, self.images, self.thumbnails)

    def tearDown(self):
        self.tmp.cleanup()

    def photo_ids(self, conn):
        return {
            row["filename"]: row["photo_id"]
            for row in conn.execute("SELECT filename, photo_id FROM album_photos").fetchall()
        }

    def test_existing_tag_schema_migrates_to_neutral(self):
        legacy = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as conn:
            conn.execute(
                "CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, created_at TEXT)"
            )
            conn.execute("INSERT INTO tags(name) VALUES ('portrait')")
            conn.commit()
        init_db(legacy)
        with connect_db(legacy) as conn:
            tag = conn.execute("SELECT * FROM tags WHERE name='portrait'").fetchone()
            self.assertEqual(tag["sensitivity"], "neutral")
            self.assertIsNone(tag["machine_key"])
            self.assertIsNone(tag["category"])

    def test_missing_analysis_skips_valid_cache_retries_stale_and_avoids_model_preload(self):
        engine = FakeImageAnalysisEngine()
        with connect_db(self.db_path) as conn:
            ids = self.photo_ids(conn)
            checksums = {
                row["id"]: row["checksum"]
                for row in conn.execute("SELECT id, checksum FROM photos").fetchall()
            }
            replace_photo_image_analysis(
                conn,
                ids["one.png"],
                checksums[ids["one.png"]],
                engine.analysis_signature,
                analysis_result(),
            )
            replace_photo_image_analysis(
                conn,
                ids["two.png"],
                checksums[ids["two.png"]],
                "obsolete-signature",
                analysis_result(),
            )
        previous = {
            "DB_PATH": app_module.DB_PATH,
            "IMAGE_ANALYSIS_ENGINE_INSTANCE": app_module.IMAGE_ANALYSIS_ENGINE_INSTANCE,
        }
        app_module.DB_PATH = self.db_path
        app_module.IMAGE_ANALYSIS_ENGINE_INSTANCE = engine
        options = {"scan_mode": "missing", "image_analysis": True}
        try:
            first = app_module.run_image_analysis_stage(
                "sync",
                options,
                [ids["one.png"], ids["two.png"]],
                sync=True,
            )
            second = app_module.run_image_analysis_stage(
                "sync",
                options,
                [ids["one.png"], ids["two.png"]],
                sync=True,
            )
        finally:
            for name, value in previous.items():
                setattr(app_module, name, value)

        self.assertEqual(first, {"total": 2, "processed": 1, "skipped": 1, "errors": 0})
        self.assertEqual(second, {"total": 2, "processed": 0, "skipped": 2, "errors": 0})
        self.assertEqual(engine.analyze_calls, 1)
        self.assertEqual(engine.preload_calls, 1)

    def test_face_identity_forces_and_locks_person_category(self):
        with connect_db(self.db_path) as conn:
            identity = create_face_identity(conn, "Alice")
            alice_tag_id = identity["tag_id"]
            self.assertEqual(
                conn.execute(
                    "SELECT category FROM tags WHERE id=?",
                    (alice_tag_id,),
                ).fetchone()["category"],
                "person",
            )
            with self.assertRaises(TagCategoryLocked):
                update_tag_settings(conn, alice_tag_id, category="constraint")

            updated = update_face_identity(conn, identity["id"], tag_name="Bob")
            bob_tag_id = updated["tag_id"]
            self.assertEqual(
                conn.execute(
                    "SELECT category FROM tags WHERE id=?",
                    (bob_tag_id,),
                ).fetchone()["category"],
                "person",
            )
            delete_face_identity(conn, identity["id"])
            self.assertEqual(
                conn.execute(
                    "SELECT category FROM tags WHERE id=?",
                    (bob_tag_id,),
                ).fetchone()["category"],
                "person",
            )
            update_tag_settings(conn, bob_tag_id, category=None)
            self.assertIsNone(
                conn.execute(
                    "SELECT category FROM tags WHERE id=?",
                    (bob_tag_id,),
                ).fetchone()["category"]
            )

    def test_init_db_migrates_existing_face_tags_to_person(self):
        with connect_db(self.db_path) as conn:
            identity = create_face_identity(conn, "Alice")
            conn.execute(
                "UPDATE tags SET category=NULL WHERE id=?",
                (identity["tag_id"],),
            )

        init_db(self.db_path)

        with connect_db(self.db_path) as conn:
            category = conn.execute(
                "SELECT category FROM tags WHERE id=?",
                (identity["tag_id"],),
            ).fetchone()["category"]
        self.assertEqual(category, "person")

    def test_effective_sensitivity_combines_analysis_and_tag_and_filters_by_ceiling(self):
        with connect_db(self.db_path) as conn:
            ids = self.photo_ids(conn)
            set_photo_tags(conn, ids["one.png"], ["bondage"])
            tag_id = conn.execute("SELECT id FROM tags WHERE name='bondage'").fetchone()["id"]
            set_tag_sensitivity(conn, tag_id, "high")
            checksum = conn.execute("SELECT checksum FROM photos WHERE id=?", (ids["two.png"],)).fetchone()["checksum"]
            replace_photo_image_analysis(
                conn,
                ids["two.png"],
                checksum,
                "test-v1",
                analysis_result("medium"),
            )

            _, neutral, _ = list_gallery_photos(conn, "output", max_sensitivity="neutral")
            _, medium, _ = list_gallery_photos(conn, "output", max_sensitivity="medium")
            _, high, _ = list_gallery_photos(conn, "output", max_sensitivity="high")

        self.assertEqual(neutral, [])
        self.assertEqual([photo["filename"] for photo in medium], ["two.png"])
        self.assertEqual({photo["filename"] for photo in high}, {"one.png", "two.png"})

    def test_image_tag_refresh_preserves_other_sources_and_global_stats_deduplicate(self):
        with connect_db(self.db_path) as conn:
            ids = self.photo_ids(conn)
            photo_id = ids["one.png"]
            set_photo_tags(conn, photo_id, ["manual"])
            face_tag = upsert_tag(conn, "face")
            conn.execute(
                "INSERT INTO photo_tags(photo_id, tag_id, source) VALUES (?, ?, 'face_auto')",
                (photo_id, face_tag["id"]),
            )
            checksum = conn.execute("SELECT checksum FROM photos WHERE id=?", (photo_id,)).fetchone()["checksum"]
            replace_photo_image_analysis(
                conn,
                photo_id,
                checksum,
                "test-v1",
                analysis_result(
                    "low",
                    [{"name": "pleated_skirt", "display_name": "pleated skirt", "score": 0.91}],
                ),
            )
            replace_photo_image_analysis(
                conn,
                photo_id,
                checksum,
                "test-v1",
                analysis_result(
                    "low",
                    [{"name": "android", "display_name": "android", "score": 0.88}],
                ),
            )
            sources = {
                row["name"]: row["source"]
                for row in conn.execute(
                    """
                    SELECT t.name, pt.source FROM photo_tags pt
                    JOIN tags t ON t.id=pt.tag_id WHERE pt.photo_id=?
                    """,
                    (photo_id,),
                ).fetchall()
            }
            stats = {row["name"]: row["occurrence_count"] for row in list_tag_stats(conn)}

        self.assertEqual(sources["manual"], "manual")
        self.assertEqual(sources["face"], "face_auto")
        self.assertEqual(sources["android"], "image_auto")
        self.assertNotIn("pleated skirt", sources)
        self.assertEqual(stats["android"], 1)

    def test_single_photo_reanalysis_api_replaces_only_automatic_tags_and_preserves_on_failure(self):
        class SuccessfulEngine:
            analysis_signature = "manual-rescan-v2"

            def analyze_path(self, image_path):
                self.image_path = Path(image_path)
                return analysis_result(
                    "low",
                    [{"name": "android", "display_name": "android", "score": 0.88}],
                )

        class FailingEngine:
            analysis_signature = "failing-v2"

            def analyze_path(self, _image_path):
                raise ImageAnalysisError("tagger unavailable")

        with connect_db(self.db_path) as conn:
            photo_id = self.photo_ids(conn)["one.png"]
            set_photo_tags(conn, photo_id, ["manual"])

        previous_db_path = app_module.DB_PATH
        previous_images_root = app_module.IMAGES_ROOT
        previous_thumbnail_root = app_module.THUMBNAIL_ROOT
        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images
        app_module.THUMBNAIL_ROOT = self.thumbnails
        try:
            client = app_module.app.test_client()
            engine = SuccessfulEngine()
            with patch.object(
                app_module,
                "get_image_analysis_engine",
                return_value=engine,
            ):
                response = client.post(
                    f"/api/photos/{photo_id}/image-analysis/rescan",
                    json={},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.get_json()["photo"]["image_analysis"]["analysis_level"],
                "low",
            )
            self.assertTrue(engine.image_path.is_file())

            with patch.object(
                app_module,
                "get_image_analysis_engine",
                return_value=FailingEngine(),
            ):
                failed = client.post(
                    f"/api/photos/{photo_id}/image-analysis/rescan",
                    json={},
                )
            self.assertEqual(failed.status_code, 422)

            with connect_db(self.db_path) as conn:
                sources = {
                    row["name"]: row["source"]
                    for row in conn.execute(
                        """
                        SELECT t.name, pt.source
                        FROM photo_tags pt
                        JOIN tags t ON t.id=pt.tag_id
                        WHERE pt.photo_id=?
                        """,
                        (photo_id,),
                    ).fetchall()
                }
                signature = conn.execute(
                    """
                    SELECT analysis_signature
                    FROM photo_image_analyses
                    WHERE photo_id=?
                    """,
                    (photo_id,),
                ).fetchone()["analysis_signature"]
            self.assertEqual(sources["manual"], "manual")
            self.assertEqual(sources["android"], "image_auto")
            self.assertEqual(signature, "manual-rescan-v2")
        finally:
            app_module.DB_PATH = previous_db_path
            app_module.IMAGES_ROOT = previous_images_root
            app_module.THUMBNAIL_ROOT = previous_thumbnail_root


if __name__ == "__main__":
    unittest.main()
