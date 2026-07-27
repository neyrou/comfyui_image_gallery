import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from PIL import Image, PngImagePlugin

import app as app_module
from gallery_db import (
    connect_db,
    create_lora_tag_mapping,
    delete_lora_tag_mapping,
    init_db,
    rescan_metadata,
    scan_albums,
    set_photo_tags,
    upsert_tag,
    update_lora_tag_mapping,
)


def create_png(path, prompt=None, workflow=None):
    info = PngImagePlugin.PngInfo()
    if prompt is not None:
        info.add_text("prompt", json.dumps(prompt))
    if workflow is not None:
        info.add_text("workflow", json.dumps(workflow))
    Image.new("RGB", (24, 24), (20, 40, 60)).save(path, pnginfo=info)


def lora_payloads():
    strengths = {
        "1": ("positive-auto.safetensors", 0.8),
        "2": ("positive-manual.safetensors", 1),
        "3": ("positive-face.safetensors", 0.25),
        "4": ("zero.safetensors", 0),
        "5": ("negative.safetensors", -0.5),
        "6": ("invalid.safetensors", "invalid"),
        "7": ("bypassed.safetensors", 1),
    }
    prompt = {
        node_id: {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"lora_name": name, "strength_model": strength},
        }
        for node_id, (name, strength) in strengths.items()
    }
    workflow = {
        "nodes": [
            {
                "id": int(node_id),
                "type": "LoraLoaderModelOnly",
                "mode": 4 if node_id == "7" else 0,
            }
            for node_id in strengths
        ],
        "links": [],
    }
    return prompt, workflow


class LoraTagMappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.images_root = self.root / "static" / "images"
        self.thumbnails = self.root / "static" / "thumbnails"
        self.db_path = self.root / "instance" / "gallery.sqlite3"
        (self.images_root / "output").mkdir(parents=True)
        self.previous = {
            "DB_PATH": app_module.DB_PATH,
            "IMAGES_ROOT": app_module.IMAGES_ROOT,
            "THUMBNAIL_ROOT": app_module.THUMBNAIL_ROOT,
        }
        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails

    def tearDown(self):
        for name, value in self.previous.items():
            setattr(app_module, name, value)
        self.tmp.cleanup()

    def test_init_db_migrates_existing_single_tag_mapping(self):
        self.db_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, created_at TEXT)"
            )
            conn.execute("INSERT INTO tags(name) VALUES ('preserved')")
            conn.execute(
                """
                CREATE TABLE lora_tag_mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lora_name TEXT NOT NULL UNIQUE,
                    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                INSERT INTO lora_tag_mappings(lora_name, tag_id)
                VALUES ('legacy.safetensors', 1)
                """
            )
            conn.commit()

        init_db(self.db_path)

        with connect_db(self.db_path) as conn:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(lora_tag_mappings)").fetchall()
            }
            mapping = conn.execute(
                """
                SELECT ltm.id, ltm.lora_name, t.name AS tag_name
                FROM lora_tag_mappings ltm
                JOIN lora_tag_mapping_tags lmtt ON lmtt.mapping_id=ltm.id
                JOIN tags t ON t.id=lmtt.tag_id
                """
            ).fetchone()
            preserved = conn.execute("SELECT name FROM tags WHERE name='preserved'").fetchone()
        self.assertNotIn("tag_id", columns)
        self.assertEqual(mapping["id"], 1)
        self.assertEqual(mapping["lora_name"], "legacy.safetensors")
        self.assertEqual(mapping["tag_name"], "preserved")
        self.assertEqual(preserved["name"], "preserved")

    def test_mapping_api_validates_catalog_duplicates_and_deletion(self):
        prompt, workflow = lora_payloads()
        create_png(self.images_root / "output" / "source.png", prompt, workflow)
        scan_albums(self.db_path, self.images_root, self.thumbnails, scan_metadata=True)
        client = app_module.app.test_client()

        initial = client.get("/api/lora-tag-mappings")
        self.assertEqual(initial.status_code, 200)
        catalog = {item["lora_name"] for item in initial.get_json()["loras"]}
        self.assertIn("positive-auto.safetensors", catalog)
        self.assertNotIn("bypassed.safetensors", catalog)

        self.assertEqual(client.post("/api/lora-tag-mappings", json={}).status_code, 400)
        self.assertEqual(
            client.post(
                "/api/lora-tag-mappings",
                json={"lora_name": "unknown.safetensors", "tag_names": ["style"]},
            ).status_code,
            400,
        )
        self.assertEqual(
            client.post(
                "/api/lora-tag-mappings",
                json={"lora_name": "positive-auto.safetensors", "tag_names": []},
            ).status_code,
            400,
        )
        self.assertEqual(
            client.post(
                "/api/lora-tag-mappings",
                json={"lora_name": "positive-auto.safetensors", "tag_names": ["style", 3]},
            ).status_code,
            400,
        )
        created = client.post(
            "/api/lora-tag-mappings",
            json={
                "lora_name": "positive-auto.safetensors",
                "tag_names": [" style ", "portrait", "style", "", "cinematic"],
            },
        )
        self.assertEqual(created.status_code, 201)
        mapping = created.get_json()["mapping"]
        self.assertEqual(
            [tag["name"] for tag in mapping["tags"]],
            ["cinematic", "portrait", "style"],
        )
        self.assertEqual(
            client.post(
                "/api/lora-tag-mappings",
                json={"lora_name": "positive-auto.safetensors", "tag_names": ["other"]},
            ).status_code,
            409,
        )
        self.assertEqual(
            client.patch(
                f"/api/lora-tag-mappings/{mapping['id']}",
                json={"tag_names": []},
            ).status_code,
            400,
        )
        self.assertEqual(
            client.patch(
                "/api/lora-tag-mappings/999999",
                json={"tag_names": ["missing"]},
            ).status_code,
            404,
        )
        updated = client.patch(
            f"/api/lora-tag-mappings/{mapping['id']}",
            json={"tag_names": [" editorial ", "night", "editorial", ""]},
        )
        self.assertEqual(updated.status_code, 200)
        updated_mapping = updated.get_json()["mapping"]
        self.assertEqual(updated_mapping["id"], mapping["id"])
        self.assertEqual(
            [tag["name"] for tag in updated_mapping["tags"]],
            ["editorial", "night"],
        )

        listed = client.get("/api/lora-tag-mappings").get_json()["mappings"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["lora_name"], "positive-auto.safetensors")
        self.assertEqual(
            [tag["name"] for tag in listed[0]["tags"]],
            ["editorial", "night"],
        )
        self.assertEqual(client.delete(f"/api/lora-tag-mappings/{mapping['id']}").status_code, 200)
        self.assertEqual(client.delete(f"/api/lora-tag-mappings/{mapping['id']}").status_code, 404)

    def test_json_rescan_applies_only_positive_active_loras_and_preserves_other_sources(self):
        prompt, workflow = lora_payloads()
        image_path = self.images_root / "output" / "source.png"
        create_png(image_path, prompt, workflow)
        scan_albums(self.db_path, self.images_root, self.thumbnails)

        with connect_db(self.db_path) as conn:
            photo_id = conn.execute("SELECT id FROM photos").fetchone()["id"]
            mappings = {
                "positive-auto.safetensors": create_lora_tag_mapping(
                    conn, "positive-auto.safetensors", ["style", "cinematic"]
                ),
                "positive-manual.safetensors": create_lora_tag_mapping(
                    conn, "positive-manual.safetensors", ["manual-shared"]
                ),
                "positive-face.safetensors": create_lora_tag_mapping(
                    conn, "positive-face.safetensors", ["face-shared"]
                ),
                "zero.safetensors": create_lora_tag_mapping(
                    conn, "zero.safetensors", ["zero-tag"]
                ),
                "negative.safetensors": create_lora_tag_mapping(
                    conn, "negative.safetensors", ["negative-tag"]
                ),
                "invalid.safetensors": create_lora_tag_mapping(
                    conn, "invalid.safetensors", ["invalid-tag"]
                ),
                "bypassed.safetensors": create_lora_tag_mapping(
                    conn, "bypassed.safetensors", ["bypassed-tag"]
                ),
            }
            set_photo_tags(conn, photo_id, ["manual-shared"])
            face_tag = upsert_tag(conn, "face-shared")
            conn.execute(
                "INSERT INTO photo_tags(photo_id, tag_id, source) VALUES (?, ?, 'face_auto')",
                (photo_id, face_tag["id"]),
            )
            rescan_metadata(conn, photo_id, image_path)
            tags = {
                row["name"]: row["source"]
                for row in conn.execute(
                    """
                    SELECT t.name, pt.source
                    FROM photo_tags pt JOIN tags t ON t.id=pt.tag_id
                    WHERE pt.photo_id=?
                    """,
                    (photo_id,),
                ).fetchall()
            }

        self.assertEqual(tags["style"], "lora_auto")
        self.assertEqual(tags["cinematic"], "lora_auto")
        self.assertEqual(tags["manual-shared"], "manual")
        self.assertEqual(tags["face-shared"], "face_auto")
        for excluded in ("zero-tag", "negative-tag", "invalid-tag", "bypassed-tag"):
            self.assertNotIn(excluded, tags)

        with connect_db(self.db_path) as conn:
            update_lora_tag_mapping(
                conn,
                mappings["positive-auto.safetensors"]["id"],
                ["editorial", "night"],
            )
            rescan_metadata(conn, photo_id, image_path)
            edited = {
                row["name"]: row["source"]
                for row in conn.execute(
                    """
                    SELECT t.name, pt.source
                    FROM photo_tags pt JOIN tags t ON t.id=pt.tag_id
                    WHERE pt.photo_id=?
                    """,
                    (photo_id,),
                ).fetchall()
            }
        self.assertNotIn("style", edited)
        self.assertNotIn("cinematic", edited)
        self.assertEqual(edited["editorial"], "lora_auto")
        self.assertEqual(edited["night"], "lora_auto")
        self.assertEqual(edited["manual-shared"], "manual")
        self.assertEqual(edited["face-shared"], "face_auto")

        with connect_db(self.db_path) as conn:
            delete_lora_tag_mapping(conn, mappings["positive-auto.safetensors"]["id"])
            rescan_metadata(conn, photo_id, image_path)
            refreshed = {
                row["name"]: row["source"]
                for row in conn.execute(
                    """
                    SELECT t.name, pt.source
                    FROM photo_tags pt JOIN tags t ON t.id=pt.tag_id
                    WHERE pt.photo_id=?
                    """,
                    (photo_id,),
                ).fetchall()
            }
        self.assertNotIn("editorial", refreshed)
        self.assertNotIn("night", refreshed)
        self.assertEqual(refreshed["manual-shared"], "manual")
        self.assertEqual(refreshed["face-shared"], "face_auto")


if __name__ == "__main__":
    unittest.main()
