"""Hermes image_gen provider plugin: local ComfyUI text-to-image backend.

Talks to the ComfyUI HTTP API (default ``127.0.0.1:8188``) and generates with two
local models: Qwen-Image 2.1 (quality / in-image CJK text, ~4-6 min) and Z-Image
Turbo (fast drafts, ~1 min). Auto-starts ComfyUI when it is not running. No API
key, no internet.

Selection: ``model`` kwarg -> ``COMFYUI_IMAGE_MODEL`` -> ``image_gen.comfyui.model``
-> default. Server: ``COMFYUI_DIR`` / ``COMFYUI_PORT`` / ``COMFYUI_PYTHON`` env
vars override defaults."""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO, resolve_aspect_ratio, success_response)
from agent import provider_media
from plugins.image_gen._common import (
    StaticImageGenProvider, error_factory, prompt_required_error, resolve_static_model)

logger = logging.getLogger(__name__)

COMFY_DIR = os.path.expanduser(os.environ.get("COMFYUI_DIR", "~/Github/AI-Infra/ComfyUI"))
COMFY_PORT = os.environ.get("COMFYUI_PORT", "8188")
SERVER = f"http://127.0.0.1:{COMFY_PORT}"
# the gateway interpreter has no torch; boot ComfyUI with the interpreter it is installed for
COMFY_PYTHON = os.environ.get("COMFYUI_PYTHON", "python3")

_MODELS: Dict[str, Dict[str, Any]] = {
    "qwen21": {
        "display": "Qwen-Image 2.1 (local)",
        "speed": "~4-6 min",
        "strengths": "Highest quality; best in-image CJK/English text rendering; posters",
        "price": "local/free",
    },
    "zimage": {
        "display": "Z-Image Turbo (local)",
        "speed": "~1 min",
        "strengths": "Fast drafts and iteration; weak text rendering",
        "price": "local/free",
    },
}
DEFAULT_MODEL = "qwen21"

# aspect alias -> (width, height); multiples of 32 as Qwen 2.1 requires
_SIZES = {"square": (1024, 1024), "landscape": (1344, 768), "portrait": (768, 1344)}

_MODEL_SETTINGS = {
    "qwen21": {"unet": "qwen_image_2.1_int8_convrot.safetensors",
               "steps": 25, "sampler": "euler", "auraflow_shift": None},
    "zimage": {"unet": "z_image_turbo_bf16.safetensors",
               "steps": 8, "sampler": "res_multistep", "auraflow_shift": 3},
}
_CLIP = {"qwen21": "qwen3vl_8b_int8_convrot.safetensors", "zimage": "qwen_3_4b_fp8_mixed.safetensors"}
_CLIP_TYPE = {"qwen21": "qwen_image", "zimage": "lumina2"}
_VAE = {"qwen21": "qwen_image_2.1_vae_bf16.safetensors", "zimage": "z_image_turbo_ae.safetensors"}


def _http_json(path: str, payload: Any = None, timeout: float = 30) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(SERVER + path, data, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


_START_LOCK = threading.Lock()


def _ensure_server(attempt_start: bool = True) -> None:
    try:
        _http_json("/system_stats", timeout=5)
        return
    except Exception:
        if not attempt_start:
            raise RuntimeError(f"ComfyUI not running ({SERVER})")
    logger.info("ComfyUI not running, auto-starting…")
    with _START_LOCK:
        try:  # a concurrent turn may have finished starting it while we waited
            _http_json("/system_stats", timeout=5)
            return
        except Exception:
            pass
        with open(os.path.join(COMFY_DIR, "comfyui-boot.log"), "ab") as log:
            subprocess.Popen([COMFY_PYTHON, "main.py", "--port", COMFY_PORT], cwd=COMFY_DIR,
                             stdout=log, stderr=log, start_new_session=True)
        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(2)
            try:
                _http_json("/system_stats", timeout=5)
                return
            except Exception:
                pass
    raise RuntimeError(f"ComfyUI auto-start failed, see {COMFY_DIR}/comfyui-boot.log")


def _build_workflow(prompt: str, model_id: str, width: int, height: int, seed: int) -> Dict[str, Any]:
    s = _MODEL_SETTINGS[model_id]
    wf: Dict[str, Any] = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": s["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": _CLIP[model_id], "type": _CLIP_TYPE[model_id], "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": _VAE[model_id]}},
    }
    model_src = ["1", 0]
    if s["auraflow_shift"] is not None:
        wf["8"] = {"class_type": "ModelSamplingAuraFlow",
                   "inputs": {"model": model_src, "shift": s["auraflow_shift"]}}
        model_src = ["8", 0]
    if model_id == "qwen21":
        wf["4"] = {"class_type": "TextEncodeQwenImage21",
                   "inputs": {"clip": ["2", 0], "prompt": prompt, "negative_prompt": "",
                              "resolution": min(width, height)}}
        positive, negative = ["4", 0], ["4", 1]
    else:
        wf["4"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}}
        wf["5"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": ""}}
        positive, negative = ["4", 0], ["5", 0]
    wf["9"] = {"class_type": "EmptyLatentImage" if model_id == "qwen21" else "EmptySD3LatentImage",
               "inputs": {"width": width, "height": height, "batch_size": 1}}
    wf["6"] = {"class_type": "KSampler",
               "inputs": {"model": model_src, "positive": positive, "negative": negative,
                          "latent_image": ["9", 0], "seed": seed, "steps": s["steps"], "cfg": 1.0,
                          "sampler_name": s["sampler"], "scheduler": "simple", "denoise": 1.0}}
    wf["10"] = {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}}
    wf["7"] = {"class_type": "SaveImage", "inputs": {"images": ["10", 0], "filename_prefix": "hermes_comfyui"}}
    return wf


def _resolve_model(explicit: Optional[str]) -> str:
    return resolve_static_model(_MODELS, DEFAULT_MODEL, env_var="COMFYUI_IMAGE_MODEL",
                                config_key="comfyui", explicit=explicit)[0]


class ComfyuiImageGenProvider(StaticImageGenProvider):
    """Local ComfyUI text-to-image (Qwen-Image 2.1 / Z-Image Turbo)."""

    provider_id = "comfyui"
    label = "ComfyUI (local)"
    models = _MODELS
    default_model_id = DEFAULT_MODEL
    setup = {"name": "ComfyUI (local)", "badge": "local",
             "tag": "Local ComfyUI text-to-image, no key, no internet", "env_vars": []}

    def get_setup_schema(self) -> Dict[str, Any]:
        # base StaticImageGenProvider routes through api_key_setup_schema, which is for
        # single-env-var auth; this dict is already in the ProviderBase schema shape
        return dict(self.setup)

    def capabilities(self) -> Dict[str, Any]:
        return {"modalities": ["text"], "max_reference_images": 0}

    def generate(
        self, prompt: str, aspect_ratio: str = DEFAULT_ASPECT_RATIO, *,
        image_url: Optional[str] = None, reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        if not prompt:
            return prompt_required_error("comfyui", aspect)
        model_id = _resolve_model(kwargs.get("model"))
        width, height = _SIZES.get(aspect, _SIZES["square"])
        seed = random.randint(0, 2**48 - 1)
        t0 = time.time()
        fail = error_factory("comfyui", aspect, model=model_id, prompt=prompt)
        try:
            _ensure_server()
            resp = _http_json("/prompt", {"prompt": _build_workflow(
                prompt, model_id, width, height, seed)})
            pid = resp.get("prompt_id")
            if not pid:
                return fail(f"ComfyUI submit failed: {resp}", "api_error")
            deadline = time.time() + 900
            misses = 0
            while time.time() < deadline:
                time.sleep(5)
                try:
                    hist = _http_json(f"/history/{pid}")
                except Exception:  # transient blips must not kill a multi-minute job
                    misses += 1
                    if misses >= 3:
                        raise
                    continue
                misses = 0
                if pid not in hist:
                    continue
                entry = hist[pid]
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    detail = ""
                    for msg in status.get("messages", []):
                        if msg[0] == "execution_error":
                            detail = f"{msg[1].get('node_type')}: {msg[1].get('exception_message')}"
                    return fail(f"ComfyUI execution failed {detail}".strip(), "api_error")
                for node_out in entry.get("outputs", {}).values():
                    for img in node_out.get("images", []):
                        url = (f"/view?filename={img['filename']}"
                               f"&subfolder={img['subfolder']}&type={img['type']}")
                        raw = urllib.request.urlopen(SERVER + url, timeout=120).read()
                        path = provider_media.save_bytes("images", raw, prefix="comfyui",
                                                         extension="png")
                        return success_response(
                            image=str(path), model=model_id, prompt=prompt, aspect_ratio=aspect,
                            provider="comfyui",
                            extra={"seed": seed, "seconds": round(time.time() - t0),
                                   "size": f"{width}x{height}"})
                return fail("ComfyUI finished without an output image", "empty_response")
            return fail("ComfyUI generation timed out (>900s)", "timeout")
        except Exception as exc:
            logger.debug("ComfyUI generation failed", exc_info=True)
            return fail(f"ComfyUI generation failed: {exc}", "api_error")


def register(ctx) -> None:
    """Plugin entry point — wire ``ComfyuiImageGenProvider`` into the registry."""
    ctx.register_image_gen_provider(ComfyuiImageGenProvider())
