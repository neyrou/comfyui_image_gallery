import copy
import json
import mimetypes
import random
import re
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse
from urllib import error, request

import websocket

from metadata_extractor import _is_blacklisted_lora
from comfy_graph import (
    QWEN_CONFIG_TYPE,
    WorkflowIndex,
    discover_qwen_references,
    input_slot,
    parse_link,
    workflow_widget_value,
)


COMFY_BASE_URL = "http://127.0.0.1:8188"
MAX_SEED = 1125899906842624
CURRENT_WORKFLOW_ID = "current"
WORKFLOW_ROOT = Path(__file__).resolve().parent / "comfyui-workflows"
I2V_IDLE_PROMPT_SUFFIX = (
    "subtle idle waiting motion only, minimal movement, no abrupt motion, no energetic movement, "
    "preserve the original pose, preserve the original framing, preserve visual coherence with the source image"
)
REGISTERED_WORKFLOW_MODES = {"t2i", "i2i", "i2v"}


class ComfyUnavailable(RuntimeError):
    pass


class ComfyGenerationError(RuntimeError):
    pass


class ComfyGenerationCancelled(ComfyGenerationError):
    pass


class ComfyPromptUnavailable(ValueError):
    pass


def load_workflow_registry(workflow_root=WORKFLOW_ROOT):
    workflow_root = Path(workflow_root).resolve()
    registry_path = workflow_root / "workflow_registry.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Registre ComfyUI invalide: {exc}") from exc

    workflows = registry.get("workflows") if isinstance(registry, dict) else None
    if not isinstance(workflows, dict):
        raise ValueError("Le registre ComfyUI doit contenir un objet workflows")

    validated = {}
    for registry_id, raw_entry in workflows.items():
        if not isinstance(raw_entry, dict):
            raise ValueError(f"Workflow invalide: {registry_id}")
        entry = copy.deepcopy(raw_entry)
        workflow_id = str(entry.get("id") or registry_id).strip()
        if workflow_id != str(registry_id) or not re.fullmatch(r"[A-Za-z0-9_.-]+", workflow_id):
            raise ValueError(f"Identifiant de workflow invalide: {registry_id}")
        filename = str(entry.get("filename") or "").strip()
        if not filename or Path(filename).name != filename or Path(filename).suffix.lower() != ".json":
            raise ValueError(f"Fichier de workflow invalide: {workflow_id}")
        template_path = (workflow_root / filename).resolve()
        if template_path.parent != workflow_root:
            raise ValueError(f"Chemin de workflow non autorise: {workflow_id}")
        mode = str(entry.get("mode") or "").lower()
        if mode not in REGISTERED_WORKFLOW_MODES:
            raise ValueError(f"Mode de workflow invalide: {workflow_id}")
        try:
            template = json.loads(template_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Workflow {workflow_id} invalide: {exc}") from exc
        if not isinstance(template, dict):
            raise ValueError(f"Le workflow {workflow_id} doit etre un prompt API ComfyUI")

        entry["id"] = workflow_id
        entry["mode"] = mode
        entry["output_kind"] = str(entry.get("output_kind") or "image").lower()
        entry["prompt"] = _validate_registry_node(template, workflow_id, entry.get("prompt"), "prompt")
        entry["output"] = _validate_registry_node(template, workflow_id, entry.get("output"), "output")
        if entry["output_kind"] not in {"image", "video"}:
            raise ValueError(f"Type de sortie invalide: {workflow_id}")
        if mode in {"i2i", "i2v"}:
            entry["input_image"] = _validate_registry_node(
                template, workflow_id, entry.get("input_image"), "input_image"
            )
        elif entry.get("input_image") is not None:
            entry["input_image"] = _validate_registry_node(
                template, workflow_id, entry.get("input_image"), "input_image"
            )
        if entry.get("seed") is not None:
            entry["seed"] = _validate_registry_node(template, workflow_id, entry.get("seed"), "seed")
        entry["preview"] = [
            _validate_registry_node(template, workflow_id, node_id, "preview")
            for node_id in (entry.get("preview") or [])
        ]
        entry["dimension_nodes"] = [
            _validate_registry_node(template, workflow_id, node_id, "dimension_nodes")
            for node_id in (entry.get("dimension_nodes") or [])
        ]
        validated[workflow_id] = {"config": entry, "template": template}
    return validated


def _validate_registry_node(template, workflow_id, node_id, field):
    if node_id in (None, ""):
        raise ValueError(f"Node {field} manquant pour {workflow_id}")
    node_id = str(node_id)
    if node_id not in template or not isinstance(template[node_id], dict):
        raise ValueError(f"Node {field} {node_id} introuvable dans {workflow_id}")
    return node_id


def list_registered_workflows(workflow_root=WORKFLOW_ROOT):
    items = [
        {
            "id": CURRENT_WORKFLOW_ID,
            "label": "Workflow de l'image actuelle",
            "mode": "current",
            "output_kind": "image",
        }
    ]
    for item in load_workflow_registry(workflow_root).values():
        config = item["config"]
        items.append(
            {
                "id": config["id"],
                "label": config.get("label") or config["id"],
                "mode": config["mode"],
                "output_kind": config["output_kind"],
            }
        )
    return items


def get_registered_workflow(workflow_id, workflow_root=WORKFLOW_ROOT):
    workflow_id = str(workflow_id or CURRENT_WORKFLOW_ID)
    if workflow_id == CURRENT_WORKFLOW_ID:
        return None
    item = load_workflow_registry(workflow_root).get(workflow_id)
    if not item:
        raise ValueError(f"Workflow ComfyUI inconnu: {workflow_id}")
    return copy.deepcopy(item)


class ComfyClient:
    def __init__(self, base_url=COMFY_BASE_URL, timeout=2):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def is_available(self):
        try:
            self.get_json("/system_stats")
            return True
        except ComfyUnavailable:
            return False

    def queue_status(self):
        queue = self.get_json("/queue", timeout=max(self.timeout, 10))
        running = queue.get("queue_running", queue.get("Running", [])) if isinstance(queue, dict) else []
        pending = queue.get("queue_pending", queue.get("Pending", [])) if isinstance(queue, dict) else []
        running_count = len(running) if isinstance(running, list) else 0
        pending_count = len(pending) if isinstance(pending, list) else 0
        return {
            "running_count": running_count,
            "pending_count": pending_count,
            "total_count": running_count + pending_count,
        }

    def get_json(self, path, timeout=None):
        try:
            with request.urlopen(self.base_url + path, timeout=timeout or self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, error.URLError, json.JSONDecodeError) as exc:
            raise ComfyUnavailable(str(exc)) from exc

    def post_json(self, path, payload, timeout=None):
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.base_url + path,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout or self.timeout) as response:
                body = response.read()
                return json.loads(body.decode("utf-8")) if body else {}
        except (OSError, error.URLError, json.JSONDecodeError) as exc:
            raise ComfyUnavailable(str(exc)) from exc

    def cancel_prompt(self, prompt_id):
        prompt_id = str(prompt_id or "").strip()
        if not prompt_id:
            return False

        req = request.Request(
            f"{self.base_url}/api/jobs/{quote(prompt_id, safe='')}/cancel",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=max(self.timeout, 10)) as response:
                body = response.read()
                data = json.loads(body.decode("utf-8")) if body else {}
                return bool(data.get("cancelled", True))
        except error.HTTPError as exc:
            if exc.code not in {404, 405}:
                raise ComfyUnavailable(str(exc)) from exc
        except (OSError, error.URLError, json.JSONDecodeError) as exc:
            raise ComfyUnavailable(str(exc)) from exc

        queue = self.get_json("/queue", timeout=max(self.timeout, 10))
        running = self._queue_prompt_ids(queue.get("queue_running") or [])
        pending = self._queue_prompt_ids(queue.get("queue_pending") or [])
        if prompt_id in pending:
            self.post_json("/queue", {"delete": [prompt_id]}, timeout=max(self.timeout, 10))
            return True
        if prompt_id in running:
            self.post_json("/interrupt", {}, timeout=max(self.timeout, 10))
            return True
        return False

    @staticmethod
    def _queue_prompt_ids(items):
        prompt_ids = set()
        for item in items:
            if isinstance(item, dict):
                prompt_id = item.get("prompt_id") or item.get("id")
            elif isinstance(item, (list, tuple)) and len(item) > 1:
                prompt_id = item[1]
            else:
                prompt_id = None
            if prompt_id is not None:
                prompt_ids.add(str(prompt_id))
        return prompt_ids

    def upload_image(self, image_path, overwrite=False):
        boundary = f"----gallery-{uuid.uuid4().hex}"
        image_path = Path(image_path)
        content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        body = b"".join(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="image"; filename="{image_path.name}"\r\n'.encode("utf-8"),
                f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                image_path.read_bytes(),
                b"\r\n",
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="overwrite"\r\n\r\n{str(bool(overwrite)).lower()}\r\n'.encode("utf-8"),
                f"--{boundary}--\r\n".encode("utf-8"),
            ]
        )
        req = request.Request(
            self.base_url + "/upload/image",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=max(self.timeout, 10)) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, error.URLError, json.JSONDecodeError) as exc:
            raise ComfyUnavailable(str(exc)) from exc
        name = data.get("name") or image_path.name
        subfolder = data.get("subfolder")
        return f"{subfolder}/{name}" if subfolder else name

    def get_input_image(self, image_name):
        normalized = str(image_name or "").replace("\\", "/").strip("/")
        if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("Invalid ComfyUI input image name")
        parts = normalized.split("/")
        query = urlencode(
            {
                "filename": parts[-1],
                "subfolder": "/".join(parts[:-1]),
                "type": "input",
            }
        )
        try:
            with request.urlopen(self.base_url + "/view?" + query, timeout=max(self.timeout, 10)) as response:
                return response.read(), response.headers.get_content_type()
        except (OSError, error.URLError) as exc:
            raise ComfyUnavailable(str(exc)) from exc

    def queue_prompt(self, prompt, client_id=None, workflow=None):
        payload = {"prompt": prompt, "client_id": client_id or str(uuid.uuid4())}
        if workflow is not None:
            payload["extra_data"] = {
                "workflow": workflow,
                "extra_pnginfo": {"workflow": workflow},
            }
        return self.post_json("/prompt", payload, timeout=max(self.timeout, 10))

    def run_prompt(
        self,
        prompt,
        workflow,
        client_id,
        progress_callback=None,
        cancel_callback=None,
        queued_callback=None,
    ):
        self._raise_if_cancelled(cancel_callback)
        ws = None
        try:
            ws = websocket.create_connection(self.websocket_url(client_id), timeout=max(self.timeout, 10))
            ws.settimeout(1)
        except Exception:
            ws = None

        self._raise_if_cancelled(cancel_callback)
        queued = self.queue_prompt(prompt, client_id=client_id, workflow=workflow)
        prompt_id = queued.get("prompt_id")
        if not prompt_id:
            raise ComfyGenerationError("ComfyUI did not return a prompt_id")
        self._progress(progress_callback, state="queued", prompt_id=prompt_id)
        if queued_callback:
            queued_callback(prompt_id)
        self._raise_if_cancelled(cancel_callback, prompt_id)

        if ws is None:
            history = self.wait_for_history(prompt_id, cancel_callback=cancel_callback)
            self._progress(progress_callback, state="done", prompt_id=prompt_id)
            return prompt_id, history

        try:
            history = self.listen_for_completion(
                ws,
                prompt_id,
                progress_callback=progress_callback,
                cancel_callback=cancel_callback,
            )
            return prompt_id, history
        except websocket.WebSocketException:
            history = self.wait_for_history(prompt_id, cancel_callback=cancel_callback)
            self._progress(progress_callback, state="done", prompt_id=prompt_id)
            return prompt_id, history
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def websocket_url(self, client_id):
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}/ws?clientId={client_id}"

    def listen_for_completion(self, ws, prompt_id, progress_callback=None, timeout=3600, cancel_callback=None):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._raise_if_cancelled(cancel_callback, prompt_id)
            try:
                message = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if isinstance(message, bytes):
                preview = extract_preview_bytes(message)
                if preview:
                    self._progress(progress_callback, preview=preview)
                continue
            try:
                data = json.loads(message)
            except (TypeError, json.JSONDecodeError):
                continue
            event_type = data.get("type")
            payload = data.get("data") or {}
            if event_type == "execution_interrupted" and payload.get("prompt_id") == prompt_id:
                raise ComfyGenerationCancelled("Generation annulee")
            if event_type == "executing":
                node = payload.get("node")
                self._progress(progress_callback, state="running", prompt_id=prompt_id, node=node)
                if payload.get("prompt_id") == prompt_id and node is None:
                    history = self.wait_for_history(prompt_id, timeout=30, cancel_callback=cancel_callback)
                    self._progress(progress_callback, state="done", prompt_id=prompt_id, node=None)
                    return history
            elif event_type == "progress":
                self._progress(
                    progress_callback,
                    state="running",
                    prompt_id=prompt_id,
                    value=payload.get("value"),
                    max=payload.get("max"),
                )
            elif event_type in {"executed", "status"}:
                self._progress(progress_callback, state="running", prompt_id=prompt_id)
        raise ComfyGenerationError("Generation timed out")

    def _progress(self, progress_callback, **payload):
        if progress_callback:
            progress_callback(payload)

    def _raise_if_cancelled(self, cancel_callback, prompt_id=None):
        if cancel_callback and cancel_callback():
            if prompt_id:
                self.cancel_prompt(prompt_id)
            raise ComfyGenerationCancelled("Generation annulee")

    def wait_for_history(self, prompt_id, timeout=180, interval=1, cancel_callback=None):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._raise_if_cancelled(cancel_callback, prompt_id)
            history = self.get_json(f"/history/{prompt_id}", timeout=max(self.timeout, 10))
            if prompt_id in history:
                return history[prompt_id]
            time.sleep(interval)
        raise ComfyGenerationError("Generation timed out")


def loads_json(value):
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def comfy_node_title(prompt, workflow, node_id):
    if node_id is None:
        return None
    node_id = str(node_id)
    prompt_node = prompt.get(node_id, {}) if isinstance(prompt, dict) else {}
    workflow_node = normalize_workflow_nodes(workflow).get(node_id, {})
    candidates = (
        workflow_node.get("title"),
        (prompt_node.get("_meta") or {}).get("title"),
        (workflow_node.get("properties") or {}).get("Node name for S&R"),
        workflow_node.get("type"),
        prompt_node.get("class_type"),
    )
    for candidate in candidates:
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return f"node {node_id}"


def list_lora_catalog(conn):
    rows = conn.execute(
        """
        SELECT lora_name, COUNT(*) AS usage_count, MAX(strength_model) AS last_strength
        FROM photo_loras
        GROUP BY lora_name
        ORDER BY lora_name COLLATE NOCASE
        """
    ).fetchall()
    return [
        {
            "lora_name": row["lora_name"],
            "usage_count": row["usage_count"],
            "last_strength": row["last_strength"],
        }
        for row in rows
        if row["lora_name"] and not _is_blacklisted_lora(row["lora_name"])
    ]


def build_edit_options(detail, lora_catalog):
    metadata = detail.get("metadata") or {}
    prompt = loads_json(metadata.get("raw_prompt_json"))
    workflow = loads_json(metadata.get("raw_workflow_json"))
    if not isinstance(prompt, dict):
        raise ComfyPromptUnavailable("Cette image ne contient pas de prompt ComfyUI exploitable")

    workflow_nodes = normalize_workflow_nodes(workflow)
    workflow_links = normalize_workflow_links(workflow)
    bypassed_ids = bypassed_node_ids(workflow_nodes)
    nodes = normalize_prompt_nodes(prompt)
    active_nodes = {node_id: node for node_id, node in nodes.items() if node_id not in bypassed_ids}
    prompt_node_ids = prompt_text_node_ids(active_nodes)
    seed = find_seed(active_nodes)
    steps = find_steps(active_nodes)
    references = reference_options(prompt, workflow)
    return {
        "workflow_id": CURRENT_WORKFLOW_ID,
        "mode": "current",
        "output_kind": "image",
        "prompt": find_prompt_text(active_nodes, prompt_node_ids) or metadata.get("prompt") or "",
        "prompt_node_ids": prompt_node_ids,
        "seed": seed,
        "steps": steps,
        "loras": lora_options(nodes, workflow_nodes, bypassed_ids),
        "references": references,
        "images": image_options(nodes, workflow_nodes, workflow_links, bypassed_ids),
        "lora_catalog": lora_catalog,
        "capabilities": {
            "prompt": True,
            "seed": seed is not None,
            "steps": steps is not None,
            "loras": True,
            "references": True,
        },
    }


def build_registered_edit_options(detail, lora_catalog, workflow_id, workflow_root=WORKFLOW_ROOT):
    item = get_registered_workflow(workflow_id, workflow_root)
    config = item["config"]
    prompt = item["template"]
    nodes = normalize_prompt_nodes(prompt)
    prompt_node = nodes[config["prompt"]]
    template_prompt = _node_text_value(prompt_node) or ""
    if config["mode"] == "i2v":
        source_prompt = str((detail.get("metadata") or {}).get("prompt") or "").strip()
        prompt_text = append_i2v_idle_suffix(source_prompt)
    else:
        prompt_text = template_prompt
    seed = _registered_seed_value(nodes, config)
    steps = find_steps(nodes)
    loras = lora_options(nodes, {}, set())
    return {
        "workflow_id": config["id"],
        "mode": config["mode"],
        "output_kind": config["output_kind"],
        "prompt": prompt_text,
        "prompt_node_ids": [config["prompt"]],
        "seed": None if seed is None else str(seed),
        "steps": steps,
        "loras": loras,
        "references": [],
        "images": [],
        "lora_catalog": lora_catalog,
        "capabilities": {
            "prompt": True,
            "seed": seed is not None,
            "steps": steps is not None,
            "loras": bool(loras),
            "references": False,
        },
    }


def append_i2v_idle_suffix(prompt_text):
    prompt_text = str(prompt_text or "").strip()
    if I2V_IDLE_PROMPT_SUFFIX.lower() in prompt_text.lower():
        return prompt_text
    return f"{prompt_text}, {I2V_IDLE_PROMPT_SUFFIX}" if prompt_text else I2V_IDLE_PROMPT_SUFFIX


def _node_text_value(node):
    inputs = node.get("inputs") or {}
    for key in ("text", "prompt", "value"):
        value = inputs.get(key)
        if isinstance(value, str):
            return value
    return None


def _set_node_text(node, value):
    inputs = node.setdefault("inputs", {})
    for key in ("text", "prompt", "value"):
        if key in inputs and not isinstance(inputs.get(key), list):
            inputs[key] = value
            return
    raise ValueError("Le node de prompt mappe ne contient pas de champ texte")


def _registered_seed_value(nodes, config):
    if config.get("seed"):
        node = nodes.get(config["seed"], {})
        for key in ("seed", "seed_noise", "noise_seed"):
            value = (node.get("inputs") or {}).get(key)
            if not isinstance(value, list) and value is not None:
                return value
    return current_seed(nodes)


def reference_options(prompt, workflow):
    references = discover_qwen_references(prompt, workflow)
    for item in references:
        item["thumbnail_url"] = f"/api/comfy/input-preview?filename={quote(item['image_name'], safe='')}"
    return references


def patch_prompt(detail, payload, uploaded_images=None, rng=None):
    patched, _workflow, info = patch_prompt_and_workflow(detail, payload, uploaded_images=uploaded_images, rng=rng)
    return patched, info


def patch_prompt_and_workflow(
    detail,
    payload,
    uploaded_images=None,
    rng=None,
    source_image_name=None,
    source_filename=None,
    workflow_root=WORKFLOW_ROOT,
):
    workflow_id = str(payload.get("workflow_id") or CURRENT_WORKFLOW_ID)
    if workflow_id != CURRENT_WORKFLOW_ID:
        return patch_registered_prompt(
            detail,
            payload,
            workflow_id,
            source_image_name=source_image_name,
            source_filename=source_filename,
            rng=rng,
            workflow_root=workflow_root,
        )

    metadata = detail.get("metadata") or {}
    prompt = loads_json(metadata.get("raw_prompt_json"))
    workflow = loads_json(metadata.get("raw_workflow_json"))
    if not isinstance(prompt, dict):
        raise ComfyPromptUnavailable("Cette image ne contient pas de prompt ComfyUI exploitable")

    patched = copy.deepcopy(prompt)
    patched_workflow = copy.deepcopy(workflow) if isinstance(workflow, dict) else None
    nodes = normalize_prompt_nodes(patched)
    workflow_nodes = normalize_workflow_nodes(patched_workflow)
    bypassed_ids = bypassed_node_ids(workflow_nodes)
    active_nodes = {node_id: node for node_id, node in nodes.items() if node_id not in bypassed_ids}

    prompt_text = str(payload.get("prompt") or "").strip()
    if prompt_text:
        apply_prompt_text(active_nodes, prompt_text)
        apply_workflow_prompt_text(patched_workflow, active_nodes, prompt_text)

    steps = payload.get("steps")
    if steps not in (None, ""):
        apply_steps(active_nodes, int(steps))
        apply_workflow_steps(patched_workflow, active_nodes, int(steps))

    seed = current_seed(active_nodes)
    if payload.get("seed_mode") == "random" or seed is None:
        seed = (rng or random.SystemRandom()).randint(0, MAX_SEED)
    apply_seed(active_nodes, seed)
    apply_workflow_seed(patched_workflow, active_nodes, seed)

    for lora in payload.get("loras") or []:
        if lora.get("new") or not lora.get("node_id"):
            continue
        node_id = str(lora.get("node_id"))
        node = nodes.get(node_id)
        if not node or node.get("class_type") != "LoraLoaderModelOnly":
            continue
        name = str(lora.get("lora_name") or current_lora_name(node) or "").strip()
        if not name or _is_blacklisted_lora(name):
            continue
        strength = float(lora.get("strength_model", current_lora_strength(node) or 1))
        if not lora.get("enabled", True):
            strength = 0.0
        inputs = node.setdefault("inputs", {})
        inputs["lora_name"] = name
        inputs["strength_model"] = strength
        widgets = node.get("widgets_values")
        if isinstance(widgets, list):
            if widgets:
                widgets[0] = name
            if len(widgets) > 1:
                widgets[1] = strength
        apply_workflow_lora(patched_workflow, node_id, name, strength, enabled=bool(lora.get("enabled", True)))

    uploaded_images = uploaded_images or {}
    for node_id, image_name in uploaded_images.items():
        node = nodes.get(str(node_id))
        if node and node.get("class_type") == "LoadImage" and image_name:
            node.setdefault("inputs", {})["image"] = image_name
            widgets = node.get("widgets_values")
            if isinstance(widgets, list) and widgets:
                widgets[0] = image_name
            apply_workflow_image(patched_workflow, str(node_id), image_name)

    reference_payload = payload.get("references") or []
    if reference_payload and any(
        "reference_id" in item or "input_name" in item or "enabled" in item
        for item in reference_payload
        if isinstance(item, dict)
    ):
        if not isinstance(patched_workflow, dict):
            raise ValueError("Le workflow visuel ComfyUI est requis pour modifier les references")
        apply_reference_configuration(patched, patched_workflow, reference_payload)

    new_loras = [
        item
        for item in payload.get("loras") or []
        if isinstance(item, dict) and (item.get("new") or not item.get("node_id"))
    ]
    if new_loras:
        if not isinstance(patched_workflow, dict):
            raise ValueError("Le workflow visuel ComfyUI est requis pour ajouter un LoRA")
        insert_new_loras(patched, patched_workflow, new_loras)

    return patched, patched_workflow, {
        "seed": seed,
        "workflow_id": CURRENT_WORKFLOW_ID,
        "mode": "current",
        "output_kind": None,
        "output_node": None,
        "preview_nodes": [],
    }


def patch_registered_prompt(
    detail,
    payload,
    workflow_id,
    source_image_name=None,
    source_filename=None,
    rng=None,
    workflow_root=WORKFLOW_ROOT,
):
    item = get_registered_workflow(workflow_id, workflow_root)
    config = item["config"]
    patched = copy.deepcopy(item["template"])
    nodes = normalize_prompt_nodes(patched)

    prompt_text = str(payload.get("prompt") or "").strip()
    if config["mode"] == "i2v":
        prompt_text = append_i2v_idle_suffix(prompt_text)
    if prompt_text:
        _set_node_text(nodes[config["prompt"]], prompt_text)

    steps = payload.get("steps")
    if steps not in (None, ""):
        apply_steps(nodes, int(steps))

    seed = _registered_seed_value(nodes, config)
    if payload.get("seed_mode") == "random" or seed is None:
        seed = (rng or random.SystemRandom()).randint(0, MAX_SEED)
    if config.get("seed"):
        seed_node = nodes[config["seed"]]
        seed_inputs = seed_node.setdefault("inputs", {})
        seed_key = next(
            (key for key in ("seed", "seed_noise", "noise_seed") if key in seed_inputs),
            "seed",
        )
        seed_inputs[seed_key] = seed
        seed_node["is_changed"] = [seed]
    else:
        apply_seed(nodes, seed)

    _patch_existing_loras(nodes, payload.get("loras") or [])

    if config["mode"] in {"i2i", "i2v"}:
        if not source_image_name:
            raise ValueError("L'image source n'a pas pu etre envoyee a ComfyUI")
        input_node = nodes[config["input_image"]]
        input_node.setdefault("inputs", {})["image"] = source_image_name

    if config["mode"] == "i2v":
        width = detail.get("width")
        height = detail.get("height")
        target_width, target_height = i2v_dimensions(width, height, config)
        for node_id in config.get("dimension_nodes") or []:
            dimension_inputs = nodes[node_id].setdefault("inputs", {})
            dimension_inputs["width"] = target_width
            dimension_inputs["height"] = target_height
        output_inputs = nodes[config["output"]].setdefault("inputs", {})
        output_inputs["filename_prefix"] = f"video/{video_output_stem(source_filename)}_i2v"

    return patched, None, {
        "seed": seed,
        "workflow_id": config["id"],
        "mode": config["mode"],
        "output_kind": config["output_kind"],
        "output_node": config["output"],
        "preview_nodes": list(config.get("preview") or []),
    }


def _patch_existing_loras(nodes, requested_loras):
    for lora in requested_loras:
        if not isinstance(lora, dict) or lora.get("new") or not lora.get("node_id"):
            continue
        node = nodes.get(str(lora.get("node_id")))
        if not node or node.get("class_type") != "LoraLoaderModelOnly":
            continue
        name = str(lora.get("lora_name") or current_lora_name(node) or "").strip()
        if not name or _is_blacklisted_lora(name):
            continue
        strength = float(lora.get("strength_model", current_lora_strength(node) or 1))
        if not lora.get("enabled", True):
            strength = 0.0
        inputs = node.setdefault("inputs", {})
        inputs["lora_name"] = name
        inputs["strength_model"] = strength


def i2v_dimensions(width, height, config):
    try:
        width = int(width)
        height = int(height)
    except (TypeError, ValueError) as exc:
        raise ValueError("Dimensions de l'image source indisponibles") from exc
    if width <= 0 or height <= 0:
        raise ValueError("Dimensions de l'image source invalides")
    target_min = max(1, int(config.get("target_min_dimension") or 480))
    multiple = max(1, int(config.get("round_larger_dimension_to") or 16))
    if width == height:
        return target_min, target_min
    scale = target_min / min(width, height)
    larger = max(width, height) * scale
    rounding_mode = str(config.get("rounding_mode") or "nearest")
    if rounding_mode == "up":
        rounded_larger = int(-(-larger // multiple) * multiple)
    elif rounding_mode == "down":
        rounded_larger = max(multiple, int(larger // multiple) * multiple)
    else:
        rounded_larger = max(multiple, int(round(larger / multiple)) * multiple)
    return (rounded_larger, target_min) if width > height else (target_min, rounded_larger)


def video_output_stem(filename):
    stem = Path(str(filename or "image")).stem.strip() or "image"
    stem = re.sub(r"[^\w.-]+", "_", stem, flags=re.UNICODE).strip("._")
    return stem or "image"


def apply_reference_configuration(prompt, workflow, requested_references):
    requested = [copy.deepcopy(item) for item in requested_references if isinstance(item, dict)]
    if not requested:
        raise ValueError("Au moins une reference est requise")
    if not any(bool(item.get("enabled", True)) for item in requested):
        raise ValueError("Au moins une reference active est requise")

    target_id = find_qwen_config_target(workflow)
    existing = {item["reference_id"]: item for item in discover_qwen_references(prompt, workflow)}
    subgraph_template = reference_subgraph_template(workflow, next(iter(existing.values()), None))
    seen = set()
    for item in requested:
        reference_id = item.get("reference_id")
        if reference_id:
            reference_id = str(reference_id)
            if reference_id not in existing:
                raise ValueError(f"Reference Qwen inconnue: {reference_id}")
            if reference_id in seen:
                raise ValueError(f"Reference Qwen dupliquee: {reference_id}")
            seen.add(reference_id)
            item["reference_id"] = reference_id
        else:
            image_name = str(item.get("input_name") or "").strip()
            if not image_name:
                raise ValueError("Une nouvelle reference doit fournir input_name")
            item["reference_id"] = add_reference_subgraph(prompt, workflow, image_name, subgraph_template)

    index = WorkflowIndex(workflow)
    references = {item["reference_id"]: item for item in discover_qwen_references(prompt, workflow)}
    active_ids = []
    for position, item in enumerate(requested):
        reference_id = item["reference_id"]
        reference = references.get(reference_id)
        if not reference:
            raise ValueError(f"Impossible de relire la reference Qwen: {reference_id}")
        enabled = bool(item.get("enabled", True))
        image_name = str(item.get("input_name") or reference["image_name"]).strip()
        set_reference_image(prompt, index, reference, image_name)
        qwen_ref = index.nodes[reference_id]
        qwen_ref.node["mode"] = 0 if enabled else 4
        set_workflow_widget(qwen_ref.node, "to_ref", True)
        set_workflow_widget(qwen_ref.node, "ref_main_image", position == 0)
        if enabled:
            materialize_reference_branch(prompt, index, reference_id)
            qwen_prompt = prompt[reference_id]
            qwen_prompt.setdefault("inputs", {})["to_ref"] = True
            qwen_prompt["inputs"]["ref_main_image"] = position == 0
            active_ids.append(reference_id)
        else:
            prompt.pop(reference_id, None)

    for position, reference_id in enumerate(active_ids):
        qwen_inputs = prompt[reference_id].setdefault("inputs", {})
        if position == 0:
            qwen_inputs.pop("configs", None)
        else:
            qwen_inputs["configs"] = [active_ids[position - 1], 0]
    if not active_ids:
        raise ValueError("Au moins une reference active est requise")
    prompt_target = prompt.get(str(target_id))
    if not prompt_target:
        raise ValueError("Encodeur Qwen introuvable dans le prompt executable")
    prompt_target.setdefault("inputs", {})["configs"] = [active_ids[-1], 0]

    rebuild_reference_workflow_chain(workflow, requested, references, str(target_id))


def find_qwen_config_target(workflow):
    candidates = []
    for node in workflow.get("nodes", []):
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("type") or "")
        if "TextEncodeQwen" in node_type and input_slot(node, "configs") is not None:
            candidates.append(str(node["id"]))
    if len(candidates) != 1:
        raise ValueError("Le workflow doit contenir un unique encodeur Qwen avec une entree configs")
    return candidates[0]


def set_reference_image(prompt, index, reference, image_name):
    image_id = reference["image_node_id"]
    image_ref = index.nodes.get(image_id)
    if not image_ref:
        raise ValueError(f"LoadImage de reference introuvable: {image_id}")
    set_workflow_widget(image_ref.node, "image", image_name)
    if "image" in image_ref.node:
        image_ref.node["image"] = image_name
    image_ref.node.setdefault("properties", {}).pop("image", None)
    if image_id in prompt:
        prompt[image_id].setdefault("inputs", {})["image"] = image_name


def materialize_reference_branch(prompt, index, qwen_id):
    incoming = {}
    for edge in index.edges:
        incoming.setdefault(edge.target, []).append(edge)
    stack = [qwen_id]
    ordered = []
    seen = set()
    while stack:
        locator = stack.pop()
        if locator in seen:
            continue
        seen.add(locator)
        ordered.append(locator)
        for edge in incoming.get(locator, []):
            if locator == qwen_id and edge.input_name == "configs":
                continue
            stack.append(edge.origin)
    for locator in reversed(ordered):
        if locator not in prompt and locator in index.nodes:
            prompt[locator] = workflow_node_to_prompt(index, locator)


def workflow_node_to_prompt(index, locator):
    ref = index.nodes[locator]
    inputs = {}
    for slot, item in enumerate(ref.node.get("inputs") or []):
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = item["name"]
        incoming = [edge for edge in index.incoming(locator) if edge.target_slot == slot]
        if incoming:
            edge = incoming[0]
            inputs[name] = [edge.origin, edge.origin_slot]
            continue
        if "widget" in item:
            value = workflow_widget_value(ref.node, name)
            if value is not None:
                inputs[name] = value
    result = {"inputs": inputs, "class_type": ref.node_type}
    title = (ref.node.get("properties") or {}).get("Node name for S&R") or ref.node.get("title")
    if title:
        result["_meta"] = {"title": title}
    return result


def reference_subgraph_template(workflow, reference):
    defaults = {
        "max_size": 1536,
        "settings": {
            "ref_crop": "center", "ref_upscale": "lanczos", "to_vl": True,
            "vl_resize": True, "vl_target_size": 384, "vl_crop": "center", "vl_upscale": "bicubic",
        },
        "loader_properties": {"Node name for S&R": "LoadImage"},
        "adaptive_properties": {"Node name for S&R": "QwenEditAdaptiveLongestEdge"},
        "qwen_properties": {"Node name for S&R": QWEN_CONFIG_TYPE},
    }
    if not reference:
        return defaults
    index = WorkflowIndex(workflow)
    qwen_ref = index.nodes.get(reference["reference_id"])
    loader_ref = index.nodes.get(reference["image_node_id"])
    if not qwen_ref or not loader_ref:
        return defaults
    template = copy.deepcopy(defaults)
    template["loader_properties"] = copy.deepcopy(loader_ref.node.get("properties") or defaults["loader_properties"])
    template["qwen_properties"] = copy.deepcopy(qwen_ref.node.get("properties") or defaults["qwen_properties"])
    for name in template["settings"]:
        value = workflow_widget_value(qwen_ref.node, name)
        if value is not None:
            template["settings"][name] = value
    longest_edge_sources = index.incoming(reference["reference_id"], "ref_longest_edge")
    if longest_edge_sources:
        adaptive_ref = index.nodes.get(longest_edge_sources[0].origin)
        if adaptive_ref and adaptive_ref.node_type == "QwenEditAdaptiveLongestEdge":
            template["adaptive_properties"] = copy.deepcopy(adaptive_ref.node.get("properties") or defaults["adaptive_properties"])
            template["max_size"] = workflow_widget_value(adaptive_ref.node, "max_size") or defaults["max_size"]
    return template


def add_reference_subgraph(prompt, workflow, image_name, template=None):
    template = copy.deepcopy(template or reference_subgraph_template(workflow, None))
    settings = template["settings"]
    outer_id = next_root_node_id(workflow)
    subgraph_id = str(uuid.uuid4())
    loader_id = f"{outer_id}:1"
    adaptive_id = f"{outer_id}:2"
    qwen_id = f"{outer_id}:3"
    prompt[loader_id] = {
        "inputs": {"image": image_name},
        "class_type": "LoadImage",
        "_meta": {"title": "Load Image"},
    }
    prompt[adaptive_id] = {
        "inputs": {"max_size": template["max_size"], "image": [loader_id, 0]},
        "class_type": "QwenEditAdaptiveLongestEdge",
        "_meta": {"title": "Qwen Edit Adaptive Longest Edge"},
    }
    prompt[qwen_id] = {
        "inputs": {
            "to_ref": True,
            "ref_main_image": False,
            "ref_longest_edge": [adaptive_id, 0],
            "ref_crop": settings["ref_crop"],
            "ref_upscale": settings["ref_upscale"],
            "to_vl": settings["to_vl"],
            "vl_resize": settings["vl_resize"],
            "vl_target_size": settings["vl_target_size"],
            "vl_crop": settings["vl_crop"],
            "vl_upscale": settings["vl_upscale"],
            "image": [loader_id, 0],
            "mask": [loader_id, 1],
        },
        "class_type": QWEN_CONFIG_TYPE,
        "_meta": {"title": "Qwen Edit Config Preparer"},
    }

    root_nodes = workflow.setdefault("nodes", [])
    target = next((node for node in root_nodes if "TextEncodeQwen" in str(node.get("type") or "")), {})
    target_pos = target.get("pos") or [2200, 1200]
    existing_count = len((workflow.get("definitions") or {}).get("subgraphs") or [])
    root_nodes.append(
        {
            "id": outer_id,
            "type": subgraph_id,
            "pos": [float(target_pos[0]) - 480, float(target_pos[1]) + 420 + existing_count * 180],
            "size": [330, 150],
            "flags": {},
            "order": max((int(node.get("order", 0)) for node in root_nodes), default=0) + 1,
            "mode": 0,
            "inputs": [
                {"name": "configs", "type": "LIST", "link": None},
                {"name": "image", "type": "COMBO", "widget": {"name": "image"}, "link": None},
            ],
            "outputs": [{"name": "configs", "type": "LIST", "links": []}],
            "properties": {"proxyWidgets": [["1", "image"]]},
            "widgets_values": [image_name],
            "title": f"Reference Qwen {outer_id}",
        }
    )
    definition = build_reference_subgraph_definition(subgraph_id, image_name, template)
    workflow.setdefault("definitions", {}).setdefault("subgraphs", []).append(definition)
    workflow["last_node_id"] = max(int(workflow.get("last_node_id") or 0), outer_id)
    return qwen_id


def build_reference_subgraph_definition(subgraph_id, image_name, template):
    settings = template["settings"]
    links = [
        {"id": 1, "origin_id": -10, "origin_slot": 1, "target_id": 1, "target_slot": 0, "type": "COMBO"},
        {"id": 2, "origin_id": 1, "origin_slot": 0, "target_id": 2, "target_slot": 0, "type": "IMAGE"},
        {"id": 3, "origin_id": 1, "origin_slot": 0, "target_id": 3, "target_slot": 0, "type": "IMAGE"},
        {"id": 4, "origin_id": 1, "origin_slot": 1, "target_id": 3, "target_slot": 2, "type": "MASK"},
        {"id": 5, "origin_id": 2, "origin_slot": 0, "target_id": 3, "target_slot": 5, "type": "INT"},
        {"id": 6, "origin_id": -10, "origin_slot": 0, "target_id": 3, "target_slot": 1, "type": "LIST"},
        {"id": 7, "origin_id": 3, "origin_slot": 0, "target_id": -20, "target_slot": 0, "type": "LIST"},
    ]
    loader_inputs = [
        {"name": "image", "type": "COMBO", "widget": {"name": "image"}, "link": 1},
        {"name": "upload", "type": "IMAGEUPLOAD", "widget": {"name": "upload"}},
    ]
    qwen_inputs = [
        {"name": "image", "type": "IMAGE", "link": 3},
        {"name": "configs", "type": "LIST", "link": 6},
        {"name": "mask", "type": "MASK", "link": 4},
    ]
    for name, value_type in (
        ("to_ref", "BOOLEAN"), ("ref_main_image", "BOOLEAN"), ("ref_longest_edge", "INT"),
        ("ref_crop", "COMBO"), ("ref_upscale", "COMBO"), ("to_vl", "BOOLEAN"),
        ("vl_resize", "BOOLEAN"), ("vl_target_size", "INT"), ("vl_crop", "COMBO"), ("vl_upscale", "COMBO"),
    ):
        item = {"name": name, "type": value_type, "widget": {"name": name}}
        if name == "ref_longest_edge":
            item["link"] = 5
        qwen_inputs.append(item)
    return {
        "id": subgraph_id,
        "version": 1,
        "state": {"lastGroupId": 0, "lastNodeId": 3, "lastLinkId": 7, "lastRerouteId": 0},
        "revision": 0,
        "config": {},
        "name": "Qwen Reference",
        "inputNode": {"id": -10, "bounding": [-180, 80, 120, 100]},
        "outputNode": {"id": -20, "bounding": [760, 80, 120, 60]},
        "inputs": [
            {"id": str(uuid.uuid4()), "name": "configs", "type": "LIST", "linkIds": [6], "pos": [-80, 100]},
            {"id": str(uuid.uuid4()), "name": "image", "type": "COMBO", "linkIds": [1], "pos": [-80, 120]},
        ],
        "outputs": [
            {"id": str(uuid.uuid4()), "name": "configs", "type": "LIST", "linkIds": [7], "pos": [780, 100]}
        ],
        "widgets": [],
        "nodes": [
            {
                "id": 1, "type": "LoadImage", "pos": [0, 180], "size": [320, 300], "flags": {}, "order": 0, "mode": 0,
                "inputs": loader_inputs,
                "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [2, 3]}, {"name": "MASK", "type": "MASK", "links": [4]}],
                "properties": copy.deepcopy(template["loader_properties"]), "widgets_values": [image_name, "image"],
            },
            {
                "id": 2, "type": "QwenEditAdaptiveLongestEdge", "pos": [350, 330], "size": [280, 90], "flags": {}, "order": 1, "mode": 0,
                "inputs": [{"name": "image", "type": "IMAGE", "link": 2}, {"name": "max_size", "type": "INT", "widget": {"name": "max_size"}}],
                "outputs": [{"name": "longest_edge", "type": "INT", "links": [5]}],
                "properties": copy.deepcopy(template["adaptive_properties"]), "widgets_values": [template["max_size"]],
            },
            {
                "id": 3, "type": QWEN_CONFIG_TYPE, "pos": [350, 40], "size": [360, 250], "flags": {}, "order": 2, "mode": 0,
                "inputs": qwen_inputs,
                "outputs": [{"name": "configs", "type": "LIST", "links": [7]}, {"name": "config", "type": "ANY", "links": []}],
                "properties": copy.deepcopy(template["qwen_properties"]),
                "widgets_values": [
                    True, False, template["max_size"], settings["ref_crop"], settings["ref_upscale"],
                    settings["to_vl"], settings["vl_resize"], settings["vl_target_size"], settings["vl_crop"], settings["vl_upscale"],
                ],
            },
        ],
        "groups": [],
        "links": links,
        "extra": {"workflowRendererVersion": "LG"},
    }


def rebuild_reference_workflow_chain(workflow, requested, references, target_id):
    endpoints = []
    for item in requested:
        reference = references[item["reference_id"]]
        endpoint = reference.get("endpoint")
        if not endpoint or endpoint.get("input_slot") is None:
            raise ValueError(f"Reference Qwen non chainable: {item['reference_id']}")
        endpoints.append(endpoint)
    target = root_node_map(workflow).get(str(target_id))
    target_slot = input_slot(target or {}, "configs")
    if target_slot is None:
        raise ValueError("Entree configs Qwen introuvable")
    endpoint_nodes = {endpoint["node_id"] for endpoint in endpoints}
    retained = []
    for raw in workflow.get("links", []):
        parsed = parse_link(raw)
        if not parsed:
            retained.append(raw)
            continue
        _link_id, origin, origin_slot_value, target_node, target_slot_value, _ = parsed
        remove = (
            (str(origin) in endpoint_nodes and int(origin_slot_value) == 0)
            or (str(target_node) in endpoint_nodes and int(target_slot_value) in {endpoint["input_slot"] for endpoint in endpoints if endpoint["node_id"] == str(target_node)})
            or (str(target_node) == str(target_id) and int(target_slot_value) == target_slot)
        )
        if not remove:
            retained.append(raw)
    workflow["links"] = retained
    for position in range(1, len(endpoints)):
        add_root_link(workflow, endpoints[position - 1]["node_id"], endpoints[position - 1]["output_slot"], endpoints[position]["node_id"], endpoints[position]["input_slot"], "LIST")
    add_root_link(workflow, endpoints[-1]["node_id"], endpoints[-1]["output_slot"], target_id, target_slot, "LIST")
    rebuild_root_link_slots(workflow)


def insert_new_loras(prompt, workflow, loras):
    active = []
    for item in loras:
        name = str(item.get("lora_name") or "").strip()
        if not item.get("enabled", True) or not name or _is_blacklisted_lora(name):
            continue
        active.append((name, float(item.get("strength_model", 1.0))))
    if not active:
        return
    predecessor, consumers = prompt_model_insertion_point(prompt)
    workflow_predecessor, workflow_targets = workflow_model_insertion_point(workflow)
    new_ids = []
    previous = str(predecessor)
    for name, strength in active:
        node_id = next_available_node_id(prompt, workflow)
        prompt[node_id] = {
            "inputs": {"lora_name": name, "strength_model": strength, "model": [previous, 0]},
            "class_type": "LoraLoaderModelOnly",
            "_meta": {"title": "Load LoRA"},
        }
        add_workflow_lora_node(workflow, int(node_id), name, strength)
        new_ids.append(node_id)
        previous = node_id
    for node, input_name in consumers:
        node.setdefault("inputs", {})[input_name] = [previous, 0]

    target_link_ids = {item[0] for item in workflow_targets}
    workflow["links"] = [
        raw for raw in workflow.get("links", [])
        if not (parse_link(raw) and parse_link(raw)[0] in target_link_ids)
    ]
    previous_workflow = str(workflow_predecessor)
    for node_id in new_ids:
        add_root_link(workflow, previous_workflow, 0, node_id, 0, "MODEL")
        previous_workflow = node_id
    for _link_id, target_id, target_slot_value in workflow_targets:
        add_root_link(workflow, previous_workflow, 0, target_id, target_slot_value, "MODEL")
    rebuild_root_link_slots(workflow)


def prompt_model_insertion_point(prompt):
    nodes = normalize_prompt_nodes(prompt)
    samplers = [node for node in nodes.values() if "Sampler" in str(node.get("class_type") or "") and isinstance((node.get("inputs") or {}).get("model"), list)]
    sources = {str(node["inputs"]["model"][0]) for node in samplers}
    if len(sources) != 1:
        raise ValueError("Le workflow doit avoir une unique chaine MODEL vers les samplers")
    predecessor = sources.pop()
    current = predecessor
    seen = set()
    while current not in seen:
        seen.add(current)
        node = nodes.get(current)
        if not node:
            break
        if node.get("class_type") == "UNETLoader":
            break
        model_input = (node.get("inputs") or {}).get("model")
        if not isinstance(model_input, list):
            break
        current = str(model_input[0])
    if nodes.get(current, {}).get("class_type") != "UNETLoader":
        raise ValueError("Load Diffusion Model introuvable dans la chaine MODEL")
    consumers = []
    for node in nodes.values():
        for name, value in (node.get("inputs") or {}).items():
            if name == "model" and isinstance(value, list) and str(value[0]) == predecessor:
                consumers.append((node, name))
    if not consumers:
        raise ValueError("Aucun consommateur MODEL a reconnecter")
    return predecessor, consumers


def workflow_model_insertion_point(workflow):
    nodes = root_node_map(workflow)
    sampler_targets = []
    for node_id, node in nodes.items():
        if "Sampler" not in str(node.get("type") or ""):
            continue
        slot = input_slot(node, "model")
        if slot is not None:
            sampler_targets.append((node_id, slot))
    links = []
    for raw in workflow.get("links", []):
        parsed = parse_link(raw)
        if parsed and (str(parsed[3]), int(parsed[4])) in sampler_targets:
            links.append(parsed)
    origins = {str(item[1]) for item in links}
    if len(origins) != 1:
        raise ValueError("La chaine MODEL visuelle est ambigue")
    predecessor = origins.pop()
    targets = []
    for raw in workflow.get("links", []):
        parsed = parse_link(raw)
        if parsed and str(parsed[1]) == predecessor and str(parsed[5]) == "MODEL":
            targets.append((parsed[0], str(parsed[3]), int(parsed[4])))
    if not targets:
        raise ValueError("Aucun lien MODEL visuel a reconnecter")
    return predecessor, targets


def add_workflow_lora_node(workflow, node_id, name, strength):
    nodes = workflow.setdefault("nodes", [])
    max_x = max((float((node.get("pos") or [0])[0]) for node in nodes), default=0)
    nodes.append(
        {
            "id": node_id,
            "type": "LoraLoaderModelOnly",
            "pos": [max_x + 320, 1000 + len(nodes) * 4],
            "size": [270, 82],
            "flags": {},
            "order": max((int(node.get("order", 0)) for node in nodes), default=0) + 1,
            "mode": 0,
            "inputs": [
                {"name": "model", "type": "MODEL", "link": None},
                {"name": "lora_name", "type": "COMBO", "widget": {"name": "lora_name"}},
                {"name": "strength_model", "type": "FLOAT", "widget": {"name": "strength_model"}},
            ],
            "outputs": [{"name": "MODEL", "type": "MODEL", "links": []}],
            "properties": {"Node name for S&R": "LoraLoaderModelOnly"},
            "widgets_values": [name, strength],
        }
    )
    workflow["last_node_id"] = max(int(workflow.get("last_node_id") or 0), node_id)


def root_node_map(workflow):
    return {str(node["id"]): node for node in workflow.get("nodes", []) if isinstance(node, dict) and "id" in node}


def next_root_node_id(workflow):
    values = [int(node["id"]) for node in workflow.get("nodes", []) if str(node.get("id", "")).isdigit()]
    values.append(int(workflow.get("last_node_id") or 0))
    return max(values, default=0) + 1


def next_available_node_id(prompt, workflow):
    prompt_values = [int(node_id) for node_id in prompt if str(node_id).isdigit()]
    workflow_values = [int(node["id"]) for node in workflow.get("nodes", []) if str(node.get("id", "")).isdigit()]
    workflow_values.append(int(workflow.get("last_node_id") or 0))
    return str(max(prompt_values + workflow_values, default=0) + 1)


def add_root_link(workflow, origin_id, origin_slot_value, target_id, target_slot_value, link_type):
    next_link_id = max(
        [int(parse_link(raw)[0]) for raw in workflow.get("links", []) if parse_link(raw) and str(parse_link(raw)[0]).isdigit()]
        + [int(workflow.get("last_link_id") or 0)],
        default=0,
    ) + 1
    workflow.setdefault("links", []).append(
        [next_link_id, int(origin_id) if str(origin_id).isdigit() else origin_id, int(origin_slot_value), int(target_id) if str(target_id).isdigit() else target_id, int(target_slot_value), link_type]
    )
    workflow["last_link_id"] = next_link_id
    return next_link_id


def rebuild_root_link_slots(workflow):
    nodes = root_node_map(workflow)
    for node in nodes.values():
        for item in node.get("inputs") or []:
            if isinstance(item, dict) and "link" in item:
                item["link"] = None
        for item in node.get("outputs") or []:
            if isinstance(item, dict):
                item["links"] = []
    for raw in workflow.get("links", []):
        parsed = parse_link(raw)
        if not parsed:
            continue
        link_id, origin_id, origin_slot_value, target_id, target_slot_value, _ = parsed
        origin = nodes.get(str(origin_id))
        target = nodes.get(str(target_id))
        if origin and int(origin_slot_value) < len(origin.get("outputs") or []):
            origin["outputs"][int(origin_slot_value)].setdefault("links", []).append(link_id)
        if target and int(target_slot_value) < len(target.get("inputs") or []):
            target["inputs"][int(target_slot_value)]["link"] = link_id


def normalize_prompt_nodes(prompt):
    if not isinstance(prompt, dict):
        return {}
    return {str(node_id): node for node_id, node in prompt.items() if isinstance(node, dict)}


def normalize_workflow_nodes(workflow):
    if not isinstance(workflow, dict):
        return {}
    return {str(node["id"]): node for node in workflow.get("nodes", []) if isinstance(node, dict) and "id" in node}


def normalize_workflow_links(workflow):
    if not isinstance(workflow, dict):
        return []
    links = []
    for link in workflow.get("links", []):
        if isinstance(link, list) and len(link) >= 5:
            links.append({"origin": str(link[1]), "target": str(link[3])})
    return links


def bypassed_node_ids(workflow_nodes):
    return {
        node_id
        for node_id, node in workflow_nodes.items()
        if node.get("mode") == 4 or node.get("flags", {}).get("bypassed") is True
    }


def prompt_text_node_ids(nodes):
    linked_sources = set()
    for node in nodes.values():
        prompt_input = node.get("inputs", {}).get("prompt")
        if isinstance(prompt_input, list) and prompt_input:
            linked_sources.add(str(prompt_input[0]))
    ids = [
        node_id
        for node_id in linked_sources
        if nodes.get(node_id, {}).get("class_type") == "PrimitiveStringMultiline"
    ]
    if ids:
        return sorted(ids, key=int_or_text_key)
    for node_id, node in nodes.items():
        inputs = node.get("inputs", {})
        if any(isinstance(inputs.get(key), str) for key in ("value", "prompt", "text", "positive")):
            ids.append(node_id)
    return sorted(set(ids), key=int_or_text_key)


def find_prompt_text(nodes, node_ids):
    for node_id in node_ids:
        node = nodes.get(str(node_id), {})
        inputs = node.get("inputs", {})
        for key in ("value", "prompt", "text", "positive"):
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def apply_prompt_text(nodes, prompt_text):
    ids = prompt_text_node_ids(nodes)
    if not ids:
        raise ValueError("Aucun node de prompt texte n'a ete trouve")
    for node_id in ids:
        inputs = nodes[node_id].setdefault("inputs", {})
        for key in ("value", "prompt", "text", "positive"):
            if key in inputs and not isinstance(inputs.get(key), list):
                inputs[key] = prompt_text
                break
        else:
            inputs["value"] = prompt_text


def find_seed(nodes):
    seed = current_seed(nodes)
    return None if seed is None else str(seed)


def current_seed(nodes):
    for node in nodes.values():
        inputs = node.get("inputs", {})
        for key in ("seed_noise", "seed"):
            value = inputs.get(key)
            if value is not None and not isinstance(value, list):
                return value
        if node.get("class_type", "").lower().startswith("seed"):
            changed = node.get("is_changed")
            if isinstance(changed, list) and changed:
                return changed[0]
    return None


def apply_seed(nodes, seed):
    for node in nodes.values():
        inputs = node.setdefault("inputs", {})
        if "seed_noise" in inputs and not isinstance(inputs.get("seed_noise"), list):
            inputs["seed_noise"] = seed
        if "seed" in inputs and not isinstance(inputs.get("seed"), list):
            inputs["seed"] = seed
        if node.get("class_type", "").lower().startswith("seed"):
            inputs["seed"] = seed
            node["is_changed"] = [seed]


def find_steps(nodes):
    values = []
    for node in nodes.values():
        value = node.get("inputs", {}).get("steps")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(int(value))
    return values[0] if values else None


def apply_steps(nodes, steps):
    if steps < 1:
        raise ValueError("Le nombre de steps doit etre positif")
    changed = False
    for node in nodes.values():
        inputs = node.setdefault("inputs", {})
        if "steps" in inputs and isinstance(inputs.get("steps"), (int, float)):
            inputs["steps"] = steps
            changed = True
    if not changed:
        raise ValueError("Aucun champ steps numerique n'a ete trouve")


def workflow_node_map(workflow):
    return normalize_workflow_nodes(workflow)


def apply_workflow_prompt_text(workflow, active_nodes, prompt_text):
    if not isinstance(workflow, dict):
        return
    workflow_nodes = workflow_node_map(workflow)
    for node_id in prompt_text_node_ids(active_nodes):
        node = active_nodes.get(str(node_id), {})
        inputs = node.get("inputs", {})
        widget_name = next(
            (key for key in ("value", "prompt", "text", "positive") if key in inputs and not isinstance(inputs.get(key), list)),
            "value",
        )
        set_workflow_widget(workflow_nodes.get(str(node_id)), widget_name, prompt_text)


def apply_workflow_steps(workflow, active_nodes, steps):
    if not isinstance(workflow, dict):
        return
    workflow_nodes = workflow_node_map(workflow)
    for node_id, node in active_nodes.items():
        if "steps" in node.get("inputs", {}):
            set_workflow_widget(workflow_nodes.get(str(node_id)), "steps", steps)


def apply_workflow_seed(workflow, active_nodes, seed):
    if not isinstance(workflow, dict):
        return
    workflow_nodes = workflow_node_map(workflow)
    for node_id, node in active_nodes.items():
        inputs = node.get("inputs", {})
        if "seed_noise" in inputs:
            set_workflow_widget(workflow_nodes.get(str(node_id)), "seed_noise", seed)
        if "seed" in inputs:
            set_workflow_widget(workflow_nodes.get(str(node_id)), "seed", seed)
        if node.get("class_type", "").lower().startswith("seed"):
            workflow_node = workflow_nodes.get(str(node_id))
            if workflow_node and isinstance(workflow_node.get("widgets_values"), list):
                ensure_widget_index(workflow_node, 0)
                workflow_node["widgets_values"][0] = seed
    seed_widgets = workflow.get("seed_widgets")
    if isinstance(seed_widgets, dict):
        for node_id, widget_index in seed_widgets.items():
            workflow_node = workflow_nodes.get(str(node_id))
            if workflow_node and isinstance(widget_index, int):
                ensure_widget_index(workflow_node, widget_index)
                workflow_node["widgets_values"][widget_index] = seed


def apply_workflow_lora(workflow, node_id, name, strength, enabled=True):
    if not isinstance(workflow, dict):
        return
    workflow_node = workflow_node_map(workflow).get(str(node_id))
    if not workflow_node:
        return
    workflow_node["mode"] = 0 if enabled else 4
    set_workflow_widget(workflow_node, "lora_name", name)
    set_workflow_widget(workflow_node, "strength_model", strength)


def apply_workflow_image(workflow, node_id, image_name):
    if not isinstance(workflow, dict):
        return
    workflow_node = workflow_node_map(workflow).get(str(node_id))
    if not workflow_node:
        return
    set_workflow_widget(workflow_node, "image", image_name)
    properties = workflow_node.setdefault("properties", {})
    if isinstance(properties, dict) and "image" in properties:
        properties["image"] = image_name


def set_workflow_widget(workflow_node, widget_name, value):
    if not workflow_node:
        return False
    index = workflow_widget_index(workflow_node, widget_name)
    if index is None:
        fallback = fallback_widget_index(workflow_node, widget_name)
        if fallback is None:
            return False
        index = fallback
    ensure_widget_index(workflow_node, index)
    workflow_node["widgets_values"][index] = value
    return True


def workflow_widget_index(workflow_node, widget_name):
    widget_index = 0
    for item in workflow_node.get("inputs") or []:
        if not isinstance(item, dict) or "widget" not in item:
            continue
        if item.get("name") == widget_name or item.get("widget", {}).get("name") == widget_name:
            return widget_index
        widget_index += 1
    return None


def fallback_widget_index(workflow_node, widget_name):
    node_type = workflow_node.get("type")
    if node_type == "LoadImage" and widget_name == "image":
        return 0
    if node_type == "LoraLoaderModelOnly":
        return {"lora_name": 0, "strength_model": 1}.get(widget_name)
    if str(node_type).lower().startswith("seed") and widget_name == "seed":
        return 0
    return None


def ensure_widget_index(workflow_node, index):
    widgets = workflow_node.setdefault("widgets_values", [])
    while len(widgets) <= index:
        widgets.append(None)


def lora_options(nodes, workflow_nodes, bypassed_ids):
    options = []
    for node_id, node in nodes.items():
        if node.get("class_type") != "LoraLoaderModelOnly":
            continue
        name = current_lora_name(node)
        if not name or _is_blacklisted_lora(name):
            continue
        strength = current_lora_strength(node)
        enabled = node_id not in bypassed_ids and strength != 0
        options.append(
            {
                "node_id": node_id,
                "lora_name": name,
                "strength_model": strength,
                "enabled": enabled,
                "title": workflow_nodes.get(node_id, {}).get("properties", {}).get("Node name for S&R") or node.get("_meta", {}).get("title") or node_id,
            }
        )
    return sorted(options, key=lambda item: int_or_text_key(item["node_id"]))


def current_lora_name(node):
    inputs = node.get("inputs", {})
    if inputs.get("lora_name"):
        return str(inputs["lora_name"])
    widgets = node.get("widgets_values")
    if isinstance(widgets, list) and widgets:
        return str(widgets[0])
    return None


def current_lora_strength(node):
    inputs = node.get("inputs", {})
    if isinstance(inputs.get("strength_model"), (int, float)):
        return inputs["strength_model"]
    widgets = node.get("widgets_values")
    if isinstance(widgets, list) and len(widgets) > 1 and isinstance(widgets[1], (int, float)):
        return widgets[1]
    return None


def image_options(nodes, workflow_nodes, workflow_links, bypassed_ids):
    active_sinks = active_image_sinks(nodes, workflow_nodes, bypassed_ids)
    graph = active_graph_edges(workflow_links, bypassed_ids)
    images = []
    for node_id, node in nodes.items():
        if node.get("class_type") != "LoadImage" or node_id in bypassed_ids:
            continue
        image_name = node.get("inputs", {}).get("image")
        if not image_name:
            continue
        if workflow_nodes and not has_active_path(node_id, active_sinks, graph):
            continue
        images.append({"node_id": node_id, "image_name": str(image_name)})
    return sorted(images, key=lambda item: int_or_text_key(item["node_id"]))


def active_image_sinks(prompt_nodes, workflow_nodes, bypassed_ids):
    sink_types = {"SaveImage", "PreviewImage", "Image Comparer (rgthree)"}
    sink_ids = {
        node_id
        for node_id, node in prompt_nodes.items()
        if node.get("class_type") in sink_types and node_id not in bypassed_ids
    }
    for node_id, node in workflow_nodes.items():
        if node.get("type") in sink_types and node_id not in bypassed_ids:
            sink_ids.add(node_id)
    return sink_ids


def active_graph_edges(workflow_links, bypassed_ids):
    graph = {}
    for link in workflow_links:
        if link["origin"] in bypassed_ids or link["target"] in bypassed_ids:
            continue
        graph.setdefault(link["origin"], set()).add(link["target"])
    return graph


def has_active_path(start_id, sink_ids, graph):
    seen = set()
    stack = [str(start_id)]
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        if node_id in sink_ids and node_id != str(start_id):
            return True
        stack.extend(graph.get(node_id, set()) - seen)
    return False


def extract_history_filenames(history, output_node=None, output_kind=None):
    filenames = []
    outputs = history.get("outputs") or {}
    if output_node is not None:
        node_output = outputs.get(str(output_node)) or outputs.get(output_node) or {}
        node_outputs = [node_output]
    else:
        node_outputs = outputs.values()
    keys = ("videos",) if output_kind == "video" else ("images", "gifs", "videos")
    for node_output in node_outputs:
        for key in keys:
            for item in node_output.get(key) or []:
                filename = item.get("filename")
                if filename:
                    subfolder = item.get("subfolder")
                    filenames.append(f"{subfolder}/{filename}" if subfolder else filename)
    return filenames


def extract_preview_bytes(message):
    for marker in (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"):
        index = message.find(marker)
        if index >= 0:
            return message[index:]
    if len(message) > 8:
        return message[8:]
    return None


def int_or_text_key(value):
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)
