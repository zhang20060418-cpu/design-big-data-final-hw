"""Verify bundled CLIP files; recover missing/LFS-pointer assets from the pinned source."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> None:
    app = Path(__file__).resolve().parents[1]
    ml = app / "detector_runtime"
    pins = json.loads((app / "backend/models/frozen_clip_manifest.json").read_text(encoding="utf-8"))
    encoder = pins["clip_directory"] + "/"
    for relative, expected in pins["assets_sha256"].items():
        target = (ml / relative).resolve()
        if not target.is_relative_to(ml.resolve()):
            raise ValueError("Asset outside detector directory")
        valid = target.is_file() and digest(target) == expected
        if not valid and relative.startswith(encoder):
            from huggingface_hub import hf_hub_download
            filename = relative[len(encoder):]
            cached = Path(hf_hub_download(repo_id=pins["clip_model"], revision=pins["clip_revision"],
                filename=filename, cache_dir=str(ml / ".cache/huggingface"), token=False))
            if digest(cached) != expected:
                raise ValueError("Downloaded asset SHA mismatch: " + relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".part")
            shutil.copyfile(cached, temporary)
            temporary.replace(target)
        if not target.is_file() or digest(target) != expected:
            raise ValueError("Frozen asset SHA mismatch: " + relative)
        print("Verified:", relative)


if __name__ == "__main__":
    main()
