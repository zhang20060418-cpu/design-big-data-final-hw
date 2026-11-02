"""Run the real offline CLIP + PNG10 inference worker on a few bundled pages."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    data = root / "research/sample_data"
    output = args.output_dir or root / "research/outputs/clip_sample_smoke"
    output.mkdir(parents=True, exist_ok=True)
    with (data / "manifest.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))[:max(1, min(40, args.limit))]
    request = output / "request.json"
    request.write_text(json.dumps({"work_point": "f1", "pages": [
        {"document_id": row["group_id"], "slide_index": index,
         "png_path": str((data / "images" / f'{row["slide_id"]}.png').resolve())}
        for index, row in enumerate(rows, 1)]}, ensure_ascii=False), encoding="utf-8")
    result = output / "results.json"
    process = subprocess.run([sys.executable, "-B", str(root / "backend/app/services/clip_inference_worker.py"),
                              "--request", str(request), "--output", str(result)],
                             capture_output=True, text=True, encoding="utf-8", timeout=600)
    (output / "worker.log").write_text(process.stdout + process.stderr, encoding="utf-8")
    payload = json.loads(result.read_text(encoding="utf-8"))
    if process.returncode or payload.get("status") != "complete":
        raise RuntimeError(payload.get("error") or f"Worker incomplete: {output}")
    assert len(payload["results"]) == len(rows)
    assert all(r["status"] == "ok" for r in payload["results"])
    assert payload["classifier_fits"] == 0 and payload["network_downloads"] == 0
    print(json.dumps({"status": "passed", "pages": len(rows), "device": payload["device"],
                      "clip_forwards": payload["vision_forward_attempts"], "classifier_fits": 0,
                      "network_downloads": 0, "results": str(result)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
