"""Validation helpers for generated financial PNG images."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image


def validate_financial_png(path: str | Path, *, min_width: int = 1900, min_height: int = 1000) -> str:
    """Return an issue string when a generated PNG is not safe to send."""

    image_path = Path(path)
    try:
        with Image.open(image_path) as opened:
            alpha_issue = transparent_png_issue(opened)
            if alpha_issue:
                return alpha_issue
            image = opened.convert("RGB")
            width, height = image.size
            if width < min_width or height < min_height:
                return f"image_too_small:{width}x{height}"
            table_like_pct, unique_sample_colors = table_like_pixel_stats(image)
            if unique_sample_colors < 8:
                return f"image_probably_blank:{unique_sample_colors}_colors"
            if table_like_pct < 55.0:
                return f"image_not_table_like:{table_like_pct:.2f}%"
            contamination_issue = visual_contamination_issue(image)
            if contamination_issue:
                return contamination_issue
    except Exception as exc:
        return f"image_unreadable:{type(exc).__name__}: {exc}"
    return ""


def transparent_png_issue(image: Image.Image) -> str:
    """Return an issue if a PNG has transparent or semi-transparent pixels."""

    if image.mode not in {"RGBA", "LA"} and "transparency" not in image.info:
        return ""
    alpha = image.convert("RGBA").getchannel("A")
    min_alpha, max_alpha = alpha.getextrema()
    if min_alpha < 255 or max_alpha < 255:
        transparent_pct = sum(1 for value in alpha.resize((160, 90)).getdata() if value < 255) / (160 * 90) * 100
        return f"image_has_transparency:{transparent_pct:.2f}%"
    return ""


def image_file_issues(image_root: Path) -> list[dict[str, object]]:
    """Check saved PNGs are readable, nonblank, and table-like."""

    if not image_root.exists():
        return [{"path": str(image_root), "issue": "image_root_missing"}]
    issues: list[dict[str, object]] = []
    for path in sorted(image_root.rglob("*.png")):
        issue = validate_financial_png(path)
        if issue:
            issues.append({"path": str(path), "issue": issue})
    return issues


def table_like_pixel_stats(image: Image.Image) -> tuple[float, int]:
    """Return percentage of sampled pixels matching the expected table palette."""

    width, height = image.size
    sample_total = 0
    table_like = 0
    unique: set[tuple[int, int, int]] = set()
    step = max(6, min(width, height) // 140)
    for y in range(0, height, step):
        for x in range(0, width, step):
            rgb = image.getpixel((x, y))
            unique.add(rgb)
            sample_total += 1
            if is_table_palette_pixel(rgb):
                table_like += 1
    pct = (table_like / sample_total) * 100 if sample_total else 0.0
    return pct, len(unique)


def visual_contamination_issue(image: Image.Image) -> str:
    """Return an issue for photo/screenshot-like contamination."""

    colorfulness, high_saturation_pct, dark_pct = visual_contamination_stats(image)
    if colorfulness > 55.0 and high_saturation_pct > 18.0:
        return f"image_photo_like:{colorfulness:.1f}_colorfulness:{high_saturation_pct:.1f}%_saturation"
    if dark_pct > 16.0 and high_saturation_pct > 8.0:
        return f"image_photo_dark_region:{dark_pct:.1f}%_dark:{high_saturation_pct:.1f}%_saturation"
    return ""


def visual_contamination_stats(image: Image.Image) -> tuple[float, float, float]:
    """Return colorfulness, high-saturation percentage, and very-dark percentage."""

    width, height = image.size
    sample_width = min(360, width)
    sample_height = max(1, int(height * (sample_width / max(width, 1))))
    sampled = image.resize((sample_width, sample_height)).convert("RGB")
    arr = np.asarray(sampled, dtype=np.float32)
    red_green = arr[:, :, 0] - arr[:, :, 1]
    yellow_blue = 0.5 * (arr[:, :, 0] + arr[:, :, 1]) - arr[:, :, 2]
    colorfulness = float(
        np.sqrt(np.std(red_green) ** 2 + np.std(yellow_blue) ** 2)
        + 0.3 * np.sqrt(np.mean(red_green) ** 2 + np.mean(yellow_blue) ** 2)
    )
    max_channel = arr.max(axis=2)
    min_channel = arr.min(axis=2)
    saturation = np.zeros_like(max_channel)
    np.divide(max_channel - min_channel, max_channel, out=saturation, where=max_channel > 0)
    high_saturation_pct = float((saturation > 0.35).mean() * 100.0)
    dark_pct = float((arr.mean(axis=2) < 40.0).mean() * 100.0)
    return colorfulness, high_saturation_pct, dark_pct


def is_table_palette_pixel(rgb: tuple[int, int, int]) -> bool:
    """Return whether an RGB pixel belongs to the expected financial table palette."""

    r, g, b = rgb
    if r > 245 and g > 245 and b > 245:
        return True
    if 190 <= r <= 235 and 215 <= g <= 245 and 225 <= b <= 255:
        return True
    if g > 120 and r < 235 and b < 235:
        return True
    if r < 60 and g < 100 and b < 130:
        return True
    if r > 150 and g < 90 and b < 90:
        return True
    if abs(r - g) <= 8 and abs(g - b) <= 8 and 120 <= r <= 245:
        return True
    return False
