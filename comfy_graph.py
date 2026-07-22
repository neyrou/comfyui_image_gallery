from collections import deque
from dataclasses import dataclass


QWEN_CONFIG_TYPE = "QwenEditConfigPreparer"


@dataclass
class WorkflowNodeRef:
    locator: str
    node: dict
    graph: dict
    outer_id: str | None = None
    definition: dict | None = None
    bypassed: bool = False

    @property
    def node_type(self):
        return str(self.node.get("type") or "")

    @property
    def in_subgraph(self):
        return self.outer_id is not None


@dataclass
class WorkflowEdge:
    origin: str
    origin_slot: int
    target: str
    target_slot: int
    input_name: str | None
    link_id: int | str | None = None


class WorkflowIndex:
    def __init__(self, workflow):
        self.workflow = workflow if isinstance(workflow, dict) else {}
        self.nodes: dict[str, WorkflowNodeRef] = {}
        self.edges: list[WorkflowEdge] = []
        self.root_nodes = {
            str(node["id"]): node
            for node in self.workflow.get("nodes", [])
            if isinstance(node, dict) and "id" in node
        }
        definitions = self.workflow.get("definitions") or {}
        self.definitions = {
            str(item["id"]): item
            for item in definitions.get("subgraphs", [])
            if isinstance(item, dict) and item.get("id")
        }
        self._build()

    def _build(self):
        for node_id, node in self.root_nodes.items():
            definition = self.definitions.get(str(node.get("type")))
            if definition:
                for internal in definition.get("nodes", []):
                    if not isinstance(internal, dict) or "id" not in internal:
                        continue
                    locator = f"{node_id}:{internal['id']}"
                    self.nodes[locator] = WorkflowNodeRef(
                        locator,
                        internal,
                        definition,
                        outer_id=node_id,
                        definition=definition,
                        bypassed=is_bypassed(node) or is_bypassed(internal),
                    )
                self._add_internal_edges(node_id, definition)
            else:
                self.nodes[node_id] = WorkflowNodeRef(
                    node_id,
                    node,
                    self.workflow,
                    bypassed=is_bypassed(node),
                )

        for raw in self.workflow.get("links", []):
            parsed = parse_link(raw)
            if not parsed:
                continue
            link_id, origin_id, origin_slot, target_id, target_slot, _link_type = parsed
            origins = self._resolve_root_origin(str(origin_id), int(origin_slot))
            targets = self._resolve_root_target(str(target_id), int(target_slot))
            for origin_locator, resolved_origin_slot in origins:
                for target_locator, resolved_target_slot in targets:
                    self.edges.append(
                        WorkflowEdge(
                            origin_locator,
                            resolved_origin_slot,
                            target_locator,
                            resolved_target_slot,
                            self.input_name(target_locator, resolved_target_slot),
                            link_id,
                        )
                    )

    def _add_internal_edges(self, outer_id, definition):
        for raw in definition.get("links", []):
            parsed = parse_link(raw)
            if not parsed:
                continue
            link_id, origin_id, origin_slot, target_id, target_slot, _link_type = parsed
            if int(origin_id) < 0 or int(target_id) < 0:
                continue
            origin = f"{outer_id}:{origin_id}"
            target = f"{outer_id}:{target_id}"
            self.edges.append(
                WorkflowEdge(
                    origin,
                    int(origin_slot),
                    target,
                    int(target_slot),
                    self.input_name(target, int(target_slot)),
                    link_id,
                )
            )

    def _resolve_root_origin(self, node_id, slot):
        node = self.root_nodes.get(node_id)
        definition = self.definitions.get(str((node or {}).get("type")))
        if not definition:
            return [(node_id, slot)]
        output = (definition.get("outputs") or [])[slot] if slot < len(definition.get("outputs") or []) else {}
        link_ids = set(output.get("linkIds") or [])
        resolved = []
        for raw in definition.get("links", []):
            parsed = parse_link(raw)
            if not parsed:
                continue
            link_id, origin_id, origin_slot, target_id, target_slot, _ = parsed
            if int(target_id) == -20 and int(target_slot) == slot and (not link_ids or link_id in link_ids):
                resolved.append((f"{node_id}:{origin_id}", int(origin_slot)))
        return resolved

    def _resolve_root_target(self, node_id, slot):
        node = self.root_nodes.get(node_id)
        definition = self.definitions.get(str((node or {}).get("type")))
        if not definition:
            return [(node_id, slot)]
        input_def = (definition.get("inputs") or [])[slot] if slot < len(definition.get("inputs") or []) else {}
        link_ids = set(input_def.get("linkIds") or [])
        resolved = []
        for raw in definition.get("links", []):
            parsed = parse_link(raw)
            if not parsed:
                continue
            link_id, origin_id, origin_slot, target_id, target_slot, _ = parsed
            if int(origin_id) == -10 and int(origin_slot) == slot and (not link_ids or link_id in link_ids):
                resolved.append((f"{node_id}:{target_id}", int(target_slot)))
        return resolved

    def input_name(self, locator, slot):
        ref = self.nodes.get(str(locator))
        inputs = (ref.node.get("inputs") or []) if ref else []
        if 0 <= int(slot) < len(inputs) and isinstance(inputs[int(slot)], dict):
            return inputs[int(slot)].get("name")
        return None

    def incoming(self, locator, input_name=None):
        return [
            edge
            for edge in self.edges
            if edge.target == str(locator) and (input_name is None or edge.input_name == input_name)
        ]

    def outgoing(self, locator):
        return [edge for edge in self.edges if edge.origin == str(locator)]

    def endpoint(self, locator):
        ref = self.nodes.get(str(locator))
        if not ref:
            return None
        if not ref.in_subgraph:
            return {
                "node_id": str(ref.node["id"]),
                "input_slot": input_slot(ref.node, "configs"),
                "output_slot": output_slot(ref.node, "configs", 0),
            }
        root = self.root_nodes.get(str(ref.outer_id), {})
        return {
            "node_id": str(ref.outer_id),
            "input_slot": input_slot(root, "configs"),
            "output_slot": output_slot(root, "configs", 0),
        }


def is_bypassed(node):
    return bool(node.get("mode") == 4 or (node.get("flags") or {}).get("bypassed") is True)


def parse_link(raw):
    if isinstance(raw, list) and len(raw) >= 5:
        link_type = raw[5] if len(raw) > 5 else None
        return raw[0], raw[1], raw[2], raw[3], raw[4], link_type
    if isinstance(raw, dict):
        return (
            raw.get("id"),
            raw.get("origin_id"),
            raw.get("origin_slot", 0),
            raw.get("target_id"),
            raw.get("target_slot", 0),
            raw.get("type"),
        )
    return None


def input_slot(node, name):
    for index, item in enumerate(node.get("inputs") or []):
        if isinstance(item, dict) and item.get("name") == name:
            return index
    return None


def output_slot(node, name, default=None):
    for index, item in enumerate(node.get("outputs") or []):
        if isinstance(item, dict) and item.get("name") == name:
            return index
    return default


def prompt_nodes(prompt):
    if not isinstance(prompt, dict):
        return {}
    return {
        str(node_id): node
        for node_id, node in prompt.items()
        if isinstance(node, dict) and node.get("class_type")
    }


def prompt_graph_edges(nodes):
    edges = []
    for target_id, node in nodes.items():
        for name, value in (node.get("inputs") or {}).items():
            if isinstance(value, list) and value:
                edges.append(WorkflowEdge(str(value[0]), int(value[1] if len(value) > 1 else 0), target_id, 0, name))
    return edges


def discover_qwen_references(prompt, workflow=None):
    pnodes = prompt_nodes(prompt)
    if isinstance(workflow, dict) and workflow.get("nodes"):
        index = WorkflowIndex(workflow)
        nodes = index.nodes
        edges = index.edges
        qwen_ids = [locator for locator, ref in nodes.items() if ref.node_type == QWEN_CONFIG_TYPE]
        bypassed = {locator for locator, ref in nodes.items() if ref.bypassed}
    else:
        index = None
        nodes = {
            node_id: WorkflowNodeRef(node_id, {"id": node_id, "type": node.get("class_type")}, {})
            for node_id, node in pnodes.items()
        }
        edges = prompt_graph_edges(pnodes)
        qwen_ids = [node_id for node_id, node in pnodes.items() if node.get("class_type") == QWEN_CONFIG_TYPE]
        bypassed = set()

    incoming = {}
    for edge in edges:
        incoming.setdefault(edge.target, []).append(edge)

    references = []
    for qwen_id in qwen_ids:
        image_edges = [edge for edge in incoming.get(qwen_id, []) if edge.input_name == "image"]
        loader_id = nearest_upstream_type(image_edges, incoming, nodes, "LoadImage")
        if not loader_id:
            continue
        prompt_qwen = pnodes.get(qwen_id, {})
        prompt_loader = pnodes.get(loader_id, {})
        qwen_ref = nodes[qwen_id]
        loader_ref = nodes[loader_id]
        image_name = (prompt_loader.get("inputs") or {}).get("image") or workflow_widget_value(loader_ref.node, "image")
        if not image_name:
            continue
        main_value = (prompt_qwen.get("inputs") or {}).get("ref_main_image")
        if main_value is None:
            main_value = workflow_widget_value(qwen_ref.node, "ref_main_image")
        predecessor = nearest_upstream_qwen(
            [edge for edge in incoming.get(qwen_id, []) if edge.input_name == "configs"], incoming, nodes
        )
        references.append(
            {
                "reference_id": qwen_id,
                "config_node_id": qwen_id,
                "image_node_id": loader_id,
                "image_name": str(image_name),
                "enabled": qwen_id not in bypassed,
                "is_main": bool(main_value),
                "in_subgraph": qwen_ref.in_subgraph,
                "predecessor_id": predecessor,
                "endpoint": index.endpoint(qwen_id) if index else None,
            }
        )
    return order_references(references)


def nearest_upstream_type(start_edges, incoming, nodes, node_type):
    queue = deque(edge.origin for edge in start_edges)
    seen = set()
    while queue:
        node_id = queue.popleft()
        if node_id in seen:
            continue
        seen.add(node_id)
        ref = nodes.get(node_id)
        if ref and ref.node_type == node_type:
            return node_id
        queue.extend(edge.origin for edge in incoming.get(node_id, []))
    return None


def nearest_upstream_qwen(start_edges, incoming, nodes):
    return nearest_upstream_type(start_edges, incoming, nodes, QWEN_CONFIG_TYPE)


def order_references(references):
    by_id = {item["reference_id"]: item for item in references}
    children = {}
    for item in references:
        predecessor = item.get("predecessor_id")
        if predecessor in by_id:
            children.setdefault(predecessor, []).append(item["reference_id"])
    starts = [item for item in references if item.get("predecessor_id") not in by_id]
    starts.sort(key=lambda item: (not item.get("is_main"), locator_key(item["reference_id"])))
    ordered = []
    seen = set()
    queue = deque(item["reference_id"] for item in starts)
    while queue:
        reference_id = queue.popleft()
        if reference_id in seen or reference_id not in by_id:
            continue
        seen.add(reference_id)
        ordered.append(by_id[reference_id])
        queue.extend(sorted(children.get(reference_id, []), key=locator_key))
    ordered.extend(sorted((item for item in references if item["reference_id"] not in seen), key=lambda item: locator_key(item["reference_id"])))
    return ordered


def locator_key(value):
    parts = str(value).split(":")
    return tuple((0, int(part)) if part.lstrip("-").isdigit() else (1, part) for part in parts)


def workflow_widget_value(node, widget_name):
    index = 0
    for item in node.get("inputs") or []:
        if not isinstance(item, dict) or "widget" not in item:
            continue
        if item.get("name") == widget_name or (item.get("widget") or {}).get("name") == widget_name:
            values = node.get("widgets_values") or []
            return values[index] if index < len(values) else None
        index += 1
    fallbacks = {
        "LoadImage": {"image": 0},
        QWEN_CONFIG_TYPE: {
            "to_ref": 0,
            "ref_main_image": 1,
            "ref_longest_edge": 2,
            "ref_crop": 3,
            "ref_upscale": 4,
            "to_vl": 5,
            "vl_resize": 6,
            "vl_target_size": 7,
            "vl_crop": 8,
            "vl_upscale": 9,
        },
    }
    fallback = fallbacks.get(str(node.get("type")), {}).get(widget_name)
    values = node.get("widgets_values") or []
    return values[fallback] if fallback is not None and fallback < len(values) else None
