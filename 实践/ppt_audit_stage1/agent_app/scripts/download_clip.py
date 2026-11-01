"""Fetch only the three pinned public CLIP assets, never the user's datasets."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def main() -> None:
    from huggingface_hub import hf_hub_download

    app = Path(__file__).resolve().parents[1]
    ml = app.parent / "ml_stage4"
    pins = json.loads((app / "backend/models/frozen_clip_manifest.json").read_text(encoding="utf-8"))
    for relative, expected in pins["assets_sha256"].items():
        target = ml / relative
        if relative.startswith("models/huggingface/"):
            parts = Path(relative).parts
            snapshot = parts.index("snapshots")
            revision = parts[snapshot + 1]
            if revision != pins["clip_revision"]:
                raise ValueError("CLIP revision differs from frozen manifest")
            filename = "/".join(parts[snapshot + 2:])
            if not target.is_file():
                hf_hub_download(repo_id="openai/clip-vit-base-patch32", revision=revision,
                                filename=filename, cache_dir=str(ml / "models/huggingface"), token=False)
        with target.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise ValueError("Frozen asset SHA mismatch: " + relative)
        print("Verified:", relative)


if __name__ == "__main__":
    main()
