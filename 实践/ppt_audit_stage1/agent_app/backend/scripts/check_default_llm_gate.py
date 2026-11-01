"""Live isolated QA: real PPTX/R2, explicit Mock LLM; no provider requests."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time

import httpx

APP = Path(__file__).resolve().parents[2]
OUT = APP / 'docs/4.4BC/llm_gate_checks'
FIXTURE = APP / 'fixtures/integration_14_slides.pptx'
BASE = 'http://127.0.0.1:8011'
SETTING_FIELDS = ('api_base_url', 'api_model', 'api_timeout_s', 'api_max_retries',
    'api_max_concurrency', 'api_temperature', 'max_calls_per_task',
    'allow_private_base_url', 'allow_insecure_base_url', 'response_format_json')


def main():
    OUT.mkdir(exist_ok=True)
    digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    records = {}
    with httpx.Client(base_url=BASE, trust_env=False, timeout=180) as c:
        initial = c.get('/api/settings').json()
        assert not initial['api_key_set'], 'Refusing to overwrite an existing QA credential'
        saved = {k: initial[k] for k in SETTING_FIELDS if k in initial}
        configured = dict(saved, api_base_url='http://127.0.0.1:9/v1',
            api_model='QA-explicit-Mock-only', api_key='not-a-real-key-local-QA',
            max_calls_per_task=8, allow_private_base_url=True, allow_insecure_base_url=True)

        def run_case(name, **overrides):
            options = dict(llm_backend='mock', review_strategy='full_review',
                review_mode='manual', manual_pages=[4], max_api_calls=1,
                max_review_pages=14, enable_visual=False)
            options.update(overrides)
            with FIXTURE.open('rb') as fh:
                r = c.post('/api/tasks', files={'file': (FIXTURE.name, fh,
                    'application/vnd.openxmlformats-officedocument.presentationml.presentation')},
                    data={'name': f'5秒自动复检联调 {name}', 'options': json.dumps(options)})
            r.raise_for_status()
            tid = r.json()['id']
            print(f'{name}_task_id={tid}', flush=True)
            waiting = None
            samples = []
            started = time.monotonic()
            while time.monotonic() - started < 420:
                task = c.get(f'/api/tasks/{tid}').json()
                gate = task['review_gate']
                if gate['state'] in ('waiting', 'executing'):
                    assert not task['report_ready'], 'Intermediate output exposed before final review'
                    assert c.get(f'/api/tasks/{tid}/report?format=json').status_code == 409
                if gate['state'] == 'waiting':
                    if waiting is None:
                        waiting = dict(gate)
                        assert gate['countdown_seconds'] == 5 and gate['can_skip']
                    assert gate['deadline_at'] == waiting['deadline_at'], 'Polling reset the deadline'
                    samples.append(gate['remaining_ms'])
                if task['status'] in ('completed', 'completed_with_errors', 'failed'):
                    break
                time.sleep(.15)
            assert task['status'] == 'completed', task.get('error')
            payload = c.get(f'/api/tasks/{tid}/report?format=json').json()
            assert payload['verification_pipeline']['llm_actual_http_calls'] == 0
            if name == 'automatic':
                assert task['options']['enable_mllm'] is True
                assert waiting is not None and len(samples) > 2
                assert gate['state'] == 'completed' and gate['outcome'] == 'mock_only'
                assert payload['verification_pipeline']['final_review_status'] == 'mock_only'
                assert payload['reviews'] and all(r.get('mock') for r in payload['reviews'])
                start = datetime.fromisoformat(gate['execution_started_at'])
                deadline = datetime.fromisoformat(gate['deadline_at'])
                assert start >= deadline
                late = c.post(f'/api/tasks/{tid}/skip-review')
                assert late.status_code == 409, 'Late skip must not pretend to cancel a started request'
                assert c.get(f'/api/tasks/{tid}/review-gate').json()['gate_id'] == gate['gate_id']
            else:
                assert waiting is None and gate['state'] == 'disabled'
                assert not payload['reviews']
                expected = 'zero_budget' if name == 'zero_budget' else 'review_disabled'
                assert gate['skip_reason'] == expected
            (OUT / f'{name}_report.json').write_text(json.dumps(payload,
                ensure_ascii=False, indent=1), encoding='utf8')
            records[name] = {'task_id': tid, 'options': task['options'],
                'gate': gate, 'waiting_observed': waiting is not None,
                'deadline_unchanged_across_polls': True,
                'waiting_samples_ms': samples, 'pipeline': payload['verification_pipeline'],
                'actual_external_llm_calls': 0, 'elapsed_s': round(time.monotonic() - started, 2)}
            print(f'{name}=passed', flush=True)

        try:
            c.put('/api/settings', json=configured).raise_for_status()
            assert c.get('/api/system/capabilities').json()['modules']['mllm']['state'] == 'ready'
            run_case('automatic')  # Omits enable_mllm: actual API-aware backend default.
            run_case('explicit_off', enable_mllm=False)
            run_case('zero_budget', enable_mllm=True, max_api_calls=0)
        finally:
            c.put('/api/settings', json=dict(saved, api_key='')).raise_for_status()
        assert not c.get('/api/settings').json()['api_key_set']
        run_case('no_api_default')
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == digest
    result = {'status': 'passed', 'checked_at': datetime.now().astimezone().isoformat(),
        'base_url': BASE, 'fixture_sha256': digest, 'fixture_unchanged': True,
        'QA_settings_restored': True, 'actual_external_llm_calls': 0, 'tasks': records}
    (OUT / 'online_gate_checks.json').write_text(json.dumps(result,
        ensure_ascii=False, indent=2), encoding='utf8')
    print('online_gate_checks=passed', flush=True)


if __name__ == '__main__':
    main()
