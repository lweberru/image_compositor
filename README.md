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
- `base_image` (required): URL or `/local/…` image for the base layer.
- `layers` (optional): List of overlay layers (each supports `image`, `x`, `y`, `opacity`, `scale`).
- `output_name` (optional): File name for the output.
- `cache_key` (optional): If provided and cached file exists, the service returns that file.
- `format` (optional): `png` or `jpg` (default: `png`).

**Response**
- `local_url`: Path under `/local/…`
- `filename`: Full path on the filesystem

### `image_compositor.file_exists`

Checks if a local file exists in `/config/www`.

**Fields**
- `local_url` (optional): `/local/…` URL to check.
- `path` (optional): Target path relative to `/config` (default: `www/image_compositor`).
- `filename` (optional): Filename to check in the given path.

**Response**
- `exists`: `true` if the file exists, otherwise `false`.

### `image_compositor.clear_cache`

Deletes cached images by prefix.

**Fields**
- `prefix` (optional): File prefix to delete.

## Notes

- This integration is intended to be generic and reusable across cards and dashboards.
- The compose service is designed for server-side caching and deterministic outputs.

## License

MIT
