# Image Compositor (Home Assistant)

A minimal Home Assistant integration to compose a base image with overlay layers on the server side.

## Installation (HACS)

1. Add this repository as a custom repository in HACS.
2. Install the **Image Compositor** integration.
3. Restart Home Assistant.

## Add via UI

Settings → Devices & Services → Add Integration → **Image Compositor**.

## Services

### `image_compositor.compose`

Composes a base image with overlay layers and writes the result under `/config/www/image_compositor/`.

**Fields**
 - `base_image` (required): URL or `/local/...` image for the base layer.
 - `layers` (optional): List of overlay layers, each supports:
	 - `image` (required): Overlay PNG/SVG with transparency.
	 - `x`/`y` (optional): Pixel offset relative to the base image.
	 - `opacity` (optional): 0.0–1.0.
	 - `scale` (optional): Scale multiplier (1.0 = 100%).
 - `output_name` (optional): File name for the output.
 - `cache_key` (optional): If provided and cached file exists, returns the cached file.
 - `format` (optional): `png` or `jpg` (default: `png`).
 - `output_path` (optional): Target path relative to `/config` (default: `www/image_compositor`).

**Response**
 - `local_url`: Path under `/local/...`
 - `filename`: Full path on the filesystem

### Example: compose (BMW overlay)
```yaml
service: image_compositor.compose
data:
  base_image: /local/image_compositor/assets/base.png
  output_name: bmw_state.png
  cache_key: bmw_state_hash
  format: png
  output_path: www/image_compositor
  layers:
    - image: /local/image_compositor/assets/door_front_left_open.png
      x: 0
      y: 0
      opacity: 1.0
      scale: 1.0
    - image: /local/image_compositor/assets/hood_open.png
      x: 0
      y: 0
      opacity: 1.0
      scale: 1.0
    - image: /local/image_compositor/assets/tire_warn.png
      x: 270
      y: 220
      opacity: 1.0
      scale: 1.0
```

**Layer Hinweise**
- `image` ist die Overlay‑Datei (PNG mit transparentem Hintergrund).
- `x`/`y` sind Pixel‑Koordinaten relativ zum Basisbild.
- `opacity` ist 0.0–1.0.
- `scale` skaliert das Overlay relativ zum Original (1.0 = 100%).

### `image_compositor.file_exists`

Checks if a local file exists in `/config/www`.
**Fields**
 - `local_url` (optional): `/local/...` URL to check.
 - `path` (optional): Target path relative to `/config` (default: `www/image_compositor`).
 - `filename` (optional): Filename to check in the given path.

**Response**
 - `exists`: `true` if the file exists, otherwise `false`.

### Example: file_exists
```yaml
service: image_compositor.file_exists
data:
  local_url: /local/image_compositor/assets/base.png
```

### `image_compositor.clear_cache`

Deletes cached images by prefix.

**Fields**
 - `prefix` (optional): File prefix to delete.

### Example: clear_cache
```yaml
service: image_compositor.clear_cache
data:
  prefix: bmw_state_
```

### `image_compositor.ensure_assets`

Generates and caches assets via Home Assistant `ai_task.generate_image`, OpenAI, or Google Gemini (generations/edits). Optionally applies a mask or derives an overlay from a base image.

**Fields**
 - `output_path` (optional): Target path relative to `/config` (default: `www/image_compositor/assets`).
 - `task_name_prefix` (optional): Prefix for `ai_task` task names.
 - `provider` (optional): Provider config.
  - `type`: `ai_task`, `openai`, or `gemini`.
   - `entity_id` (ai_task): Target ai_task entity.
   - `service_data` (ai_task, optional): Extra ai_task service data.
   - `api_key` (openai): OpenAI API key.
   - `model` (openai): Image model (e.g. `gpt-image-1`).
   - `size` (openai, optional): Output size (e.g. `1024x1024`).
   - `api_key` (gemini): Google AI API key.
  - `model` (gemini, optional): Image-capable Gemini model (default `gemini-2.0-flash-preview-image-generation`).
   - `service_data` (gemini, optional): Extra payload fields for `generateContent`.
 - `assets` (required): List of asset specs.
   - `name`, `prompt`, `filename` (required)
  - `mask_url` (optional): Mask for transparency or inpainting (recommended for best alignment).
   - `format` (optional): `png` or `jpg`.
   - `attempts` (optional): Retry count.
  - `base_ref` (openai/gemini, optional): Name of another asset to use as base.
  - `base_image` (openai/gemini, optional): Base image URL or `/local/...`.
   - `derive_overlay` (openai/gemini, optional): Derive overlay by diffing base and edited image.

### Example: ensure_assets (Gemini inpainting)
```yaml
service: image_compositor.ensure_assets
data:
  provider:
    type: gemini
    api_key: !secret google_ai_api_key
    model: gemini-2.0-flash-preview-image-generation
  assets:
    - name: base_front
      prompt: "Studio photo of a 2023 BMW 320d, front 3/4 view, clean background"
      filename: base_front.png
    - name: door_fl_open
      prompt: "Same car and view, front left door open"
      filename: door_fl_open.png
      mask_url: /local/masks/door_fl_mask.png
      base_ref: base_front
      derive_overlay: true
```

**Response**
 - `assets`: List of generated asset records (`name`, `local_url`, `filename`, `cached`, `error`).

### Example: ensure_assets (OpenAI inpainting)
```yaml
service: image_compositor.ensure_assets
data:
  provider:
    type: openai
    api_key: !secret openai_api_key
    model: gpt-image-1
    size: 1024x1024
  assets:
    - name: base_front
      prompt: "Studio photo of a 2023 BMW 320d, front 3/4 view, clean background"
      filename: base_front.png
    - name: door_fl_open
      prompt: "Same car and view, front left door open"
      filename: door_fl_open.png
      mask_url: /local/masks/door_fl_mask.png
      base_ref: base_front
      derive_overlay: true
```

### Example: ensure_assets (ai_task, no inpainting)
```yaml
service: image_compositor.ensure_assets
data:
  task_name_prefix: BMW Assets
  provider:
    type: ai_task
    entity_id: ai_task.google_ai_task
  assets:
    - name: base_front
      prompt: "Studio photo of a 2023 BMW 320d, front 3/4 view, clean background"
      filename: base_front.png
    - name: door_fl_open
      prompt: "Same car and view, front left door open, transparent background"
      filename: door_fl_open.png
      mask_url: /local/masks/door_fl_mask.png
```

Note: `ai_task` image generation does not provide deterministic inpainting against a fixed base image. For exact BMW panel overlays (doors/windows/hood/trunk/sunroof aligned to the base), use `openai` or `gemini` with `base_ref`/`base_image` and `derive_overlay` (plus masks where available).

If `mask_url` points to a file that does not exist, the asset generation continues without mask (best-effort fallback).

### Example: ensure_assets (BMW full set, Gemini inpainting)
```yaml
service: image_compositor.ensure_assets
data:
  task_name_prefix: BMW Assets
  provider:
    type: gemini
    api_key: !secret google_ai_api_key
    model: gemini-2.0-flash-preview-image-generation
  assets:
    - name: base
      prompt: "Studio photo of a 2023 BMW 320d, front 3/4 view, clean background"
      filename: base.png
    - name: door_front_left_open
      prompt: "Same car and view, front left door open, transparent background, only the opened part visible"
      filename: door_front_left_open.png
      base_ref: base
      derive_overlay: true
    - name: door_front_right_open
      prompt: "Same car and view, front right door open, transparent background, only the opened part visible"
      filename: door_front_right_open.png
      base_ref: base
      derive_overlay: true
    - name: door_rear_left_open
      prompt: "Same car and view, rear left door open, transparent background, only the opened part visible"
      filename: door_rear_left_open.png
      base_ref: base
      derive_overlay: true
    - name: door_rear_right_open
      prompt: "Same car and view, rear right door open, transparent background, only the opened part visible"
      filename: door_rear_right_open.png
      base_ref: base
      derive_overlay: true
    - name: window_front_left_open
      prompt: "Same car and view, front left window open, transparent background, only the opened part visible"
      filename: window_front_left_open.png
      base_ref: base
      derive_overlay: true
    - name: window_front_right_open
      prompt: "Same car and view, front right window open, transparent background, only the opened part visible"
      filename: window_front_right_open.png
      base_ref: base
      derive_overlay: true
    - name: window_rear_left_open
      prompt: "Same car and view, rear left window open, transparent background, only the opened part visible"
      filename: window_rear_left_open.png
      base_ref: base
      derive_overlay: true
    - name: window_rear_right_open
      prompt: "Same car and view, rear right window open, transparent background, only the opened part visible"
      filename: window_rear_right_open.png
      base_ref: base
      derive_overlay: true
    - name: hood_open
      prompt: "Same car and view, hood open, transparent background, only the opened part visible"
      filename: hood_open.png
      base_ref: base
      derive_overlay: true
    - name: trunk_open
      prompt: "Same car and view, trunk open, transparent background, only the opened part visible"
      filename: trunk_open.png
      base_ref: base
      derive_overlay: true
    - name: sunroof_open
      prompt: "Same car and view, sunroof open, transparent background, only the opened part visible"
      filename: sunroof_open.png
      base_ref: base
      derive_overlay: true
    - name: sunroof_tilt
      prompt: "Same car and view, sunroof tilted, transparent background, only the opened part visible"
      filename: sunroof_tilt.png
      base_ref: base
      derive_overlay: true
    - name: tire_ok
      prompt: "Small green circle icon, transparent background"
      filename: tire_ok.png
    - name: tire_warn
      prompt: "Small yellow circle icon, transparent background"
      filename: tire_warn.png
    - name: tire_error
      prompt: "Small red circle icon, transparent background"
      filename: tire_error.png
```

## Notes

- This integration is intended to be generic and reusable across cards and dashboards.
- The compose service is designed for server-side caching and deterministic outputs.

## License

MIT
