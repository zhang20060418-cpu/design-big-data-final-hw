"""Persistent, at-most-once gate between local screening and LLM review.

Only the worker can claim execution. Browser polling is read-only; explicit
skip wins only before the persisted deadline. Interrupted executing gates are
never replayed automatically, because the prior API outcome may be unknown.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Callable
import uuid

WARNING = '跳过LLM复检可能导致误诊或漏检；当前结果仅为首轮检测候选，请谨慎使用。'
_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
STATES = {'waiting', 'executing', 'skipped', 'disabled', 'completed'}


class GateConflict(ValueError):
    """A skip cannot be accepted, or an execution token does not own this gate."""


class GateStateError(RuntimeError):
    """Fail closed rather than reset a corrupted persisted gate."""


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec='milliseconds')


def _path(task_dir: Path) -> Path:
    return Path(task_dir) / 'analysis' / 'review_gate.json'


@contextmanager
def _locked(task_dir: Path):
    """Coordinate worker threads and independent backend processes."""
    key = str(Path(task_dir).resolve())
    with _locks[key]:
        folder = _path(task_dir).parent
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / 'review_gate.lock').open('a+b') as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load(task_dir: Path) -> dict | None:
    path = _path(task_dir)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(state, dict) or state.get('schema_version') != 'review-gate-1'
                or state.get('state') not in STATES or not state.get('gate_id')):
            raise ValueError('invalid gate schema')
        deadline = state.get('deadline_epoch')
        if state['state'] == 'waiting' and (not isinstance(deadline, (int,float)) or not math.isfinite(deadline)):
            raise ValueError('waiting gate has no valid deadline')
        return state
    except (OSError, ValueError, TypeError) as exc:
        raise GateStateError('LLM等待记录无法读取；为避免重复调用，未自动重建。') from exc


def _write(task_dir: Path, data: dict) -> None:
    path = _path(task_dir)
    temp = path.with_name(f'.review_gate.{uuid.uuid4().hex}.tmp')
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _public(data: dict | None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if data is None:
        return dict(state='not_ready', eligible=False, deadline_at=None, remaining_ms=0,
                    countdown_seconds=None, can_skip=False, skip_reason=None, skipped_by_user=False,
                    execution_started_at=None, completed_at=None, outcome=None, warning=None,
                    duplicate_execution_guard=True, skipped_before_review=False)
    remaining = max(0, math.ceil((data.get('deadline_epoch', now) - now) * 1000)) if data['state'] == 'waiting' else 0
    return {k: data.get(k) for k in ('gate_id', 'state', 'eligible', 'created_at', 'deadline_at',
            'countdown_seconds', 'skip_reason', 'skipped_by_user', 'execution_started_at', 'completed_at',
            'outcome', 'warning')} | dict(skipped_before_review=bool(data.get('skipped_before_review')), remaining_ms=remaining,
            can_skip=data['state']=='waiting' and remaining>0, duplicate_execution_guard=True)


def read(task_dir: Path) -> dict:
    # Do not create a file or reset a countdown in response to GET polling.
    if not _path(task_dir).exists():
        with _locks[str(Path(task_dir).resolve())]:
            return _public(_load(task_dir))
    with _locked(task_dir):
        return _public(_load(task_dir))


def _seconds(value: float | None) -> float:
    result = float(os.environ.get('PPT_AGENT_REVIEW_GATE_SECONDS', '5')) if value is None else float(value)
    if not math.isfinite(result) or not 0 <= result <= 60:
        raise ValueError('LLM等待秒数必须在0至60之间；默认5秒。')
    return result


def prepare(task_dir: Path, eligible: bool, reason: str | None = None,
            delay_seconds: float | None = None) -> dict:
    with _locked(task_dir):
        saved = _load(task_dir)
        if saved is not None:
            return _public(saved)
        now = time.time()
        duration = _seconds(delay_seconds)
        saved = dict(schema_version='review-gate-1', gate_id=uuid.uuid4().hex,
            state='waiting' if eligible else 'disabled', eligible=bool(eligible),
            created_at=_iso(now), countdown_seconds=duration if eligible else None,
            deadline_epoch=now+duration if eligible else None,
            deadline_at=_iso(now+duration) if eligible else None,
            skip_reason=None if eligible else reason or 'review_disabled_or_unconfigured',
            skipped_by_user=False, execution_started_at=None, completed_at=None,
            execution_token=None, outcome=None, warning=None)
        _write(task_dir, saved)
        return _public(saved, now)


def skip(task_dir: Path, reason: str = 'user_skipped', *, allow_before_ready: bool = False) -> dict:
    """Skip an existing window, or persist an authorized early opt-out.

    Only the task router may authorize the early case after checking task status,
    saved review options and the pending LLM stage. This cancels LLM review only;
    the worker must finish its local screening before writing the report.
    """
    with _locked(task_dir):
        saved = _load(task_dir)
        if saved is None:
            if not allow_before_ready:
                raise GateConflict('首轮检测尚未结束，当前没有可跳过的LLM等待窗口。')
            now = time.time()
            saved = dict(schema_version='review-gate-1', gate_id=uuid.uuid4().hex,
                state='skipped', eligible=True, created_at=_iso(now),
                countdown_seconds=None, deadline_epoch=None, deadline_at=None,
                skip_reason='user_skipped_before_review', skipped_by_user=True,
                skipped_before_review=True, execution_started_at=None,
                completed_at=_iso(now), execution_token=None, outcome='skipped', warning=WARNING)
            _write(task_dir, saved)
            return _public(saved, now)
        if saved['state'] == 'skipped':
            return _public(saved)
        if saved['state'] != 'waiting':
            raise GateConflict('LLM复检已开始或等待窗口不可用，不能再跳过。')
        if time.time() >= saved['deadline_epoch']:
            raise GateConflict('5秒等待窗口已结束，LLM复检即将开始，不能再跳过。')
        saved.update(state='skipped', skip_reason=reason, skipped_by_user=True,
                     completed_at=_iso(time.time()), outcome='skipped', warning=WARNING)
        _write(task_dir,saved)
        return _public(saved)


def _claim(task_dir: Path) -> dict:
    with _locked(task_dir):
        saved = _load(task_dir)
        if saved is None:
            raise GateStateError('尚未登记LLM等待窗口。')
        if saved['state'] != 'waiting':
            return dict(execute=False, execution_token=None, gate=_public(saved),
                        reason='execution_already_started' if saved['state']=='executing' else saved['state'])
        if time.time() < saved['deadline_epoch']:
            return dict(execute=False, execution_token=None, gate=_public(saved), reason='waiting')
        token = uuid.uuid4().hex
        saved.update(state='executing', execution_token=token, execution_started_at=_iso(time.time()),
                     owner_pid=os.getpid(), outcome=None)
        _write(task_dir,saved)
        return dict(execute=True, execution_token=token, gate=_public(saved), reason='deadline_elapsed')


def wait_and_claim(task_dir: Path, eligible: bool, reason: str | None = None,
                   delay_seconds: float | None = None, on_tick: Callable[[dict], None] | None = None,
                   poll_s: float = .05) -> dict:
    prepare(task_dir,eligible,reason,delay_seconds)
    while True:
        decision = _claim(task_dir)
        if decision['reason'] != 'waiting':
            return decision
        if on_tick is not None:
            on_tick(decision['gate'])
        time.sleep(min(max(.01,poll_s),max(.001,decision['gate']['remaining_ms']/1000)))


def complete(task_dir: Path, execution_token: str, outcome: str = 'completed', reason: str | None = None) -> dict:
    if outcome not in {'completed','failed','partial','mock_only','not_checked'}:
        raise ValueError('LLM结束状态非法。')
    with _locked(task_dir):
        saved = _load(task_dir)
        if saved is None or not execution_token or saved.get('execution_token') != execution_token:
            raise GateConflict('无权完成该LLM复检；执行令牌不匹配。')
        if saved['state'] == 'completed':
            return _public(saved)
        if saved['state'] != 'executing':
            raise GateConflict('该LLM复检不在执行中。')
        saved.update(state='completed', completed_at=_iso(time.time()), outcome=outcome)
        if reason:
            saved['completion_reason']=reason
        _write(task_dir,saved)
        return _public(saved)
