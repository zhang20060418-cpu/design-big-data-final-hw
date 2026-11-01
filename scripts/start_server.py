"""Start a detached backend with file logs and no inherited caller pipes."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--stdout", type=Path, required=True)
    parser.add_argument("--stderr", type=Path, required=True)
    args = parser.parse_args()
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    with args.stdout.open("ab", buffering=0) as out, args.stderr.open("ab", buffering=0) as err:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.port)],
            cwd=args.backend, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            close_fds=True, creationflags=flags,
        )
    print(process.pid, flush=True)


if __name__ == "__main__":
    main()
