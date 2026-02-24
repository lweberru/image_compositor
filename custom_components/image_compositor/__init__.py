"""Image Compositor integration."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
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
        vol.Optional("cleanup", default=False): bool,
        vol.Optional("cleanup_grace_hours", default=24): vol.Coerce(float),
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
        vol.Optional("threshold", default=16): int,
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


def _metadata_filename_for(filename: str) -> str:
    stem, _ = os.path.splitext(filename)
    return f"{stem}.json"


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _find_existing_asset_for_hash(output_dir: Path, image_hash: str, exclude: Path | None = None) -> Path | None:
    for metadata_file in output_dir.glob("*.json"):
        try:
            payload = json.loads(metadata_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("image_hash") or "").strip() != image_hash:
            continue
        filename = str(payload.get("filename") or "").strip()
        if not filename:
            continue
        candidate = Path(filename)
        if not candidate.is_absolute():
            candidate = output_dir / candidate
        if exclude and candidate.resolve() == exclude.resolve():
            continue
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _build_asset_metadata(
    *,
    name: str,
    filename: str,
    local_url: str,
    output_path: str,
    task_name_prefix: str,
    provider: dict[str, Any],
    provider_type: str,
    prompt: str,
    image_hash: str,
    asset: dict[str, Any],
    cached: bool,
    deduplicated: bool,
) -> dict[str, Any]:
    provider_meta = {
        "type": provider_type,
        "model": provider.get("model"),
        "size": provider.get("size"),
        "entity_id": provider.get("entity_id") or provider.get("ha_entity_id"),
    }
    source_meta = {
        "base_ref": asset.get("base_ref"),
        "base_image": asset.get("base_image"),
        "mask_url": asset.get("mask_url") or asset.get("mask"),
        "postprocess": asset.get("postprocess"),
        "derive_overlay": bool(asset.get("derive_overlay")),
        "attempts": int(asset.get("attempts") or 2),
    }
    if isinstance(asset.get("metadata"), dict):
        source_meta["custom"] = asset.get("metadata")

    return {
        "name": name,
        "filename": filename,
        "local_url": local_url,
        "output_path": output_path,
        "task_name_prefix": task_name_prefix,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cached": cached,
        "deduplicated": deduplicated,
        "prompt": prompt,
        "prompt_hash": _hash_text(prompt),
        "image_hash": image_hash,
        "provider": provider_meta,
        "source": source_meta,
    }


def _write_asset_metadata_file(path: Path, metadata: dict[str, Any]) -> None:
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _is_asset_metadata(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    return "filename" in payload and "local_url" in payload and "provider" in payload


def _extract_metadata_target_path(metadata_file: Path, payload: dict[str, Any], output_dir: Path) -> Path | None:
    filename = str(payload.get("filename") or "").strip()
    if not filename:
        return None
    path = Path(filename)
    if not path.is_absolute():
        path = output_dir / path
    return path


def _is_older_than(path: Path, min_age_seconds: float) -> bool:
    if min_age_seconds <= 0:
        return True
    try:
        age_seconds = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
    except Exception:  # noqa: BLE001
        return False
    return age_seconds >= min_age_seconds


def _cleanup_orphan_assets(
    output_dir: Path,
    *,
    keep_image_names: set[str],
    keep_metadata_names: set[str],
    min_age_seconds: float,
) -> dict[str, Any]:
    image_suffixes = {".png", ".jpg", ".jpeg", ".webp"}
    removed_images: list[str] = []
    removed_metadata: list[str] = []
    preserved_orphan_images: list[str] = []
    kept_recent_metadata: list[str] = []

    metadata_files = [entry for entry in output_dir.glob("*.json") if entry.is_file()]
    referenced_images: set[str] = set()
    parsed_metadata: list[tuple[Path, dict[str, Any]]] = []

    for metadata_file in metadata_files:
        try:
            payload = json.loads(metadata_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not _is_asset_metadata(payload):
            continue
        parsed_metadata.append((metadata_file, payload))
        target_path = _extract_metadata_target_path(metadata_file, payload, output_dir)
        if target_path:
            referenced_images.add(target_path.name)

    keep_all_images = referenced_images | set(keep_image_names)

    for entry in output_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in image_suffixes:
            continue
        if entry.name in keep_all_images:
            continue
        preserved_orphan_images.append(str(entry))

    for metadata_file, payload in parsed_metadata:
        if metadata_file.name in keep_metadata_names:
            continue
        target_path = _extract_metadata_target_path(metadata_file, payload, output_dir)
        if target_path and target_path.exists():
            continue
        if not _is_older_than(metadata_file, min_age_seconds):
            kept_recent_metadata.append(str(metadata_file))
            continue
        try:
            metadata_file.unlink()
            removed_metadata.append(str(metadata_file))
        except Exception:  # noqa: BLE001
            continue

    return {
        "removed_images": removed_images,
        "removed_metadata": removed_metadata,
        "preserved_orphan_images": preserved_orphan_images,
        "kept_recent_metadata": kept_recent_metadata,
        "grace_hours": round(float(min_age_seconds) / 3600.0, 3),
        "removed_count": len(removed_images) + len(removed_metadata),
    }


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


def _postprocess_icon_overlay(
    image_bytes: bytes,
    *,
    light_threshold: int = 238,
    low_saturation_threshold: int = 42,
    max_size: int | None = 96,
) -> bytes:
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(image_bytes)).convert("RGBA")
    hsv = img.convert("HSV")

    rgba_data = list(img.getdata())
    hsv_data = list(hsv.getdata())
    processed: list[tuple[int, int, int, int]] = []

    for (r, g, b, a), (_h, s, v) in zip(rgba_data, hsv_data):
        if a == 0:
            processed.append((r, g, b, 0))
            continue
        if v >= light_threshold and s <= low_saturation_threshold:
            processed.append((r, g, b, 0))
            continue
        processed.append((r, g, b, a))

    img.putdata(processed)

    alpha = img.getchannel("A")
    bbox = alpha.getbbox()
    if bbox:
        img = img.crop(bbox)

    if max_size and max_size > 0:
        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _postprocess_composite_with_base(base_bytes: bytes, image_bytes: bytes) -> bytes:
    from io import BytesIO

    from PIL import Image

    base_img = Image.open(BytesIO(base_bytes)).convert("RGBA")
    edited_img = Image.open(BytesIO(image_bytes)).convert("RGBA")
    if edited_img.size != base_img.size:
        edited_img = edited_img.resize(base_img.size)

    composed = base_img.copy()
    composed.alpha_composite(edited_img)

    out = BytesIO()
    composed.save(out, format="PNG")
    return out.getvalue()


def _mask_target_constraint_text(target_name: str) -> str:
    key = str(target_name or "").lower()
    rules = [
        "Interpret left/right strictly from vehicle perspective, not from viewer image perspective.",
        "Do not change camera angle, perspective, crop, or vehicle position.",
    ]

    if key == "door_front_left_open":
        rules.append("Open only the front left door. Keep front right, rear left and rear right doors closed.")
    elif key == "door_front_right_open":
        rules.append("Open only the front right door. Keep front left, rear left and rear right doors closed.")
    elif key == "door_rear_left_open":
        rules.append("Open only the rear left door. Keep front left, front right and rear right doors closed.")
    elif key == "door_rear_right_open":
        rules.append("Open only the rear right door. Keep front left, front right and rear left doors closed.")
    elif key == "window_front_left_open":
        rules.append("Open only the front left window glass area. Keep all doors closed and keep all other windows closed.")
    elif key == "window_front_right_open":
        rules.append("Open only the front right window glass area. Keep all doors closed and keep all other windows closed.")
    elif key == "window_rear_left_open":
        rules.append("Open only the rear left window glass area. Keep all doors closed and keep all other windows closed.")
    elif key == "window_rear_right_open":
        rules.append("Open only the rear right window glass area. Keep all doors closed and keep all other windows closed.")

    return " ".join(rules)


def _mask_ratio(mask_bytes: bytes) -> float:
    from io import BytesIO

    from PIL import Image

    mask = Image.open(BytesIO(mask_bytes)).convert("L")
    hist = mask.histogram()
    total = float(sum(hist)) or 1.0
    white = float(sum(hist[1:]))
    return white / total


def _target_roi_candidates(target_name: str) -> list[tuple[float, float, float, float]]:
    key = str(target_name or "").lower()

    if key.startswith("door_") or key.startswith("window_"):
        if key.startswith("window_"):
            # Narrower ROIs for window targets (two mirrored orientation hypotheses).
            if "front" in key:
                return [
                    (0.12, 0.32, 0.36, 0.58),
                    (0.64, 0.32, 0.88, 0.58),
                    (0.08, 0.26, 0.92, 0.62),
                ]
            if "rear" in key:
                return [
                    (0.38, 0.28, 0.68, 0.56),
                    (0.32, 0.28, 0.62, 0.56),
                    (0.08, 0.24, 0.92, 0.62),
                ]

        if "front" in key:
            y0, y1 = (0.28, 0.74)
        elif "rear" in key:
            y0, y1 = (0.34, 0.86)
        else:
            y0, y1 = (0.28, 0.86)

        if "left" in key or "right" in key:
            # Two mirrored candidates because model outputs can flip perceived side.
            return [
                (0.04, y0, 0.62, y1),
                (0.38, y0, 0.96, y1),
                (0.04, y0, 0.96, y1),
            ]
        return [(0.04, y0, 0.96, y1)]

    if key == "hood_open":
        return [(0.00, 0.24, 0.62, 0.76), (0.00, 0.20, 0.72, 0.82)]
    if key == "trunk_open":
        return [(0.38, 0.22, 1.00, 0.88), (0.28, 0.18, 1.00, 0.92)]
    if key.startswith("sunroof_"):
        return [(0.30, 0.12, 0.74, 0.54), (0.22, 0.08, 0.82, 0.60)]
    return [(0.00, 0.00, 1.00, 1.00)]


def _apply_target_roi_to_mask(mask_bytes: bytes, target_name: str) -> bytes:
    from io import BytesIO

    from PIL import Image

    mask = Image.open(BytesIO(mask_bytes)).convert("L")
    mask = mask.point(lambda p: 255 if p > 127 else 0)
    width, height = mask.size

    original_hist = mask.histogram()
    original_white = int(sum(original_hist[1:]))
    if original_white <= 0:
        return mask_bytes

    candidates = _target_roi_candidates(target_name)
    best_mask = mask
    best_white = original_white

    for x0, y0, x1, y1 in candidates:
        left = max(0, min(width, int(width * x0)))
        top = max(0, min(height, int(height * y0)))
        right = max(left + 1, min(width, int(width * x1)))
        bottom = max(top + 1, min(height, int(height * y1)))

        candidate = Image.new("L", (width, height), 0)
        crop = mask.crop((left, top, right, bottom))
        candidate.paste(crop, (left, top))
        white = int(sum(candidate.histogram()[1:]))
        if white > best_white:
            best_mask = candidate
            best_white = white

    # If ROI would over-cut almost everything, keep the original mask.
    if best_white < max(32, int(original_white * 0.15)):
        best_mask = mask

    out = BytesIO()
    best_mask.save(out, format="PNG")
    return out.getvalue()


def _synthesize_window_fallback_mask(mask_bytes: bytes, target_name: str) -> bytes:
    from io import BytesIO

    from PIL import Image, ImageDraw

    src = Image.open(BytesIO(mask_bytes)).convert("L")
    src = src.point(lambda p: 255 if p > 127 else 0)
    width, height = src.size
    candidates = _target_roi_candidates(target_name)

    best_box: tuple[int, int, int, int] | None = None
    best_score = -1
    for x0, y0, x1, y1 in candidates[:2]:
        left = max(0, min(width, int(width * x0)))
        top = max(0, min(height, int(height * y0)))
        right = max(left + 1, min(width, int(width * x1)))
        bottom = max(top + 1, min(height, int(height * y1)))
        score = int(sum(src.crop((left, top, right, bottom)).histogram()[1:]))
        if score > best_score:
            best_score = score
            best_box = (left, top, right, bottom)

    if not best_box:
        # ultimate fallback
        best_box = (
            int(width * 0.2),
            int(height * 0.3),
            int(width * 0.55),
            int(height * 0.56),
        )

    out_mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(out_mask)
    left, top, right, bottom = best_box
    radius = max(2, int((bottom - top) * 0.12))
    try:
        draw.rounded_rectangle([left, top, right, bottom], radius=radius, fill=255)
    except Exception:  # noqa: BLE001
        draw.rectangle([left, top, right, bottom], fill=255)

    out = BytesIO()
    out_mask.save(out, format="PNG")
    return out.getvalue()


def _mask_retry_prompt(prompt: str, target_name: str, too_large: bool) -> str:
    base = str(prompt or "").strip()
    constraint = _mask_target_constraint_text(target_name)
    if too_large:
        return (
            f"{base} STRICT: The changed region is currently too large. "
            "Change only the minimal target region and keep all surrounding panels and windows untouched. "
            f"{constraint}"
        )
    return (
        f"{base} STRICT: Ensure a clearly visible change for the requested target only, "
        "without changing unrelated parts. "
        f"{constraint}"
    )


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
            target_constraints = _mask_target_constraint_text(name)
            prompt = target.get("prompt") or (
                f"{base_prompt}. ONLY change this part: {description}. "
                "Keep identical camera angle, vehicle scale, geometry, paint, reflections, background and lighting. "
                "Do not alter any other region. Return a full-frame edited image. "
                f"{target_constraints}"
            )
            filename = _safe_filename(target.get("filename") or name, "png")
            full_path = output_dir / filename

            edited_bytes: bytes | None = None
            error: str | None = None

            async def _generate_edited(prompt_text: str) -> bytes | None:
                if provider_type == "openai":
                    return await _openai_edit_image(
                        hass,
                        str(api_key),
                        base_bytes,
                        None,
                        prompt_text,
                        model,
                        openai_size,
                    )
                return await _gemini_edit_image(
                    hass,
                    str(api_key),
                    base_bytes,
                    None,
                    prompt_text,
                    model,
                    provider_service_data=provider_service_data,
                )

            try:
                edited_bytes = await _generate_edited(prompt)
            except Exception as err:  # noqa: BLE001
                error = str(err)

            if not edited_bytes:
                results.append({"name": name, "error": error or "no image returned"})
                continue

            try:
                min_ratio = 0.0012
                max_ratio = 0.28
                mask_bytes = await hass.async_add_executor_job(
                    _derive_binary_mask_from_base,
                    base_bytes,
                    edited_bytes,
                    threshold,
                )
                mask_bytes = await hass.async_add_executor_job(_apply_target_roi_to_mask, mask_bytes, name)
                ratio = await hass.async_add_executor_job(_mask_ratio, mask_bytes)

                if ratio < min_ratio or ratio > max_ratio:
                    retry_prompt = _mask_retry_prompt(prompt, name, ratio > max_ratio)
                    retry_threshold = max(1, min(255, threshold + 6 if ratio > max_ratio else threshold - 4))
                    try:
                        retry_edited = await _generate_edited(retry_prompt)
                    except Exception:  # noqa: BLE001
                        retry_edited = None
                    if retry_edited:
                        retry_mask_bytes = await hass.async_add_executor_job(
                            _derive_binary_mask_from_base,
                            base_bytes,
                            retry_edited,
                            retry_threshold,
                        )
                        retry_mask_bytes = await hass.async_add_executor_job(
                            _apply_target_roi_to_mask,
                            retry_mask_bytes,
                            name,
                        )
                        retry_ratio = await hass.async_add_executor_job(_mask_ratio, retry_mask_bytes)

                        def _penalty(value: float) -> float:
                            if value < min_ratio:
                                return 100.0 + (min_ratio - value)
                            if value > max_ratio:
                                return 100.0 + (value - max_ratio)
                            return abs(0.05 - value)

                        if _penalty(retry_ratio) < _penalty(ratio):
                            mask_bytes = retry_mask_bytes
                            ratio = retry_ratio

                if str(name).lower().startswith("window_") and ratio < min_ratio:
                    mask_bytes = await hass.async_add_executor_job(
                        _synthesize_window_fallback_mask,
                        mask_bytes,
                        name,
                    )
                    ratio = await hass.async_add_executor_job(_mask_ratio, mask_bytes)

                full_path.write_bytes(mask_bytes)
                results.append(
                    {
                        "name": name,
                        "local_url": _local_url_from_path(output_path, filename),
                        "filename": str(full_path),
                        "area_ratio": round(float(ratio), 6),
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
        cleanup = bool(call.data.get("cleanup"))
        cleanup_grace_hours_raw = float(call.data.get("cleanup_grace_hours") or 24)
        cleanup_grace_hours = max(0.0, cleanup_grace_hours_raw)
        task_name_prefix = str(call.data.get("task_name_prefix") or "Image Compositor")
        assets = call.data.get("assets") or []
        referenced_base_files: set[str] = set()
        for entry in assets:
            if not isinstance(entry, dict):
                continue
            base_ref = entry.get("base_ref")
            if not base_ref:
                continue
            referenced_base_files.add(_safe_filename(str(base_ref), "png"))

        results: list[dict[str, Any]] = []
        keep_image_names: set[str] = set(referenced_base_files)
        keep_metadata_names: set[str] = set()

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
            local_url = _local_url_from_path(output_path, filename)
            metadata_filename = _metadata_filename_for(filename)
            metadata_path = output_dir / metadata_filename
            metadata_local_url = _local_url_from_path(output_path, metadata_filename)
            provider_type = str(provider.get("type") or "ai_task").lower()

            if full_path.exists() and not force_generation:
                cached_hash: str | None = None
                metadata_valid = False
                if metadata_path.exists() and metadata_path.is_file():
                    try:
                        existing_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                        if _is_asset_metadata(existing_metadata):
                            metadata_valid = True
                            existing_hash = str(existing_metadata.get("image_hash") or "").strip()
                            if existing_hash:
                                cached_hash = existing_hash
                    except Exception:  # noqa: BLE001
                        metadata_valid = False

                if not cached_hash:
                    cached_hash = _hash_bytes(full_path.read_bytes())

                if not metadata_valid:
                    metadata = _build_asset_metadata(
                        name=name,
                        filename=str(full_path),
                        local_url=local_url,
                        output_path=output_path,
                        task_name_prefix=task_name_prefix,
                        provider=_safe_dict(provider),
                        provider_type=provider_type,
                        prompt=prompt,
                        image_hash=cached_hash,
                        asset=asset,
                        cached=True,
                        deduplicated=False,
                    )
                    _write_asset_metadata_file(metadata_path, metadata)
                results.append(
                    {
                        "name": name,
                        "local_url": local_url,
                        "metadata_local_url": metadata_local_url,
                        "metadata_filename": str(metadata_path),
                        "filename": str(full_path),
                        "image_hash": cached_hash,
                        "deduplicated": False,
                        "cached": True,
                    }
                )
                keep_image_names.add(filename)
                keep_metadata_names.add(metadata_filename)
                continue

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
                postprocess = str(asset.get("postprocess") or "").strip().lower()

                if derive_overlay and base_bytes:
                    image_bytes = await hass.async_add_executor_job(
                        _derive_overlay_from_base, base_bytes, image_bytes
                    )
                elif mask_url:
                    mask_bytes = await _try_fetch_optional_image_bytes(hass, str(mask_url))
                    if mask_bytes:
                        image_bytes = await hass.async_add_executor_job(_apply_alpha_mask, image_bytes, mask_bytes)

                if postprocess == "icon_overlay":
                    max_size_raw = asset.get("icon_max_size")
                    max_size: int | None = None
                    if max_size_raw not in (None, ""):
                        try:
                            max_size = max(1, int(max_size_raw))
                        except Exception:  # noqa: BLE001
                            max_size = 96
                    else:
                        max_size = 96
                    image_bytes = await hass.async_add_executor_job(
                        _postprocess_icon_overlay,
                        image_bytes,
                        238,
                        42,
                        max_size,
                    )
                elif postprocess == "composite_with_base" and base_bytes:
                    image_bytes = await hass.async_add_executor_job(
                        _postprocess_composite_with_base,
                        base_bytes,
                        image_bytes,
                    )

                image_hash = _hash_bytes(image_bytes)
                protect_requested_filename = filename in referenced_base_files
                deduplicated = False
                selected_path = full_path
                selected_filename = filename

                if not protect_requested_filename:
                    duplicate_path = _find_existing_asset_for_hash(output_dir, image_hash, exclude=full_path)
                    if duplicate_path and duplicate_path.exists():
                        selected_path = duplicate_path
                        selected_filename = duplicate_path.name
                        deduplicated = True
                    else:
                        full_path.write_bytes(image_bytes)
                else:
                    full_path.write_bytes(image_bytes)

                selected_local_url = _local_url_from_path(output_path, selected_filename)
                selected_metadata_filename = _metadata_filename_for(selected_filename)
                selected_metadata_path = output_dir / selected_metadata_filename
                selected_metadata_local_url = _local_url_from_path(output_path, selected_metadata_filename)
                metadata = _build_asset_metadata(
                    name=name,
                    filename=str(selected_path),
                    local_url=selected_local_url,
                    output_path=output_path,
                    task_name_prefix=task_name_prefix,
                    provider=_safe_dict(provider),
                    provider_type=provider_type,
                    prompt=prompt,
                    image_hash=image_hash,
                    asset=asset,
                    cached=False,
                    deduplicated=deduplicated,
                )
                _write_asset_metadata_file(selected_metadata_path, metadata)

                results.append(
                    {
                        "name": name,
                        "local_url": selected_local_url,
                        "metadata_local_url": selected_metadata_local_url,
                        "metadata_filename": str(selected_metadata_path),
                        "filename": str(selected_path),
                        "image_hash": image_hash,
                        "deduplicated": deduplicated,
                        "cached": False,
                    }
                )
                keep_image_names.add(selected_filename)
                keep_metadata_names.add(selected_metadata_filename)
            except Exception as err:  # noqa: BLE001
                results.append(
                    {
                        "name": name,
                        "error": str(err),
                        "cached": False,
                    }
                )

        if cleanup:
            cleanup_result = _cleanup_orphan_assets(
                output_dir,
                keep_image_names=keep_image_names,
                keep_metadata_names=keep_metadata_names,
                min_age_seconds=cleanup_grace_hours * 3600.0,
            )
            return {"assets": results, "cleanup": cleanup_result}

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
