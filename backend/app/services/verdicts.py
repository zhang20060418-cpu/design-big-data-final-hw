"""Human verdicts on individual findings.

A reviewer who has opened the render and the evidence panel can dismiss a
finding as a system/AI misjudgement.  The dismissal never edits or deletes the
detector observation: it is stored separately in ``reviews/verdicts.json``,
re-applied on every report rebuild, and only affects counting, the report body,
the generated advice and the exported prompt.  Restoring a finding removes the
record and the original observation becomes visible again unchanged.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

VERDICT_STATUSES = ("dismissed", "confirmed")
DISMISSED = "dismissed"


def _path(directory: Path) -> Path:
    return Path(directory) / "reviews" / "verdicts.json"


def load(directory: Path) -> dict[str, dict]:
    """Read the verdict map; a corrupt file degrades to "no human verdict"."""
    try:
        data = json.loads(_path(directory).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    items = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return {}
    return {str(key): value for key, value in items.items()
            if isinstance(value, dict) and value.get("status") in VERDICT_STATUSES}


def save(directory: Path, verdicts: dict[str, dict]) -> None:
    path = _path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"schema_version": 1, "verdicts": verdicts},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def record(directory: Path, issue_id: str, status: str, note: str = "") -> dict[str, dict]:
    """``dismissed`` writes a record; any other status restores the finding."""
    verdicts = load(directory)
    if status == DISMISSED:
        verdicts[str(issue_id)] = {
            "status": DISMISSED,
            "note": str(note or "")[:300],
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    else:
        verdicts.pop(str(issue_id), None)
    save(directory, verdicts)
    return verdicts


def annotate(issues: list[dict], verdicts: dict[str, dict]) -> list[dict]:
    """Project the verdict map onto issues, clearing stale annotations."""
    for issue in issues:
        verdict = verdicts.get(str(issue.get("issue_id")))
        if verdict and verdict.get("status") == DISMISSED:
            issue["user_verdict"] = DISMISSED
            issue["user_verdict_note"] = verdict.get("note") or ""
            issue["user_verdict_at"] = verdict.get("updated_at")
        else:
            issue.pop("user_verdict", None)
            issue.pop("user_verdict_note", None)
            issue.pop("user_verdict_at", None)
    return issues


def is_dismissed(issue: dict) -> bool:
    return issue.get("user_verdict") == DISMISSED


def is_visible(issue: dict) -> bool:
    """Visible findings drive every count, page list, advice and prompt."""
    return issue.get("review_status") != "rejected" and not is_dismissed(issue)
