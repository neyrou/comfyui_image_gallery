import copy
import json
import mimetypes
import random
import time
import uuid
from pathlib import Path
from urllib import error, request

from metadata_extractor import _is_blacklisted_lora


COMFY_BASE_URL = "http://127.0.0.1:8188"
MAX_SEED = 1125899906842624


class ComfyUnavailable(RuntimeError):
    pass


class ComfyGenerationError(RuntimeError):
    pass


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
                return json.loads(response.read().decode("utf-8"))
        except (OSError, error.URLError, json.JSONDecodeError) as exc:
            raise ComfyUnavailable(str(exc)) from exc

    def upload_image(self, image_path):
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
                b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n',
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

    def queue_prompt(self, prompt):
        return self.post_json("/prompt", {"prompt": prompt, "client_id": str(uuid.uuid4())}, timeout=max(self.timeout, 10))

    def wait_for_history(self, prompt_id, timeout=180, interval=1):
        deadline = time.time() + timeout
        while time.time() < deadline:
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
        raise ValueError("Cette image ne contient pas de prompt ComfyUI exploitable")

    workflow_nodes = normalize_workflow_nodes(workflow)
    workflow_links = normalize_workflow_links(workflow)
    bypassed_ids = bypassed_node_ids(workflow_nodes)
    nodes = normalize_prompt_nodes(prompt)
    active_nodes = {node_id: node for node_id, node in nodes.items() if node_id not in bypassed_ids}
    prompt_node_ids = prompt_text_node_ids(active_nodes)
    seed = find_seed(active_nodes)
    steps = find_steps(active_nodes)
    return {
        "prompt": find_prompt_text(active_nodes, prompt_node_ids) or metadata.get("prompt") or "",
        "prompt_node_ids": prompt_node_ids,
        "seed": seed,
        "steps": steps,
        "loras": lora_options(nodes, workflow_nodes, bypassed_ids),
        "images": image_options(nodes, workflow_nodes, workflow_links, bypassed_ids),
        "lora_catalog": lora_catalog,
    }


def patch_prompt(detail, payload, uploaded_images=None, rng=None):
    metadata = detail.get("metadata") or {}
    prompt = loads_json(metadata.get("raw_prompt_json"))
    workflow = loads_json(metadata.get("raw_workflow_json"))
    if not isinstance(prompt, dict):
        raise ValueError("Cette image ne contient pas de prompt ComfyUI exploitable")

    patched = copy.deepcopy(prompt)
    nodes = normalize_prompt_nodes(patched)
    workflow_nodes = normalize_workflow_nodes(workflow)
    bypassed_ids = bypassed_node_ids(workflow_nodes)
    active_nodes = {node_id: node for node_id, node in nodes.items() if node_id not in bypassed_ids}

    prompt_text = str(payload.get("prompt") or "").strip()
    if prompt_text:
        apply_prompt_text(active_nodes, prompt_text)

    steps = payload.get("steps")
    if steps not in (None, ""):
        apply_steps(active_nodes, int(steps))

    seed = current_seed(active_nodes)
    if payload.get("seed_mode") == "random" or seed is None:
        seed = (rng or random.SystemRandom()).randint(0, MAX_SEED)
    apply_seed(active_nodes, seed)

    for lora in payload.get("loras") or []:
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

    uploaded_images = uploaded_images or {}
    for node_id, image_name in uploaded_images.items():
        node = nodes.get(str(node_id))
        if node and node.get("class_type") == "LoadImage" and image_name:
            node.setdefault("inputs", {})["image"] = image_name
            widgets = node.get("widgets_values")
            if isinstance(widgets, list) and widgets:
                widgets[0] = image_name

    return patched, {"seed": seed}


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


def extract_history_filenames(history):
    filenames = []
    for node_output in (history.get("outputs") or {}).values():
        for key in ("images", "gifs"):
            for item in node_output.get(key) or []:
                filename = item.get("filename")
                if filename:
                    subfolder = item.get("subfolder")
                    filenames.append(f"{subfolder}/{filename}" if subfolder else filename)
    return filenames


def int_or_text_key(value):
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)
