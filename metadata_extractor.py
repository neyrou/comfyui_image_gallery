import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image


LORA_BLACKLIST_EXACT = {
    "qwen-image-edit-2511-lightning-4steps-v1.0-fp32.safetensors",
}
LORA_BLACKLIST_CONTAINS = ("lightning",)
AUTHENTIC_FILENAME_PATTERN = re.compile(
    r"^(?:PXL_|IMG[-_]|DSC[-_]|DSCN|DSCF|MVIMG_|CAM_|CAMERA_|WEBCAM_)",
    re.IGNORECASE,
)


@dataclass
class ExtractedMetadata:
    prompt: str | None = None
    unet_name: str | None = None
    seed_noise: int | str | None = None
    seed: int | str | None = None
    used_images: list[str] = field(default_factory=list)
    loras: list[dict] = field(default_factory=list)
    raw_prompt: dict | list | str | None = None
    raw_workflow: dict | list | str | None = None
    is_comfyui: bool = False
    is_authentic: bool = False


def extract_from_image(image_path: str | Path) -> ExtractedMetadata:
    image_path = Path(image_path)
    with Image.open(image_path) as image:
        prompt_raw = image.info.get("prompt")
        workflow_raw = image.info.get("workflow")
        exif = image.getexif()
        has_camera_exif = any(
            str(exif.get(tag_id) or "").strip()
            for tag_id in (271, 272)  # Make, Model
        )
    metadata = extract_from_comfy_payloads(prompt_raw, workflow_raw)
    metadata.is_authentic = not metadata.is_comfyui and (
        has_camera_exif or bool(AUTHENTIC_FILENAME_PATTERN.match(image_path.name))
    )
    return metadata


def extract_from_prompt_json(prompt_json: str | dict, workflow_json: str | dict | None = None) -> ExtractedMetadata:
    return extract_from_comfy_payloads(prompt_json, workflow_json)


def extract_from_comfy_payloads(prompt_payload, workflow_payload=None) -> ExtractedMetadata:
    prompt = _loads_json(prompt_payload)
    workflow = _loads_json(workflow_payload)
    if workflow is None and isinstance(prompt, dict) and "nodes" in prompt and "links" in prompt:
        workflow = prompt

    prompt_nodes = _normalize_prompt_nodes(prompt)
    workflow_nodes = _normalize_workflow_nodes(workflow)
    workflow_links = _normalize_workflow_links(workflow)
    bypassed_ids = _bypassed_node_ids(workflow_nodes)
    active_prompt_nodes = {
        node_id: node
        for node_id, node in prompt_nodes.items()
        if node_id not in bypassed_ids
    }

    metadata = ExtractedMetadata(raw_prompt=prompt, raw_workflow=workflow)
    metadata.is_comfyui = bool(prompt_nodes) or bool(
        workflow_nodes
        and isinstance(workflow, dict)
        and isinstance(workflow.get("links"), list)
    )
    metadata.prompt = _extract_prompt(active_prompt_nodes)
    metadata.unet_name = _extract_unet_name(active_prompt_nodes)
    metadata.seed_noise, metadata.seed = _extract_seed(active_prompt_nodes)
    metadata.loras = _extract_loras(active_prompt_nodes)
    metadata.used_images = _extract_used_images(active_prompt_nodes, workflow_nodes, workflow_links, bypassed_ids)
    return metadata


def _loads_json(value):
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _normalize_prompt_nodes(prompt):
    if not isinstance(prompt, dict):
        return {}
    nodes = {}
    for node_id, node in prompt.items():
        if isinstance(node, dict) and "class_type" in node:
            nodes[str(node_id)] = node
    return nodes


def _normalize_workflow_nodes(workflow):
    if not isinstance(workflow, dict):
        return {}
    normalized = {}
    for node in workflow.get("nodes", []):
        if isinstance(node, dict) and "id" in node:
            normalized[str(node["id"])] = node
    return normalized


def _normalize_workflow_links(workflow):
    if not isinstance(workflow, dict):
        return []
    links = []
    for link in workflow.get("links", []):
        if isinstance(link, list) and len(link) >= 5:
            links.append(
                {
                    "origin": str(link[1]),
                    "target": str(link[3]),
                }
            )
    return links


def _bypassed_node_ids(workflow_nodes):
    return {
        node_id
        for node_id, node in workflow_nodes.items()
        if node.get("mode") == 4 or node.get("flags", {}).get("bypassed") is True
    }


def _extract_prompt(nodes):
    linked_prompt_sources = set()
    for node in nodes.values():
        inputs = node.get("inputs", {})
        prompt_input = inputs.get("prompt")
        if isinstance(prompt_input, list) and prompt_input:
            linked_prompt_sources.add(str(prompt_input[0]))

    for node_id in linked_prompt_sources:
        node = nodes.get(node_id)
        if node and node.get("class_type") == "PrimitiveStringMultiline":
            value = node.get("inputs", {}).get("value")
            if isinstance(value, str) and value.strip():
                return value.strip()

    for node in nodes.values():
        inputs = node.get("inputs", {})
        for key in ("value", "prompt", "text", "positive"):
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _extract_seed(nodes):
    seed_noise = None
    seed = None
    for node in nodes.values():
        inputs = node.get("inputs", {})
        if seed_noise is None and "seed_noise" in inputs:
            seed_noise = inputs.get("seed_noise")
        if seed is None and "seed" in inputs and not isinstance(inputs.get("seed"), list):
            seed = inputs.get("seed")
        if seed is None and node.get("class_type", "").lower().startswith("seed"):
            changed = node.get("is_changed")
            if isinstance(changed, list) and changed:
                seed = changed[0]
    return seed_noise, seed


def _extract_unet_name(nodes):
    for node in nodes.values():
        inputs = node.get("inputs", {})
        unet_name = inputs.get("unet_name")
        if unet_name is not None and not isinstance(unet_name, list):
            return str(unet_name)
        if node.get("class_type") == "UNETLoader":
            widgets_values = node.get("widgets_values")
            if isinstance(widgets_values, list) and widgets_values:
                return str(widgets_values[0])
    return None


def _extract_loras(nodes):
    loras = []
    for node_id, node in nodes.items():
        if node.get("class_type") != "LoraLoaderModelOnly":
            continue
        inputs = node.get("inputs", {})
        lora_name = inputs.get("lora_name")
        if lora_name is None and isinstance(node.get("widgets_values"), list) and node["widgets_values"]:
            lora_name = node["widgets_values"][0]
        if not lora_name or _is_blacklisted_lora(str(lora_name)):
            continue
        strength = inputs.get("strength_model")
        if strength is None and isinstance(node.get("widgets_values"), list) and len(node["widgets_values"]) > 1:
            strength = node["widgets_values"][1]
        loras.append(
            {
                "node_id": node_id,
                "lora_name": str(lora_name),
                "strength_model": strength,
            }
        )
    return loras


def _is_blacklisted_lora(name):
    lowered = name.lower().replace("/", "\\")
    basename = lowered.rsplit("\\", 1)[-1]
    if basename in LORA_BLACKLIST_EXACT:
        return True
    return any(token in lowered for token in LORA_BLACKLIST_CONTAINS)


def _extract_used_images(prompt_nodes, workflow_nodes, workflow_links, bypassed_ids):
    active_sinks = _active_image_sinks(prompt_nodes, workflow_nodes, bypassed_ids)
    active_graph = _active_graph_edges(workflow_links, bypassed_ids)
    images = []
    for node_id, node in prompt_nodes.items():
        if node.get("class_type") != "LoadImage":
            continue
        image_name = node.get("inputs", {}).get("image")
        if not image_name:
            continue
        if not workflow_nodes:
            images.append(str(image_name))
            continue
        if _has_active_path(node_id, active_sinks, active_graph):
            images.append(str(image_name))
    return sorted(set(images))


def _active_image_sinks(prompt_nodes, workflow_nodes, bypassed_ids):
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


def _active_graph_edges(workflow_links, bypassed_ids):
    graph = {}
    for link in workflow_links:
        origin = link["origin"]
        target = link["target"]
        if origin in bypassed_ids or target in bypassed_ids:
            continue
        graph.setdefault(origin, set()).add(target)
    return graph


def _has_active_path(start_id, sink_ids, graph):
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
