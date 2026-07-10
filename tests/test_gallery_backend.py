import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image, PngImagePlugin

import app as app_module
from gallery_db import connect_db, list_albums, list_gallery_photos, scan_albums


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

        rescan = client.post(f"/api/photos/{source_id}/metadata/rescan")
        self.assertEqual(rescan.status_code, 200)
        self.assertEqual(rescan.get_json()["photo"]["metadata"]["prompt"], "prompt from png")

        link = client.post(f"/api/photos/{source_id}/links", json={"target_photo_id": target_id, "type": "variant"})
        self.assertEqual(link.status_code, 200)
        self.assertEqual(link.get_json()["photo"]["links"][0]["type"], "variant")


if __name__ == "__main__":
    unittest.main()
