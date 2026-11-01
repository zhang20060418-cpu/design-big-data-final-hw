#!/usr/bin/env python3
"""4.3D: offline, frozen CLIP letterbox experiment on train/validation only.

White padding and bicubic interpolation are fixed before validation results.
No official data, existing features/model/report, or test image is modified.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import random
import sys
import time
from collections import Counter, defaultdict

BASE = Path(__file__).resolve().parents[1]
SPLIT = BASE / "reports/dataset_split.csv"
OLD_INDEX = BASE / "features/feature_index.csv"
FEATURES = BASE / "features/clip_embeddings_full_slide_trainval.npy"
INDEX = BASE / "features/full_slide_feature_index.csv"
GALLERY = BASE / "reports/full_slide_preprocessing_comparison.png"
REVISION = "3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268"
SPLIT_SHA = "e70f1ff2be8cde44d7bcf8c60ed4c8bf0ba5f368062baa6980ce96131d63d487"
HASHES = {
    "config.json": "b575ef3c36f2a057fa19e221650105052d61cc9c1a972ec15019c6261ec98770",
    "preprocessor_config.json": "910e70b3956ac9879ebc90b22fb3bc8a75b6a0677814500101a4c072bd7857bd",
    "pytorch_model.bin": "a63082132ba4f97a80bea76823f544493bffa8082296d62d71581a4feff1576f",
}
SNAPSHOT = BASE / "models/huggingface/models--openai--clip-vit-base-patch32/snapshots" / REVISION
RECIPE = {
    "model_id": "openai/clip-vit-base-patch32", "revision": REVISION,
    "resize": "aspect-preserving fit; scale=224/max(width,height); round each dimension",
    "interpolation": "Pillow.Resampling.BICUBIC", "padding_rgb": [255, 255, 255],
    "placement": "center; floor left/top; remaining pixel right/bottom",
    "canvas": [224, 224], "processor_do_resize": False,
    "processor_do_center_crop": False, "processor_do_rescale": True,
    "processor_rescale_factor": 1 / 255,
    "image_mean": [0.48145466, 0.4578275, 0.40821073],
    "image_std": [0.26862954, 0.26130258, 0.27577711],
    "feature_definition": "unnormalized projected image_embeds",
    "batch_size": 1, "dtype": "float32", "model_file_sha256": HASHES,
}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def json_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def log(event, **data):
    print(json.dumps(dict(event=event, **data), ensure_ascii=False), flush=True)


def install_test_guard(split_rows):
    """Forbid even accidentally opening a held-out raw page or test artifact."""
    denied = set()
    for row in split_rows:
        if row["split"] not in ("train", "validation"):
            sid = row["slide_id"]
            denied.add(os.path.normcase(str(BASE / "data/images" / f"{sid}.png")))
            for kind in ("annotations", "descriptions"):
                denied.add(os.path.normcase(str(BASE / f"data/slideaudit/data/{kind}/{sid}.json")))
    for name in ("models/clip_lr_4labels.joblib", "reports/clip_lr_results.md", "reports/test_predictions.csv"):
        denied.add(os.path.normcase(str(BASE / name)))

    def guard(event, args):
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = os.path.normcase(os.path.abspath(os.fsdecode(args[0])))
            if path in denied:
                raise RuntimeError("4.3D forbids held-out/excluded data or old test artifacts: " + path)
    sys.addaudithook(guard)


def load_rows():
    if digest(SPLIT) != SPLIT_SHA:
        raise ValueError("Accepted split changed")
    split_rows = read_csv(SPLIT)
    install_test_guard(split_rows)
    allowed = {r["slide_id"]: r for r in split_rows if r["split"] in ("train", "validation")}
    if len(allowed) != 1840 or Counter(r["split"] for r in allowed.values()) != Counter(train=1512, validation=328):
        raise ValueError("Wrong train/validation IDs or counts")
    old = [r for r in read_csv(OLD_INDEX) if r["slide_id"] in allowed]
    if len(old) != 1840 or len({r["slide_id"] for r in old}) != 1840:
        raise ValueError("Baseline feature index mismatch")
    old.sort(key=lambda r: int(r["slide_id"][6:]))
    groups = defaultdict(list)
    rows = []
    for i, previous in enumerate(old):
        sid = previous["slide_id"]
        split = allowed[sid]
        if (previous["group_id"], previous["split"], previous["model_revision"]) != (split["group_id"], split["split"], REVISION):
            raise ValueError("Baseline/split/CLIP provenance disagreement")
        image_hash = digest(BASE / f"data/images/{sid}.png")
        if image_hash != previous["image_sha256"]:
            raise ValueError("A train/validation image changed since 4.3C")
        rows.append(dict(row_index=str(i), baseline_row_index=previous["row_index"],
                         slide_id=sid, group_id=split["group_id"], split=split["split"], image_sha256=image_hash))
        groups[split["group_id"]].append(split)
    if len(groups) != 460 or any(len(v) != 4 or len({r["split"] for r in v}) != 1 for v in groups.values()):
        raise ValueError("Train/validation source groups are not intact/disjoint")
    for name, value in HASHES.items():
        if digest(SNAPSHOT / name) != value:
            raise ValueError("Frozen CLIP model/processor changed")
    return rows


def letterbox(rgb):
    from PIL import Image
    scale = 224 / max(rgb.size)
    width, height = (max(1, min(224, int(round(v * scale)))) for v in rgb.size)
    left, top = (224 - width) // 2, (224 - height) // 2
    resized = rgb.resize((width, height), resample=Image.Resampling.BICUBIC)
    canvas = Image.new("RGB", (224, 224), (255, 255, 255))
    canvas.paste(resized, (left, top))
    resized.close()
    return canvas, dict(content_width=str(width), content_height=str(height),
                        pad_left=str(left), pad_top=str(top),
                        pad_right=str(224 - width - left), pad_bottom=str(224 - height - top))


def encoded_canvas(processor, canvas, np):
    result = processor(images=canvas, do_resize=False, do_center_crop=False,
                       do_rescale=True, do_normalize=True, return_tensors="pt")
    values = result["pixel_values"].numpy()
    expected = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1) / 255
    expected = (expected - np.asarray(RECIPE["image_mean"], dtype=np.float32)[:, None, None]) / np.asarray(RECIPE["image_std"], dtype=np.float32)[:, None, None]
    if values.shape != (1, 3, 224, 224) or not np.allclose(values[0], expected, atol=1e-6, rtol=1e-6):
        raise ValueError("Processor altered the full canvas or normalization")
    return result


def gallery(rows, processor, np):
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    rng = random.Random(42)
    selected = []
    for subset in ("train", "validation"):
        selected.extend(rng.sample([r for r in rows if r["split"] == subset], 3))
    width, row_height, header = 1260, 330, 65
    sheet = Image.new("RGB", (width, header + row_height * len(selected)), "#e8edf2")
    draw = ImageDraw.Draw(sheet)
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = ImageFont.truetype(str(font_path), 18) if font_path.exists() else ImageFont.load_default(size=18)
    small = ImageFont.truetype(str(font_path), 15) if font_path.exists() else ImageFont.load_default(size=15)
    for j, text in enumerate(("Original page (all content)", "Official center crop: 224 x 224", "Full page + WHITE padding: 224 x 224")):
        draw.text((15 + j * 420, 20), text, fill="#122333", font=font)
    for i, row in enumerate(selected):
        y = header + i * row_height
        with Image.open(BASE / f"data/images/{row['slide_id']}.png") as opened:
            rgb = opened.convert("RGB")
        actual = processor(images=rgb, do_rescale=False, do_normalize=False, return_tensors="np")["pixel_values"][0]
        cropped = Image.fromarray(actual.transpose(1, 2, 0).astype(np.uint8))
        padded, geometry = letterbox(rgb)
        encoded_canvas(processor, padded, np)
        for j, picture in enumerate((rgb, cropped, padded)):
            preview = ImageOps.contain(picture, (396, 270), method=Image.Resampling.NEAREST if j else Image.Resampling.BICUBIC)
            x = j * 420 + (420 - preview.width) // 2
            sheet.paste(preview, (x, y + 32 + (270 - preview.height) // 2))
            preview.close()
        draw.text((12, y + 7), f"{row['slide_id']} / {row['split']} / {rgb.width} x {rgb.height}", fill="#182b3a", font=small)
        draw.text((852, y + 7), f"Content: {geometry['content_width']} x {geometry['content_height']}", fill="#182b3a", font=small)
        draw.line((0, y + row_height - 1, width, y + row_height - 1), fill="#c0cad3")
        rgb.close(); cropped.close(); padded.close()
    GALLERY.parent.mkdir(exist_ok=True)
    temporary = GALLERY.with_suffix(".png.part")
    sheet.save(temporary, format="PNG")
    temporary.replace(GALLERY)
    log("gallery_created", seed=42, sample_ids=[r["slide_id"] for r in selected])


def feature_row(row, feature_hash, input_hash, device):
    return dict(row, model_revision=REVISION, config_sha256=HASHES["config.json"],
                preprocessor_sha256=HASHES["preprocessor_config.json"], checkpoint_sha256=HASHES["pytorch_model.bin"],
                feature_sha256=feature_hash, input_manifest_sha256=input_hash,
                recipe_sha256=json_digest(RECIPE), padding_rgb="255,255,255",
                interpolation="Pillow.Resampling.BICUBIC", extraction_device=device,
                feature_dimension="512", dtype="float32", feature_definition=RECIPE["feature_definition"], batch_size="1")


def main():
    if not os.path.samefile(sys.executable, BASE / ".venv/Scripts/python.exe"):
        raise RuntimeError("Use ml_stage4/.venv/Scripts/python.exe")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                      HF_HOME=str(BASE / "models/huggingface"), TOKENIZERS_PARALLELISM="false")
    import numpy as np
    rows = load_rows()
    input_hash = json_digest([{"slide_id": r["slide_id"], "image_sha256": r["image_sha256"]} for r in rows])
    if FEATURES.exists() and INDEX.exists():
        existing = read_csv(INDEX)
        feature_hash = digest(FEATURES)
        values = np.load(FEATURES, allow_pickle=False)
        if len(existing) != 1840 or values.shape != (1840, 512) or values.dtype != np.float32 or not np.isfinite(values).all() or not (np.linalg.norm(values, axis=1) > 0).all():
            raise ValueError("Existing full-slide feature artifacts invalid")
        for old, row in zip(existing, rows):
            expected = feature_row(row, feature_hash, input_hash, old["extraction_device"])
            if any(old.get(key) != value for key, value in expected.items()):
                raise ValueError("Cached feature/input/recipe mismatch; refusing mixed experiment")
        if GALLERY.is_file():
            log("reused_verified_features", shape=list(values.shape), feature_sha256=feature_hash)
            return
    import torch
    from PIL import Image
    from transformers import CLIPVisionModelWithProjection
    try:
        from transformers import CLIPImageProcessorPil as CLIPImageProcessor
    except ImportError:
        from transformers import CLIPImageProcessor
    processor = CLIPImageProcessor.from_pretrained(str(SNAPSHOT), local_files_only=True)
    if not np.array_equal(processor.image_mean, RECIPE["image_mean"]) or not np.array_equal(processor.image_std, RECIPE["image_std"]):
        raise ValueError("Original normalization parameters changed")
    log("processor_confirmed", actual_config=processor.to_dict(), recipe=RECIPE)
    gallery(rows, processor, np)
    model, loading = CLIPVisionModelWithProjection.from_pretrained(str(SNAPSHOT), local_files_only=True,
        torch_dtype=torch.float32, use_safetensors=False, output_loading_info=True)
    if any(loading.get(k) for k in ("missing_keys", "mismatched_keys", "error_msgs")):
        raise ValueError("Visual/projection checkpoint not fully loaded")
    model.requires_grad_(False)
    model.eval()
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    try:
        model.to(device)
    except torch.OutOfMemoryError:
        model.to("cpu"); torch.cuda.empty_cache(); device = "cpu"
    values = np.empty((1840, 512), dtype=np.float32)
    devices = []
    start = time.monotonic()
    with torch.inference_mode():
        for i, row in enumerate(rows):
            raw = (BASE / f"data/images/{row['slide_id']}.png").read_bytes()
            if hashlib.sha256(raw).hexdigest() != row["image_sha256"]:
                raise ValueError("Input PNG changed")
            with Image.open(io.BytesIO(raw)) as opened:
                opened.load()
                if opened.format != "PNG":
                    raise ValueError("Input is not PNG")
                rgb = opened.convert("RGB")
            canvas, geometry = letterbox(rgb)
            row.update(geometry)
            row["original_width"], row["original_height"] = str(rgb.width), str(rgb.height)
            rgb.close()
            encoded = encoded_canvas(processor, canvas, np)
            canvas.close()

            def infer():
                pixels = encoded["pixel_values"].to(device=device, dtype=torch.float32)
                return model(pixel_values=pixels).image_embeds.detach().cpu().float().numpy()[0]
            try:
                vector = infer()
            except torch.OutOfMemoryError:
                if device == "cpu":
                    raise
                model.to("cpu"); torch.cuda.empty_cache(); device = "cpu"
                log("oom_cpu_fallback", completed=i)
                vector = infer()
            if vector.shape != (512,) or not np.isfinite(vector).all() or np.linalg.norm(vector) <= 0:
                raise ValueError("Non-finite/zero/malformed full-slide vector")
            values[i] = vector
            devices.append(device)
            if (i + 1) % 64 == 0 or i + 1 == 1840:
                log("progress", completed=i + 1, total=1840, device=device, elapsed_seconds=round(time.monotonic() - start, 1))
    if digest(SPLIT) != SPLIT_SHA:
        raise ValueError("Split changed")
    temporary = FEATURES.with_suffix(".npy.part")
    with temporary.open("wb") as handle:
        np.save(handle, values, allow_pickle=False)
    feature_hash = digest(temporary)
    index_rows = [feature_row(r, feature_hash, input_hash, d) for r, d in zip(rows, devices)]
    temporary_index = INDEX.with_suffix(".csv.part")
    with temporary_index.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(index_rows[0]))
        writer.writeheader(); writer.writerows(index_rows)
    temporary.replace(FEATURES); temporary_index.replace(INDEX)
    log("completed", shape=list(values.shape), dtype=str(values.dtype), device_counts=dict(Counter(devices)),
        feature_sha256=feature_hash, input_manifest_sha256=input_hash, recipe_sha256=json_digest(RECIPE),
        elapsed_seconds=round(time.monotonic() - start, 1), test_pages_read=0)


if __name__ == "__main__":
    main()
