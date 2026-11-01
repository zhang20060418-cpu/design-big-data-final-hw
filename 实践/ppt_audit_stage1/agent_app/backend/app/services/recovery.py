"""Explicit user-authorized recovery creates a new attempt; old results stay intact.

Only source-bound, decoded local artefacts may be reused. The runner validates
cached review contracts again; this module never sends an external request.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import threading
from typing import Any
import uuid

from PIL import Image

from ..config import DATA_DIR, DEFECT_TYPES, MAX_SLIDES, MAX_UPLOAD_BYTES
from ..schemas import ReviewOptions
from ..storage import now_iso, store_for
from .pptx_parser import InvalidPptxError, sha256_file, validate_package

ACTIVE = {'queued', 'running', 'preparing'}
TERMINAL = {'completed', 'completed_with_errors', 'failed'}
_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)


class RecoveryError(ValueError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


def directory(task: dict) -> Path:
    return DATA_DIR / ('demo_tasks' if task['mode'] == 'demo' else 'tasks') / task['id']


def _load(path: Path, fallback: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf8'))
    except (OSError, ValueError, TypeError):
        return fallback


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1, allow_nan=False), encoding='utf8')
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def _locked(parent: dict):
    root = directory(parent)
    with _locks[str(root.resolve())]:
        folder = root / 'recovery'
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / 'attempts.lock').open('a+b') as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'\0'); handle.flush()
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


def _reviews_and_plan(parent: dict) -> tuple[list, dict]:
    root = directory(parent)
    reviews = _load(root / 'analysis/reviews.json')
    plan = _load(root / 'analysis/review_plan.json')
    if not isinstance(reviews, list) or not isinstance(plan, dict):
        report = _load(root / 'report/report.json', {})
        if not isinstance(reviews, list):
            reviews = report.get('reviews', []) if isinstance(report, dict) else []
        if not isinstance(plan, dict):
            plan = report.get('review_plan', {}) if isinstance(report, dict) else {}
    return reviews if isinstance(reviews, list) else [], plan if isinstance(plan, dict) else {}


def _successful(reviews: list, plan: dict) -> list[dict]:
    calls = {c.get('call_id'): c for c in plan.get('calls', []) if isinstance(c, dict)}
    valid = []
    for review in reviews:
        if not isinstance(review, dict) or review.get('status') != 'ok':
            continue
        call = calls.get(review.get('call_id'))
        verdicts = review.get('verdicts')
        if not call or not isinstance(verdicts, list) or not verdicts:
            continue
        ids = [v.get('candidate_id') for v in verdicts if isinstance(v, dict)]
        if len(ids) != len(verdicts) or len(ids) != len(set(ids)) or set(ids) != set(call.get('candidate_ids', [])):
            continue
        valid.append(deepcopy(review))
    return valid


def availability(task: dict) -> dict:
    root = directory(task)
    exists = (root / 'source/source.pptx').is_file()
    stages = {s.get('key'): s for s in task.get('stages', [])}
    # Running progress polls should not repeatedly parse potentially large plans.
    reviews, plan = _reviews_and_plan(task) if task['status'] in TERMINAL else ([], {})
    successes = _successful(reviews, plan)
    pending = max(0, len(plan.get('calls', [])) - len(successes))
    enabled_incomplete = any(s.get('status') in ('pending', 'running', 'failed') for s in stages.values())
    llm_skipped = stages.get('mllm', {}).get('status') in ('skipped', 'unsupported')
    incomplete = task['status'] in ('failed', 'completed_with_errors') or enabled_incomplete or llm_skipped or pending > 0
    allowed = task['status'] in TERMINAL and exists and task['mode'] == 'real'
    if task['mode'] != 'real':
        reason = '演示任务请重新创建演示，不与真实审查恢复混用。'
    elif task['status'] in ACTIVE:
        reason = '任务仍在运行，请等待结束；不会重复启动审查。'
    elif task['status'] in ('ready', 'draft'):
        reason = '任务尚未开始，请到配置页开始审查。'
    elif not exists:
        reason = '原始 PPTX 已缺失，无法恢复，请重新上传。'
    else:
        reason = '创建新的审查尝试并保留原结果；继续会复用通过校验的工件。'
    reusable = [key for key in ('validate', 'parse', 'render', 'structure', 'visual')
                if stages.get(key, {}).get('status') == 'done']
    snapshot = task.get('api_snapshot') or {}
    return {'available': allowed, 'can_continue': allowed and incomplete,
        'can_restart': allowed, 'reason': reason, 'continue_label': '继续审查',
        'reused_stages': reusable, 'pending_review_calls': pending,
        'additional_api_calls_possible': bool(allowed and incomplete),
        'parent_task_id': snapshot.get('recovery_parent_task_id'),
        'action': snapshot.get('recovery_action'),
        'lineage': {'parent_task_id': snapshot.get('recovery_parent_task_id'),
                    'action': snapshot.get('recovery_action'),
                    'reused_stages': snapshot.get('recovery_reused_stages', [])},
        'original_results_preserved': True,
        'reuse_note': '此处为可复用阶段；执行恢复时还会校验源文件、图片与证据。'}


def _inside(root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError('恢复工件路径越界或文件缺失')
    return resolved


def _rewrite(value: Any, source: Path, target: Path) -> Any:
    """Move internal absolute paths only; do not interpret user prose as paths."""
    if isinstance(value, dict):
        return {k: _rewrite(v, source, target) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite(v, source, target) for v in value]
    if isinstance(value, str):
        for old, new in ((str(source), str(target)), (source.as_posix(), target.as_posix())):
            if value == old or value.startswith(old + '/') or value.startswith(old + '\\'):
                return new + value[len(old):]
    return value


def _copy_valid_prepared(parent: dict, child: dict, digest: str, context: dict) -> bool:
    source, target = directory(parent), directory(child)
    document = parent.get('document')
    detail = _load(source / 'parse/slides_detail.json')
    payload = _load(source / 'renders/slides.json')
    if not isinstance(document, dict) or document.get('sha256') != digest or not isinstance(detail, dict) or not isinstance(payload, dict):
        context['warnings'].append('解析或渲染记录缺失/源摘要不匹配，将重新解析渲染。')
        return False
    result = payload.get('result', {})
    slides = result.get('slides', [])
    count = int(document.get('slide_count') or 0)
    meta = result.get('meta') or {}
    if (not result.get('ok') or count <= 0 or len(slides) != count
            or meta.get('source_sha256_before') != digest or meta.get('source_sha256_after') != digest):
        context['warnings'].append('渲染未全部成功或缺少源文件绑定，不复用不完整导出。')
        return False
    transfers = []
    try:
        if {int(s['index']) for s in slides} != set(range(1, count + 1)):
            raise ValueError('页号不完整/不唯一')
        for slide in slides:
            if slide.get('status') != 'rendered' or not slide.get('png'):
                raise ValueError('存在未渲染页面')
            png = _inside(source / 'renders', slide['png'])
            with Image.open(png) as image:
                image.load()
                if image.size != (slide.get('width'), slide.get('height')):
                    raise ValueError('PNG像素尺寸与记录不一致')
                ratio = float(document['slide_width_in']) / float(document['slide_height_in'])
                if abs(image.width / image.height - ratio) > 0.01:
                    raise ValueError('PNG与页面比例不一致')
            actual = sha256_file(png)
            if slide.get('sha256') and slide['sha256'] != actual:
                raise ValueError('PNG摘要不一致')
            transfers.append((png, target / 'renders' / png.relative_to(source / 'renders'), actual))
            if slide.get('thumb'):
                thumb = _inside(source / 'renders/thumbs', slide['thumb'])
                transfers.append((thumb, target / 'renders/thumbs' / thumb.relative_to(source / 'renders/thumbs'), sha256_file(thumb)))
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
        context['warnings'].append(f'渲染校验未通过，将重新执行：{exc}')
        return False
    for old, new, expected in transfers:
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old, new)
        if sha256_file(new) != expected:
            raise RecoveryError('copy_integrity_failed', '恢复工件复制校验失败，未启动审查。')
        context['artifact_sha256'][str(new.relative_to(target))] = expected
    _write(target / 'parse/slides_detail.json', _rewrite(detail, source, target))
    _write(target / 'renders/slides.json', _rewrite(payload, source, target))
    for filename in ('native_contexts.json', 'native_measurement_report.json', 'render_report.json'):
        value = _load(source / 'renders' / filename)
        if value is not None:
            _write(target / 'renders' / filename, _rewrite(value, source, target))
    native = target / 'renders/native_contexts.json'
    if native.is_file():
        # Internal paths changed to the new attempt, so context bytes change
        # even though measured values do not. Bind copied metadata to those
        # actual new bytes; retaining the parent's hash would break safe reuse.
        context_digest = sha256_file(native)
        for filename in ('slides.json', 'native_measurement_report.json', 'render_report.json'):
            value = _load(target / 'renders' / filename)
            def bind(node):
                if isinstance(node, dict):
                    for key, item in node.items():
                        if key == 'context_sha256':
                            node[key] = context_digest
                        else:
                            bind(item)
                elif isinstance(node, list):
                    for item in node:
                        bind(item)
            if isinstance(value, (dict, list)):
                bind(value)
                _write(target / 'renders' / filename, value)
    context['reused_stages'].extend(['validate', 'parse', 'render'])
    store_for(child['mode']).update(child['id'], document_json=_rewrite(document, source, target), slide_count=count)
    return True


def _copy_analysis(parent: dict, child: dict, context: dict) -> None:
    source, target = directory(parent), directory(child)
    stages = {s.get('key'): s for s in parent['stages']}
    claim_data = _load(source / 'analysis/claims.json')
    observations = _load(source / 'analysis/observations.json')
    claims = claim_data.get('claims') if isinstance(claim_data, dict) else None
    count = int(parent.get('slide_count') or (parent.get('document') or {}).get('slide_count') or 0)
    expected = {(page, defect) for page in range(1, count + 1) for defect in DEFECT_TYPES}
    valid = (stages.get('structure', {}).get('status') == 'done' and isinstance(claims, list)
        and isinstance(observations, list) and len(claims) == len(expected)
        and all(isinstance(c, dict) for c in claims)
        and {(c.get('slide_index'), c.get('defect_type')) for c in claims} == expected)
    if valid:
        context['screening_reusable'] = True
        context['reused_stages'].append('structure')
        for value in claims:
            value['document_id'] = child['id']
        _write(target / 'analysis/claims.json', _rewrite(claim_data, source, target))
        _write(target / 'analysis/observations.json', _rewrite(observations, source, target))
        snapshot = _load(source / 'analysis/screening_snapshot.json')
        if isinstance(snapshot, dict):
            _write(target / 'analysis/screening_snapshot.json', _rewrite(snapshot, source, target))
    visual = _load(source / 'analysis/visual_results.json')
    if valid and isinstance(visual, dict) and stages.get('visual', {}).get('status') in ('done', 'skipped'):
        _write(target / 'analysis/visual_results.json', _rewrite(visual, source, target))
        context['visual_reusable'] = True
        if stages['visual']['status'] == 'done':
            context['reused_stages'].append('visual')
    if not valid:
        context['warnings'].append('首轮观察不完整或阶段未成功，检测器将重新运行；不会把未知当作完成。')


def create_attempt(parent: dict, action: str, *, idempotency_key: str | None = None,
                   llm_backend: str | None = None) -> tuple[dict, bool]:
    """Create exactly one child per user request. Caller submits it once."""
    if action not in ('continue', 'restart'):
        raise RecoveryError('invalid_action', '恢复操作必须为 continue 或 restart。', 400)
    store = store_for(parent['mode'])
    with _locked(parent):
        current = store.get(parent['id'])
        if current is None:
            raise RecoveryError('not_found', '父任务不存在。', 404)
        status = availability(current)
        if not status['can_restart'] or (action == 'continue' and not status['can_continue']):
            raise RecoveryError('recovery_unavailable', status['reason'])
        registry_path = directory(parent) / 'recovery/attempts.json'
        registry = _load(registry_path) if registry_path.exists() else []
        if not isinstance(registry, list):
            raise RecoveryError('recovery_registry_invalid', '恢复记录损坏，未重复创建任务。')
        for item in registry:
            child = store.get(item.get('task_id', ''))
            if not child:
                continue
            if idempotency_key and item.get('idempotency_key') == idempotency_key:
                if item.get('action') != action:
                    raise RecoveryError('idempotency_conflict', '同一请求标识不能用于不同恢复操作。')
                return item['response'], False
            if child['status'] in ACTIVE:
                raise RecoveryError('recovery_already_active', f"已有恢复任务正在运行：{child['id']}，请等待结束。")
        source = directory(current) / 'source/source.pptx'
        digest = sha256_file(source)
        if not current.get('source_sha256') or digest != current['source_sha256']:
            raise RecoveryError('source_integrity_failed', '原始PPTX摘要缺失或已变化，停止恢复，请重新上传。')
        try:
            validate_package(source, MAX_UPLOAD_BYTES, MAX_SLIDES)
        except InvalidPptxError as exc:
            raise RecoveryError('invalid_source_package', f'原始PPTX解析校验未通过：{exc.message}') from exc
        options = ReviewOptions.model_validate(current['options']).model_dump()
        if action == 'continue' and status['can_continue']:
            options.update(enable_mllm=True, mllm_user_choice=True)
            if options['review_mode'] == 'off':
                options['review_mode'] = 'risk_only'
        if llm_backend is not None:
            options['llm_backend'] = llm_backend
        child = store.create(name=(current['name'][:95] + (' · 继续审查' if action == 'continue' else ' · 重新审查')),
            options=options, source_name=current['source_name'], size_bytes=source.stat().st_size,
            api_snapshot={'recovery_parent_task_id': parent['id'], 'recovery_action': action})
        target = directory(child)
        context = {'version': 'recovery-context-1', 'action': action, 'parent_task_id': parent['id'],
            'parent_mode': parent['mode'], 'created_at': now_iso(), 'source_sha256': digest,
            'reused_stages': [], 'screening_reusable': False, 'visual_reusable': False,
            'cached_reviews': [], 'cached_review_plan': {}, 'parent_risks': [],
            'parent_stage_status': {s['key']: s['status'] for s in current['stages']},
            'parent_usage': {}, 'artifact_sha256': {}, 'warnings': []}
        try:
            (target / 'source').mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / 'source/source.pptx')
            if sha256_file(target / 'source/source.pptx') != digest:
                raise RecoveryError('copy_integrity_failed', 'PPTX复制摘要校验失败，未启动恢复。')
            store.update(child['id'], source_sha256=digest)
            if action == 'continue' and _copy_valid_prepared(current, child, digest, context):
                _copy_analysis(current, child, context)
                records, plan = _reviews_and_plan(current)
                context['cached_reviews'] = _rewrite(_successful(records, plan), directory(current), target)
                context['cached_review_plan'] = _rewrite(plan, directory(current), target)
                for call in context['cached_review_plan'].get('calls', []):
                    for image in call.get('images', []):
                        value = image.get('png_path')
                        if value and Path(value).is_absolute():
                            resolved = Path(value).resolve()
                            if not resolved.is_relative_to(target.resolve()):
                                raise RecoveryError('evidence_path_invalid', '父复核计划含跨任务图片，停止恢复。')
                            image['png_path'] = str(resolved.relative_to(target))
                parent_report = _load(directory(current) / 'report/report.json', {})
                if isinstance(parent_report, dict):
                    context['parent_risks'] = _rewrite(parent_report.get('risks', []), directory(current), target)
                attempt_calls = sum(int(r.get('actual_http_calls') or 0) for r in records if isinstance(r, dict))
                previous_usage = ((parent_report.get('recovery') or {}).get('parent_usage') or {}) if isinstance(parent_report, dict) else {}
                previous_calls = int(previous_usage.get('cumulative_http_calls', previous_usage.get('actual_http_calls', 0)) or 0)
                context['parent_usage'] = {'actual_http_calls': attempt_calls,
                    'parent_attempt_http_calls': attempt_calls,
                    'cumulative_http_calls': previous_calls + attempt_calls,
                    'successful_records': len(context['cached_reviews']),
                    'note': '历史调用单独保留；新尝试只计本次新请求，仍受任务和全局预算约束。'}
            for folder in ('parse', 'renders', 'analysis'):
                for copied in (target / folder).rglob('*.json') if (target / folder).exists() else []:
                    context['artifact_sha256'][str(copied.relative_to(target))] = sha256_file(copied)
            store.update(child['id'], api_snapshot_json={'recovery_parent_task_id': parent['id'],
                'recovery_action': action, 'recovery_reused_stages': context['reused_stages']})
            _write(target / 'analysis/recovery_context.json', context)
            pending = max(0, len(context['cached_review_plan'].get('calls', [])) - len(context['cached_reviews']))
            response = {'task_id': child['id'], 'id': child['id'], 'mode': child['mode'],
                'parent_task_id': parent['id'], 'action': action, 'reused_stages': context['reused_stages'],
                'pending_review_calls': pending, 'additional_api_calls_possible': bool(options['enable_mllm']),
                'message': '已创建新的审查尝试，原结果保持不变；新增复核可能产生API调用。',
                'original_results_preserved': True}
            registry.append({'task_id': child['id'], 'action': action, 'created_at': now_iso(),
                'idempotency_key': idempotency_key, 'response': response})
            _write(registry_path, registry)
            return response, True
        except Exception as exc:
            store.update(child['id'], status='failed', error=f'恢复准备失败：{exc}', finished_at=now_iso())
            raise
