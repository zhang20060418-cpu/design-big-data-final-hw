"""Test bootstrap: every test runs against a throw-away data directory."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

TEST_DATA = Path(tempfile.mkdtemp(prefix="slidesentinel_test_"))
os.environ["PPT_AGENT_DATA_DIR"] = str(TEST_DATA)
os.environ["PPT_AGENT_RENDER_LONG_EDGE"] = "960"
os.environ["PPT_AGENT_RENDER_TIMEOUT"] = "240"
os.environ["PPT_AGENT_REVIEW_GATE_SECONDS"] = "0"  # Production default remains 5; gate tests exercise its real deadline.
# The test HTTP fixture is bound to loopback. Do not route its fake API key or
# requests through an inherited workstation proxy; leave app/provider settings
# and external-network proxy behaviour unchanged outside this test process.
os.environ["NO_PROXY"] = ",".join(filter(None, [os.environ.get("NO_PROXY", ""), "127.0.0.1", "localhost", "::1"]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def fixture_pptx() -> Path:
    path = Path(__file__).resolve().parents[2] / "fixtures" / "demo_deck.pptx"
    assert path.exists(), "demo fixture missing"
    return path


def upload(client: TestClient, fixture: Path, *, prepare_only: bool = False,
           options: dict | None = None, name: str = "pytest") -> dict:
    with fixture.open("rb") as fh:
        response = client.post(
            "/api/tasks",
            files={"file": ("测试 文稿.pptx", fh,
                            "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
            data={"name": name, "options": __import__("json").dumps(options or {}),
                  "prepare_only": str(prepare_only).lower()},
        )
    assert response.status_code == 201, response.text
    return response.json()


def wait(client: TestClient, task_id: str, timeout: float = 180.0, mode: str = "real") -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/tasks/{task_id}?mode={mode}").json()
        if last["status"] in ("completed", "completed_with_errors", "failed"):
            return last
        time.sleep(0.6)
    raise AssertionError(f"task did not finish: {last and last.get('status')}")


def wait_prepared(client: TestClient, task_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["status"] not in ("draft", "preparing"):
            return task
        time.sleep(0.5)
    raise AssertionError("prepare did not finish")


def pytest_sessionfinish(session, exitstatus):  # noqa: ANN001
    shutil.rmtree(TEST_DATA, ignore_errors=True)
