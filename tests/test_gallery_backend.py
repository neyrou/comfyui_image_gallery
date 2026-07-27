import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, PngImagePlugin

import app as app_module
import gallery_db as gallery_db_module
from gallery_db import (
    ScanCancelled,
    connect_db,
    find_photo_file,
    get_album_by_name,
    get_photo_detail,
    list_album_tag_stats,
    list_albums,
    list_gallery_photos,
    scan_album,
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


def create_oriented_jpeg(path):
    image = Image.new("RGB", (40, 20), (20, 40, 60))
    exif = Image.Exif()
    exif[274] = 6
    image.save(path, exif=exif)


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

    def test_scan_can_target_only_one_album(self):
        create_png(self.images_root / "output" / "output.png")
        create_png(self.images_root / "Celine" / "celine.png", color=(60, 40, 20))
        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails

        response = app_module.app.test_client().post(
            "/api/scan",
            json={"album": "output", "metadata": False, "sync": True},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["summary"]["albums"], 1)
        with connect_db(self.db_path) as conn:
            scanned_albums = {
                row["name"]
                for row in conn.execute(
                    """
                    SELECT DISTINCT a.name
                    FROM albums a
                    JOIN album_photos ap ON ap.album_id=a.id
                    WHERE ap.is_missing=0
                    """
                ).fetchall()
            }
        self.assertEqual(scanned_albums, {"output"})

    def test_thumbnail_applies_exif_orientation_and_can_be_refreshed(self):
        image_path = self.images_root / "output" / "oriented.jpg"
        create_oriented_jpeg(image_path)
        scan_albums(self.db_path, self.images_root, self.thumbnails)

        with connect_db(self.db_path) as conn:
            photo = conn.execute("SELECT id, checksum FROM photos").fetchone()
        thumbnail_path = self.thumbnails / f'{photo["checksum"]}.jpg'
        with Image.open(thumbnail_path) as thumbnail:
            self.assertEqual(thumbnail.size, (20, 40))

        Image.new("RGB", (5, 5), (255, 0, 0)).save(thumbnail_path)
        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        response = app_module.app.test_client().post(f'/api/photos/{photo["id"]}/thumbnail/refresh')

        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.get_json()["photo"]["thumbnail_url"], r"\.jpg\?v=\d+$")
        with Image.open(thumbnail_path) as thumbnail:
            self.assertEqual(thumbnail.size, (20, 40))

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

    def test_scan_preserves_album_when_its_path_is_unavailable(self):
        image_path = self.images_root / "Celine" / "offline.png"
        create_png(image_path, prompt="preserved metadata")
        scan_albums(self.db_path, self.images_root, self.thumbnails, scan_metadata=True)

        with connect_db(self.db_path) as conn:
            photo_id = conn.execute(
                "SELECT photo_id FROM album_photos WHERE album_id=(SELECT id FROM albums WHERE name='Celine')"
            ).fetchone()["photo_id"]
            set_photo_tags(conn, photo_id, ["preserved-tag"])
            conn.execute(
                "UPDATE photo_metadata SET prompt='preserved metadata' WHERE photo_id=?",
                (photo_id,),
            )

        shutil.rmtree(self.images_root / "Celine")
        summary = scan_albums(self.db_path, self.images_root, self.thumbnails)

        with connect_db(self.db_path) as conn:
            album, photos, total = list_gallery_photos(conn, "Celine")
            detail = get_photo_detail(conn, photo_id)
            membership = conn.execute(
                "SELECT is_missing FROM album_photos WHERE album_id=? AND photo_id=?",
                (album["id"], photo_id),
            ).fetchone()

        self.assertEqual(total, 1)
        self.assertEqual(photos[0]["id"], photo_id)
        self.assertEqual(membership["is_missing"], 0)
        self.assertIn("Album path is unavailable", album["scan_error"])
        self.assertEqual([tag["name"] for tag in detail["tags"]], ["preserved-tag"])
        self.assertEqual(detail["metadata"]["prompt"], "preserved metadata")
        self.assertFalse(detail["memberships"][0]["available"])
        self.assertTrue(any(error["album"] == "Celine" for error in summary["errors"]))

    def test_scan_marks_deleted_file_missing_after_complete_traversal(self):
        kept_path = self.images_root / "Celine" / "kept.png"
        deleted_path = self.images_root / "Celine" / "deleted.png"
        create_png(kept_path)
        create_png(deleted_path, color=(30, 50, 70))
        scan_albums(self.db_path, self.images_root, self.thumbnails)

        deleted_path.unlink()
        scan_albums(self.db_path, self.images_root, self.thumbnails)

        with connect_db(self.db_path) as conn:
            states = {
                row["filename"]: row["is_missing"]
                for row in conn.execute(
                    "SELECT filename, is_missing FROM album_photos WHERE album_id=(SELECT id FROM albums WHERE name='Celine')"
                ).fetchall()
            }
        self.assertEqual(states, {"deleted.png": 1, "kept.png": 0})

    def test_scan_preserves_unreadable_file_but_reconciles_other_files(self):
        unreadable_path = self.images_root / "Celine" / "unreadable.png"
        deleted_path = self.images_root / "Celine" / "deleted.png"
        create_png(unreadable_path)
        create_png(deleted_path, color=(30, 50, 70))
        scan_albums(self.db_path, self.images_root, self.thumbnails)

        deleted_path.unlink()
        with patch("gallery_db.checksum_file", side_effect=OSError("temporarily unreadable")):
            scan_albums(self.db_path, self.images_root, self.thumbnails)

        with connect_db(self.db_path) as conn:
            states = {
                row["filename"]: row["is_missing"]
                for row in conn.execute(
                    "SELECT filename, is_missing FROM album_photos WHERE album_id=(SELECT id FROM albums WHERE name='Celine')"
                ).fetchall()
            }
            album = get_album_by_name(conn, "Celine")
        self.assertEqual(states, {"deleted.png": 1, "unreadable.png": 0})
        self.assertEqual(album["scan_error"], "temporarily unreadable")

    def test_scan_preserves_unseen_files_after_partial_traversal_failure(self):
        first_path = self.images_root / "Celine" / "first.png"
        second_path = self.images_root / "Celine" / "second.png"
        create_png(first_path)
        create_png(second_path, color=(30, 50, 70))
        scan_albums(self.db_path, self.images_root, self.thumbnails)

        with connect_db(self.db_path) as conn:
            album = get_album_by_name(conn, "Celine")

            def partial_walk(_root):
                yield first_path
                raise OSError("subdirectory unavailable")

            with patch("gallery_db._iter_image_files", side_effect=partial_walk):
                result = scan_album(conn, album, self.thumbnails)

            states = {
                row["filename"]: row["is_missing"]
                for row in conn.execute(
                    "SELECT filename, is_missing FROM album_photos WHERE album_id=?",
                    (album["id"],),
                ).fetchall()
            }
            refreshed_album = get_album_by_name(conn, "Celine")

        self.assertEqual(states, {"first.png": 0, "second.png": 0})
        self.assertEqual(result["error"], "subdirectory unavailable")
        self.assertEqual(refreshed_album["scan_error"], "subdirectory unavailable")

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

    def test_batch_tags_add_remove_and_validate_atomically(self):
        create_png(self.images_root / "output" / "one.png")
        create_png(self.images_root / "output" / "two.png", color=(30, 50, 70))
        scan_albums(self.db_path, self.images_root, self.thumbnails)
        with connect_db(self.db_path) as conn:
            photo_ids = {
                row["filename"]: row["photo_id"]
                for row in conn.execute("SELECT filename, photo_id FROM album_photos").fetchall()
            }
            set_photo_tags(conn, photo_ids["one.png"], ["old", "remove-me"])
            set_photo_tags(conn, photo_ids["two.png"], ["old"])

        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        client = app_module.app.test_client()

        added = client.patch(
            "/api/photos/batch/tags",
            json={
                "photo_ids": [photo_ids["one.png"], photo_ids["two.png"], photo_ids["one.png"]],
                "operation": "add",
                "tags": [" new ", "new", "old", ""],
            },
        )
        self.assertEqual(added.status_code, 200)
        self.assertEqual(added.get_json()["summary"]["updated"], 2)

        removed = client.patch(
            "/api/photos/batch/tags",
            json={
                "photo_ids": list(photo_ids.values()),
                "operation": "remove",
                "tags": ["remove-me", "not-present"],
            },
        )
        self.assertEqual(removed.status_code, 200)
        with connect_db(self.db_path) as conn:
            one_tags = {tag["name"] for tag in get_photo_detail(conn, photo_ids["one.png"])["tags"]}
            two_tags = {tag["name"] for tag in get_photo_detail(conn, photo_ids["two.png"])["tags"]}
        self.assertEqual(one_tags, {"old", "new"})
        self.assertEqual(two_tags, {"old", "new"})

        invalid = client.patch(
            "/api/photos/batch/tags",
            json={"photo_ids": [photo_ids["one.png"]], "operation": "replace", "tags": ["bad"]},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(
            client.patch(
                "/api/photos/batch/tags",
                json={"photo_ids": [], "operation": "add", "tags": ["bad"]},
            ).status_code,
            400,
        )
        self.assertEqual(
            client.patch(
                "/api/photos/batch/tags",
                json={"photo_ids": list(range(1, 102)), "operation": "add", "tags": ["bad"]},
            ).status_code,
            400,
        )
        missing = client.patch(
            "/api/photos/batch/tags",
            json={"photo_ids": [photo_ids["one.png"], 999999], "operation": "add", "tags": ["atomic"]},
        )
        self.assertEqual(missing.status_code, 404)
        with connect_db(self.db_path) as conn:
            one_tags = {tag["name"] for tag in get_photo_detail(conn, photo_ids["one.png"])["tags"]}
        self.assertNotIn("atomic", one_tags)

    def test_batch_album_copy_skips_collisions_and_continues_after_failure(self):
        one = self.images_root / "output" / "one.png"
        two = self.images_root / "output" / "two.png"
        missing = self.images_root / "output" / "missing.png"
        create_png(one, color=(10, 20, 30))
        create_png(two, color=(20, 30, 40))
        create_png(missing, color=(30, 40, 50))
        shutil.copyfile(one, self.images_root / "Celine" / "already-one.png")
        create_png(self.images_root / "Celine" / "two.png", color=(90, 80, 70))
        scan_albums(self.db_path, self.images_root, self.thumbnails)
        with connect_db(self.db_path) as conn:
            photo_ids = {
                row["filename"]: row["photo_id"]
                for row in conn.execute(
                    """
                    SELECT ap.filename, ap.photo_id
                    FROM album_photos ap JOIN albums a ON a.id=ap.album_id
                    WHERE a.name='output'
                    """
                ).fetchall()
            }
        missing.unlink()

        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        client = app_module.app.test_client()
        response = client.post(
            "/api/photos/batch/album-copy",
            json={
                "photo_ids": [photo_ids["one.png"], photo_ids["two.png"], photo_ids["missing.png"]],
                "source_album_name": "output",
                "destination_album_name": "Celine",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["summary"], {"requested": 3, "copied": 1, "skipped": 1, "failed": 1})
        statuses = {result["photo_id"]: result for result in data["results"]}
        self.assertEqual(statuses[photo_ids["one.png"]]["status"], "skipped")
        self.assertTrue(statuses[photo_ids["one.png"]]["favorite"])
        self.assertEqual(statuses[photo_ids["two.png"]]["status"], "copied")
        self.assertTrue(statuses[photo_ids["two.png"]]["favorite"])
        self.assertEqual(statuses[photo_ids["missing.png"]]["status"], "failed")
        self.assertTrue((self.images_root / "Celine" / "two-1.png").is_file())

        repeated = client.post(
            "/api/photos/batch/album-copy",
            json={
                "photo_ids": [photo_ids["two.png"]],
                "source_album_name": "output",
                "destination_album_name": "Celine",
            },
        )
        self.assertEqual(repeated.get_json()["summary"]["skipped"], 1)
        self.assertFalse((self.images_root / "Celine" / "two-2.png").exists())

    def test_scan_error_does_not_make_an_accessible_album_unavailable(self):
        source = self.images_root / "output" / "source.png"
        create_png(source)
        scan_albums(self.db_path, self.images_root, self.thumbnails)
        with connect_db(self.db_path) as conn:
            photo_id = conn.execute(
                "SELECT photo_id FROM album_photos WHERE filename='source.png'"
            ).fetchone()["photo_id"]
            conn.execute("UPDATE albums SET scan_error='One file could not be read' WHERE name='Celine'")
            albums = {album["name"]: album for album in list_albums(conn)}
        self.assertTrue(albums["Celine"]["available"])

        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        response = app_module.app.test_client().post(
            "/api/photos/batch/album-copy",
            json={
                "photo_ids": [photo_id],
                "source_album_name": "output",
                "destination_album_name": "Celine",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["summary"]["copied"], 1)
        self.assertTrue((self.images_root / "Celine" / "source.png").is_file())

    def test_available_album_hides_stale_path_unavailable_error(self):
        stale_error = f"Album path is unavailable: {self.images_root / 'Celine'}"
        with connect_db(self.db_path) as conn:
            scan_albums(self.db_path, self.images_root, self.thumbnails)
            conn.execute(
                "UPDATE albums SET scan_error=? WHERE name='Celine'",
                (stale_error,),
            )
            albums = {album["name"]: album for album in list_albums(conn)}

        self.assertTrue(albums["Celine"]["available"])
        self.assertIsNone(albums["Celine"]["scan_error"])

        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        response = app_module.app.test_client().get("/?album=Celine")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Album illisible", html)
        self.assertNotIn(stale_error, html)

    def test_scan_retries_album_path_while_network_share_wakes_up(self):
        image_path = self.images_root / "Celine" / "network.png"
        create_png(image_path)
        real_scandir = os.scandir
        attempts = 0

        def flaky_scandir(path):
            nonlocal attempts
            if Path(path) == self.images_root / "Celine" and attempts < 2:
                attempts += 1
                raise OSError("network share is waking up")
            return real_scandir(path)

        with patch("gallery_db.os.scandir", side_effect=flaky_scandir), patch("gallery_db.time.sleep"):
            summary = scan_albums(self.db_path, self.images_root, self.thumbnails)

        with connect_db(self.db_path) as conn:
            album = get_album_by_name(conn, "Celine")
        self.assertEqual(attempts, 2)
        self.assertIsNone(album["scan_error"])
        self.assertFalse(summary["errors"])

    def test_batch_delete_removes_selected_photos_and_validates_before_deleting(self):
        one = self.images_root / "output" / "one.png"
        two = self.images_root / "output" / "two.png"
        create_png(one)
        create_png(two, color=(30, 50, 70))
        scan_albums(self.db_path, self.images_root, self.thumbnails)
        with connect_db(self.db_path) as conn:
            photo_ids = {
                row["filename"]: row["photo_id"]
                for row in conn.execute("SELECT filename, photo_id FROM album_photos").fetchall()
            }

        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        client = app_module.app.test_client()

        missing = client.delete(
            "/api/photos/batch",
            json={"photo_ids": [photo_ids["one.png"], 999999]},
        )
        self.assertEqual(missing.status_code, 404)
        self.assertTrue(one.is_file())

        response = client.delete(
            "/api/photos/batch",
            json={"photo_ids": [photo_ids["one.png"], photo_ids["two.png"]]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["summary"], {"requested": 2, "deleted": 2, "failed": 0})
        self.assertFalse(one.exists())
        self.assertFalse(two.exists())
        with connect_db(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0], 0)

    def test_batch_metadata_scan_continues_after_unreadable_file(self):
        prompt = {"1": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "batch prompt"}}}
        valid = self.images_root / "output" / "valid.png"
        broken = self.images_root / "output" / "broken.png"
        create_png(valid, prompt=__import__("json").dumps(prompt))
        create_png(broken, color=(80, 30, 20))
        scan_albums(self.db_path, self.images_root, self.thumbnails)
        with connect_db(self.db_path) as conn:
            photo_ids = {
                row["filename"]: row["photo_id"]
                for row in conn.execute("SELECT filename, photo_id FROM album_photos").fetchall()
            }
        broken.write_bytes(b"not an image")

        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        client = app_module.app.test_client()
        response = client.post(
            "/api/photos/batch/metadata/rescan",
            json={"photo_ids": [photo_ids["valid.png"], photo_ids["broken.png"]]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["summary"], {"requested": 2, "scanned": 1, "failed": 1})
        with connect_db(self.db_path) as conn:
            self.assertEqual(get_photo_detail(conn, photo_ids["valid.png"])["metadata"]["prompt"], "batch prompt")

    def test_gallery_renders_multi_selection_controls(self):
        create_png(self.images_root / "output" / "one.png")
        scan_albums(self.db_path, self.images_root, self.thumbnails)
        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        response = app_module.app.test_client().get("/?album=output")
        html = response.get_data(as_text=True)
        self.assertIn('id="selection-actions"', html)
        self.assertIn('data-batch-action="album"', html)
        self.assertIn('class="selection-checkbox"', html)
        self.assertIn('id="batch-tag-modal"', html)
        self.assertNotIn('id="face-admin-button"', html)
        self.assertIn('id="config-section-albums"', html)
        self.assertIn('id="config-section-tags"', html)
        self.assertIn('id="config-section-faces"', html)
        self.assertIn('data-batch-action="faces"', html)
        self.assertIn('class="danger-menu-item" data-batch-action="delete"', html)
        self.assertNotIn('id="face-admin-modal"', html)
        self.assertIn('id="admin-modal"', html)
        self.assertIn('id="detail-faces"', html)
        self.assertIn('id="scan-options-modal"', html)
        self.assertIn('id="scan-options-scope"', html)
        self.assertIn('id="scan-options-metadata"', html)
        self.assertIn('id="scan-options-faces"', html)
        self.assertIn('id="scan-options-force-faces"', html)
        self.assertIn('id="scan-options-image-analysis" type="checkbox" disabled', html)
        self.assertIn('id="scan-status-cancel"', html)

    def test_incremental_scan_skips_unchanged_reprocesses_modified_and_marks_deleted(self):
        unchanged = self.images_root / "output" / "unchanged.png"
        modified = self.images_root / "output" / "modified.png"
        deleted = self.images_root / "output" / "deleted.png"
        create_png(unchanged)
        create_png(modified, color=(30, 50, 70))
        create_png(deleted, color=(70, 50, 30))
        scan_albums(self.db_path, self.images_root, self.thumbnails)

        create_png(modified, color=(90, 20, 40))
        current_mtime = modified.stat().st_mtime
        os.utime(modified, (current_mtime + 5, current_mtime + 5))
        deleted.unlink()

        with patch("gallery_db.checksum_file", wraps=gallery_db_module.checksum_file) as checksum:
            summary = scan_albums(
                self.db_path,
                self.images_root,
                self.thumbnails,
                rescan_existing=False,
            )

        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["missing"], 2)
        self.assertEqual(checksum.call_count, 1)
        with connect_db(self.db_path) as conn:
            rows = conn.execute(
                "SELECT filename, is_missing FROM album_photos WHERE album_id=(SELECT id FROM albums WHERE name='output')"
            ).fetchall()
        states = {}
        for row in rows:
            states.setdefault(row["filename"], []).append(row["is_missing"])
        self.assertEqual(states["unchanged.png"], [0])
        self.assertEqual(sorted(states["modified.png"]), [0, 1])
        self.assertEqual(states["deleted.png"], [1])

    def test_incremental_json_scan_skips_metadata_until_full_rescan(self):
        image_path = self.images_root / "output" / "metadata.png"
        create_png(image_path, prompt="stored prompt")
        scan_albums(self.db_path, self.images_root, self.thumbnails)

        incremental = scan_albums(
            self.db_path,
            self.images_root,
            self.thumbnails,
            scan_metadata=True,
            rescan_existing=False,
        )
        with connect_db(self.db_path) as conn:
            self.assertIsNone(conn.execute("SELECT prompt FROM photo_metadata").fetchone())

        full = scan_albums(
            self.db_path,
            self.images_root,
            self.thumbnails,
            scan_metadata=True,
            rescan_existing=True,
        )
        with connect_db(self.db_path) as conn:
            metadata_count = conn.execute("SELECT COUNT(*) AS total FROM photo_metadata").fetchone()["total"]
        self.assertEqual(incremental["skipped"], 1)
        self.assertEqual(full["processed"], 1)
        self.assertEqual(metadata_count, 1)

    def test_cancelled_scan_does_not_mark_unvisited_membership_missing(self):
        kept = self.images_root / "output" / "a-kept.png"
        removed = self.images_root / "output" / "z-removed.png"
        create_png(kept)
        create_png(removed, color=(70, 50, 30))
        scan_albums(self.db_path, self.images_root, self.thumbnails)
        removed.unlink()
        checks = {"count": 0}

        def cancel_after_traversal():
            checks["count"] += 1
            return checks["count"] >= 3

        with self.assertRaises(ScanCancelled):
            scan_albums(
                self.db_path,
                self.images_root,
                self.thumbnails,
                rescan_existing=False,
                cancel_callback=cancel_after_traversal,
                album_name="output",
            )

        with connect_db(self.db_path) as conn:
            missing = conn.execute(
                "SELECT is_missing FROM album_photos WHERE filename='z-removed.png'"
            ).fetchone()["is_missing"]
        self.assertEqual(missing, 0)

    def test_scan_api_validates_options_and_cancel_is_idempotent(self):
        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        client = app_module.app.test_client()

        unsupported = client.post("/api/scan", json={"image_analysis": True})
        invalid = client.post("/api/scan", json={"metadata": "yes"})
        self.assertEqual(unsupported.status_code, 400)
        self.assertEqual(invalid.status_code, 400)

        original = app_module.scan_status_snapshot()
        try:
            app_module.SCAN_STATUS.update(
                {
                    "active": True,
                    "job_id": "scan-test",
                    "state": "running",
                    "cancel_requested": False,
                    "face_job_id": None,
                }
            )
            first = client.post("/api/scan/jobs/scan-test/cancel", json={})
            second = client.post("/api/scan/jobs/scan-test/cancel", json={})
            missing = client.post("/api/scan/jobs/missing/cancel", json={})
            self.assertEqual(first.status_code, 202)
            self.assertEqual(second.status_code, 202)
            self.assertEqual(first.get_json()["job"]["state"], "cancel_requested")
            self.assertEqual(missing.status_code, 404)
        finally:
            with app_module.SCAN_LOCK:
                app_module.SCAN_STATUS.clear()
                app_module.SCAN_STATUS.update(original)

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
