#!/usr/bin/env python3
"""4.3E frozen CLIP: left/center/right square crops, train/validation only.

Keep all old artifacts intact. No annotations, downloads, training, or test
image/feature access. A separate index binds every row and preprocessing recipe.
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import sys
import time
from collections import Counter

from extract_full_slide_features import (
    BASE, HASHES, REVISION, SNAPSHOT, SPLIT, SPLIT_SHA,
    digest, encoded_canvas, json_digest, load_rows, log, read_csv,
)

FEATURES = BASE / "features/multiregion_embeddings_trainval.npy"
INDEX = BASE / "features/multiregion_feature_index.csv"
FULL_INDEX = BASE / "features/full_slide_feature_index.csv"
RECIPE = {
    "model_id": "openai/clip-vit-base-patch32", "revision": REVISION,
    "region_order": ["left", "center", "right"], "region_count": 3,
    "landscape": "side=height; x=[0,(width-height)//2,width-height]; y=0",
    "portrait": "white-pad horizontally to height x height; repeat that square three times",
    "ultrawide": "reject width>3*height, since three height-sized squares cannot cover it",
    "interpolation": "Pillow.Resampling.BICUBIC", "region_resize": [224, 224],
    "processor_do_resize": False, "processor_do_center_crop": False,
    "processor_do_rescale": True, "processor_rescale_factor": 1 / 255,
    "image_mean": [0.48145466, 0.4578275, 0.40821073],
    "image_std": [0.26862954, 0.26130258, 0.27577711],
    "feature_definition": "unnormalized projected image_embeds",
    "feature_shape_per_page": [3, 512], "dtype": "float32", "batch_size": 1,
    "model_file_sha256": HASHES,
}


def source_rows():
    # Reuse only the previous extractor's safe train/val input audit. It never
    # reads labels or any feature matrix, and installs the held-out-file guard.
    rows = load_rows()
    full = read_csv(FULL_INDEX)
    if len(full) != 1840:
        raise ValueError("Full-slide index must contain train/validation only")
    for row, old in zip(rows, full):
        for name in ("row_index", "baseline_row_index", "slide_id", "group_id", "split", "image_sha256"):
            if row[name] != old[name]:
                raise ValueError("Old full-slide index/input mapping changed")
        if old["model_revision"] != REVISION:
            raise ValueError("Old full-slide model revision changed")
    return rows


def geometry(width, height):
    if width >= height:
        if width > 3 * height:
            raise ValueError("Three fixed square crops cannot cover this ultra-wide page")
        xs = (0, (width - height) // 2, width - height)
        if xs[1] > height or xs[2] > xs[1] + height:
            raise ValueError("Fixed crops have a coverage gap")
        rule = "landscape_square_crops"
    else:
        xs = (0, 0, 0)
        rule = "portrait_white_pad_square_repeat"
    return dict(original_width=str(width), original_height=str(height),
                crop_left_x=str(xs[0]), crop_center_x=str(xs[1]), crop_right_x=str(xs[2]),
                crop_side=str(height), safe_rule=rule)


def index_row(row, feature_hash, input_hash, source_index_hash, device):
    return dict(row, model_revision=REVISION, config_sha256=HASHES["config.json"],
                preprocessor_sha256=HASHES["preprocessor_config.json"],
                checkpoint_sha256=HASHES["pytorch_model.bin"],
                feature_sha256=feature_hash, input_manifest_sha256=input_hash,
                source_full_slide_index_sha256=source_index_hash,
                recipe_sha256=json_digest(RECIPE), region_count="3", feature_dimension="512",
                feature_shape="3x512", dtype="float32", region_order="left|center|right",
                feature_definition=RECIPE["feature_definition"],
                interpolation=RECIPE["interpolation"], batch_size="1", extraction_device=device)


def validate(values, np):
    if values.shape != (1840, 3, 512) or values.dtype != np.float32:
        raise ValueError("Expected float32 multi-region array of shape (1840,3,512)")
    if not np.isfinite(values).all() or not (np.linalg.norm(values, axis=2) > 0).all():
        raise ValueError("Invalid multi-region values or zero region vectors")


def main():
    if not os.path.samefile(sys.executable, BASE / ".venv/Scripts/python.exe"):
        raise RuntimeError("Use ml_stage4/.venv/Scripts/python.exe")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                      HF_HOME=str(BASE / "models/huggingface"), TOKENIZERS_PARALLELISM="false")
    import numpy as np
    from PIL import Image
    rows = source_rows()
    source_index_hash = digest(FULL_INDEX)
    input_hash = json_digest([{"slide_id": r["slide_id"], "image_sha256": r["image_sha256"]} for r in rows])
    if FEATURES.is_file() and INDEX.is_file():
        existing = read_csv(INDEX)
        values = np.load(FEATURES, allow_pickle=False)
        validate(values, np)
        feature_hash = digest(FEATURES)
        if len(existing) != len(rows):
            raise ValueError("Cached index length differs")
        for row, old in zip(rows, existing):
            expected = index_row(row, feature_hash, input_hash, source_index_hash, old["extraction_device"])
            if any(old.get(k) != value for k, value in expected.items()):
                raise ValueError("Cached region features/input/recipe differ")
            expected_geometry = geometry(int(old["original_width"]), int(old["original_height"]))
            if any(old.get(k) != value for k, value in expected_geometry.items()):
                raise ValueError("Cached region geometry differs")
        log("reused_verified_multiregion", shape=list(values.shape), feature_sha256=feature_hash,
            recipe_sha256=json_digest(RECIPE), test_data_accessed=False)
        return
    import torch
    from transformers import CLIPVisionModelWithProjection
    try:
        from transformers import CLIPImageProcessorPil as CLIPImageProcessor
    except ImportError:
        from transformers import CLIPImageProcessor
    processor = CLIPImageProcessor.from_pretrained(str(SNAPSHOT), local_files_only=True)
    if not np.array_equal(processor.image_mean, RECIPE["image_mean"]) or not np.array_equal(processor.image_std, RECIPE["image_std"]):
        raise ValueError("Frozen CLIP normalization changed")
    model, loading = CLIPVisionModelWithProjection.from_pretrained(
        str(SNAPSHOT), local_files_only=True, torch_dtype=torch.float32,
        use_safetensors=False, output_loading_info=True)
    if any(loading.get(k) for k in ("missing_keys", "mismatched_keys", "error_msgs")):
        raise ValueError("Pretrained visual/projection weights did not fully load")
    model.requires_grad_(False)
    model.eval()
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    try:
        model.to(device)
    except torch.OutOfMemoryError:
        model.to("cpu"); torch.cuda.empty_cache(); device = "cpu"
    values = np.empty((1840, 3, 512), dtype=np.float32)
    devices = []
    start = time.monotonic()
    log("started", pages=1840, regions=3, batch_size=1, device=device, model_revision=REVISION,
        recipe_sha256=json_digest(RECIPE))
    with torch.inference_mode():
        for i, row in enumerate(rows):
            data = (BASE / f"data/images/{row['slide_id']}.png").read_bytes()
            if hashlib.sha256(data).hexdigest() != row["image_sha256"]:
                raise ValueError("Input PNG changed")
            with Image.open(io.BytesIO(data)) as opened:
                opened.load()
                if opened.format != "PNG":
                    raise ValueError("Input is not PNG")
                rgb = opened.convert("RGB")
            row.update(geometry(rgb.width, rgb.height))
            side = rgb.height
            if rgb.width < side:
                padded = Image.new("RGB", (side, side), (255, 255, 255))
                padded.paste(rgb, ((side - rgb.width) // 2, 0))
                rgb.close(); rgb = padded
            for j, key in enumerate(("crop_left_x", "crop_center_x", "crop_right_x")):
                left = int(row[key])
                square = rgb.crop((left, 0, left + side, side))
                resized = square.resize((224, 224), resample=Image.Resampling.BICUBIC)
                square.close()
                # This checks every pixel after preprocessing against the exact
                # 224x224 RGB crop, proving there is no subsequent crop/resize.
                encoded = encoded_canvas(processor, resized, np)
                resized.close()

                def infer():
                    pixels = encoded["pixel_values"].to(device=device, dtype=torch.float32)
                    return model(pixel_values=pixels).image_embeds.detach().cpu().float().numpy()[0]
                try:
                    vector = infer()
                except torch.OutOfMemoryError:
                    if device == "cpu":
                        raise
                    model.to("cpu"); torch.cuda.empty_cache(); device = "cpu"
                    log("oom_cpu_fallback", page=i, region=j)
                    vector = infer()
                if vector.shape != (512,) or not np.isfinite(vector).all() or np.linalg.norm(vector) <= 0:
                    raise ValueError("Invalid region image embedding")
                values[i, j] = vector
            rgb.close()
            devices.append(device)
            if (i + 1) % 64 == 0 or i + 1 == 1840:
                log("progress", completed_pages=i + 1, total_pages=1840, device=device,
                    elapsed_seconds=round(time.monotonic() - start, 1))
    validate(values, np)
    if digest(SPLIT) != SPLIT_SHA or digest(FULL_INDEX) != source_index_hash:
        raise ValueError("Source split/index changed during extraction")
    temporary = FEATURES.with_suffix(".npy.part")
    with temporary.open("wb") as handle:
        np.save(handle, values, allow_pickle=False)
    feature_hash = digest(temporary)
    index_rows = [index_row(r, feature_hash, input_hash, source_index_hash, d) for r, d in zip(rows, devices)]
    temporary_index = INDEX.with_suffix(".csv.part")
    with temporary_index.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(index_rows[0]))
        writer.writeheader(); writer.writerows(index_rows)
    temporary.replace(FEATURES); temporary_index.replace(INDEX)
    log("completed", shape=list(values.shape), dtype=str(values.dtype),
        model_revision=REVISION, feature_sha256=feature_hash,
        input_manifest_sha256=input_hash, recipe_sha256=json_digest(RECIPE),
        device_counts=dict(Counter(devices)), safe_rules=dict(Counter(r["safe_rule"] for r in rows)),
        elapsed_seconds=round(time.monotonic() - start, 1), test_data_accessed=False)


if __name__ == "__main__":
    main()
