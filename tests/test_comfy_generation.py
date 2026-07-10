import json
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image, PngImagePlugin

import app as app_module
from comfy_generation import ComfyClient, build_edit_options, list_lora_catalog, patch_prompt_and_workflow
from gallery_db import connect_db, init_db, upsert_photo


def create_png(path, color=(20, 40, 60), prompt=None, workflow=None):
    info = PngImagePlugin.PngInfo()
    if prompt is not None:
        info.add_text("prompt", prompt)
    if workflow is not None:
        info.add_text("workflow", workflow)
    Image.new("RGB", (24, 24), color).save(path, pnginfo=info)


def sample_prompt():
    return {
        "1": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "old prompt"}},
        "2": {"class_type": "TextEncode", "inputs": {"prompt": ["1", 0]}},
        "3": {"class_type": "Seed (rgthree)", "inputs": {"seed": 5}, "is_changed": [5]},
        "4": {"class_type": "KSampler", "inputs": {"seed": ["3", 0], "steps": 4}},
        "5": {"class_type": "LoadImage", "inputs": {"image": "ref.png"}},
        "6": {"class_type": "SaveImage", "inputs": {"images": ["5", 0]}},
        "7": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"lora_name": "kept.safetensors", "strength_model": 0.7, "model": ["4", 0]},
        },
        "8": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"lora_name": "lightning.safetensors", "strength_model": 1.0, "model": ["7", 0]},
        },
    }


def sample_workflow():
    return {
        "nodes": [
            {
                "id": 1,
                "type": "PrimitiveStringMultiline",
                "mode": 0,
                "pos": [10, 20],
                "inputs": [{"name": "value", "widget": {"name": "value"}}],
                "widgets_values": ["old prompt"],
            },
            {"id": 2, "type": "TextEncode", "mode": 0},
            {"id": 3, "type": "Seed (rgthree)", "mode": 0, "widgets_values": [5]},
            {
                "id": 4,
                "type": "KSampler",
                "mode": 0,
                "inputs": [
                    {"name": "seed", "widget": {"name": "seed"}},
                    {"name": "steps", "widget": {"name": "steps"}},
                ],
                "widgets_values": [5, 4],
            },
            {
                "id": 5,
                "type": "LoadImage",
                "mode": 0,
                "inputs": [{"name": "image", "widget": {"name": "image"}}],
                "widgets_values": ["ref.png", "image"],
            },
            {"id": 6, "type": "SaveImage", "mode": 0},
            {
                "id": 7,
                "type": "LoraLoaderModelOnly",
                "mode": 0,
                "inputs": [
                    {"name": "lora_name", "widget": {"name": "lora_name"}},
                    {"name": "strength_model", "widget": {"name": "strength_model"}},
                ],
                "widgets_values": ["kept.safetensors", 0.7],
            },
            {"id": 8, "type": "LoraLoaderModelOnly", "mode": 0},
        ],
        "links": [[1, 5, 0, 6, 0, "IMAGE"]],
        "groups": [{"id": 1, "title": "group", "bounding": [0, 0, 100, 100]}],
        "seed_widgets": {"3": 0},
    }


class FixedRng:
    def randint(self, _minimum, _maximum):
        return 99


class CapturingComfyClient(ComfyClient):
    def __init__(self):
        super().__init__(base_url="http://example.test")
        self.payload = None

    def post_json(self, _path, payload, timeout=None):
        self.payload = payload
        return {"prompt_id": "prompt-1"}


class FakeComfyClient:
    output_root = None
    available = True
    queued_prompt = None
    queued_workflow = None
    uploaded_paths = []

    def is_available(self):
        return self.available

    def upload_image(self, image_path):
        self.uploaded_paths.append(Path(image_path).name)
        return f"uploaded/{Path(image_path).name}"

    def run_prompt(self, prompt, workflow, client_id, progress_callback=None):
        type(self).queued_prompt = prompt
        type(self).queued_workflow = workflow
        if progress_callback:
            progress_callback({"state": "queued", "prompt_id": "prompt-1"})
            progress_callback({"state": "running", "node": "4", "value": 1, "max": 6})
        generated_path = self.output_root / "generated.png"
        create_png(generated_path, color=(80, 90, 100))
        if progress_callback:
            progress_callback({"preview": generated_path.read_bytes()})
        return "prompt-1", {"outputs": {"6": {"images": [{"filename": "generated.png", "subfolder": ""}]}}}


class ComfyGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.images_root = self.root / "static" / "images"
        self.thumbnails = self.root / "static" / "thumbnails"
        self.db_path = self.root / "instance" / "gallery.sqlite3"
        (self.images_root / "output").mkdir(parents=True)
        init_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lora_catalog_excludes_blacklist_and_patch_prompt(self):
        image_path = self.images_root / "output" / "source.png"
        create_png(image_path, prompt=json.dumps(sample_prompt()), workflow=json.dumps(sample_workflow()))

        with connect_db(self.db_path) as conn:
            photo_id = upsert_photo(conn, "source-checksum", 24, 24, image_path.stat().st_size)
            conn.execute(
                "INSERT INTO photo_metadata(photo_id, prompt, seed, raw_prompt_json, raw_workflow_json) VALUES (?, ?, ?, ?, ?)",
                (photo_id, "old prompt", "5", json.dumps(sample_prompt()), json.dumps(sample_workflow())),
            )
            conn.execute(
                "INSERT INTO photo_loras(photo_id, node_id, lora_name, strength_model) VALUES (?, ?, ?, ?)",
                (photo_id, "7", "kept.safetensors", 0.7),
            )
            conn.execute(
                "INSERT INTO photo_loras(photo_id, node_id, lora_name, strength_model) VALUES (?, ?, ?, ?)",
                (photo_id, "8", "lightning.safetensors", 1.0),
            )
            detail = {
                "metadata": {
                    "prompt": "old prompt",
                    "raw_prompt_json": json.dumps(sample_prompt()),
                    "raw_workflow_json": json.dumps(sample_workflow()),
                }
            }
            catalog = list_lora_catalog(conn)

        self.assertEqual([item["lora_name"] for item in catalog], ["kept.safetensors"])
        options = build_edit_options(detail, catalog)
        self.assertEqual(options["prompt"], "old prompt")
        self.assertEqual(options["steps"], 4)
        self.assertEqual([item["node_id"] for item in options["loras"]], ["7"])
        self.assertEqual([item["node_id"] for item in options["images"]], ["5"])

        patched, patched_workflow, info = patch_prompt_and_workflow(
            detail,
            {
                "prompt": "new prompt",
                "seed_mode": "random",
                "steps": 8,
                "loras": [{"node_id": "7", "lora_name": "kept.safetensors", "strength_model": 0.25, "enabled": False}],
            },
            uploaded_images={"5": "uploaded/ref.png"},
            rng=FixedRng(),
        )
        self.assertEqual(info["seed"], 99)
        self.assertEqual(patched["1"]["inputs"]["value"], "new prompt")
        self.assertEqual(patched["3"]["inputs"]["seed"], 99)
        self.assertEqual(patched["4"]["inputs"]["steps"], 8)
        self.assertEqual(patched["7"]["inputs"]["strength_model"], 0.0)
        self.assertEqual(patched["5"]["inputs"]["image"], "uploaded/ref.png")
        self.assertEqual(patched_workflow["groups"], sample_workflow()["groups"])
        self.assertEqual(patched_workflow["links"], sample_workflow()["links"])
        self.assertEqual(patched_workflow["nodes"][0]["pos"], [10, 20])
        self.assertEqual(patched_workflow["nodes"][0]["widgets_values"][0], "new prompt")
        self.assertEqual(patched_workflow["nodes"][2]["widgets_values"][0], 99)
        self.assertEqual(patched_workflow["nodes"][3]["widgets_values"][1], 8)
        self.assertEqual(patched_workflow["nodes"][4]["widgets_values"][0], "uploaded/ref.png")
        self.assertEqual(patched_workflow["nodes"][6]["mode"], 4)
        self.assertEqual(patched_workflow["nodes"][6]["widgets_values"], ["kept.safetensors", 0.0])

    def test_queue_prompt_sends_workflow_as_extra_pnginfo(self):
        client = CapturingComfyClient()
        workflow = sample_workflow()

        queued = client.queue_prompt(sample_prompt(), client_id="job-1", workflow=workflow)

        self.assertEqual(queued["prompt_id"], "prompt-1")
        self.assertEqual(client.payload["client_id"], "job-1")
        self.assertEqual(client.payload["extra_data"]["workflow"], workflow)
        self.assertEqual(client.payload["extra_data"]["extra_pnginfo"]["workflow"], workflow)

    def test_comfy_status_and_generate_endpoint_with_fake_client(self):
        source_prompt = json.dumps(sample_prompt())
        source_workflow = json.dumps(sample_workflow())
        create_png(self.images_root / "output" / "source.png", prompt=source_prompt, workflow=source_workflow)
        create_png(self.images_root / "output" / "ref.png", color=(10, 90, 10))

        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        previous_factory = app_module.COMFY_CLIENT_FACTORY
        FakeComfyClient.output_root = self.images_root / "output"
        FakeComfyClient.available = True
        FakeComfyClient.queued_prompt = None
        FakeComfyClient.queued_workflow = None
        FakeComfyClient.uploaded_paths = []
        app_module.COMFY_CLIENT_FACTORY = FakeComfyClient
        previous_scan_albums = app_module.scan_albums
        try:
            client = app_module.app.test_client()
            self.assertEqual(client.post("/api/scan", json={"metadata": True, "sync": True}).status_code, 200)
            with connect_db(self.db_path) as conn:
                source_id = conn.execute(
                    "SELECT p.id FROM photos p JOIN album_photos ap ON ap.photo_id=p.id WHERE ap.filename='source.png'"
                ).fetchone()["id"]
                ref_id = conn.execute(
                    "SELECT p.id FROM photos p JOIN album_photos ap ON ap.photo_id=p.id WHERE ap.filename='ref.png'"
                ).fetchone()["id"]

            status = client.get("/api/comfy/status")
            self.assertEqual(status.status_code, 200)
            self.assertTrue(status.get_json()["available"])

            FakeComfyClient.available = False
            unavailable = client.get("/api/comfy/status")
            self.assertEqual(unavailable.status_code, 200)
            self.assertFalse(unavailable.get_json()["available"])
            refused = client.post(f"/api/photos/{source_id}/comfy/generate", json={"prompt": "x"})
            self.assertEqual(refused.status_code, 503)
            FakeComfyClient.available = True

            options = client.get(f"/api/photos/{source_id}/comfy/edit-options")
            self.assertEqual(options.status_code, 200)
            self.assertEqual(options.get_json()["options"]["prompt"], "old prompt")

            def fail_scan(*_args, **_kwargs):
                raise AssertionError("scan_albums must not be called after generation")

            app_module.scan_albums = fail_scan
            generated = client.post(
                f"/api/photos/{source_id}/comfy/generate",
                json={
                    "prompt": "generated prompt",
                    "seed_mode": "keep",
                    "steps": 6,
                    "loras": [{"node_id": "7", "lora_name": "kept.safetensors", "strength_model": 0.5, "enabled": True}],
                    "references": [{"node_id": "5", "photo_id": ref_id}],
                },
            )
            self.assertEqual(generated.status_code, 202)
            job_id = generated.get_json()["job"]["id"]
            generated_photo = None
            job = None
            for _ in range(30):
                job_response = client.get(f"/api/comfy/jobs/{job_id}")
                self.assertEqual(job_response.status_code, 200)
                job = job_response.get_json()["job"]
                if job["state"] == "done":
                    generated_photo = job["photo"]
                    break
                time.sleep(0.05)
            self.assertIsNotNone(generated_photo)
            self.assertTrue(job["preview_available"])
            preview = client.get(f"/api/comfy/jobs/{job_id}/preview")
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(generated_photo["memberships"][0]["filename"], "generated.png")
            self.assertEqual(FakeComfyClient.queued_prompt["1"]["inputs"]["value"], "generated prompt")
            self.assertEqual(FakeComfyClient.queued_prompt["4"]["inputs"]["steps"], 6)
            self.assertEqual(FakeComfyClient.queued_prompt["5"]["inputs"]["image"], "uploaded/ref.png")
            self.assertIsNotNone(FakeComfyClient.queued_workflow)
            self.assertEqual(FakeComfyClient.queued_workflow["nodes"][0]["widgets_values"][0], "generated prompt")
            self.assertEqual(FakeComfyClient.queued_workflow["groups"], sample_workflow()["groups"])

            refreshed = client.get(f"/api/photos/{source_id}").get_json()["photo"]
            self.assertIn("variant", [link["type"] for link in refreshed["links"]])
        finally:
            app_module.COMFY_CLIENT_FACTORY = previous_factory
            app_module.scan_albums = previous_scan_albums


if __name__ == "__main__":
    unittest.main()
