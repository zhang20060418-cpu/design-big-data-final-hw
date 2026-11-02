#!/usr/bin/env python3
"""Run only eight frozen-CLIP image feature checks in detector_runtime/.venv.

The public model revision is resolved once and retained in the JSON result.
An existing compatible Hugging Face snapshot is reused before any download.
Only config.json, preprocessor_config.json and one checkpoint are requested.
No labels/descriptions are read and no classifier is fitted.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone, timedelta

BASE = Path(__file__).resolve().parents[1]
VENV_PYTHON = BASE / ".venv/Scripts/python.exe"
SPLIT = BASE / "reports/dataset_split.csv"
RESULT = BASE / "outputs/clip_smoke_test.json"
FEATURES = BASE / "features/clip_smoke_features.npy"
CACHE = BASE / ".cache/huggingface"
MODEL_ID = "openai/clip-vit-base-patch32"
MODEL_REVISION = "3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268"
CHECKPOINT_SHA256 = "a63082132ba4f97a80bea76823f544493bffa8082296d62d71581a4feff1576f"
SPLIT_SHA256 = "e70f1ff2be8cde44d7bcf8c60ed4c8bf0ba5f368062baa6980ce96131d63d487"
# Selected without labels from the 60 PNG files already present before 4.3B.
SLIDE_IDS = ("slide_0001", "slide_0003", "slide_0007", "slide_0011",
             "slide_0013", "slide_0015", "slide_0016", "slide_0021")
# The verified fixed revision publishes one PyTorch checkpoint, no safetensors.
CHECKPOINTS = ("pytorch_model.bin",)


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_result(result):
    RESULT.parent.mkdir(exist_ok=True)
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cache_roots():
    roots = [CACHE]
    for key in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(key):
            roots.append(Path(os.environ[key]))
    if os.environ.get("HF_HOME"):
        roots.append(Path(os.environ["HF_HOME"]) / "hub")
    roots.append(Path.home() / ".cache/huggingface/hub")
    return list(dict.fromkeys(p.resolve() for p in roots))


def compatible_snapshot(path):
    if not (path / "config.json").is_file() or not (path / "preprocessor_config.json").is_file():
        return None
    try:
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
        if config.get("model_type") not in ("clip", "clip_vision_model"):
            return None
        if int(config.get("projection_dim", 512)) != 512:
            return None
    except (OSError, ValueError, TypeError):
        return None
    return next((name for name in CHECKPOINTS if (path / name).is_file()), None)


def obtain_snapshot(result, previous, roots, offline):
    from huggingface_hub import HfApi, hf_hub_download

    previous_model = previous.get("model", {})
    revision = previous_model.get("revision", MODEL_REVISION) if previous_model.get("id") == MODEL_ID else MODEL_REVISION
    if revision != MODEL_REVISION:
        raise ValueError("Model revision differs from the frozen experiment revision")
    if revision is not None and re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("Previous result contains an invalid immutable model revision")
    packaged = BASE / "models/clip_encoder"
    packaged_checkpoint = compatible_snapshot(packaged)
    if packaged_checkpoint:
        result["model"] = dict(id=MODEL_ID, revision=MODEL_REVISION, snapshot=str(packaged),
                               checkpoint=packaged_checkpoint, source="bundled_frozen_encoder",
                               random_initialization=False)
        write_result(result)
        return packaged, packaged_checkpoint
    repo_dir = "models--" + MODEL_ID.replace("/", "--")
    snapshots = []
    for root in roots:
        folder = root / repo_dir / "snapshots"
        if folder.is_dir():
            snapshots.extend(p for p in sorted(folder.iterdir())
                             if p.is_dir() and re.fullmatch(r"[0-9a-f]{40}", p.name))
    result["model_cache_roots_checked"] = [str(p) for p in roots]
    for path in snapshots:
        if revision is not None and path.name != revision:
            continue
        checkpoint = compatible_snapshot(path)
        if checkpoint:
            result["model"] = dict(id=MODEL_ID, revision=path.name, snapshot=str(path),
                                   checkpoint=checkpoint, source="reused_complete_cache",
                                   random_initialization=False)
            write_result(result)
            return path, checkpoint
    if offline:
        raise RuntimeError("No complete compatible frozen CLIP snapshot exists; offline mode cannot download")
    # Anonymous access avoids unrelated/expired HF_TOKEN values in the host.
    info = HfApi(token=False).model_info(MODEL_ID, revision=revision, token=False)
    if re.fullmatch(r"[0-9a-f]{40}", info.sha or "") is None:
        raise ValueError("Hugging Face did not return an immutable 40-character model commit")
    if revision is not None and info.sha != revision:
        raise ValueError("Previously frozen model revision did not resolve identically")
    names = {item.rfilename for item in info.siblings}
    checkpoint = next((name for name in CHECKPOINTS if name in names), None)
    required = ("config.json", "preprocessor_config.json", checkpoint)
    if checkpoint is None or not all(name in names for name in required):
        raise ValueError("Pinned public model lacks the required image-only files")
    result["model"] = dict(id=MODEL_ID, revision=info.sha, checkpoint=checkpoint,
                           source="pinned_public_huggingface", requested_files=list(required),
                           random_initialization=False)
    # Persist the pin before downloading weights; a failed run reuses this pin.
    write_result(result)
    CACHE.mkdir(parents=True, exist_ok=True)
    paths = [Path(hf_hub_download(MODEL_ID, filename=name, revision=info.sha,
                                cache_dir=str(CACHE), token=False)) for name in required]
    snapshot = paths[0].parent
    if compatible_snapshot(snapshot) != checkpoint:
        raise ValueError("Downloaded frozen snapshot failed the compatibility check")
    result["model"]["snapshot"] = str(snapshot)
    return snapshot, checkpoint


def run(result, previous, offline):
    roots = cache_roots()
    # All subsequent model-cache writes stay on the project's D drive.
    os.environ.update(HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_XET="1",
                      HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false",
                      HF_HOME=str(CACHE), HF_HUB_CACHE=str(CACHE),
                      HUGGINGFACE_HUB_CACHE=str(CACHE), PYTHONDONTWRITEBYTECODE="1")
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
    import numpy as np
    from PIL import Image
    import torch
    from transformers import CLIPVisionModelWithProjection
    try:
        from transformers import CLIPImageProcessorPil as CLIPImageProcessor
    except ImportError:
        from transformers import CLIPImageProcessor

    result["packages"] = {}
    for name in ("torch", "torchvision", "transformers", "huggingface-hub",
                 "safetensors", "numpy", "Pillow", "scikit-learn"):
        try:
            result["packages"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result["packages"][name] = "not_installed"
    before = sha256(SPLIT)
    if before != SPLIT_SHA256:
        raise ValueError("Accepted dataset_split.csv changed; this smoke test will not reinterpret it")
    with SPLIT.open(encoding="utf-8-sig", newline="") as handle:
        splits = {row["slide_id"]: row["split"] for row in csv.DictReader(handle)}
    if any(splits.get(sid) != "train" for sid in SLIDE_IDS):
        raise ValueError("Every smoke-test input must belong to the accepted train split")
    images = []
    result["inputs"] = []
    for sid in SLIDE_IDS:
        path = BASE / f"data/images/{sid}.png"
        with Image.open(path) as opened:
            opened.load()
            if opened.format != "PNG":
                raise ValueError(f"Not a PNG input: {sid}")
            rgb = opened.convert("RGB")
            images.append(rgb)
        result["inputs"].append(dict(slide_id=sid, split="train", path=str(path),
                                     sha256=sha256(path), size=list(rgb.size), mode="RGB"))
    result["split_sha256_before"] = before
    result["cuda"] = dict(torch_build=torch.version.cuda,
                          available=bool(torch.cuda.is_available()),
                          device_count=torch.cuda.device_count())
    if result["cuda"]["available"]:
        result["cuda"].update(name=torch.cuda.get_device_name(0),
                               capability=list(torch.cuda.get_device_capability(0)),
                               total_memory_bytes=torch.cuda.get_device_properties(0).total_memory)
    snapshot, checkpoint = obtain_snapshot(result, previous, roots, offline)
    file_hashes = {name: sha256(snapshot / name) for name in ("config.json", "preprocessor_config.json", checkpoint)}
    if file_hashes[checkpoint] != CHECKPOINT_SHA256:
        raise ValueError("Cached pretrained checkpoint differs from the fixed Hugging Face LFS SHA-256")
    result["model"]["file_sha256"] = file_hashes
    processor = CLIPImageProcessor.from_pretrained(str(snapshot), local_files_only=True)
    model, loading = CLIPVisionModelWithProjection.from_pretrained(
        str(snapshot), local_files_only=True, torch_dtype=torch.float32,
        use_safetensors=checkpoint.endswith(".safetensors"), output_loading_info=True)
    missing = sorted(loading.get("missing_keys") or [])
    mismatched = list(loading.get("mismatched_keys") or [])
    errors = list(loading.get("error_msgs") or [])
    result["weight_loading"] = dict(missing_keys=missing, mismatched_keys=mismatched,
                                    error_msgs=errors,
                                    unexpected_key_count=len(loading.get("unexpected_keys", [])))
    if missing or mismatched or errors:
        raise ValueError("Pretrained vision weights were not fully loaded; refusing random/missing weights")
    model.requires_grad_(False)
    model.eval()
    if int(model.config.projection_dim) != 512:
        raise ValueError("Frozen CLIP projection dimension is not 512")
    device = "cuda:0" if result["cuda"]["available"] else "cpu"
    result["initial_device"] = device
    result["batch_size"] = 1
    result["oom_fallback"] = False

    def extract(selected_device):
        model.to(selected_device)
        vectors = []
        with torch.inference_mode():
            for image in images:
                inputs = processor(images=image, return_tensors="pt")
                pixels = inputs["pixel_values"].to(device=selected_device, dtype=torch.float32)
                output = model(pixel_values=pixels)
                vectors.append(output.image_embeds.detach().cpu().float().numpy())
                del output, pixels, inputs
        return np.concatenate(vectors, axis=0).astype(np.float32, copy=False)

    try:
        values = extract(device)
    except torch.OutOfMemoryError:
        if not device.startswith("cuda"):
            raise
        result["oom_fallback"] = True
        model.to("cpu")
        # Releases only allocations in this process, never another application.
        torch.cuda.empty_cache()
        device = "cpu"
        values = extract(device)
    norms = np.linalg.norm(values, axis=1)
    checks = dict(shape=list(values.shape), dtype=str(values.dtype),
                  all_finite=bool(np.isfinite(values).all()),
                  all_norms_positive=bool((norms > 0).all()),
                  zero_vectors=int((norms == 0).sum()),
                  nan_values=int(np.isnan(values).sum()),
                  infinite_values=int(np.isinf(values).sum()),
                  min_value=float(values.min()), max_value=float(values.max()),
                  max_absolute_value=float(np.abs(values).max()),
                  l2_norms=[float(value) for value in norms],
                  expected_dimension=512, successful_images=int(values.shape[0]))
    result["feature_checks"] = checks
    if values.shape != (8, 512) or values.dtype != np.float32 or not checks["all_finite"] or not checks["all_norms_positive"]:
        raise ValueError("Eight-image CLIP smoke feature validation failed")
    if sha256(SPLIT) != before:
        raise ValueError("Accepted split changed during the smoke test")
    FEATURES.parent.mkdir(exist_ok=True)
    np.save(FEATURES, values, allow_pickle=False)
    result.update(status="passed", final_device=device, feature_path=str(FEATURES),
                  feature_sha256=sha256(FEATURES), split_sha256_after=sha256(SPLIT),
                  split_unchanged=True, classifier_training_performed=False,
                  clip_training_performed=False, feature_definition="unnormalized projected image_embeds",
                  feature_rows=list(SLIDE_IDS))
    return result


def main():
    if not VENV_PYTHON.is_file() or not os.path.samefile(sys.executable, VENV_PYTHON):
        raise SystemExit("Run only with detector_runtime/.venv/Scripts/python.exe")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Require an already complete frozen model cache")
    args = parser.parse_args()
    previous = json.loads(RESULT.read_text(encoding="utf-8")) if RESULT.is_file() else {}
    result = dict(status="started", started_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
                  executable=sys.executable, python=sys.version.split()[0],
                  model=previous.get("model", {}), offline=args.offline,
                  classifier_training_performed=False, clip_training_performed=False)
    try:
        run(result, previous, args.offline)
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__, error=str(exc)[:1000])
        if SPLIT.is_file():
            result["split_sha256_after"] = sha256(SPLIT)
            result["split_unchanged"] = result.get("split_sha256_before") == result["split_sha256_after"]
        write_result(result)
        print(json.dumps(dict(status="failed", error_type=result["error_type"], error=result["error"], result=str(RESULT))))
        return 1
    result["completed_at"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
    write_result(result)
    print(json.dumps(dict(status=result["status"], device=result["final_device"],
                          feature_shape=result["feature_checks"]["shape"],
                          model_revision=result["model"]["revision"], result=str(RESULT))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
