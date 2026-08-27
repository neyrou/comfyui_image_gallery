import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib import error

from PIL import Image, PngImagePlugin

import app as app_module
from comfy_generation import (
    I2V_IDLE_PROMPT_SUFFIX,
    ComfyClient,
    ComfyGenerationCancelled,
    build_edit_options,
    build_registered_edit_options,
    comfy_node_title,
    extract_history_filenames,
    i2v_dimensions,
    list_registered_workflows,
    load_workflow_registry,
    list_lora_catalog,
    patch_prompt_and_workflow,
)
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
        "4": {
            "class_type": "KSampler",
            "inputs": {"seed": ["3", 0], "steps": 4},
            "_meta": {"title": "Main sampler"},
        },
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


def qwen_prompt():
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": "main.png"}},
        "2": {"class_type": "QwenEditAdaptiveLongestEdge", "inputs": {"image": ["1", 0], "max_size": 1536}},
        "3": {
            "class_type": "QwenEditConfigPreparer",
            "inputs": {
                "image": ["1", 0], "mask": ["1", 1], "ref_longest_edge": ["2", 0],
                "to_ref": True, "ref_main_image": True, "ref_crop": "center", "ref_upscale": "lanczos",
                "to_vl": True, "vl_resize": True, "vl_target_size": 384, "vl_crop": "center", "vl_upscale": "bicubic",
            },
        },
        "4": {"class_type": "LoadImage", "inputs": {"image": "second.png"}},
        "5": {"class_type": "QwenEditAdaptiveLongestEdge", "inputs": {"image": ["4", 0], "max_size": 1536}},
        "7": {"class_type": "LoadImage", "inputs": {"image": "not-a-reference.png"}},
        "8": {"class_type": "PreviewImage", "inputs": {"images": ["7", 0]}},
        "9": {"class_type": "TextEncodeQwenImageEditPlusCustom_lrzjason", "inputs": {"configs": ["3", 0]}},
    }


def qwen_workflow():
    def load_node(node_id, name, mode=0):
        return {
            "id": node_id, "type": "LoadImage", "mode": mode,
            "inputs": [{"name": "image", "widget": {"name": "image"}}, {"name": "upload", "widget": {"name": "upload"}}],
            "outputs": [{"name": "IMAGE", "links": []}, {"name": "MASK", "links": []}],
            "widgets_values": [name, "image"],
        }

    def adaptive_node(node_id):
        return {
            "id": node_id, "type": "QwenEditAdaptiveLongestEdge", "mode": 0,
            "inputs": [{"name": "image", "type": "IMAGE", "link": None}, {"name": "max_size", "widget": {"name": "max_size"}}],
            "outputs": [{"name": "longest_edge", "links": []}], "widgets_values": [1536],
        }

    def config_node(node_id, main, mode):
        return {
            "id": node_id, "type": "QwenEditConfigPreparer", "mode": mode,
            "inputs": [
                {"name": "image", "type": "IMAGE", "link": None}, {"name": "configs", "type": "LIST", "link": None},
                {"name": "mask", "type": "MASK", "link": None},
                {"name": "to_ref", "widget": {"name": "to_ref"}},
                {"name": "ref_main_image", "widget": {"name": "ref_main_image"}},
                {"name": "ref_longest_edge", "widget": {"name": "ref_longest_edge"}, "link": None},
                {"name": "ref_crop", "widget": {"name": "ref_crop"}}, {"name": "ref_upscale", "widget": {"name": "ref_upscale"}},
                {"name": "to_vl", "widget": {"name": "to_vl"}}, {"name": "vl_resize", "widget": {"name": "vl_resize"}},
                {"name": "vl_target_size", "widget": {"name": "vl_target_size"}}, {"name": "vl_crop", "widget": {"name": "vl_crop"}},
                {"name": "vl_upscale", "widget": {"name": "vl_upscale"}},
            ],
            "outputs": [{"name": "configs", "links": []}],
            "widgets_values": [True, main, 1536, "center", "lanczos", True, True, 384, "center", "bicubic"],
        }

    nodes = [load_node(1, "main.png"), adaptive_node(2), config_node(3, True, 0), load_node(4, "second.png"), adaptive_node(5), config_node(6, False, 4), load_node(7, "not-a-reference.png"), {"id": 8, "type": "PreviewImage", "mode": 0, "inputs": [{"name": "images", "link": None}], "outputs": []}, {"id": 9, "type": "TextEncodeQwenImageEditPlusCustom_lrzjason", "mode": 0, "inputs": [{"name": "configs", "type": "LIST", "link": None}], "outputs": []}]
    links = [
        [1, 1, 0, 2, 0, "IMAGE"], [2, 1, 0, 3, 0, "IMAGE"], [3, 1, 1, 3, 2, "MASK"], [4, 2, 0, 3, 5, "INT"],
        [5, 4, 0, 5, 0, "IMAGE"], [6, 4, 0, 6, 0, "IMAGE"], [7, 4, 1, 6, 2, "MASK"], [8, 5, 0, 6, 5, "INT"],
        [9, 3, 0, 6, 1, "LIST"], [10, 6, 0, 9, 0, "LIST"], [11, 7, 0, 8, 0, "IMAGE"],
    ]
    return {"last_node_id": 9, "last_link_id": 11, "nodes": nodes, "links": links, "groups": [], "version": 0.4}


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
    input_root = None
    available = True
    queued_prompt = None
    queued_workflow = None
    uploaded_paths = []

    def is_available(self):
        return self.available

    def upload_image(self, image_path):
        self.uploaded_paths.append(Path(image_path).name)
        if self.input_root is not None:
            destination = Path(self.input_root) / "uploaded" / Path(image_path).name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(Path(image_path).read_bytes())
        return f"uploaded/{Path(image_path).name}"

    def get_input_image(self, image_name):
        if image_name != "uploaded/reference.png":
            raise ValueError("invalid image")
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), (1, 2, 3)).save(buffer, format="PNG")
        return buffer.getvalue(), "image/png"

    def run_prompt(
        self,
        prompt,
        workflow,
        client_id,
        progress_callback=None,
        cancel_callback=None,
        queued_callback=None,
    ):
        type(self).queued_prompt = prompt
        type(self).queued_workflow = workflow
        if progress_callback:
            progress_callback({"state": "queued", "prompt_id": "prompt-1"})
        if queued_callback:
            queued_callback("prompt-1")
        if progress_callback:
            progress_callback({"state": "running", "node": "4", "value": 1, "max": 6})
        generated_path = self.output_root / "generated.png"
        create_png(generated_path, color=(80, 90, 100))
        if progress_callback:
            progress_callback({"preview": generated_path.read_bytes()})
        return "prompt-1", {"outputs": {"6": {"images": [{"filename": "generated.png", "subfolder": ""}]}}}


class CancellableFakeComfyClient(FakeComfyClient):
    started = threading.Event()

    def run_prompt(
        self,
        prompt,
        workflow,
        client_id,
        progress_callback=None,
        cancel_callback=None,
        queued_callback=None,
    ):
        prompt_id = f"prompt-{client_id}"
        if progress_callback:
            progress_callback({"state": "queued", "prompt_id": prompt_id})
        if queued_callback:
            queued_callback(prompt_id)
        if progress_callback:
            progress_callback({"state": "running", "node": "4", "value": 1, "max": 6})
        type(self).started.set()
        deadline = time.time() + 3
        while time.time() < deadline:
            if cancel_callback and cancel_callback():
                raise ComfyGenerationCancelled("Generation annulee")
            time.sleep(0.01)
        raise AssertionError("The cancellable job was not cancelled")


class PreparingCancelComfyClient(FakeComfyClient):
    upload_started = threading.Event()
    release_upload = threading.Event()
    run_called = False

    def upload_image(self, image_path):
        type(self).upload_started.set()
        type(self).release_upload.wait(3)
        return super().upload_image(image_path)

    def run_prompt(self, *args, **kwargs):
        type(self).run_called = True
        return super().run_prompt(*args, **kwargs)


class FakeUrlResponse:
    def __init__(self, payload=b""):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class LegacyCancelClient(ComfyClient):
    def __init__(self, queue):
        super().__init__(base_url="http://example.test")
        self.queue = queue
        self.posts = []

    def get_json(self, path, timeout=None):
        self.assert_path = path
        return self.queue

    def post_json(self, path, payload, timeout=None):
        self.posts.append((path, payload))
        return {}


class ComfyGenerationTests(unittest.TestCase):
    def setUp(self):
        with app_module.COMFY_JOB_LOCK:
            app_module.COMFY_JOBS.clear()
        with app_module.COMFY_SUBMIT_CONDITION:
            app_module.COMFY_SUBMIT_QUEUE.clear()
            app_module.COMFY_SUBMIT_CONDITION.notify_all()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.images_root = self.root / "static" / "images"
        self.thumbnails = self.root / "static" / "thumbnails"
        self.db_path = self.root / "instance" / "gallery.sqlite3"
        (self.images_root / "output").mkdir(parents=True)
        (self.images_root / "input").mkdir(parents=True)
        FakeComfyClient.input_root = None
        init_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def scan_source_photo(self, *, prompt=None, workflow=None, scan_metadata=False):
        create_png(self.images_root / "output" / "source.png", prompt=prompt, workflow=workflow)
        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        client = app_module.app.test_client()
        response = client.post("/api/scan", json={"metadata": scan_metadata, "sync": True})
        self.assertEqual(response.status_code, 200)
        with connect_db(self.db_path) as conn:
            photo_id = conn.execute(
                "SELECT p.id FROM photos p JOIN album_photos ap ON ap.photo_id=p.id WHERE ap.filename='source.png'"
            ).fetchone()["id"]
        return client, photo_id

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

    def test_registered_workflows_and_i2v_options_use_source_prompt_suffix(self):
        workflows = list_registered_workflows(app_module.COMFY_WORKFLOW_ROOT)
        self.assertEqual(workflows[0]["id"], "current")
        self.assertIn("I2V_Wan2.2", [item["id"] for item in workflows])

        options = build_registered_edit_options(
            {"metadata": {"prompt": "portrait waiting"}},
            [],
            "I2V_Wan2.2",
            app_module.COMFY_WORKFLOW_ROOT,
        )
        self.assertEqual(options["mode"], "i2v")
        self.assertEqual(options["output_kind"], "video")
        self.assertTrue(options["prompt"].startswith("portrait waiting"))
        self.assertEqual(options["prompt"].count(I2V_IDLE_PROMPT_SUFFIX), 1)
        self.assertFalse(options["capabilities"]["references"])

    def test_i2v_registered_patch_injects_source_dimensions_prompt_and_filename(self):
        detail = {"width": 1000, "height": 1501, "metadata": {"prompt": "portrait"}}
        prompt, workflow, info = patch_prompt_and_workflow(
            detail,
            {"workflow_id": "I2V_Wan2.2", "prompt": f"portrait, {I2V_IDLE_PROMPT_SUFFIX}", "seed_mode": "keep"},
            source_image_name="uploaded/portrait.png",
            source_filename="portrait.png",
            workflow_root=app_module.COMFY_WORKFLOW_ROOT,
        )
        self.assertIsNone(workflow)
        self.assertEqual(prompt["97"]["inputs"]["image"], "uploaded/portrait.png")
        self.assertEqual(prompt["116:93"]["inputs"]["text"].count(I2V_IDLE_PROMPT_SUFFIX), 1)
        self.assertEqual((prompt["116:120"]["inputs"]["width"], prompt["116:120"]["inputs"]["height"]), (480, 720))
        self.assertEqual((prompt["116:117"]["inputs"]["width"], prompt["116:117"]["inputs"]["height"]), (480, 720))
        self.assertEqual(prompt["108"]["inputs"]["filename_prefix"], "video/portrait_i2v")
        self.assertEqual(info["output_node"], "108")
        self.assertEqual(info["output_kind"], "video")
        self.assertEqual(i2v_dimensions(1501, 1000, {"target_min_dimension": 480, "round_larger_dimension_to": 16}), (720, 480))
        self.assertEqual(i2v_dimensions(512, 512, {"target_min_dimension": 480}), (480, 480))

    def test_extract_history_filenames_can_target_video_output_node(self):
        history = {
            "outputs": {
                "116:116": {"images": [{"filename": "preview.png", "subfolder": ""}]},
                "108": {"videos": [{"filename": "portrait_i2v_00001.mp4", "subfolder": "video"}]},
            }
        }
        self.assertEqual(
            extract_history_filenames(history, output_node="108", output_kind="video"),
            ["video/portrait_i2v_00001.mp4"],
        )

    def test_workflow_registry_rejects_unsafe_template_path(self):
        registry_root = self.root / "unsafe-workflows"
        registry_root.mkdir()
        (registry_root / "workflow_registry.json").write_text(
            json.dumps({"workflows": {"unsafe": {"id": "unsafe", "filename": "../outside.json", "mode": "t2i", "prompt": "1", "output": "2"}}}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Fichier de workflow invalide"):
            load_workflow_registry(registry_root)

    def test_queue_prompt_sends_workflow_as_extra_pnginfo(self):
        client = CapturingComfyClient()
        workflow = sample_workflow()

        queued = client.queue_prompt(sample_prompt(), client_id="job-1", workflow=workflow)

        self.assertEqual(queued["prompt_id"], "prompt-1")
        self.assertEqual(client.payload["client_id"], "job-1")
        self.assertEqual(client.payload["extra_data"]["workflow"], workflow)
        self.assertEqual(client.payload["extra_data"]["extra_pnginfo"]["workflow"], workflow)

    def test_queue_status_counts_running_and_pending_prompts(self):
        client = LegacyCancelClient(
            {
                "queue_running": [[0, "running-1"], [1, "running-2"]],
                "queue_pending": [[2, "pending-1"]],
            }
        )

        self.assertEqual(
            client.queue_status(),
            {"running_count": 2, "pending_count": 1, "total_count": 3},
        )
        with patch("app.get_comfy_client", return_value=client):
            response = app_module.app.test_client().get("/api/comfy/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["queue"]["total_count"], 3)

    def test_comfy_node_title_prefers_custom_workflow_title_and_falls_back_to_prompt_metadata(self):
        prompt = sample_prompt()
        workflow = sample_workflow()
        workflow["nodes"][3]["title"] = "Sampler personnalise"

        self.assertEqual(comfy_node_title(prompt, workflow, "4"), "Sampler personnalise")
        del workflow["nodes"][3]["title"]
        self.assertEqual(comfy_node_title(prompt, workflow, "4"), "Main sampler")
        self.assertEqual(comfy_node_title(prompt, workflow, "999"), "node 999")

    def test_cancel_prompt_uses_targeted_endpoint(self):
        client = ComfyClient(base_url="http://example.test")
        response = FakeUrlResponse(json.dumps({"cancelled": True}).encode("utf-8"))

        with patch("comfy_generation.request.urlopen", return_value=response) as urlopen:
            self.assertTrue(client.cancel_prompt("prompt-1"))

        request_arg = urlopen.call_args.args[0]
        self.assertEqual(request_arg.full_url, "http://example.test/api/jobs/prompt-1/cancel")
        self.assertEqual(request_arg.get_method(), "POST")

    def test_cancel_prompt_falls_back_to_legacy_queue_routes(self):
        unsupported = error.HTTPError("http://example.test/api/jobs/prompt/cancel", 404, "missing", {}, None)
        cases = [
            (
                {"queue_running": [], "queue_pending": [[1, "pending-prompt"]]},
                "pending-prompt",
                [("/queue", {"delete": ["pending-prompt"]})],
                True,
            ),
            (
                {"queue_running": [[1, "running-prompt"]], "queue_pending": []},
                "running-prompt",
                [("/interrupt", {})],
                True,
            ),
            ({"queue_running": [], "queue_pending": []}, "missing-prompt", [], False),
        ]
        for queue, prompt_id, expected_posts, expected_result in cases:
            with self.subTest(prompt_id=prompt_id):
                client = LegacyCancelClient(queue)
                with patch("comfy_generation.request.urlopen", side_effect=unsupported):
                    self.assertEqual(client.cancel_prompt(prompt_id), expected_result)
                self.assertEqual(client.posts, expected_posts)

    def test_execution_interrupted_event_finishes_as_cancelled(self):
        class InterruptedWebSocket:
            def recv(self):
                return json.dumps({"type": "execution_interrupted", "data": {"prompt_id": "prompt-1"}})

        client = ComfyClient(base_url="http://example.test")
        with self.assertRaises(ComfyGenerationCancelled):
            client.listen_for_completion(InterruptedWebSocket(), "prompt-1", timeout=1)

    def test_qwen_reference_scan_reorder_bypass_and_add_subgraph(self):
        prompt = qwen_prompt()
        workflow = qwen_workflow()
        detail = {
            "metadata": {
                "raw_prompt_json": json.dumps(prompt),
                "raw_workflow_json": json.dumps(workflow),
            }
        }

        options = build_edit_options(detail, [])

        self.assertEqual([item["reference_id"] for item in options["references"]], ["3", "6"])
        self.assertEqual([item["enabled"] for item in options["references"]], [True, False])
        self.assertNotIn("not-a-reference.png", [item["image_name"] for item in options["references"]])

        patched, patched_workflow, _ = patch_prompt_and_workflow(
            detail,
            {
                "seed_mode": "keep",
                "references": [
                    {"reference_id": "6", "enabled": True, "input_name": "second.png"},
                    {"reference_id": "3", "enabled": False, "input_name": "main.png"},
                    {"reference_id": None, "enabled": True, "input_name": "third.png"},
                ],
            },
        )

        self.assertNotIn("3", patched)
        self.assertTrue(patched["6"]["inputs"]["ref_main_image"])
        new_qwen_id = next(node_id for node_id, node in patched.items() if ":" in node_id and node.get("class_type") == "QwenEditConfigPreparer")
        self.assertEqual(patched[new_qwen_id]["inputs"]["configs"], ["6", 0])
        self.assertEqual(patched["9"]["inputs"]["configs"], [new_qwen_id, 0])
        self.assertEqual(len(patched_workflow["definitions"]["subgraphs"]), 1)
        definition = patched_workflow["definitions"]["subgraphs"][0]
        self.assertEqual([node["type"] for node in definition["nodes"]], ["LoadImage", "QwenEditAdaptiveLongestEdge", "QwenEditConfigPreparer"])
        self.assertEqual(definition["nodes"][2]["mode"], 0)
        self.assertEqual(next(node for node in patched_workflow["nodes"] if node["id"] == 3)["mode"], 4)
        rescanned = build_edit_options(
            {"metadata": {"raw_prompt_json": patched, "raw_workflow_json": patched_workflow}},
            [],
        )
        self.assertEqual([item["reference_id"] for item in rescanned["references"]], ["6", "3", new_qwen_id])
        self.assertTrue(rescanned["references"][-1]["in_subgraph"])

    def test_new_lora_is_inserted_after_model_loader(self):
        prompt = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "model.safetensors"}},
            "2": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "seed": 1, "steps": 4}},
        }
        workflow = {
            "last_node_id": 2,
            "last_link_id": 1,
            "nodes": [
                {"id": 1, "type": "UNETLoader", "mode": 0, "inputs": [], "outputs": [{"name": "MODEL", "links": [1]}], "pos": [0, 0]},
                {"id": 2, "type": "KSampler", "mode": 0, "inputs": [{"name": "model", "link": 1}], "outputs": [], "pos": [600, 0]},
            ],
            "links": [[1, 1, 0, 2, 0, "MODEL"]],
        }
        detail = {"metadata": {"raw_prompt_json": json.dumps(prompt), "raw_workflow_json": json.dumps(workflow)}}

        patched, patched_workflow, _ = patch_prompt_and_workflow(
            detail,
            {"seed_mode": "keep", "loras": [{"new": True, "enabled": True, "lora_name": "style.safetensors", "strength_model": 1.0}]},
        )

        self.assertEqual(patched["3"]["inputs"]["model"], ["1", 0])
        self.assertEqual(patched["2"]["inputs"]["model"], ["3", 0])
        self.assertEqual(next(node for node in patched_workflow["nodes"] if node["id"] == 3)["widgets_values"], ["style.safetensors", 1.0])
        self.assertEqual([(link[1], link[3]) for link in patched_workflow["links"]], [(1, 3), (3, 2)])

    def test_new_lora_follows_bypassed_visual_lora(self):
        prompt = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "model.safetensors"}},
            "3": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "seed": 1, "steps": 4}},
        }
        workflow = {
            "last_node_id": 3, "last_link_id": 2,
            "nodes": [
                {"id": 1, "type": "UNETLoader", "mode": 0, "inputs": [], "outputs": [{"name": "MODEL", "links": [1]}]},
                {"id": 2, "type": "LoraLoaderModelOnly", "mode": 4, "inputs": [{"name": "model", "link": 1}], "outputs": [{"name": "MODEL", "links": [2]}], "widgets_values": ["old.safetensors", 1]},
                {"id": 3, "type": "KSampler", "mode": 0, "inputs": [{"name": "model", "link": 2}], "outputs": []},
            ],
            "links": [[1, 1, 0, 2, 0, "MODEL"], [2, 2, 0, 3, 0, "MODEL"]],
        }
        detail = {"metadata": {"raw_prompt_json": prompt, "raw_workflow_json": workflow}}
        patched, patched_workflow, _ = patch_prompt_and_workflow(
            detail,
            {"seed_mode": "keep", "loras": [{"new": True, "enabled": True, "lora_name": "new.safetensors", "strength_model": 1}]},
        )
        self.assertEqual(patched["4"]["inputs"]["model"], ["1", 0])
        self.assertEqual(patched["3"]["inputs"]["model"], ["4", 0])
        self.assertIn((2, 4), [(link[1], link[3]) for link in patched_workflow["links"]])
        self.assertIn((4, 3), [(link[1], link[3]) for link in patched_workflow["links"]])

    def test_generate_rejects_all_disabled_references(self):
        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        create_png(self.images_root / "output" / "source.png", prompt=json.dumps(qwen_prompt()), workflow=json.dumps(qwen_workflow()))
        previous_factory = app_module.COMFY_CLIENT_FACTORY
        app_module.COMFY_CLIENT_FACTORY = FakeComfyClient
        try:
            client = app_module.app.test_client()
            client.post("/api/scan", json={"metadata": True, "sync": True})
            with connect_db(self.db_path) as conn:
                source_id = conn.execute("SELECT id FROM photos LIMIT 1").fetchone()["id"]
            response = client.post(
                f"/api/photos/{source_id}/comfy/generate",
                json={"references": [{"reference_id": "3", "enabled": False, "input_name": "main.png"}]},
            )
            self.assertEqual(response.status_code, 400)
        finally:
            app_module.COMFY_CLIENT_FACTORY = previous_factory

    def test_reference_upload_and_input_preview(self):
        previous_factory = app_module.COMFY_CLIENT_FACTORY
        app_module.COMFY_CLIENT_FACTORY = FakeComfyClient
        FakeComfyClient.uploaded_paths = []
        FakeComfyClient.input_root = self.images_root / "input"
        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        try:
            client = app_module.app.test_client()
            image_data = tempfile.SpooledTemporaryFile()
            Image.new("RGB", (8, 8), (10, 20, 30)).save(image_data, format="PNG")
            image_data.seek(0)
            queued_job = {"job_id": "queued-reference", "state": "queued", "origin": "comfy_reference"}
            scan_status = {"job_id": "active-scan", "active": True, "queued_count": 1}
            with patch("app.enqueue_scan_job", return_value=(queued_job, scan_status)) as enqueue:
                uploaded = client.post(
                    "/api/comfy/references/upload",
                    data={"file": (image_data, "reference.png")},
                    content_type="multipart/form-data",
                )
            self.assertEqual(uploaded.status_code, 201)
            self.assertEqual(uploaded.get_json()["input_name"], "uploaded/reference.png")
            self.assertEqual(FakeComfyClient.uploaded_paths, ["reference.png"])
            self.assertEqual(uploaded.get_json()["scan_job"], queued_job)
            self.assertEqual(uploaded.get_json()["scan_status"], scan_status)
            photo = uploaded.get_json()["photo"]
            self.assertEqual(photo["memberships"][0]["album_name"], "input")
            self.assertEqual(photo["memberships"][0]["relative_path"], "uploaded/reference.png")
            self.assertIsNone(photo["metadata"])
            self.assertTrue((self.thumbnails / f"{photo['checksum']}.jpg").is_file())
            scan_options = enqueue.call_args.args[0]
            self.assertEqual(scan_options["photo_ids"], [photo["id"]])
            self.assertEqual(scan_options["scan_mode"], "missing")
            self.assertTrue(scan_options["metadata"])
            self.assertTrue(scan_options["image_analysis"])
            self.assertTrue(scan_options["skip_automatic_face_scan"])
            self.assertEqual(enqueue.call_args.kwargs, {"origin": "comfy_reference", "queue_if_busy": True})

            preview = client.get("/api/comfy/input-preview?filename=uploaded%2Freference.png")
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.data[:8], b"\x89PNG\r\n\x1a\n")

            invalid = client.post(
                "/api/comfy/references/upload",
                data={"file": (io.BytesIO(b"not an image"), "reference.txt")},
                content_type="multipart/form-data",
            )
            self.assertEqual(invalid.status_code, 400)
        finally:
            FakeComfyClient.input_root = None
            app_module.COMFY_CLIENT_FACTORY = previous_factory

    def test_automatic_reference_scan_runs_json_before_ai_without_faces(self):
        options = {
            "scope": "selection",
            "album": None,
            "photo_ids": [42],
            "scan_mode": "missing",
            "rescan_existing": False,
            "metadata": True,
            "image_analysis": True,
            "face_recognition": False,
            "force_face_rescan": False,
            "skip_automatic_face_scan": True,
        }
        stages = []
        with patch("app.run_metadata_stage", side_effect=lambda *_args, **_kwargs: stages.append("json") or {"processed": 1}), \
             patch("app.run_image_analysis_stage", side_effect=lambda *_args, **_kwargs: stages.append("ia") or {"processed": 1}), \
             patch("app.enqueue_automatic_face_scan") as automatic_faces:
            summary = app_module.run_scan_pipeline("reference", options, sync=True)

        self.assertEqual(stages, ["json", "ia"])
        self.assertEqual(summary["targeted"], 1)
        automatic_faces.assert_not_called()

    def test_reference_upload_reports_missing_input_file_without_queueing_scan(self):
        previous_factory = app_module.COMFY_CLIENT_FACTORY
        app_module.COMFY_CLIENT_FACTORY = FakeComfyClient
        FakeComfyClient.input_root = None
        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        try:
            client = app_module.app.test_client()
            image_data = io.BytesIO()
            Image.new("RGB", (8, 8), (10, 20, 30)).save(image_data, format="PNG")
            image_data.seek(0)
            with patch("app.enqueue_scan_job") as enqueue:
                response = client.post(
                    "/api/comfy/references/upload",
                    data={"file": (image_data, "missing.png")},
                    content_type="multipart/form-data",
                )
            self.assertEqual(response.status_code, 500)
            self.assertIn("Image ComfyUI input introuvable", response.get_json()["error"])
            enqueue.assert_not_called()
        finally:
            app_module.COMFY_CLIENT_FACTORY = previous_factory

    def test_input_import_keeps_album_alias_path_for_relative_membership(self):
        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        app_module.ensure_ready()
        (self.images_root / "nested").mkdir()
        input_path = self.images_root / "input" / "reference.png"
        create_png(input_path)
        aliased_input_root = self.images_root / "nested" / ".." / "input"

        with connect_db(self.db_path) as conn:
            conn.execute("UPDATE albums SET path=? WHERE name='input'", (str(aliased_input_root),))
        with connect_db(self.db_path) as conn:
            album, gallery_path = app_module.validated_comfy_input_path(conn, "reference.png")
            self.assertEqual(gallery_path, aliased_input_root / "reference.png")
            photo_id = app_module.import_photo_into_album(
                conn,
                gallery_path.resolve(),
                album,
                self.thumbnails,
                scan_metadata=False,
            )
            detail = app_module.get_photo_detail(conn, photo_id)

        self.assertEqual(detail["memberships"][0]["relative_path"], "reference.png")

    def test_automatic_reference_scans_queue_fifo_and_promote_after_completion(self):
        with app_module.SCAN_LOCK:
            original_status = dict(app_module.SCAN_STATUS)
            original_queue = list(app_module.SCAN_QUEUE)
            app_module.SCAN_QUEUE.clear()
            app_module.SCAN_STATUS.update(
                active=True,
                job_id="manual-current",
                state="running",
                origin="manual",
                cancel_requested=False,
            )
        options = {
            "scope": "selection",
            "album": None,
            "photo_ids": [1],
            "scan_mode": "missing",
            "rescan_existing": False,
            "metadata": True,
            "image_analysis": True,
            "face_recognition": False,
            "force_face_rescan": False,
            "skip_automatic_face_scan": True,
        }
        try:
            with patch("app.start_scan_job_thread") as start_thread:
                first, current = app_module.enqueue_scan_job(
                    options,
                    origin="comfy_reference",
                    queue_if_busy=True,
                )
                second, current = app_module.enqueue_scan_job(
                    options | {"photo_ids": [2]},
                    origin="comfy_reference",
                    queue_if_busy=True,
                )
                self.assertEqual(first["state"], "queued")
                self.assertEqual(second["queue_position"], 2)
                self.assertEqual(current["queued_count"], 2)

                promoted = app_module.finish_scan_job_and_promote(
                    "manual-current",
                    state="done",
                    stage="done",
                    message="done",
                    summary={},
                )
                self.assertEqual(promoted["job_id"], first["job_id"])
                self.assertEqual(promoted["queued_count"], 1)
                self.assertEqual(start_thread.call_args.args[0]["job_id"], first["job_id"])

                promoted = app_module.finish_scan_job_and_promote(
                    first["job_id"],
                    state="error",
                    stage="error",
                    message="failed",
                    summary=None,
                )
                self.assertEqual(promoted["job_id"], second["job_id"])
                self.assertEqual(promoted["queued_count"], 0)
                self.assertEqual(start_thread.call_args.args[0]["job_id"], second["job_id"])
        finally:
            with app_module.SCAN_LOCK:
                app_module.SCAN_QUEUE.clear()
                app_module.SCAN_QUEUE.extend(original_queue)
                app_module.SCAN_STATUS.clear()
                app_module.SCAN_STATUS.update(original_status)

    def test_edit_options_rescans_missing_prompt_metadata(self):
        client, source_id = self.scan_source_photo(
            prompt=json.dumps(sample_prompt()),
            workflow=json.dumps(sample_workflow()),
            scan_metadata=False,
        )

        with patch("app.rescan_metadata", wraps=app_module.rescan_metadata) as rescan:
            response = client.get(f"/api/photos/{source_id}/comfy/edit-options")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["options"]["prompt"], "old prompt")
        self.assertEqual(rescan.call_count, 1)
        with connect_db(self.db_path) as conn:
            raw_prompt = conn.execute(
                "SELECT raw_prompt_json FROM photo_metadata WHERE photo_id=?",
                (source_id,),
            ).fetchone()["raw_prompt_json"]
        self.assertEqual(json.loads(raw_prompt), sample_prompt())

    def test_edit_options_reports_missing_prompt_only_after_rescan(self):
        client, source_id = self.scan_source_photo(scan_metadata=False)

        with patch("app.rescan_metadata", wraps=app_module.rescan_metadata) as rescan:
            response = client.get(f"/api/photos/{source_id}/comfy/edit-options")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"],
            "Cette image ne contient pas de prompt ComfyUI exploitable",
        )
        self.assertEqual(rescan.call_count, 1)

    def test_registered_edit_options_work_without_embedded_comfy_prompt(self):
        client, source_id = self.scan_source_photo(scan_metadata=False)
        listed = client.get("/api/comfy/workflows")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()["workflows"][0]["id"], "current")

        response = client.get(
            f"/api/photos/{source_id}/comfy/edit-options?workflow_id=I2V_Wan2.2"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["options"]["prompt"], I2V_IDLE_PROMPT_SUFFIX)
        unknown = client.get(
            f"/api/photos/{source_id}/comfy/edit-options?workflow_id=unknown"
        )
        self.assertEqual(unknown.status_code, 400)

    def test_edit_options_rescans_invalid_prompt_metadata(self):
        client, source_id = self.scan_source_photo(
            prompt=json.dumps(sample_prompt()),
            workflow=json.dumps(sample_workflow()),
            scan_metadata=True,
        )
        with connect_db(self.db_path) as conn:
            conn.execute(
                "UPDATE photo_metadata SET raw_prompt_json='not-json' WHERE photo_id=?",
                (source_id,),
            )

        with patch("app.rescan_metadata", wraps=app_module.rescan_metadata) as rescan:
            response = client.get(f"/api/photos/{source_id}/comfy/edit-options")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["options"]["prompt"], "old prompt")
        self.assertEqual(rescan.call_count, 1)

    def test_edit_options_does_not_rescan_existing_prompt(self):
        client, source_id = self.scan_source_photo(
            prompt=json.dumps(sample_prompt()),
            workflow=json.dumps(sample_workflow()),
            scan_metadata=True,
        )

        with patch("app.rescan_metadata", wraps=app_module.rescan_metadata) as rescan:
            response = client.get(f"/api/photos/{source_id}/comfy/edit-options")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["options"]["prompt"], "old prompt")
        rescan.assert_not_called()

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

    def test_multiple_active_jobs_are_queued_and_cancelled_individually(self):
        source_prompt = json.dumps(sample_prompt())
        source_workflow = json.dumps(sample_workflow())
        create_png(self.images_root / "output" / "source.png", prompt=source_prompt, workflow=source_workflow)
        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        previous_factory = app_module.COMFY_CLIENT_FACTORY
        CancellableFakeComfyClient.output_root = self.images_root / "output"
        CancellableFakeComfyClient.started.clear()
        app_module.COMFY_CLIENT_FACTORY = CancellableFakeComfyClient
        try:
            client = app_module.app.test_client()
            client.post("/api/scan", json={"metadata": True, "sync": True})
            with connect_db(self.db_path) as conn:
                source_id = conn.execute("SELECT id FROM photos LIMIT 1").fetchone()["id"]

            self.assertIsNone(client.get("/api/comfy/jobs/current").get_json()["job"])
            generated = client.post(
                f"/api/photos/{source_id}/comfy/generate",
                json={"prompt": "cancel me", "seed_mode": "keep", "steps": 6},
            )
            self.assertEqual(generated.status_code, 202)
            job_id = generated.get_json()["job"]["id"]
            self.assertTrue(CancellableFakeComfyClient.started.wait(2))

            current = client.get("/api/comfy/jobs/current")
            self.assertEqual(current.status_code, 200)
            self.assertEqual(current.get_json()["job"]["id"], job_id)
            self.assertEqual(current.get_json()["job"]["node"], "4")
            self.assertEqual(current.get_json()["job"]["node_title"], "Main sampler")
            second = client.post(
                f"/api/photos/{source_id}/comfy/generate",
                json={"prompt": "second"},
            )
            self.assertEqual(second.status_code, 202)
            second_job_id = second.get_json()["job"]["id"]
            for _ in range(100):
                active_jobs = client.get("/api/comfy/jobs/current").get_json()["jobs"]
                if len(active_jobs) == 2 and all(job.get("prompt_id") for job in active_jobs):
                    break
                time.sleep(0.02)
            self.assertEqual([job["id"] for job in active_jobs], [job_id, second_job_id])

            cancelled = client.post(f"/api/comfy/jobs/{job_id}/cancel", json={})
            self.assertEqual(cancelled.status_code, 202)
            self.assertEqual(cancelled.get_json()["job"]["state"], "cancel_requested")
            final_job = None
            for _ in range(100):
                final_job = client.get(f"/api/comfy/jobs/{job_id}").get_json()["job"]
                if not final_job["active"]:
                    break
                time.sleep(0.02)
            self.assertEqual(final_job["state"], "cancelled")
            self.assertIsNone(final_job["photo"])
            self.assertEqual(client.post(f"/api/comfy/jobs/{job_id}/cancel", json={}).status_code, 200)
            self.assertEqual(client.post("/api/comfy/jobs/missing/cancel", json={}).status_code, 404)
            remaining = client.get("/api/comfy/jobs/current").get_json()
            self.assertEqual(remaining["job"]["id"], second_job_id)
            self.assertEqual([job["id"] for job in remaining["jobs"]], [second_job_id])
            self.assertEqual(client.post(f"/api/comfy/jobs/{second_job_id}/cancel", json={}).status_code, 202)
            for _ in range(100):
                if client.get(f"/api/comfy/jobs/{second_job_id}").get_json()["job"]["active"] is False:
                    break
                time.sleep(0.02)
            self.assertIsNone(client.get("/api/comfy/jobs/current").get_json()["job"])
        finally:
            app_module.COMFY_CLIENT_FACTORY = previous_factory

    def test_cancel_during_reference_upload_never_queues_prompt(self):
        create_png(
            self.images_root / "output" / "source.png",
            prompt=json.dumps(sample_prompt()),
            workflow=json.dumps(sample_workflow()),
        )
        create_png(self.images_root / "output" / "ref.png", color=(10, 90, 10))
        app_module.DB_PATH = self.db_path
        app_module.IMAGES_ROOT = self.images_root
        app_module.THUMBNAIL_ROOT = self.thumbnails
        previous_factory = app_module.COMFY_CLIENT_FACTORY
        PreparingCancelComfyClient.output_root = self.images_root / "output"
        PreparingCancelComfyClient.upload_started.clear()
        PreparingCancelComfyClient.release_upload.clear()
        PreparingCancelComfyClient.run_called = False
        app_module.COMFY_CLIENT_FACTORY = PreparingCancelComfyClient
        try:
            client = app_module.app.test_client()
            client.post("/api/scan", json={"metadata": True, "sync": True})
            with connect_db(self.db_path) as conn:
                source_id = conn.execute(
                    "SELECT p.id FROM photos p JOIN album_photos ap ON ap.photo_id=p.id WHERE ap.filename='source.png'"
                ).fetchone()["id"]
                ref_id = conn.execute(
                    "SELECT p.id FROM photos p JOIN album_photos ap ON ap.photo_id=p.id WHERE ap.filename='ref.png'"
                ).fetchone()["id"]
            generated = client.post(
                f"/api/photos/{source_id}/comfy/generate",
                json={"references": [{"node_id": "5", "photo_id": ref_id}]},
            )
            job_id = generated.get_json()["job"]["id"]
            self.assertTrue(PreparingCancelComfyClient.upload_started.wait(2))
            self.assertEqual(client.post(f"/api/comfy/jobs/{job_id}/cancel", json={}).status_code, 202)
            PreparingCancelComfyClient.release_upload.set()
            final_job = None
            for _ in range(100):
                final_job = client.get(f"/api/comfy/jobs/{job_id}").get_json()["job"]
                if not final_job["active"]:
                    break
                time.sleep(0.02)
            self.assertEqual(final_job["state"], "cancelled")
            self.assertFalse(PreparingCancelComfyClient.run_called)
            self.assertIsNone(final_job["prompt_id"])
        finally:
            PreparingCancelComfyClient.release_upload.set()
            app_module.COMFY_CLIENT_FACTORY = previous_factory


if __name__ == "__main__":
    unittest.main()
