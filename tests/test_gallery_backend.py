import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image, PngImagePlugin

import app as app_module
from gallery_db import (
    connect_db,
    find_photo_file,
    get_photo_detail,
    list_album_tag_stats,
    list_albums,
    list_gallery_photos,
    scan_albums,
    set_photo_tags,
)


def create_png(path, color=(20, 40, 60), prompt=None, workflow=None):
    info = PngImagePlugin.PngInfo()
    if prompt is not None:
        info.add_text("prompt", prompt)
    if workflow is not None:
        info.add_text("workflow", workflow)
    Image.new("RGB", (24, 24), color).save(path, pnginfo=info)


class GalleryBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.images_root = self.root / "static" / "images"
        self.thumbnails = self.root / "static" / "thumbnails"
        self.db_path = self.root / "instance" / "gallery.sqlite3"
        (self.images_root / "output").mkdir(parents=True)
        (self.images_root / "Celine").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_creates_album_types_and_deduplicates_by_checksum(self):
        output_image = self.images_root / "output" / "same.png"
        user_image = self.images_root / "Celine" / "same.png"
        create_png(output_image)
        shutil.copyfile(output_image, user_image)

        scan_albums(self.db_path, self.images_root, self.thumbnails)

        with connect_db(self.db_path) as conn:
            albums = {album["name"]: album for album in list_albums(conn)}
            self.assertEqual(albums["output"]["type"], "output")
            self.assertEqual(albums["Celine"]["type"], "user")
            self.assertEqual(conn.execute("SELECT COUNT(*) AS total FROM photos").fetchone()["total"], 1)
            _, photos, total = list_gallery_photos(conn, "output")
            self.assertEqual(total, 1)
            self.assertTrue(photos[0]["favorite"])
            self.assertEqual(photos[0]["album_count"], 2)
            self.assertEqual(photos[0]["user_album_count"], 1)

    def test_photo_uses_available_album_when_another_membership_is_offline(self):
        offline_album = self.images_root / "A-offline"
        offline_album.mkdir()
        available_image = self.images_root / "output" / "same.png"
        offline_image = offline_album / "same.png"
        create_png(available_image)
        shutil.copyfile(available_image, offline_image)

        scan_albums(self.db_path, self.images_root, self.thumbnails)
        shutil.rmtree(offline_album)

        with connect_db(self.db_path) as conn:
            photo_id = conn.execute("SELECT id FROM photos").fetchone()["id"]
            detail = get_photo_detail(conn, photo_id)
            memberships = {item["album_name"]: item for item in detail["memberships"]}

            self.assertFalse(memberships["A-offline"]["available"])
            self.assertTrue(memberships["output"]["available"])
            self.assertIn("/output/same.png", detail["original_url"])
            self.assertEqual(find_photo_file(conn, photo_id), available_image)

    def test_api_rescan_metadata_and_photo_links(self):
        prompt = {
            "1": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "prompt from png"}},
            "2": {"class_type": "TextEncode", "inputs": {"prompt": ["1", 0]}},
            "3": {"class_type": "LoadImage", "inputs": {"image": "ref.png"}},
            "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0]}},
        }
        workflow = {
            "nodes": [
                {"id": 1, "type": "PrimitiveStringMultiline", "mode": 0},
                {"id": 2, "type": "TextEncode", "mode": 0},
                {"id": 3, "type": "LoadImage", "mode": 0},
                {"id": 4, "type": "SaveImage", "mode": 0},
            ],
            "links": [[1, 3, 0, 4, 0, "IMAGE"]],
        }
        create_png(self.images_root / "output" / "ref.png", color=(10, 90, 10))
        create_png(self.images_root / "output" / "source.png", prompt=__import__("json").dumps(prompt), workflow=__import__("json").dumps(workflow))
        create_png(self.images_root / "output" / "target.png", color=(90, 10, 10))

        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        client = app_module.app.test_client()

        self.assertEqual(client.post("/api/scan", json={"metadata": False, "sync": True}).status_code, 200)
        with connect_db(self.db_path) as conn:
            source_id = conn.execute(
                "SELECT p.id FROM photos p JOIN album_photos ap ON ap.photo_id=p.id WHERE ap.filename='source.png'"
            ).fetchone()["id"]
            target_id = conn.execute(
                "SELECT p.id FROM photos p JOIN album_photos ap ON ap.photo_id=p.id WHERE ap.filename='target.png'"
            ).fetchone()["id"]
            ref_id = conn.execute(
                "SELECT p.id FROM photos p JOIN album_photos ap ON ap.photo_id=p.id WHERE ap.filename='ref.png'"
            ).fetchone()["id"]

        rescan = client.post(f"/api/photos/{source_id}/metadata/rescan")
        self.assertEqual(rescan.status_code, 200)
        rescanned_photo = rescan.get_json()["photo"]
        self.assertEqual(rescanned_photo["metadata"]["prompt"], "prompt from png")
        original_links = [link for link in rescanned_photo["links"] if link["type"] == "original"]
        self.assertEqual(original_links[0]["linked_photo_id"], ref_id)

        link = client.post(f"/api/photos/{source_id}/links", json={"target_photo_id": target_id, "type": "variant"})
        self.assertEqual(link.status_code, 200)
        self.assertIn("variant", [item["type"] for item in link.get_json()["photo"]["links"]])

    def test_album_tag_stats_and_gallery_filters(self):
        output_files = {
            "one.png": (20, 30, 40),
            "two.png": (30, 40, 50),
            "three.png": (40, 50, 60),
            "four.png": (50, 60, 70),
            "five.png": (60, 70, 80),
            "missing.png": (70, 80, 90),
        }
        for filename, color in output_files.items():
            create_png(self.images_root / "output" / filename, color=color)
        create_png(self.images_root / "Celine" / "other.png", color=(90, 80, 70))
        scan_albums(self.db_path, self.images_root, self.thumbnails)

        with connect_db(self.db_path) as conn:
            photo_ids = {
                row["filename"]: row["photo_id"]
                for row in conn.execute(
                    "SELECT filename, photo_id FROM album_photos WHERE is_missing=0"
                ).fetchall()
            }
            set_photo_tags(conn, photo_ids["one.png"], ["portrait", "warm"])
            set_photo_tags(conn, photo_ids["two.png"], ["portrait"])
            set_photo_tags(conn, photo_ids["three.png"], ["warm", "blocked"])
            set_photo_tags(conn, photo_ids["four.png"], ["blocked"])
            set_photo_tags(conn, photo_ids["missing.png"], ["rare"])
            set_photo_tags(conn, photo_ids["other.png"], ["portrait"])
            conn.execute(
                "UPDATE album_photos SET is_missing=1 WHERE album_id=(SELECT id FROM albums WHERE name='output') AND filename='missing.png'"
            )

            stats = list_album_tag_stats(conn, "output")
            self.assertEqual(
                [(item["name"], item["occurrence_count"]) for item in stats],
                [("blocked", 2), ("portrait", 2), ("warm", 2)],
            )

            _, photos, total = list_gallery_photos(conn, "output")
            self.assertEqual(total, 5)
            self.assertEqual(len(photos), 5)

            _, photos, total = list_gallery_photos(conn, "output", include_tags=["portrait"])
            self.assertEqual(total, 2)
            self.assertEqual({photo["filename"] for photo in photos}, {"one.png", "two.png"})

            _, photos, total = list_gallery_photos(conn, "output", include_tags=["portrait", "warm"])
            self.assertEqual(total, 1)
            self.assertEqual(photos[0]["filename"], "one.png")

            _, photos, total = list_gallery_photos(conn, "output", exclude_tags=["blocked"])
            self.assertEqual(total, 3)
            self.assertEqual({photo["filename"] for photo in photos}, {"one.png", "two.png", "five.png"})

            _, photos, total = list_gallery_photos(
                conn,
                "output",
                include_tags=["warm"],
                exclude_tags=["blocked"],
            )
            self.assertEqual(total, 1)
            self.assertEqual(photos[0]["filename"], "one.png")

            _, photos, total = list_gallery_photos(
                conn,
                "output",
                page=2,
                per_page=1,
                include_tags=["portrait"],
            )
            self.assertEqual(total, 2)
            self.assertEqual(len(photos), 1)

    def test_gallery_route_preserves_tag_filters_in_state_and_pagination(self):
        create_png(self.images_root / "output" / "one.png", color=(20, 30, 40))
        create_png(self.images_root / "output" / "two.png", color=(30, 40, 50))
        create_png(self.images_root / "output" / "blocked.png", color=(40, 50, 60))
        scan_albums(self.db_path, self.images_root, self.thumbnails)
        with connect_db(self.db_path) as conn:
            photo_ids = {
                row["filename"]: row["photo_id"]
                for row in conn.execute(
                    "SELECT filename, photo_id FROM album_photos WHERE album_id=(SELECT id FROM albums WHERE name='output')"
                ).fetchall()
            }
            set_photo_tags(conn, photo_ids["one.png"], ["portrait"])
            set_photo_tags(conn, photo_ids["two.png"], ["portrait"])
            set_photo_tags(conn, photo_ids["blocked.png"], ["blocked"])

        previous_per_page = app_module.PER_PAGE
        previous_db_path = app_module.DB_PATH
        previous_images_root = app_module.IMAGES_ROOT
        previous_thumbnail_root = app_module.THUMBNAIL_ROOT
        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        app_module.PER_PAGE = 1
        try:
            client = app_module.app.test_client()
            response = client.get("/?album=output&include_tag=portrait&exclude_tag=blocked&page=1")
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn('id="filter-button" class="toolbar-icon-button is-active"', html)
            self.assertIn('aria-pressed="true"', html)
            self.assertIn('include: ["portrait"]', html)
            self.assertIn('exclude: ["blocked"]', html)
            self.assertIn(
                'href="?album=output&amp;page=2&amp;include_tag=portrait&amp;exclude_tag=blocked"',
                html,
            )

            no_results = client.get("/?album=output&include_tag=unknown&page=1")
            self.assertIn("Aucune photo ne correspond aux filtres", no_results.get_data(as_text=True))
        finally:
            app_module.PER_PAGE = previous_per_page
            app_module.DB_PATH = previous_db_path
            app_module.IMAGES_ROOT = previous_images_root
            app_module.THUMBNAIL_ROOT = previous_thumbnail_root


if __name__ == "__main__":
    unittest.main()
