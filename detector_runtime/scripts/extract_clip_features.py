#!/usr/bin/env python3
"""Extract the frozen 4.3B CLIP visual embeddings, offline, without labels.

Completed, matching artifacts are validated and reused. An interrupted extraction
can be rerun from the beginning; only a complete validated matrix is published.
No image/model download, gradient computation, or annotation access occurs.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
from collections import Counter, defaultdict

BASE = Path(__file__).resolve().parents[1]
SPLIT = BASE / "reports/dataset_split.csv"
FEATURES = BASE / "features/clip_embeddings.npy"
INDEX = BASE / "features/feature_index.csv"
SMOKE = BASE / "outputs/clip_smoke_test.json"
MODEL_ID = "openai/clip-vit-base-patch32"
REVISION = "3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268"
SPLIT_SHA256 = "e70f1ff2be8cde44d7bcf8c60ed4c8bf0ba5f368062baa6980ce96131d63d487"
MODEL_HASHES = {
    "config.json": "b575ef3c36f2a057fa19e221650105052d61cc9c1a972ec15019c6261ec98770",
    "preprocessor_config.json": "910e70b3956ac9879ebc90b22fb3bc8a75b6a0677814500101a4c072bd7857bd",
    "pytorch_model.bin": "a63082132ba4f97a80bea76823f544493bffa8082296d62d71581a4feff1576f",
}
SNAPSHOT = (BASE / "models/clip_encoder")
DEFINITION = "unnormalized projected image_embeds"
FIELDS = ("row_index", "slide_id", "group_id", "split", "image_sha256",
          "model_id", "model_revision", "config_sha256", "preprocessor_sha256",
          "checkpoint_sha256", "feature_sha256", "input_manifest_sha256",
          "feature_dimension", "dtype", "feature_definition", "extraction_device",
          "batch_size")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def log(event, **values):
    print(json.dumps(dict(event=event, **values), ensure_ascii=False), flush=True)


def manifest_hash(rows):
    manifest = [{"slide_id": row["slide_id"], "image_sha256": row["image_sha256"]}
                for row in rows]
    data = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def inputs():
    if digest(SPLIT) != SPLIT_SHA256:
        raise ValueError("Frozen dataset_split.csv changed")
    with SPLIT.open(encoding="utf-8-sig", newline="") as handle:
        all_rows = list(csv.DictReader(handle))
    if (len(all_rows) != 2400 or len({r["slide_id"] for r in all_rows}) != 2400
            or Counter(r["split"] for r in all_rows)
            != Counter(train=1512, validation=328, test=324, excluded=236)):
        raise ValueError("Split uniqueness/count check failed")
    rows = sorted((r for r in all_rows if r["split"] != "excluded"),
                  key=lambda r: int(r["slide_id"][6:]))
    groups = defaultdict(list)
    for row in rows:
        if not row["group_id"]:
            raise ValueError("A formal page has no group_id")
        groups[row["group_id"]].append(row)
    if len(groups) != 541 or any(len(v) != 4 or len({r["split"] for r in v}) != 1
                                 for v in groups.values()):
        raise ValueError("Group integrity check failed")
    for name, expected in MODEL_HASHES.items():
        if digest(SNAPSHOT / name) != expected:
            raise ValueError(f"Frozen model file changed: {name}")
    smoke = json.loads(SMOKE.read_text(encoding="utf-8"))
    if (smoke["status"] != "passed" or smoke["model"]["revision"] != REVISION
            or smoke["model"]["file_sha256"] != MODEL_HASHES):
        raise ValueError("4.3B model/processor verification does not match")
    # Only image bytes, never labels/descriptions, are read to generate X.
    for i, row in enumerate(rows):
        row["image_sha256"] = digest(BASE / "data/images" / (row["slide_id"] + ".png"))
        row["row_index"] = str(i)
    return rows


def validate_matrix(values, np):
    if values.shape != (2164, 512) or values.dtype != np.float32:
        raise ValueError(f"Unexpected feature matrix: {values.shape}/{values.dtype}")
    if not np.isfinite(values).all() or not (np.linalg.norm(values, axis=1) > 0).all():
        raise ValueError("Non-finite feature values or zero feature vectors")


def reuse_if_complete(rows, np):
    if not (FEATURES.is_file() and INDEX.is_file()):
        return False
    with INDEX.open(encoding="utf-8", newline="") as handle:
        existing = list(csv.DictReader(handle))
    matrix_hash = digest(FEATURES)
    source_hash = manifest_hash(rows)
    if len(existing) != len(rows):
        raise ValueError("Existing feature index length mismatch; refusing to mix artifacts")
    for old, row in zip(existing, rows):
        expected = index_row(row, matrix_hash, source_hash, old["extraction_device"])
        if old != expected:
            raise ValueError("Existing feature/index/input recipe differs; refusing replacement")
    values = np.load(FEATURES, allow_pickle=False)
    validate_matrix(values, np)
    log("reused_verified_features", shape=list(values.shape), feature_sha256=matrix_hash,
        input_manifest_sha256=source_hash, model_revision=REVISION)
    return True


def index_row(row, matrix_hash, source_hash, device):
    return dict(row_index=row["row_index"], slide_id=row["slide_id"],
                group_id=row["group_id"], split=row["split"],
                image_sha256=row["image_sha256"], model_id=MODEL_ID,
                model_revision=REVISION, config_sha256=MODEL_HASHES["config.json"],
                preprocessor_sha256=MODEL_HASHES["preprocessor_config.json"],
                checkpoint_sha256=MODEL_HASHES["pytorch_model.bin"],
                feature_sha256=matrix_hash, input_manifest_sha256=source_hash,
                feature_dimension="512", dtype="float32", feature_definition=DEFINITION,
                extraction_device=device, batch_size="1")


def main():
    if not os.path.samefile(sys.executable, BASE / ".venv/Scripts/python.exe"):
        raise RuntimeError("Use detector_runtime/.venv/Scripts/python.exe")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
                      HF_HOME=str(BASE / ".cache/huggingface"), TOKENIZERS_PARALLELISM="false")
    import numpy as np
    rows = inputs()
    if reuse_if_complete(rows, np):
        return
    import torch
    from PIL import Image
    from transformers import CLIPVisionModelWithProjection
    try:
        from transformers import CLIPImageProcessorPil as CLIPImageProcessor
    except ImportError:
        from transformers import CLIPImageProcessor
    processor = CLIPImageProcessor.from_pretrained(str(SNAPSHOT), local_files_only=True)
    model, loading = CLIPVisionModelWithProjection.from_pretrained(
        str(SNAPSHOT), local_files_only=True, torch_dtype=torch.float32,
        use_safetensors=False, output_loading_info=True)
    if any(loading.get(name) for name in ("missing_keys", "mismatched_keys", "error_msgs")):
        raise ValueError("Pretrained visual/projection weights did not fully load")
    model.requires_grad_(False)
    model.eval()
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    initial_device = device
    try:
        model.to(device)
    except torch.OutOfMemoryError:
        model.to("cpu")
        torch.cuda.empty_cache()
        device = "cpu"
    values = np.empty((len(rows), 512), dtype=np.float32)
    row_devices = []
    start = time.monotonic()
    log("started", count=len(rows), batch_size=1, device=device, model_revision=REVISION,
        processor=type(processor).__name__, model_training=model.training)

    # Batch size 1 is retained for all pages to fit the 4 GiB GPU comfortably.
    # Keep only one decoded PNG and one GPU batch at a time.
    def vector(rgb):
        encoded = processor(images=rgb, return_tensors="pt")
        pixels = encoded["pixel_values"].to(device=device, dtype=torch.float32)
        return model(pixel_values=pixels).image_embeds.detach().cpu().float().numpy()[0]

    with torch.inference_mode():
        for i, row in enumerate(rows):
            path = BASE / "data/images" / (row["slide_id"] + ".png")
            # Verify the exact bytes immediately before decoding the same bytes.
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != row["image_sha256"]:
                raise ValueError(f"Input image changed during extraction: {row['slide_id']}")
            with Image.open(io.BytesIO(data)) as opened:
                opened.load()
                if opened.format != "PNG":
                    raise ValueError("Formal image is not a PNG")
                rgb = opened.convert("RGB")
            try:
                result = vector(rgb)
            except torch.OutOfMemoryError:
                if device == "cpu":
                    raise
                model.to("cpu")
                torch.cuda.empty_cache()
                device = "cpu"
                log("oom_cpu_fallback", row=i, slide_id=row["slide_id"])
                result = vector(rgb)
            finally:
                rgb.close()
            if result.shape != (512,) or not np.isfinite(result).all() or np.linalg.norm(result) <= 0:
                raise ValueError(f"Invalid image vector: {row['slide_id']}")
            values[i] = result
            row_devices.append(device)
            if (i + 1) % 64 == 0 or i + 1 == len(rows):
                log("progress", completed=i + 1, total=len(rows), device=device,
                    elapsed_seconds=round(time.monotonic() - start, 1))
    validate_matrix(values, np)
    if digest(SPLIT) != SPLIT_SHA256:
        raise ValueError("Split changed during extraction")
    FEATURES.parent.mkdir(exist_ok=True)
    temporary_features = FEATURES.with_suffix(".npy.part")
    temporary_index = INDEX.with_suffix(".csv.part")
    with temporary_features.open("wb") as handle:
        np.save(handle, values, allow_pickle=False)
    matrix_hash = digest(temporary_features)
    source_hash = manifest_hash(rows)
    with temporary_index.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(index_row(row, matrix_hash, source_hash, device_used)
                         for row, device_used in zip(rows, row_devices))
    temporary_features.replace(FEATURES)
    temporary_index.replace(INDEX)
    log("completed", shape=list(values.shape), dtype=str(values.dtype),
        feature_sha256=matrix_hash, input_manifest_sha256=source_hash,
        model_revision=REVISION, device_counts=dict(Counter(row_devices)),
        cpu_fallback=initial_device != device, elapsed_seconds=round(time.monotonic() - start, 1))


if __name__ == "__main__":
    main()
