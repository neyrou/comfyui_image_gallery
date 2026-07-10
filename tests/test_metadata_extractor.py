import json
import unittest
from pathlib import Path

from metadata_extractor import extract_from_prompt_json


ROOT = Path(__file__).resolve().parents[1]


def load_prompt_and_workflow_example():
    text = (ROOT / "prompt_exemple.json").read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    prompt, _ = decoder.raw_decode(text)
    workflow_text = text.split("tEXtworkflow ", 1)[1]
    workflow, _ = decoder.raw_decode(workflow_text)
    return prompt, workflow


class MetadataExtractorTests(unittest.TestCase):
    def test_extracts_prompt_loras_seed_and_active_images_from_example(self):
        prompt, workflow = load_prompt_and_workflow_example()

        metadata = extract_from_prompt_json(prompt, workflow)

        self.assertIn("keep same face", metadata.prompt)
        self.assertEqual(metadata.seed, 209282242741374)
        self.assertEqual(
            metadata.loras,
            [
                {
                    "node_id": "45",
                    "lora_name": "qwen-image\\qwen_tinywaist_000004650.safetensors",
                    "strength_model": 0.7,
                }
            ],
        )
        self.assertEqual(metadata.used_images, ["20170819_163242.jpg"])

    def test_ignores_bypassed_nodes_and_lightning_loras(self):
        prompt = {
            "1": {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {"lora_name": "kept.safetensors", "strength_model": 0.5},
            },
            "2": {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {"lora_name": "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-fp32.safetensors", "strength_model": 1},
            },
            "3": {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {"lora_name": "bypassed.safetensors", "strength_model": 1},
            },
        }
        workflow = {
            "nodes": [
                {"id": 1, "type": "LoraLoaderModelOnly", "mode": 0},
                {"id": 2, "type": "LoraLoaderModelOnly", "mode": 0},
                {"id": 3, "type": "LoraLoaderModelOnly", "mode": 4},
            ],
            "links": [],
        }

        metadata = extract_from_prompt_json(prompt, workflow)

        self.assertEqual([lora["lora_name"] for lora in metadata.loras], ["kept.safetensors"])


if __name__ == "__main__":
    unittest.main()
