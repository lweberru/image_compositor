"""Image Compositor integration."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any
import base64
import json
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import aiohttp_client

from .const import DOMAIN
SERVICE_COMPOSE = "compose"
SERVICE_EXISTS = "file_exists"
SERVICE_CLEAR_CACHE = "clear_cache"
SERVICE_ENSURE_ASSETS = "ensure_assets"
SERVICE_GENERATE_MASKS = "generate_masks"

OUTPUT_DIR_DEFAULT = "www/image_compositor"
ASSET_DIR_DEFAULT = "www/image_compositor/assets"
MASK_DIR_DEFAULT = "www/image_compositor/masks"

DEFAULT_MASK_TARGETS: list[dict[str, str]] = [
    {"name": "door_front_left_open", "description": "front left door open"},
    {"name": "door_front_right_open", "description": "front right door open"},
    {"name": "door_rear_left_open", "description": "rear left door open"},
    {"name": "door_rear_right_open", "description": "rear right door open"},
    {"name": "window_front_left_open", "description": "front left window open"},
    {"name": "window_front_right_open", "description": "front right window open"},
    {"name": "window_rear_left_open", "description": "rear left window open"},
    {"name": "window_rear_right_open", "description": "rear right window open"},
    {"name": "hood_open", "description": "hood open"},
    {"name": "trunk_open", "description": "trunk or tailgate open"},
    {"name": "sunroof_open", "description": "sunroof open"},
    {"name": "sunroof_tilt", "description": "sunroof tilted"},
]

COMPOSE_SCHEMA = vol.Schema(
    {
        vol.Required("base_image"): str,
        vol.Optional("layers", default=[]): list,
        vol.Optional("output_name"): str,
        vol.Optional("cache_key"): str,
        vol.Optional("format", default="png"): vol.In(["png", "jpg", "jpeg"]),
        vol.Optional("output_path"): str,
    }
)

EXISTS_SCHEMA = vol.Schema(
    {
        vol.Optional("path"): str,
        vol.Optional("filename"): str,
        vol.Optional("local_url"): str,
    }
)

CLEAR_CACHE_SCHEMA = vol.Schema(
    {
        vol.Optional("prefix"): str,
    }
)

ENSURE_ASSETS_SCHEMA = vol.Schema(
    {
        vol.Optional("output_path"): str,
        vol.Optional("task_name_prefix"): str,
        vol.Optional("provider", default={}): dict,
        vol.Optional("force", default=False): bool,
        vol.Required("assets"): list,
    }
)

GENERATE_MASKS_SCHEMA = vol.Schema(
    {
        vol.Optional("output_path"): str,
        vol.Optional("asset_path"): str,
        vol.Optional("task_name_prefix"): str,
        vol.Optional("provider", default={}): dict,
        vol.Optional("base_image"): str,
        vol.Optional("base_prompt"): str,
        vol.Optional("base_view"): str,
        vol.Optional("threshold", default=12): int,
        vol.Optional("targets"): list,
    }
)


def _normalize_output_path(raw_path: str | None) -> str:
    path = (raw_path or OUTPUT_DIR_DEFAULT).lstrip("/").rstrip("/")
    if not path.startswith("www/"):
        path = f"www/{path}"
    if ".." in Path(path).parts:
        raise vol.Invalid("Invalid path.")
    return path


def _normalize_local_url(raw_url: str | None) -> str | None:
    if not raw_url:
        return None
    trimmed = str(raw_url).strip()
    if not trimmed:
        return None
    return trimmed.split("?", 1)[0]


def _resolve_local_path(local_url: str) -> str:
    if local_url.startswith("http://") or local_url.startswith("https://"):
        parsed = urlparse(local_url)
        local_path = parsed.path
    else:
        local_path = local_url

    if local_path.startswith("/local/"):
        rel = local_path[len("/local/") :]
    elif local_path.startswith("local/"):
        rel = local_path[len("local/") :]
    else:
        raise vol.Invalid("local_url must start with /local/")

    if ".." in Path(rel).parts:
        raise vol.Invalid("Invalid local_url path.")

    rel = rel.strip("/")
    return f"www/{rel}" if rel else "www"


def _safe_filename(name: str | None, default_ext: str) -> str:
    if not name:
        return f"{hashlib.sha256(os.urandom(16)).hexdigest()[:12]}.{default_ext}"
    base = os.path.basename(name)
    if "." not in base:
        return f"{base}.{default_ext}"
    return base


def _local_url_from_path(output_path: str, filename: str) -> str:
    local_path = output_path[4:] if output_path.startswith("www/") else output_path
    local_path = local_path.strip("/")
    return f"/local/{local_path}/{filename}" if local_path else f"/local/{filename}"


async def _try_fetch_optional_image_bytes(hass: HomeAssistant, source: str | None) -> bytes | None:
    if not source:
        return None
    try:
        return await _fetch_image_bytes(hass, str(source))
    except Exception:  # noqa: BLE001
        return None


async def _fetch_image_bytes(hass: HomeAssistant, source: str) -> bytes:
    if source.startswith("/local/") or source.startswith("local/"):
        local_path = _resolve_local_path(source)
        full_path = Path(hass.config.path(local_path))
        return full_path.read_bytes()

    session = aiohttp_client.async_get_clientsession(hass)
    async with session.get(source) as resp:
        resp.raise_for_status()
        return await resp.read()


def _apply_alpha_mask(image_bytes: bytes, mask_bytes: bytes) -> bytes:
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(image_bytes)).convert("RGBA")
    mask = Image.open(BytesIO(mask_bytes)).convert("L")
    img.putalpha(mask)

    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _derive_overlay_from_base(base_bytes: bytes, edited_bytes: bytes, threshold: int = 12) -> bytes:
    from io import BytesIO

    from PIL import Image, ImageChops

    base_img = Image.open(BytesIO(base_bytes)).convert("RGBA")
    edited_img = Image.open(BytesIO(edited_bytes)).convert("RGBA")

    diff = ImageChops.difference(base_img, edited_img).convert("L")
    alpha = diff.point(lambda p: 255 if p > threshold else 0)

    overlay = edited_img.copy()
    overlay.putalpha(alpha)

    out = BytesIO()
    overlay.save(out, format="PNG")
    return out.getvalue()


def _derive_binary_mask_from_base(base_bytes: bytes, edited_bytes: bytes, threshold: int = 12) -> bytes:
    from io import BytesIO

    from PIL import Image, ImageChops, ImageFilter

    base_img = Image.open(BytesIO(base_bytes)).convert("RGBA")
    edited_img = Image.open(BytesIO(edited_bytes)).convert("RGBA")

    if edited_img.size != base_img.size:
        edited_img = edited_img.resize(base_img.size)

    diff = ImageChops.difference(base_img, edited_img).convert("L")
    mask = diff.point(lambda p: 255 if p > threshold else 0)
    mask = mask.filter(ImageFilter.MedianFilter(3))
    mask = mask.filter(ImageFilter.MaxFilter(3))
    mask = mask.filter(ImageFilter.MinFilter(3))
    mask = mask.point(lambda p: 255 if p > 127 else 0)

    out = BytesIO()
    mask.save(out, format="PNG")
    return out.getvalue()


async def _openai_edit_image(
    hass: HomeAssistant,
    api_key: str,
    base_bytes: bytes,
    mask_bytes: bytes | None,
    prompt: str,
    model: str,
    size: str,
) -> bytes:
    session = aiohttp_client.async_get_clientsession(hass)
    url = "https://api.openai.com/v1/images/edits"

    data = aiohttp_client.FormData()
    data.add_field("model", model)
    data.add_field("prompt", prompt)
    data.add_field("size", size)
    data.add_field("response_format", "b64_json")
    data.add_field("image", base_bytes, filename="base.png", content_type="image/png")
    if mask_bytes:
        data.add_field("mask", mask_bytes, filename="mask.png", content_type="image/png")

    async with session.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        data=data,
    ) as resp:
        resp.raise_for_status()
        payload = await resp.json()
        data_items = payload.get("data") or []
        if not data_items:
            raise ValueError("OpenAI response missing data")
        b64 = data_items[0].get("b64_json")
        if not b64:
            raise ValueError("OpenAI response missing b64_json")
        return base64.b64decode(b64)


async def _openai_generate_image(
    hass: HomeAssistant,
    api_key: str,
    prompt: str,
    model: str,
    size: str,
) -> bytes:
    session = aiohttp_client.async_get_clientsession(hass)
    url = "https://api.openai.com/v1/images/generations"

    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "response_format": "b64_json",
    }

    async with session.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        data_items = data.get("data") or []
        if not data_items:
            raise ValueError("OpenAI response missing data")
        b64 = data_items[0].get("b64_json")
        if not b64:
            raise ValueError("OpenAI response missing b64_json")
        return base64.b64decode(b64)


def _extract_gemini_image_bytes(payload: dict[str, Any]) -> bytes:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise ValueError("Gemini response missing candidates")

    for candidate in candidates:
        content = candidate.get("content") or {}
        parts = content.get("parts") or []
        for part in parts:
            inline_data = part.get("inlineData") or part.get("inline_data") or {}
            b64 = inline_data.get("data")
            if b64:
                return base64.b64decode(b64)

    raise ValueError("Gemini response missing inline image data")


async def _gemini_list_candidate_models(hass: HomeAssistant, api_key: str) -> list[str]:
    session = aiohttp_client.async_get_clientsession(hass)
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"

    try:
        async with session.get(url) as resp:
            if resp.status >= 400:
                return []
            data = await resp.json()
    except Exception:  # noqa: BLE001
        return []

    models = data.get("models") or []
    result: list[str] = []
    for item in models:
        name = str(item.get("name") or "")
        if name.startswith("models/"):
            name = name[len("models/") :]
        methods = item.get("supportedGenerationMethods") or []
        if not name or "generateContent" not in methods:
            continue
        lower_name = name.lower()
        if "image" in lower_name or "imagen" in lower_name:
            result.append(name)

    return result


async def _gemini_generate_with_fallback(
    hass: HomeAssistant,
    api_key: str,
    model: str,
    payload: dict[str, Any],
) -> bytes:
    session = aiohttp_client.async_get_clientsession(hass)

    known_fallbacks = [
        "gemini-2.0-flash-preview-image-generation",
        "gemini-2.5-flash-image-preview",
    ]
    discovered = await _gemini_list_candidate_models(hass, api_key)

    mapped_model = model
    if model.lower().startswith("imagen-"):
        mapped_model = "gemini-2.0-flash-preview-image-generation"

    ordered_candidates = [mapped_model, *known_fallbacks, *discovered]
    deduped: list[str] = []
    for candidate in ordered_candidates:
        if candidate and candidate not in deduped:
            deduped.append(candidate)
    models_to_try = deduped[:8]

    errors: list[str] = []

    for model_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        async with session.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
        ) as resp:
            if resp.status >= 400:
                error_text = await resp.text()
                errors.append(f"{model_name}: {resp.status}")
                unsupported = (
                    "not found" in error_text.lower()
                    or "not supported for generatecontent" in error_text.lower()
                    or "unsupported" in error_text.lower()
                )
                if unsupported:
                    continue
                raise ValueError(f"Gemini API error {resp.status}: {error_text}")

            data = await resp.json()
            try:
                return _extract_gemini_image_bytes(data)
            except ValueError as err:
                errors.append(f"{model_name}: {err}")
                continue

    joined_errors = " | ".join(errors[:6]) if errors else "no compatible Gemini image model found"
    raise ValueError(f"Gemini image generation failed. Tried models: {joined_errors}")


async def _gemini_generate_image(
    hass: HomeAssistant,
    api_key: str,
    prompt: str,
    model: str,
    provider_service_data: dict[str, Any] | None = None,
) -> bytes:
    payload: dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    if provider_service_data:
        generation_config = payload.get("generationConfig") or {}
        provided_generation_config = provider_service_data.get("generationConfig")
        if isinstance(provided_generation_config, dict):
            generation_config = {
                **generation_config,
                **provided_generation_config,
            }
            payload["generationConfig"] = generation_config
        for key, value in provider_service_data.items():
            if key == "generationConfig":
                continue
            payload[key] = value
    return await _gemini_generate_with_fallback(hass, api_key, model, payload)


async def _gemini_edit_image(
    hass: HomeAssistant,
    api_key: str,
    base_bytes: bytes,
    mask_bytes: bytes | None,
    prompt: str,
    model: str,
    provider_service_data: dict[str, Any] | None = None,
) -> bytes:
    parts: list[dict[str, Any]] = [
        {
            "text": (
                f"Edit this image according to the instruction: {prompt}. "
                "Return an image result."
            )
        },
        {
            "inline_data": {
                "mime_type": "image/png",
                "data": base64.b64encode(base_bytes).decode("utf-8"),
            }
        },
    ]

    if mask_bytes:
        parts.insert(
            1,
            {
                "text": (
                    "Use the following mask image to constrain the edited region "
                    "(white = editable, black = preserve)."
                )
            },
        )
        parts.insert(
            2,
            {
                "inline_data": {
                    "mime_type": "image/png",
                    "data": base64.b64encode(mask_bytes).decode("utf-8"),
                }
            },
        )

    payload: dict[str, Any] = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    if provider_service_data:
        generation_config = payload.get("generationConfig") or {}
        provided_generation_config = provider_service_data.get("generationConfig")
        if isinstance(provided_generation_config, dict):
            generation_config = {
                **generation_config,
                **provided_generation_config,
            }
            payload["generationConfig"] = generation_config
        for key, value in provider_service_data.items():
            if key == "generationConfig":
                continue
            payload[key] = value
    return await _gemini_generate_with_fallback(hass, api_key, model, payload)


def _extract_ai_task_urls(payload: Any) -> list[str]:
    if not payload:
        return []
    candidates = payload.get("images") or payload.get("data") or payload.get("results") or payload.get("result") or payload
    items = candidates if isinstance(candidates, list) else [candidates]
    urls: list[str] = []
    for item in items:
        if not item:
            continue
        if isinstance(item, str):
            urls.append(item)
            continue
        url = (
            item.get("url")
            or item.get("image_url")
            or item.get("media_url")
            or item.get("content_url")
            or (item.get("image") or {}).get("url")
            or item.get("local_url")
            or item.get("local_path")
        )
        if url:
            urls.append(str(url))
    return urls


def _apply_layers(base_bytes: bytes, layers: list[dict[str, Any]], output_format: str) -> bytes:
    from io import BytesIO

    from PIL import Image

    base_img = Image.open(BytesIO(base_bytes)).convert("RGBA")

    for layer in layers:
        image_source = layer.get("image")
        if not image_source:
            continue
        layer_bytes = layer["_bytes"]
        overlay = Image.open(BytesIO(layer_bytes)).convert("RGBA")

        scale = float(layer.get("scale", 1.0))
        if scale != 1.0:
            w, h = overlay.size
            overlay = overlay.resize((int(w * scale), int(h * scale)))

        opacity = float(layer.get("opacity", 1.0))
        if opacity < 1.0:
            alpha = overlay.getchannel("A")
            alpha = alpha.point(lambda p: int(p * opacity))
            overlay.putalpha(alpha)

        x = int(layer.get("x", 0))
        y = int(layer.get("y", 0))
        base_img.alpha_composite(overlay, (x, y))

    if output_format in ("jpg", "jpeg"):
        base_img = base_img.convert("RGB")

    out = BytesIO()
    base_img.save(out, format="PNG" if output_format == "png" else "JPEG")
    return out.getvalue()


async def _async_register_service(hass: HomeAssistant) -> None:
    async def _handle_compose(call: ServiceCall) -> dict[str, Any]:
        base_image = str(call.data["base_image"]).strip()
        layers = call.data.get("layers") or []
        cache_key = call.data.get("cache_key")
        output_format = str(call.data.get("format") or "png").lower()
        output_name = call.data.get("output_name")

        output_path = _normalize_output_path(call.data.get("output_path") or OUTPUT_DIR_DEFAULT)
        output_dir = Path(hass.config.path(output_path))
        output_dir.mkdir(parents=True, exist_ok=True)

        if cache_key:
            cached_name = _safe_filename(cache_key, output_format)
            cached_path = output_dir / cached_name
            if cached_path.exists():
                return {"local_url": _local_url_from_path(output_path, cached_name), "filename": str(cached_path)}

        base_bytes = await _fetch_image_bytes(hass, base_image)

        resolved_layers: list[dict[str, Any]] = []
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            image_source = layer.get("image")
            if not image_source:
                continue
            layer_bytes = await _fetch_image_bytes(hass, str(image_source))
            layer_copy = dict(layer)
            layer_copy["_bytes"] = layer_bytes
            resolved_layers.append(layer_copy)

        composed_bytes = await hass.async_add_executor_job(
            _apply_layers, base_bytes, resolved_layers, output_format
        )

        filename = _safe_filename(output_name, output_format)
        full_path = output_dir / filename
        full_path.write_bytes(composed_bytes)

        return {"local_url": _local_url_from_path(output_path, filename), "filename": str(full_path)}

    async def _handle_exists(call: ServiceCall) -> dict[str, Any]:
        local_url = _normalize_local_url(call.data.get("local_url"))
        path = call.data.get("path")
        filename = call.data.get("filename")

        if local_url:
            normalized_path = _resolve_local_path(local_url)
            full_path = Path(hass.config.path(normalized_path))
        else:
            if not path or not filename:
                raise vol.Invalid("local_url or path+filename required")
            normalized_path = _normalize_output_path(path)
            base_name = os.path.basename(filename)
            if base_name in {"", ".", ".."}:
                raise vol.Invalid("Invalid filename")
            full_path = Path(hass.config.path(normalized_path)) / base_name

        return {"exists": full_path.exists() and full_path.is_file()}

    async def _handle_clear_cache(call: ServiceCall) -> dict[str, Any]:
        prefix = call.data.get("prefix") or ""
        output_path = _normalize_output_path(None)
        output_dir = Path(hass.config.path(output_path))
        if not output_dir.exists():
            return {"deleted": 0}

        deleted = 0
        for item in output_dir.iterdir():
            if not item.is_file():
                continue
            if prefix and not item.name.startswith(prefix):
                continue
            item.unlink(missing_ok=True)
            deleted += 1

        return {"deleted": deleted}

    async def _handle_generate_masks(call: ServiceCall) -> dict[str, Any]:
        output_path = _normalize_output_path(call.data.get("output_path") or MASK_DIR_DEFAULT)
        output_dir = Path(hass.config.path(output_path))
        output_dir.mkdir(parents=True, exist_ok=True)

        asset_path = _normalize_output_path(call.data.get("asset_path") or ASSET_DIR_DEFAULT)
        asset_dir = Path(hass.config.path(asset_path))

        provider = call.data.get("provider") or {}
        provider_type = str(provider.get("type") or "gemini").lower()
        if provider_type not in {"gemini", "openai"}:
            return {
                "masks": [],
                "error": "generate_masks supports only gemini/openai providers",
            }

        base_image = call.data.get("base_image")
        if not base_image:
            candidates = sorted(
                asset_dir.glob("*_base.png"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                base_image = _local_url_from_path(asset_path, candidates[0].name)

        if not base_image:
            return {
                "masks": [],
                "error": "base_image missing and no *_base.png found in asset_path",
            }

        try:
            base_bytes = await _fetch_image_bytes(hass, str(base_image))
        except Exception as err:  # noqa: BLE001
            return {"masks": [], "error": f"failed to read base_image: {err}"}

        base_view = str(call.data.get("base_view") or "front 3/4 view").strip()
        base_prompt = str(
            call.data.get("base_prompt") or f"Same car and view ({base_view}), clean background"
        ).strip()
        threshold = int(call.data.get("threshold") or 16)

        raw_targets = call.data.get("targets") or DEFAULT_MASK_TARGETS
        targets: list[dict[str, str]] = []
        for item in raw_targets:
            if isinstance(item, str):
                targets.append({"name": item, "description": item.replace("_", " ")})
                continue
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("id") or "").strip()
            if not name:
                continue
            targets.append(
                {
                    "name": name,
                    "description": str(item.get("description") or name.replace("_", " ")),
                    "prompt": str(item.get("prompt") or "").strip(),
                    "filename": str(item.get("filename") or "").strip(),
                }
            )

        api_key = provider.get("api_key") or provider.get("token")
        if not api_key:
            return {"masks": [], "error": f"{provider_type} api_key missing"}

        provider_service_data = provider.get("service_data")
        if provider_service_data and not isinstance(provider_service_data, dict):
            try:
                provider_service_data = json.loads(str(provider_service_data))
            except Exception:  # noqa: BLE001
                provider_service_data = None

        if provider_type == "gemini":
            if not isinstance(provider_service_data, dict):
                provider_service_data = {}
            generation_config = provider_service_data.get("generationConfig")
            if not isinstance(generation_config, dict):
                generation_config = {}
            generation_config.setdefault("temperature", 0.1)
            provider_service_data["generationConfig"] = generation_config

        model = str(
            provider.get("model")
            or ("gpt-image-1" if provider_type == "openai" else "gemini-2.0-flash-preview-image-generation")
        )
        openai_size = str(provider.get("size") or "1024x1024")

        task_name_prefix = str(call.data.get("task_name_prefix") or "Image Compositor Masks")
        results: list[dict[str, Any]] = []

        for target in targets:
            name = target.get("name") or "mask"
            description = target.get("description") or name.replace("_", " ")
            prompt = target.get("prompt") or (
                f"{base_prompt}. ONLY change this part: {description}. "
                "Keep identical camera angle, vehicle scale, geometry, paint, reflections, background and lighting. "
                "Do not alter any other region. Return a full-frame edited image."
            )
            filename = _safe_filename(target.get("filename") or name, "png")
            full_path = output_dir / filename

            edited_bytes: bytes | None = None
            error: str | None = None
            try:
                if provider_type == "openai":
                    edited_bytes = await _openai_edit_image(
                        hass,
                        str(api_key),
                        base_bytes,
                        None,
                        prompt,
                        model,
                        openai_size,
                    )
                else:
                    edited_bytes = await _gemini_edit_image(
                        hass,
                        str(api_key),
                        base_bytes,
                        None,
                        prompt,
                        model,
                        provider_service_data=provider_service_data,
                    )
            except Exception as err:  # noqa: BLE001
                error = str(err)

            if not edited_bytes:
                results.append({"name": name, "error": error or "no image returned"})
                continue

            try:
                mask_bytes = await hass.async_add_executor_job(
                    _derive_binary_mask_from_base,
                    base_bytes,
                    edited_bytes,
                    threshold,
                )
                full_path.write_bytes(mask_bytes)
                results.append(
                    {
                        "name": name,
                        "local_url": _local_url_from_path(output_path, filename),
                        "filename": str(full_path),
                    }
                )
            except Exception as err:  # noqa: BLE001
                results.append({"name": name, "error": str(err)})

        return {
            "base_image": str(base_image),
            "provider": provider_type,
            "task_name_prefix": task_name_prefix,
            "masks": results,
        }

    async def _handle_ensure_assets(call: ServiceCall) -> dict[str, Any]:
        output_path = _normalize_output_path(call.data.get("output_path") or ASSET_DIR_DEFAULT)
        output_dir = Path(hass.config.path(output_path))
        output_dir.mkdir(parents=True, exist_ok=True)

        provider = call.data.get("provider") or {}
        force_generation = bool(call.data.get("force"))
        task_name_prefix = str(call.data.get("task_name_prefix") or "Image Compositor")
        assets = call.data.get("assets") or []

        results: list[dict[str, Any]] = []

        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or asset.get("id") or "asset").strip()
            prompt = str(asset.get("prompt") or "").strip()
            if not prompt:
                continue
            output_format = str(asset.get("format") or "png").lower()
            filename = _safe_filename(asset.get("filename") or name, output_format)
            full_path = output_dir / filename

            if full_path.exists() and not force_generation:
                results.append(
                    {
                        "name": name,
                        "local_url": _local_url_from_path(output_path, filename),
                        "filename": str(full_path),
                        "cached": True,
                    }
                )
                continue

            provider_type = str(provider.get("type") or "ai_task").lower()

            base_ref = asset.get("base_ref")
            base_image = asset.get("base_image")
            base_bytes: bytes | None = None
            if base_ref:
                base_filename = _safe_filename(str(base_ref), "png")
                base_path = output_dir / base_filename
                if base_path.exists():
                    base_bytes = base_path.read_bytes()
            if not base_bytes and base_image:
                try:
                    base_bytes = await _fetch_image_bytes(hass, str(base_image))
                except Exception:
                    base_bytes = None

            attempts = int(asset.get("attempts") or 2)
            last_error: str | None = None
            image_bytes: bytes | None = None

            if provider_type == "openai":
                api_key = provider.get("api_key") or provider.get("token")
                model = str(provider.get("model") or "gpt-image-1")
                size = str(provider.get("size") or "1024x1024")
                mask_url = asset.get("mask_url") or asset.get("mask")

                if not api_key:
                    last_error = "openai api_key missing"
                else:
                    for _ in range(max(1, attempts)):
                        try:
                            if base_bytes:
                                mask_bytes = await _try_fetch_optional_image_bytes(hass, str(mask_url) if mask_url else None)
                                image_bytes = await _openai_edit_image(
                                    hass, api_key, base_bytes, mask_bytes, prompt, model, size
                                )
                            else:
                                image_bytes = await _openai_generate_image(
                                    hass, api_key, prompt, model, size
                                )
                            if image_bytes:
                                break
                        except Exception as err:  # noqa: BLE001
                            last_error = str(err)

            elif provider_type == "gemini":
                api_key = provider.get("api_key") or provider.get("token")
                model = str(provider.get("model") or "gemini-2.0-flash-preview-image-generation")
                mask_url = asset.get("mask_url") or asset.get("mask")

                provider_service_data = provider.get("service_data")
                if provider_service_data and not isinstance(provider_service_data, dict):
                    try:
                        provider_service_data = json.loads(str(provider_service_data))
                    except Exception:  # noqa: BLE001
                        provider_service_data = None

                if not api_key:
                    last_error = "gemini api_key missing"
                else:
                    for _ in range(max(1, attempts)):
                        try:
                            if base_bytes:
                                mask_bytes = await _try_fetch_optional_image_bytes(hass, str(mask_url) if mask_url else None)
                                image_bytes = await _gemini_edit_image(
                                    hass,
                                    api_key,
                                    base_bytes,
                                    mask_bytes,
                                    prompt,
                                    model,
                                    provider_service_data=provider_service_data,
                                )
                            else:
                                image_bytes = await _gemini_generate_image(
                                    hass,
                                    api_key,
                                    prompt,
                                    model,
                                    provider_service_data=provider_service_data,
                                )
                            if image_bytes:
                                break
                        except Exception as err:  # noqa: BLE001
                            last_error = str(err)

            elif provider_type == "ai_task":
                service_data: dict[str, Any] = {
                    "task_name": f"{task_name_prefix}: {name}",
                    "instructions": prompt,
                }
                entity_id = provider.get("entity_id") or provider.get("ha_entity_id")
                if entity_id:
                    service_data["entity_id"] = entity_id
                service_data.update(provider.get("service_data") or {})

                urls: list[str] = []
                for _ in range(max(1, attempts)):
                    try:
                        response = await hass.services.async_call(
                            "ai_task",
                            "generate_image",
                            service_data,
                            blocking=True,
                            return_response=True,
                        )
                        payload = response.get("response") if isinstance(response, dict) else response
                        urls = _extract_ai_task_urls(payload or {})
                        if urls:
                            break
                        last_error = "no image url in response"
                    except Exception as err:  # noqa: BLE001
                        last_error = str(err)

                if urls:
                    try:
                        image_bytes = await _fetch_image_bytes(hass, urls[0])
                    except Exception as err:  # noqa: BLE001
                        last_error = str(err)
            else:
                last_error = f"unsupported provider type: {provider_type}"

            if not image_bytes:
                results.append(
                    {
                        "name": name,
                        "error": last_error or "no image returned",
                        "cached": False,
                    }
                )
                continue

            try:
                mask_url = asset.get("mask_url") or asset.get("mask")
                derive_overlay = bool(asset.get("derive_overlay"))

                if derive_overlay and base_bytes:
                    image_bytes = await hass.async_add_executor_job(
                        _derive_overlay_from_base, base_bytes, image_bytes
                    )
                elif mask_url:
                    mask_bytes = await _try_fetch_optional_image_bytes(hass, str(mask_url))
                    if mask_bytes:
                        image_bytes = await hass.async_add_executor_job(_apply_alpha_mask, image_bytes, mask_bytes)

                full_path.write_bytes(image_bytes)

                results.append(
                    {
                        "name": name,
                        "local_url": _local_url_from_path(output_path, filename),
                        "filename": str(full_path),
                        "cached": False,
                    }
                )
            except Exception as err:  # noqa: BLE001
                results.append(
                    {
                        "name": name,
                        "error": str(err),
                        "cached": False,
                    }
                )

        return {"assets": results}

    if not hass.services.has_service(DOMAIN, SERVICE_COMPOSE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_COMPOSE,
            _handle_compose,
            schema=COMPOSE_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_EXISTS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_EXISTS,
            _handle_exists,
            schema=EXISTS_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_CLEAR_CACHE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEAR_CACHE,
            _handle_clear_cache,
            schema=CLEAR_CACHE_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_ENSURE_ASSETS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_ENSURE_ASSETS,
            _handle_ensure_assets,
            schema=ENSURE_ASSETS_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_GENERATE_MASKS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GENERATE_MASKS,
            _handle_generate_masks,
            schema=GENERATE_MASKS_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    await _async_register_service(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await _async_register_service(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return True
