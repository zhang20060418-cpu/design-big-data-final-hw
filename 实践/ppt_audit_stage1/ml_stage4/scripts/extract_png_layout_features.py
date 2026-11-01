"""Fixed, label-free PNG layout statistics (4.3F-2A).

The reusable ``extract_features`` function accepts only an image.  Dataset IDs,
split/group and source metadata are confined to the outer management layer.
No annotations, descriptions, learned features or classifiers are loaded.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import sys
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, __version__ as PILLOW_VERSION


BASE = Path(__file__).resolve().parents[1]
SPLIT_PATH = BASE / "reports/dataset_split.csv"
INDEX_PATH = BASE / "features/feature_index.csv"
METADATA_PATH = BASE / "data/slideaudit/data/metadata.csv"
IMAGE_ROOT = BASE / "data/images"
CSV_PATH = BASE / "features/png_layout_trainval.csv"
RECIPE_PATH = BASE / "features/png_layout_recipe.json"
SPLIT_SHA256 = "e70f1ff2be8cde44d7bcf8c60ed4c8bf0ba5f368062baa6980ce96131d63d487"
FEATURE_NAMES = (
    "aspect_ratio",
    "edge_density",
    "flat_region_ratio",
    "gray_std",
    "grid_edge_cv",
    "border_edge_ratio",
    "horizontal_projection_spread",
    "vertical_projection_spread",
    "mean_gray",
    "mean_color_range",
)
ALGORITHM_RECIPE = {
    "version": "png-layout-v1.0.0",
    "feature_names": list(FEATURE_NAMES),
    "numeric_precision": "IEEE-754 NumPy float64 throughout, population std ddof=0",
    "color_input": "Full, unannotated PNG; alpha/transparency composited onto opaque white using Pillow; then RGB",
    "rgb": "Pillow encoded RGB uint8 converted to float64 then divided by 255.0; no linear-light conversion",
    "resize": {
        "max_long_side": 512,
        "upsample": False,
        "interpolation": "Pillow Image.Resampling.BICUBIC",
        "short_side": "max(1, floor(short_original*512/long_original + 0.5)), using integer arithmetic",
        "aspect": "preserve aspect to nearest integer pixel; no crop, stretch or padding",
    },
    "gray": "Y=(float64(0.299)*R + float64(0.587)*G) + float64(0.114)*B, in this operation order",
    "gradient": {
        "dx": "Y[y,x+1]-Y[y,x]; rightmost column=0",
        "dy": "Y[y+1,x]-Y[y,x]; bottom row=0",
        "magnitude": "sqrt((dx*dx + dy*dy)/float64(2.0))",
        "edge": "G > float64(0.08), strict; equality is not an edge",
        "flat": "G < float64(0.02), strict; equality is not flat",
        "edge_threshold": 0.08,
        "flat_threshold": 0.02,
    },
    "grid": "3x3 tiles; y boundaries=floor(k*H/3), x=floor(k*W/3), k=0..3; each pixel covered once; empty tiles for H<3 or W<3 ignored; population std(tile edge densities)/(mean+1e-6); no edges=>0",
    "border": "top/bottom ceil(H*0.10) rows, left/right ceil(W*0.10) columns, each at least 1; boolean union deduplicates corners; border edge count / all edge count; no edges=>0",
    "projection": "row/column edge counts are weights; y=i/(H-1), x=j/(W-1); singleton axis coordinate=0; weighted population std on [0,1]; no edges=>0",
    "features": {
        "aspect_ratio": "original W/H before resize",
        "edge_density": "mean(G>0.08), including derivative-zero boundary pixels",
        "flat_region_ratio": "mean(G<0.02), including derivative-zero boundary pixels",
        "gray_std": "population std(Y)",
        "grid_edge_cv": "population std(nonempty tile edge densities)/(mean+1e-6); no edges=>0",
        "border_edge_ratio": "edge pixels in outer-10% boolean-union mask / total edge pixels; zero total=>0",
        "horizontal_projection_spread": "weighted std of normalized row positions, weighted by row edge count",
        "vertical_projection_spread": "weighted std of normalized column positions, weighted by column edge count",
        "mean_gray": "mean(Y)",
        "mean_color_range": "mean(max(R,G,B)-min(R,G,B))",
    },
    "theoretical_ranges": {
        "aspect_ratio": {"min_exclusive": 0.0, "max": None},
        "edge_density": [0.0, 1.0],
        "flat_region_ratio": [0.0, 1.0],
        "gray_std": [0.0, 0.5],
        "grid_edge_cv": [0.0, math.sqrt(8.0)],
        "border_edge_ratio": [0.0, 1.0],
        "horizontal_projection_spread": [0.0, 0.5],
        "vertical_projection_spread": [0.0, 0.5],
        "mean_gray": [0.0, 1.0],
        "mean_color_range": [0.0, 1.0],
    },
    "not_equivalent_to": ["PPTX object geometry", "text density", "true whitespace", "occlusion", "cut-off label"],
}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _numeric_id(slide_id: str) -> int:
    if not slide_id.startswith("slide_") or not slide_id[6:].isdigit():
        raise ValueError(f"Invalid slide ID: {slide_id!r}")
    return int(slide_id[6:])


def _read_split() -> list[dict[str, str]]:
    if sha256_file(SPLIT_PATH) != SPLIT_SHA256:
        raise ValueError("Frozen dataset_split.csv SHA-256 changed; refusing to continue")
    with SPLIT_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 2400 or len({r["slide_id"] for r in rows}) != 2400:
        raise ValueError("Frozen split does not contain 2400 unique IDs")
    if Counter(r["split"] for r in rows) != {"train": 1512, "validation": 328, "test": 324, "excluded": 236}:
        raise ValueError("Frozen split counts mismatch")
    selected = sorted((r for r in rows if r["split"] in {"train", "validation"}), key=lambda r: _numeric_id(r["slide_id"]))
    group_splits: dict[str, set[str]] = defaultdict(set)
    group_sizes: Counter[str] = Counter()
    for row in selected:
        if not row["group_id"]:
            raise ValueError("Missing group ID in train/validation")
        group_splits[row["group_id"]].add(row["split"])
        group_sizes[row["group_id"]] += 1
    if len(group_splits) != 460 or any(len(v) != 1 for v in group_splits.values()) or set(group_sizes.values()) != {4}:
        raise ValueError("Frozen trusted groups do not remain four-page, single-split groups")
    return selected


_GUARD_INSTALLED = False


def install_read_guard(rows: list[dict[str, Any]] | None = None) -> None:
    """Deny all labels/descriptions, feature arrays and non-trainval PNG reads.

    Installed only by dataset/CLI management. Importing this module and calling
    extract_features on a future user's image does not install any global hook.
    """
    global _GUARD_INSTALLED
    if _GUARD_INSTALLED:
        return
    selected = rows if rows is not None else _read_split()
    allowed = {str((IMAGE_ROOT / f"{r['slide_id']}.png").resolve()).casefold() for r in selected}
    image_root = str(IMAGE_ROOT.resolve()).casefold() + os.sep

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event != "open" or not args or not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        try:
            path = Path(os.fsdecode(args[0])).resolve()
        except (OSError, ValueError):
            return
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        reading = not (isinstance(mode, str) and any(c in mode for c in "wax") and "+" not in mode)
        if mode is None and isinstance(flags, int) and (flags & os.O_WRONLY):
            reading = False
        if not reading:
            return
        folded = str(path).casefold()
        parts = {p.casefold() for p in path.parts}
        if {"annotations", "descriptions"} & parts:
            raise PermissionError(f"PNG-only audit blocked labels/descriptions: {path}")
        if path.suffix.casefold() in {".npy", ".npz", ".joblib"}:
            raise PermissionError(f"PNG-only audit blocked learned/test features: {path}")
        if path.suffix.casefold() == ".png" and folded.startswith(image_root) and folded not in allowed:
            raise PermissionError(f"PNG-only audit blocked test/excluded image: {path}")
        if path.name.casefold() in {"test_predictions.csv", "clip_lr_4labels.joblib"}:
            raise PermissionError(f"PNG-only audit blocked formal test artifact: {path}")

    sys.addaudithook(audit)
    _GUARD_INSTALLED = True


def build_input_rows(*, verify_images: bool = True) -> list[dict[str, Any]]:
    """Select and verify only frozen train/validation raw PNGs, never labels."""
    selected = _read_split()
    install_read_guard(selected)
    selected_ids = {r["slide_id"] for r in selected}
    with INDEX_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        index_rows = [r for r in csv.DictReader(handle) if r["slide_id"] in selected_ids]
    if len(index_rows) != 1840 or len({r["slide_id"] for r in index_rows}) != 1840:
        raise ValueError("Prior image SHA index does not cover exactly 1840 selected IDs")
    prior = {r["slide_id"]: r for r in index_rows}
    with METADATA_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        metadata = {f"slide_{int(r['id']):04d}": r for r in csv.DictReader(handle) if f"slide_{int(r['id']):04d}" in selected_ids}
    if set(metadata) != selected_ids:
        raise ValueError("Missing selected IDs in metadata")
    output: list[dict[str, Any]] = []
    for row in selected:
        slide_id = row["slide_id"]
        old = prior[slide_id]
        if (old["split"], old["group_id"]) != (row["split"], row["group_id"]):
            raise ValueError(f"Prior index/split alignment mismatch for {slide_id}")
        path = IMAGE_ROOT / f"{slide_id}.png"
        item: dict[str, Any] = {
            "slide_id": slide_id,
            "group_id": row["group_id"],
            "split": row["split"],
            "path": str(path.resolve()),
            "image_sha256": old["image_sha256"],
            "source_type": metadata[slide_id]["source_type"],
            "image_width": int(metadata[slide_id]["image_width"]),
            "image_height": int(metadata[slide_id]["image_height"]),
        }
        if not path.is_file():
            raise FileNotFoundError(f"Selected raw PNG is missing: {slide_id}")
        item["image_size_bytes"] = path.stat().st_size
        if verify_images:
            data = path.read_bytes()
            if data[:8] != b"\x89PNG\r\n\x1a\n":
                raise ValueError(f"Invalid PNG signature: {slide_id}")
            if hashlib.sha256(data).hexdigest() != item["image_sha256"]:
                raise ValueError(f"Prior raw PNG SHA-256 mismatch: {slide_id}")
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG" or image.size != (item["image_width"], item["image_height"]):
                    raise ValueError(f"PNG format/metadata dimensions mismatch: {slide_id}")
                image.load()
        output.append(item)
    return output


def _rgb_image(image: Image.Image) -> Image.Image:
    if "A" in image.getbands() or "transparency" in image.info:
        rgba = image.convert("RGBA")
        return Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba).convert("RGB")
    return image.convert("RGB")


def _prepare_image(path_or_image: str | Path | Image.Image) -> tuple[np.ndarray, int, int]:
    if isinstance(path_or_image, Image.Image):
        image = _rgb_image(path_or_image)
    elif isinstance(path_or_image, (str, os.PathLike)):
        with Image.open(path_or_image) as opened:
            if opened.format != "PNG":
                raise ValueError("Core path input must be a raw PNG")
            opened.load()
            image = _rgb_image(opened)
    else:
        raise TypeError("Expected a PNG path or a decoded Pillow image")
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("Image dimensions must be positive")
    long_side = max(width, height)
    if long_side > 512:
        # floor(scaled short side + .5) without floating-rounding ambiguity.
        if width >= height:
            resized = (512, max(1, (2 * height * 512 + width) // (2 * width)))
        else:
            resized = (max(1, (2 * width * 512 + height) // (2 * height)), 512)
        image = image.resize(resized, resample=Image.Resampling.BICUBIC)
    rgb = np.asarray(image, dtype=np.float64) / np.float64(255.0)
    return rgb, width, height


def _classify_gradient(gradient: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return gradient > np.float64(0.08), gradient < np.float64(0.02)


def _gray_and_edges(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    gray = (np.float64(0.299) * rgb[:, :, 0] + np.float64(0.587) * rgb[:, :, 1]) + np.float64(0.114) * rgb[:, :, 2]
    dx = np.zeros_like(gray, dtype=np.float64)
    dy = np.zeros_like(gray, dtype=np.float64)
    dx[:, :-1] = gray[:, 1:] - gray[:, :-1]
    dy[:-1, :] = gray[1:, :] - gray[:-1, :]
    gradient = np.sqrt((dx * dx + dy * dy) / np.float64(2.0))
    edge, flat = _classify_gradient(gradient)
    return gray, edge, flat


def _projection_spread(weights: np.ndarray) -> float:
    weights = np.asarray(weights, dtype=np.float64)
    total = np.sum(weights, dtype=np.float64)
    if total == 0:
        return 0.0
    coords = np.arange(weights.size, dtype=np.float64)
    if weights.size > 1:
        coords = coords / np.float64(weights.size - 1)
    mean = np.sum(weights * coords, dtype=np.float64) / total
    centered = coords - mean
    variance = np.sum(weights * (centered * centered), dtype=np.float64) / total
    return float(np.sqrt(variance))


def extract_features(path_or_PILimage: str | Path | Image.Image) -> dict[str, float]:
    """Return exactly ten ordered floats using only the supplied image pixels.

    This function neither reads nor accepts IDs, labels, dataset metadata,
    descriptions, group/split or any model. It is reusable for a future raw
    aspect-preserving PNG rendered from a user's PPTX.
    """
    rgb, original_width, original_height = _prepare_image(path_or_PILimage)
    gray, edge, flat = _gray_and_edges(rgb)
    height, width = gray.shape
    edge_count = int(np.count_nonzero(edge))
    densities: list[float] = []
    for row in range(3):
        y0, y1 = row * height // 3, (row + 1) * height // 3
        for col in range(3):
            x0, x1 = col * width // 3, (col + 1) * width // 3
            if y1 > y0 and x1 > x0:
                densities.append(float(np.mean(edge[y0:y1, x0:x1], dtype=np.float64)))
    tile_densities = np.asarray(densities, dtype=np.float64)
    grid_cv = float(np.std(tile_densities, ddof=0, dtype=np.float64) / (np.mean(tile_densities, dtype=np.float64) + np.float64(1e-6))) if edge_count else 0.0
    border_h, border_w = max(1, (height + 9) // 10), max(1, (width + 9) // 10)
    border = np.zeros((height, width), dtype=bool)
    border[:border_h, :] = True
    border[-border_h:, :] = True
    border[:, :border_w] = True
    border[:, -border_w:] = True
    values = (
        float(original_width / original_height),
        float(np.mean(edge, dtype=np.float64)),
        float(np.mean(flat, dtype=np.float64)),
        float(np.std(gray, ddof=0, dtype=np.float64)),
        grid_cv,
        float(np.count_nonzero(edge & border) / edge_count) if edge_count else 0.0,
        _projection_spread(np.sum(edge, axis=1, dtype=np.float64)),
        _projection_spread(np.sum(edge, axis=0, dtype=np.float64)),
        float(np.mean(gray, dtype=np.float64)),
        float(np.mean(np.max(rgb, axis=2) - np.min(rgb, axis=2), dtype=np.float64)),
    )
    result = dict(zip(FEATURE_NAMES, values, strict=True))
    _check_values(result)
    return result


def _check_values(values: dict[str, float]) -> None:
    if tuple(values) != FEATURE_NAMES:
        raise ValueError("Feature name/order mismatch")
    for name, value in values.items():
        if not isinstance(value, float) or not math.isfinite(value):
            raise ValueError(f"Nonfinite or non-float feature: {name}={value!r}")
        bounds = ALGORITHM_RECIPE["theoretical_ranges"][name]
        if name == "aspect_ratio":
            valid = value > 0
        else:
            valid = bounds[0] - 1e-12 <= value <= bounds[1] + 1e-12
        if not valid:
            raise ValueError(f"Out-of-range feature: {name}={value}")


def _statistics(matrix: np.ndarray) -> dict[str, dict[str, float | int]]:
    return {
        name: {
            "min": float(np.min(matrix[:, i])),
            "max": float(np.max(matrix[:, i])),
            "mean": float(np.mean(matrix[:, i], dtype=np.float64)),
            "std": float(np.std(matrix[:, i], ddof=0, dtype=np.float64)),
            "zero_count": int(np.count_nonzero(matrix[:, i] == 0)),
            "nonfinite_count": int(np.count_nonzero(~np.isfinite(matrix[:, i]))),
        }
        for i, name in enumerate(FEATURE_NAMES)
    }


def _scalar_oracle(image: Image.Image) -> dict[str, float]:
    """Independent Python scalar loops for a small non-resized RGB image."""
    width, height = image.size
    rgb_image = image.convert("RGB")
    pixels = [rgb_image.getpixel((x, y)) for y in range(height) for x in range(width)]
    rgb = [[tuple(c / 255.0 for c in pixels[y * width + x]) for x in range(width)] for y in range(height)]
    gray = [[(0.299 * r + 0.587 * g) + 0.114 * b for r, g, b in row] for row in rgb]
    edges, flats = [], []
    for y in range(height):
        edge_row, flat_row = [], []
        for x in range(width):
            dx = gray[y][x + 1] - gray[y][x] if x < width - 1 else 0.0
            dy = gray[y + 1][x] - gray[y][x] if y < height - 1 else 0.0
            gradient = math.sqrt((dx * dx + dy * dy) / 2.0)
            edge_row.append(gradient > 0.08)
            flat_row.append(gradient < 0.02)
        edges.append(edge_row)
        flats.append(flat_row)
    total = width * height
    edge_total = sum(sum(row) for row in edges)
    tile_densities = []
    coverage = []
    for gy in range(3):
        for gx in range(3):
            positions = [(y, x) for y in range(gy * height // 3, (gy + 1) * height // 3) for x in range(gx * width // 3, (gx + 1) * width // 3)]
            coverage.extend(positions)
            if positions:
                tile_densities.append(sum(edges[y][x] for y, x in positions) / len(positions))
    if len(coverage) != total or len(set(coverage)) != total:
        raise AssertionError("Scalar grid did not cover every pixel exactly once")
    tile_mean = sum(tile_densities) / len(tile_densities)
    tile_std = math.sqrt(sum((v - tile_mean) ** 2 for v in tile_densities) / len(tile_densities))
    bh, bw = max(1, (height + 9) // 10), max(1, (width + 9) // 10)
    border_positions = {(y, x) for y in range(height) for x in range(width) if y < bh or y >= height - bh or x < bw or x >= width - bw}
    border_edges = sum(edges[y][x] for y, x in border_positions)

    def spread(horizontal: bool) -> float:
        positions = [(y / (height - 1) if height > 1 else 0.0) if horizontal else (x / (width - 1) if width > 1 else 0.0) for y in range(height) for x in range(width) if edges[y][x]]
        if not positions:
            return 0.0
        mean = sum(positions) / len(positions)
        return math.sqrt(sum((p - mean) ** 2 for p in positions) / len(positions))

    gray_values = [v for row in gray for v in row]
    gray_mean = sum(gray_values) / total
    return dict(zip(FEATURE_NAMES, (
        width / height,
        edge_total / total,
        sum(sum(row) for row in flats) / total,
        math.sqrt(sum((v - gray_mean) ** 2 for v in gray_values) / total),
        tile_std / (tile_mean + 1e-6) if edge_total else 0.0,
        border_edges / edge_total if edge_total else 0.0,
        spread(True), spread(False), gray_mean,
        sum(max(pixel) - min(pixel) for row in rgb for pixel in row) / total,
    ), strict=True))


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run_smoke(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tests: dict[str, Any] = {}
    for name, color in (("white", (255, 255, 255)), ("black", (0, 0, 0)), ("gray", (128, 128, 128))):
        image = Image.new("RGB", (17, 11), color)
        values = extract_features(image)
        assert values["edge_density"] == 0.0 and values["flat_region_ratio"] == 1.0
        assert all(values[k] == 0.0 for k in ("grid_edge_cv", "border_edge_ratio", "horizontal_projection_spread", "vertical_projection_spread", "mean_color_range"))
        assert abs(values["mean_gray"] - color[0] / 255.0) < 1e-14
        assert values["gray_std"] < 1e-14
        tests[name] = values
    pattern = Image.new("RGB", (7, 5))
    pattern.putdata([((x * 71 + y * 19) % 256, (x * 17 + y * 89) % 256, (x * 43 + y * 53) % 256) for y in range(5) for x in range(7)])
    actual, expected = extract_features(pattern), _scalar_oracle(pattern)
    oracle_error = max(abs(actual[k] - expected[k]) for k in FEATURE_NAMES)
    assert oracle_error < 1e-12
    tests["odd_7x5_scalar_oracle"] = {"max_absolute_error": oracle_error, "actual": actual, "expected": expected, "covered_pixels": 35, "deduplicated_border_pixels": 20}
    for size in ((1, 1), (1, 5), (5, 1), (2, 2)):
        small = Image.new("RGB", size, (125, 21, 224))
        assert max(abs(extract_features(small)[k] - _scalar_oracle(small)[k]) for k in FEATURE_NAMES) < 1e-12
    tests["singleton_and_empty_grid_tiles"] = "passed: 1x1,1x5,5x1,2x2; empty tiles ignored"
    exact = np.array([0.02, 0.08, np.nextafter(0.02, -np.inf), np.nextafter(0.08, np.inf)], dtype=np.float64)
    edge, flat = _classify_gradient(exact)
    assert edge.tolist() == [False, False, False, True]
    assert flat.tolist() == [False, False, True, False]
    tests["strict_thresholds"] = {"edge": edge.tolist(), "flat": flat.tolist()}
    transparent = Image.new("RGBA", (17, 11), (15, 28, 224, 0))
    assert extract_features(transparent) == extract_features(Image.new("RGB", transparent.size, "white"))
    semi = Image.new("RGBA", (17, 11), (255, 0, 0, 128))
    reference = Image.alpha_composite(Image.new("RGBA", semi.size, "white"), semi).convert("RGB")
    assert extract_features(semi) == extract_features(reference)
    palette = Image.new("P", (17, 11), 0)
    palette.putpalette([255, 0, 0] + [0, 0, 0] * 255)
    palette.info["transparency"] = 0
    assert extract_features(palette) == extract_features(Image.new("RGB", palette.size, "white"))
    tests["alpha_white_compositing"] = "passed: alpha=0, alpha=128, palette transparency"
    low = Image.new("RGB", (800, 450), "white")
    drawing = ImageDraw.Draw(low)
    drawing.rectangle((0, 0, 399, 449), fill=(42, 80, 176))
    drawing.rectangle((90, 110, 709, 339), fill=(220, 64, 48))
    high = low.resize((1600, 900), Image.Resampling.NEAREST)
    a, b = extract_features(low), extract_features(high)
    resolution_error = {k: abs(a[k] - b[k]) for k in FEATURE_NAMES}
    assert max(resolution_error.values()) < 0.025
    assert a["aspect_ratio"] == b["aspect_ratio"]
    tests["same_ratio_resolution"] = {"input_sizes": [[800, 450], [1600, 900]], "processed_sizes": [[512, 288], [512, 288]], "absolute_differences": resolution_error, "diagnostic_tolerance": 0.025, "interpretation": "statistics need not be bitwise resolution-invariant; same physical block layout remains close after fixed downsampling"}
    # Candidate selection uses train PNG pixels only, never object/label data.
    train = [r for r in rows if r["split"] == "train"]
    pool = train[:96] + [r for r in train if r["image_width"] * 3 == r["image_height"] * 4][:24]
    pool = list({r["slide_id"]: r for r in pool}.values())
    candidates = []
    for row in pool:
        values = extract_features(row["path"])
        with Image.open(row["path"]) as image:
            thumb = _rgb_image(image).resize((64, 64), Image.Resampling.BICUBIC)
            rgb = np.asarray(thumb, dtype=np.uint8)
            quantized = rgb // 32
            codes = quantized[:, :, 0].astype(np.int32) * 64 + quantized[:, :, 1].astype(np.int32) * 8 + quantized[:, :, 2]
            counts = np.bincount(codes.ravel(), minlength=512)
            probability = counts[counts > 0] / counts.sum()
            entropy = float(-np.sum(probability * np.log2(probability)))
        candidates.append((row, values, entropy))
    selected = []
    used = set()

    def choose(category: str, options: list[Any], key: Any, reverse: bool = False) -> None:
        for row, values, entropy in sorted(options, key=key, reverse=reverse):
            if row["slide_id"] not in used:
                selected.append({"category": category, "slide_id": row["slide_id"], "image_sha256": row["image_sha256"], "original_size": [row["image_width"], row["image_height"]], "features": values, "color_entropy_for_selection_only": entropy})
                used.add(row["slide_id"])
                break

    choose("16:9", [c for c in candidates if c[0]["image_width"] * 9 == c[0]["image_height"] * 16], lambda c: _numeric_id(c[0]["slide_id"]))
    choose("4:3", [c for c in candidates if c[0]["image_width"] * 3 == c[0]["image_height"] * 4], lambda c: _numeric_id(c[0]["slide_id"]))
    choose("simple: minimum edge density in fixed PNG candidate pool", candidates, lambda c: c[1]["edge_density"])
    choose("dense: maximum edge density in fixed PNG candidate pool", candidates, lambda c: c[1]["edge_density"], reverse=True)
    choose("image-like/color-rich: maximum 64px color entropy, requires human appearance check", candidates, lambda c: c[2], reverse=True)
    choose("edge-heavy: maximum border-edge share in fixed PNG candidate pool", candidates, lambda c: c[1]["border_edge_ratio"], reverse=True)
    assert len(selected) == 6
    by_id = {r["slide_id"]: r for r in rows}
    for item in selected:
        path = by_id[item["slide_id"]]["path"]
        assert extract_features(path) == extract_features(path) == item["features"]
    tests["train_smoke_pages"] = selected
    duplicates = []
    for left, right in (("slide_0383", "slide_0459"), ("slide_0514", "slide_2390"), ("slide_1312", "slide_2227")):
        first, second = by_id[left], by_id[right]
        assert first["split"] == second["split"] == "train"
        assert Path(first["path"]).read_bytes() == Path(second["path"]).read_bytes()
        equal = extract_features(first["path"]) == extract_features(second["path"])
        assert equal
        duplicates.append({"ids": [left, right], "image_sha256": first["image_sha256"], "byte_equal": True, "all_ten_features_exact_equal": equal})
    tests["known_duplicate_pairs"] = duplicates
    tests["all_results_finite"] = True
    tests["status"] = "passed"
    cache = BASE / ".cache/png_layout_smoke"
    cache.mkdir(parents=True, exist_ok=True)
    collage = Image.new("RGB", (960, 3 * 235), "#dddddd")
    canvas = ImageDraw.Draw(collage)
    for i, item in enumerate(selected):
        with Image.open(by_id[item["slide_id"]]["path"]) as image:
            preview = _rgb_image(image)
            preview.thumbnail((468, 205), Image.Resampling.BICUBIC)
            x, y = (i % 2) * 480, (i // 2) * 235
            collage.paste(preview, (x, y + 25))
            canvas.text((x + 4, y + 5), item["slide_id"] + " / " + item["category"].split(":")[0], fill="black")
    collage.save(cache / "train_smoke_contact_sheet.png")
    _atomic_json(cache / "smoke_result.json", tests)
    return tests


def extract_all(rows: list[dict[str, Any]], smoke: dict[str, Any]) -> dict[str, Any]:
    if smoke.get("status") != "passed":
        raise RuntimeError("Smoke checks must pass before formal extraction")
    values = []
    for i, row in enumerate(rows, start=1):
        features = extract_features(row["path"])
        values.append([features[name] for name in FEATURE_NAMES])
        if i % 256 == 0 or i == len(rows):
            print(json.dumps({"event": "extracted", "completed": i, "total": len(rows)}), flush=True)
    matrix = np.asarray(values, dtype=np.float64)
    if matrix.shape != (1840, 10) or not np.isfinite(matrix).all():
        raise ValueError("Formal feature matrix shape/finite check failed")
    by_hash: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        by_hash[row["image_sha256"]].append(i)
    duplicate_sets = [indices for indices in by_hash.values() if len(indices) > 1]
    for indices in duplicate_sets:
        for i in indices[1:]:
            if not np.array_equal(matrix[indices[0]], matrix[i]):
                raise ValueError("Identical PNG bytes produced different features")
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CSV_PATH.with_name(CSV_PATH.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["slide_id", *FEATURE_NAMES])
        for row, vector in zip(rows, matrix, strict=True):
            writer.writerow([row["slide_id"], *(format(float(v), ".17g") for v in vector)])
    os.replace(temporary, CSV_PATH)
    manifest = [{"slide_id": r["slide_id"], "image_sha256": r["image_sha256"]} for r in rows]
    source_statistics = {}
    for source in sorted({r["source_type"] for r in rows}):
        indices = [i for i, r in enumerate(rows) if r["source_type"] == source]
        source_statistics[source] = {"count": len(indices), "feature_statistics": _statistics(matrix[indices])}
    recipe: dict[str, Any] = {
        "feature_names": list(FEATURE_NAMES),
        "algorithm": ALGORITHM_RECIPE,
        "inputs": {
            "scope": ["train", "validation"],
            "count": len(rows),
            "split_counts": dict(Counter(r["split"] for r in rows)),
            "image_root": "data/images",
            "manifest": manifest,
            "manifest_sha256": canonical_sha256(manifest),
            "manifest_definition": "numeric slide_id ordered [{slide_id,image_sha256}]; UTF-8 JSON ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False",
            "reference_sha_index": "features/feature_index.csv; identity/hash metadata only; no .npy read",
            "metadata": "data/slideaudit/data/metadata.csv; dimensions/integrity and source-only diagnostics",
            "raw_png_validation": "all 1840 signatures, SHA256 against prior index, Pillow full decoding and metadata dimensions passed",
        },
        "split_sha256": sha256_file(SPLIT_PATH),
        "csv_sha256": sha256_file(CSV_PATH),
        "source_sha256": sha256_file(Path(__file__)),
        "versions": {"python": platform.python_version(), "numpy": np.__version__, "pillow": PILLOW_VERSION},
        "feature_statistics": _statistics(matrix),
        "source_statistics": source_statistics,
        "source_type_use": "diagnostics only; absent from numeric feature matrix and core function",
        "duplicate_pngs": {"sets": len(duplicate_sets), "pages": sum(len(s) for s in duplicate_sets), "all_ten_features_exact_equal": True, "known_pairs": smoke["known_duplicate_pairs"]},
        "serialization": "CSV UTF-8 LF; 17 significant digits, binary64 exact round trip; row order=numeric slide_id",
        "smoke_status": smoke["status"],
        "scope_guards": "runtime audit hook blocks annotations, descriptions, all .npy/.npz/.joblib, and test/excluded raw PNGs",
        "no_training": True,
        "payload_hash_definition": "canonical_sha256 of this JSON object after removing only payload_sha256",
    }
    recipe["payload_sha256"] = canonical_sha256(recipe)
    _atomic_json(RECIPE_PATH, recipe)
    return {"status": "passed", "rows": len(rows), "shape": list(matrix.shape), "csv_sha256": recipe["csv_sha256"], "recipe_payload_sha256": recipe["payload_sha256"], "input_manifest_sha256": recipe["inputs"]["manifest_sha256"], "duplicate_png_sets": len(duplicate_sets), "outputs": [str(CSV_PATH), str(RECIPE_PATH)]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-only", action="store_true", help="Run pixel-only smoke checks, do not create formal feature CSV/recipe")
    args = parser.parse_args()
    rows = build_input_rows()
    smoke = run_smoke(rows)
    if args.smoke_only:
        print(json.dumps(smoke, ensure_ascii=False, allow_nan=False, sort_keys=True))
    else:
        print(json.dumps(extract_all(rows, smoke), ensure_ascii=False, allow_nan=False, sort_keys=True))


if __name__ == "__main__":
    main()
