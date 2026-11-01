"""Persisted opt-out must never be bypassed when an interrupted task resumes."""
import json
from types import SimpleNamespace

from PIL import Image
import pytest

from app.schemas import ReviewOptions
from app.services import orchestrator, review_agent, review_gate
from app.services.pptx_parser import parse_document
from app.storage import real_store


@pytest.mark.parametrize('strategy', [None, 'full_review'])
def test_persisted_disabled_gate_prevents_provider_calls_after_config_change(
        monkeypatch, fixture_pptx, strategy):
    options = ReviewOptions(enable_structure=False, enable_six_detectors=False,
        enable_visual=False, enable_mllm=True, review_mode='all',
        review_strategy=strategy, max_api_calls=1, max_review_pages=1).model_dump()
    task = real_store.create(name='persisted gate regression', options=options,
        source_name=fixture_pptx.name, size_bytes=fixture_pptx.stat().st_size)
    directory = orchestrator.task_dir_for(task)
    (directory / 'renders').mkdir(parents=True)
    Image.new('RGB', (160, 90), 'white').save(directory / 'renders/slide_1.png')
    document, detail = parse_document(fixture_pptx)
    prepared = {'document': document, 'detail': detail, 'render': {'ok': True,
        'slides': [{'index': 1, 'status': 'rendered', 'png': 'slide_1.png'}]}}
    monkeypatch.setattr(orchestrator, '_prepared_artifacts', lambda *_: prepared)
    monkeypatch.setattr(orchestrator, 'settings_store', SimpleNamespace(
        all=lambda: {'api_base_url': 'https://example.invalid/v1',
                     'api_model': 'test-only', 'max_calls_per_task': 1},
        get_api_key=lambda: 'test-only-not-a-real-key'))
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('No provider call may bypass a persisted disabled gate')
    monkeypatch.setattr(orchestrator, 'review_pages', forbidden)
    monkeypatch.setattr(review_agent, 'review_plan', forbidden)
    review_gate.prepare(directory, eligible=False, reason='review_disabled')

    orchestrator.run_task(task['id'], 'real')

    final = real_store.get(task['id'])
    assert final['status'] == 'completed', final.get('error')
    assert calls == []
    assert next(s for s in final['stages'] if s['key'] == 'mllm')['status'] == 'skipped'
    payload = json.loads((directory / 'report/report.json').read_text(encoding='utf8'))
    assert payload['review_gate']['state'] == 'disabled'
    assert payload['reviews'] == []
