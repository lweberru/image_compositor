"""Image Compositor integration."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import aiohttp_client

DOMAIN = "image_compositor"
SERVICE_COMPOSE = "compose"
SERVICE_EXISTS = "file_exists"
SERVICE_CLEAR_CACHE = "clear_cache"

OUTPUT_DIR_DEFAULT = "www/image_compositor"

COMPOSE_SCHEMA = vol.Schema(
    {
        vol.Required("base_image"): str,
        vol.Optional("layers", default=[]): list,
        vol.Optional("output_name"): str,
        vol.Optional("cache_key"): str,
        vol.Optional("format", default="png"): vol.In(["png", "jpg", "jpeg"]),
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


async def _fetch_image_bytes(hass: HomeAssistant, source: str) -> bytes:
    if source.startswith("/local/") or source.startswith("local/"):
        local_path = _resolve_local_path(source)
        full_path = Path(hass.config.path(local_path))
        return full_path.read_bytes()

    session = aiohttp_client.async_get_clientsession(hass)
    async with session.get(source) as resp:
        resp.raise_for_status()
        return await resp.read()


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

        output_path = _normalize_output_path(None)
        output_dir = Path(hass.config.path(output_path))
        output_dir.mkdir(parents=True, exist_ok=True)

        if cache_key:
            cached_name = _safe_filename(cache_key, output_format)
            cached_path = output_dir / cached_name
            if cached_path.exists():
                local_path = output_path[4:] if output_path.startswith("www/") else output_path
                local_path = local_path.strip("/")
                local_url = f"/local/{local_path}/{cached_name}" if local_path else f"/local/{cached_name}"
                return {"local_url": local_url, "filename": str(cached_path)}

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

        local_path = output_path[4:] if output_path.startswith("www/") else output_path
        local_path = local_path.strip("/")
        local_url = f"/local/{local_path}/{filename}" if local_path else f"/local/{filename}"

        return {"local_url": local_url, "filename": str(full_path)}

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


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    await _async_register_service(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await _async_register_service(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return True
