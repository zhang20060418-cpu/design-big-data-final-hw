"""Pure postprocessing of the single preregistered independent PPTX score batch.

This program opens no model, image, feature, annotation or SlideAudit file. Its
only scoring inputs are the registered execution manifest and probabilities.csv.
It cannot fit, predict, select a threshold, alter a label, or select a model.
Run --fixture before the formal scoring script is registered; --run-dir later
reuses saved probabilities, never rerunning model inference.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import hashlib
import html
import io
import json
from pathlib import Path
import tempfile
import zipfile

import numpy as np

from pptx_g2_metrics import LABELS, evaluate_strata, paired_cluster_bootstrap


ML = Path(__file__).resolve().parents[1]
MODELS = ('A', 'C', 'H', 'C9')
MODES = ('f1', 'high_recall', 'low_fpr')
LABEL_ZH = dict(zip(LABELS, ('字号不合理', '内容遮挡', '布局拥挤', '内容溢出或裁切')))
TARGET = dict(zip(('font_small', 'occlusion', 'clutter', 'overflow'), LABELS))
EXPECTED = {'A_old_natural': 84, 'B_controlled': 24, 'C_v2_image': 100,
            'D_v3_editable': 144, 'E_all_natural': 328, 'E_A': 282,
            'D_github': 85, 'D_local': 59, 'E_C_natural': 46}
STRATUM_ZH = {'A_old_natural': '旧84页自然原稿', 'B_controlled': '24页可控修改',
              'C_v2_image': 'V2图片式100页', 'D_v3_editable': 'V3可编辑144页',
              'E_all_natural': '全部328页自然原稿', 'E_A': '主分析E_A：282页A来源自然原稿',
              'D_github': 'V3 GitHub下载85页', 'D_local': 'V3中文本地59页',
              'E_C_natural': '46页C来源脚本/重建原稿'}
MODE_ZH = {'f1': 'F1', 'high_recall': '高召回', 'low_fpr': '低误报'}


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def csv_write(path, records, fields=None):
    rows = list(records)
    if fields is None:
        fields = list(rows[0]) if rows else []
    with Path(path).open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator='\n')
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ('N/A' if v is None else
                                json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(v, (dict, list)) else v)
                             for k, v in row.items()})


def load_scores(directory, formal=True):
    manifest = read_json(directory / 'execution_manifest.json')
    with (directory / 'probabilities.csv').open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    if not rows or len({r['sample_id'] for r in rows}) != len(rows):
        raise ValueError('Empty or nonunique sample IDs')
    if tuple(manifest['label_order']) != LABELS:
        raise ValueError('Registered label order mismatch')
    y = np.array([[int(r['true_' + label]) for label in LABELS] for r in rows], dtype=np.int8)
    if not np.isin(y, [0, 1]).all():
        raise ValueError('Missing or illegal saved truth')
    probabilities = {model: np.array([[float(r[model + '_prob_' + label]) for label in LABELS]
                                     for r in rows], dtype=np.float64) for model in MODELS}
    for p in probabilities.values():
        if p.shape != y.shape or not np.isfinite(p).all() or np.any((p < 0) | (p > 1)):
            raise ValueError('Saved probability contract violated')
    thresholds = manifest['thresholds']
    if set(thresholds) != set(MODELS) or any(set(thresholds[m]) != set(MODES) for m in MODELS):
        raise ValueError('Missing preregistered model/workpoint')
    for m in MODELS:
        for mode in MODES:
            t = np.asarray(thresholds[m][mode], dtype=float)
            if t.shape != (4,) or not np.isfinite(t).all() or np.any((t < 0) | (t > 1)):
                raise ValueError('Invalid registered threshold')
    np.testing.assert_array_equal(probabilities['H'][:, 0], probabilities['A'][:, 0])
    np.testing.assert_array_equal(probabilities['H'][:, 1:], probabilities['C'][:, 1:])
    for mode in MODES:
        if thresholds['H'][mode] != [thresholds['A'][mode][0], *thresholds['C'][mode][1:]]:
            raise ValueError('H threshold route mismatch')
    if formal:
        completion = read_json(directory / 'score_completion.json')
        if len(rows) != 352 or manifest['formal_score_passes'] != 1 or completion['formal_score_passes'] != 1:
            raise ValueError('Not the registered complete single pass')
        if completion['sample_count'] != 352 or completion['probabilities_sha256'] != sha(directory / 'probabilities.csv'):
            raise ValueError('Score completion/probability hash mismatch')
        registered_sha = manifest['script_sha256']['analyze_pptx_g2.py']
        if registered_sha != sha(__file__):
            amendment = read_json(directory / 'postprocessing_amendment.json')
            allowed = (manifest['batch_id'] == 'pptx-g2-20261028T154004'
                       and registered_sha == '7076fae625608080a25f486d8d132351ec4a54311734475ef3b68fb7987cc649'
                       and amendment['version'] == 'doc-family-natural-controlled-scope-fix-v1'
                       and amendment['batch_id'] == manifest['batch_id']
                       and amendment['original_registered_sha256'] == registered_sha
                       and amendment['amended_sha256'] == sha(__file__)
                       and amendment['formal_predictions_recomputed'] is False
                       and amendment['analysis_results_seen_before_amendment'] is False
                       and amendment['frozen_rules_changed'] is False
                       and amendment['principal_strata_changed'] is False)
            if not allowed:
                raise ValueError('Unregistered analysis source or invalid named technical amendment')
        if manifest['bootstrap'] != {'B': 2000, 'seed': 42, 'primary': 'source_family_id',
                                     'sensitivity': 'document_id', 'global_paired_cluster_draws': True}:
            raise ValueError('Bootstrap registration differs from fixed protocol')
    return manifest, rows, y, probabilities, thresholds


def build_strata(rows, formal=True):
    def mask(predicate):
        return np.array([predicate(r) for r in rows], dtype=bool)
    natural = lambda r: r['natural_or_controlled'] == 'natural'
    strata = {
        'A_old_natural': mask(lambda r: r['cohort_id'] == 'v1' and natural(r)),
        'B_controlled': mask(lambda r: r['natural_or_controlled'] == 'controlled'),
        'C_v2_image': mask(lambda r: r['cohort_id'] == 'v2' and natural(r)),
        'D_v3_editable': mask(lambda r: r['cohort_id'] == 'v3' and natural(r)),
        'E_all_natural': mask(natural),
        'E_A': mask(lambda r: natural(r) and r['source_class'] == 'A'),
        'D_github': mask(lambda r: r['cohort_id'] == 'v3' and r['source_scope'] == 'github_download'),
        'D_local': mask(lambda r: r['cohort_id'] == 'v3' and r['source_scope'] == 'local'),
        'E_C_natural': mask(lambda r: natural(r) and r['source_class'] == 'C'),
    }
    if formal:
        found = {name: int(v.sum()) for name, v in strata.items()}
        if found != EXPECTED:
            raise ValueError(f'Fixed preregistered population mismatch: {found}')
        if len({r['document_id'] for r in rows}) != 23 or len({r['group_id'] for r in rows}) != 328:
            raise ValueError('Document/group count differs from freeze registry')
        if len({r['source_family_id'] for r in rows}) != 8:
            raise ValueError('Source-family count differs from preregistered map')
    return strata


def flatten_points(points, rows, strata):
    labels, summaries = [], []
    for entry in points:
        name, model, mode, metrics = (entry[k] for k in ('stratum', 'model', 'mode', 'metrics'))
        selected = [r for r, keep in zip(rows, strata[name]) if keep]
        base = {'stratum': name, 'model': model, 'mode': mode,
                'n_pages': len(selected), 'n_documents': len({r['document_id'] for r in selected}),
                'n_source_families': len({r['source_family_id'] for r in selected})}
        for label in metrics['labels']:
            labels.append({**base, **label})
        summaries.append({**base, **{'macro_' + k: v for k, v in metrics['macro'].items()},
                          **{'micro_' + k: v for k, v in metrics['micro'].items()},
                          **metrics['page'],
                          'available_label_macro': metrics['macro_available']})
    return labels, summaries


def partition_metrics(rows, y, probabilities, thresholds, field):
    partitions = sorted({(r[field], r['natural_or_controlled']) for r in rows})
    identities = {f'partition_{i:04d}': (value, origin)
                  for i, (value, origin) in enumerate(partitions)}
    strata = {key: np.array([r[field] == value and r['natural_or_controlled'] == origin
                             for r in rows], dtype=bool)
              for key, (value, origin) in identities.items()}
    points = evaluate_strata(y, probabilities, thresholds, strata)
    labels, summaries = flatten_points(points, rows, strata)
    look = {(r['stratum'], r['model'], r['mode']): r for r in summaries}
    return [{field: identities[row['stratum']][0],
             'natural_or_controlled': identities[row['stratum']][1],
             **{k: v for k, v in row.items() if k != 'stratum'},
             **{k: v for k, v in look[(row['stratum'], row['model'], row['mode'])].items()
                if k not in row and k != 'stratum'}} for row in labels]


def write_npy(archive, name, values):
    with archive.open(name + '.npy', 'w') as handle:
        np.lib.format.write_array(handle, np.asarray(values), allow_pickle=False)


def bootstrap(directory, rows, y, probabilities, thresholds, strata, *, B=2000, seed=42):
    summaries = []
    details = {}
    for field, short in [('source_family_id', 'family'), ('document_id', 'document')]:
        archive_path = directory / f'bootstrap_{short}_replicates.npz'
        with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            def callback(summary, values, reasons):
                key = summary['replicate_key']
                write_npy(archive, key + '_values', values)
                write_npy(archive, key + '_reason_codes', reasons)
            result = paired_cluster_bootstrap(
                y, probabilities, thresholds, strata, [r[field] for r in rows],
                B=B, seed=seed, keep_replicates=False, callback=callback)
            write_npy(archive, 'draw_counts', result['draw_counts'])
            write_npy(archive, 'cluster_ids', np.array(result['cluster_ids'], dtype=str))
            write_npy(archive, 'reason_definitions_json', np.array(json.dumps(result['reason_definitions'], sort_keys=True)))
        summaries.extend({'cluster_unit': field, **r} for r in result['summary'])
        details[field] = {k: v for k, v in result.items() if k not in ('draw_counts', 'summary', 'replicates')}
        details[field]['replicate_file'] = archive_path.name
        details[field]['endpoint_count'] = len(result['summary'])
        print(json.dumps({'stage': 'bootstrap_complete', 'cluster_unit': field,
                          'endpoints': len(result['summary']), 'B': B}, ensure_ascii=False), flush=True)
    csv_write(directory / 'bootstrap_summary.csv', summaries)
    return summaries, details


def controlled_pairs(rows, y, probabilities, thresholds):
    originals = {}
    for i, r in enumerate(rows):
        if r['natural_or_controlled'] == 'natural':
            if r['group_id'] in originals:
                raise ValueError('More than one original for a group')
            originals[r['group_id']] = i
    pairs = []
    for i, row in enumerate(rows):
        if row['natural_or_controlled'] != 'controlled':
            continue
        if row['modification_type'] not in TARGET or row['group_id'] not in originals:
            raise ValueError('Unregistered modification or missing original pair')
        k = originals[row['group_id']]
        original = rows[k]
        if original['document_id'] != row['document_id']:
            raise ValueError('Controlled/original document mismatch')
        for model in MODELS:
            for mode in MODES:
                for j, label in enumerate(LABELS):
                    p, p0, t = probabilities[model][i, j], probabilities[model][k, j], thresholds[model][mode][j]
                    pred, pred0 = int(p >= t), int(p0 >= t)
                    truth, truth0 = int(y[i, j]), int(y[k, j])
                    pairs.append({'sample_id': row['sample_id'], 'original_sample_id': original['sample_id'],
                                  'document_id': row['document_id'], 'group_id': row['group_id'],
                                  'modification_type': row['modification_type'], 'target_intention_label': TARGET[row['modification_type']],
                                  'is_intended_target': label == TARGET[row['modification_type']],
                                  'model': model, 'mode': mode, 'label': label,
                                  'original_true': truth0, 'modified_true': truth,
                                  'original_positive_labels': int(y[k].sum()), 'modified_positive_labels': int(y[i].sum()),
                                  'original_probability': float(p0), 'modified_probability': float(p),
                                  'probability_delta': float(p - p0), 'threshold': float(t),
                                  'original_alert': pred0, 'modified_alert': pred, 'alert_delta': pred - pred0,
                                  'modified_tp': truth * pred, 'modified_fp': (1 - truth) * pred,
                                  'modified_fn': truth * (1 - pred), 'modified_tn': (1 - truth) * (1 - pred)})
    groups = defaultdict(list)
    for p in pairs:
        groups[(p['model'], p['mode'], p['modification_type'], p['label'])].append(p)
    summary = []
    for (model, mode, modification, label), group in sorted(groups.items()):
        npos = sum(r['modified_true'] for r in group)
        nneg = len(group) - npos
        tp = sum(r['modified_tp'] for r in group)
        fp = sum(r['modified_fp'] for r in group)
        delta = [r['probability_delta'] for r in group]
        summary.append({'model': model, 'mode': mode, 'modification_type': modification, 'label': label,
                        'is_intended_target': label == TARGET[modification], 'n_pairs': len(group),
                        'n_actual_positive': npos, 'n_actual_negative': nneg,
                        'tp': tp, 'fp': fp, 'fn': npos - tp, 'tn': nneg - fp,
                        'actual_positive_hit_rate': tp / npos if npos else None,
                        'actual_negative_fpr': fp / nneg if nneg else None,
                        'mean_probability_delta': float(np.mean(delta)),
                        'median_probability_delta': float(np.median(delta)),
                        'n_probability_increased': sum(v > 0 for v in delta),
                        'n_alerts_before': sum(r['original_alert'] for r in group),
                        'n_alerts_after': sum(r['modified_alert'] for r in group),
                        'n_alerts_added': sum(r['alert_delta'] > 0 for r in group),
                        'n_alerts_removed': sum(r['alert_delta'] < 0 for r in group)})
    return pairs, summary


def fmt(value, digits=4):
    return 'N/A' if value is None else f'{value:.{digits}f}'


def pct(value):
    return 'N/A' if value is None else f'{value * 100:.1f}%'


def table(headers, records):
    def cell(v):
        return str(v).replace('|', '\\|').replace('\n', ' ')
    return '\n'.join(['| ' + ' | '.join(map(cell, headers)) + ' |',
                      '| ' + ' | '.join(['---'] * len(headers)) + ' |',
                      *['| ' + ' | '.join(map(cell, row)) + ' |' for row in records]])


def ci_text(row):
    if row is None:
        return 'N/A（无记录）'
    interval = ('N/A' if row['ci_low'] is None else
                f"[{row['ci_low']:.4f}, {row['ci_high']:.4f}]")
    return f"{interval}；有效{row['n_valid']}/{row['n_bootstrap']}；{row['ci_status']}"


def svg_grouped_bar(path, title, categories, series, *, y_label='数值', lower=0, upper=1):
    width, height = 1100, 520
    left, right, top, bottom = 85, 35, 85, 120
    pw, ph = width - left - right, height - top - bottom
    colors = ['#275dad', '#c24c27', '#339078', '#8051aa']
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           '<rect width="100%" height="100%" fill="white"/>',
           '<g font-family="Microsoft YaHei,Arial,sans-serif" fill="#172334">',
           f'<text x="{left}" y="32" font-size="21" font-weight="bold">{html.escape(title)}</text>',
           f'<text x="15" y="70" font-size="12">{html.escape(y_label)}</text>']
    for tick in np.linspace(lower, upper, 6):
        y = top + ph * (upper - tick) / (upper - lower)
        out += [f'<line x1="{left}" x2="{left + pw}" y1="{y:.2f}" y2="{y:.2f}" stroke="#dce3eb"/>',
                f'<text x="{left - 10}" y="{y + 4:.2f}" text-anchor="end" font-size="12">{tick:.2f}</text>']
    group_w = pw / max(1, len(categories))
    bar_w = min(36, group_w * .72 / max(1, len(series)))
    for j, (name, values) in enumerate(series.items()):
        color = colors[j % len(colors)]
        lx = left + j * 115
        out += [f'<rect x="{lx}" y="48" width="15" height="12" fill="{color}"/>',
                f'<text x="{lx + 21}" y="59" font-size="13">{html.escape(name)}</text>']
        for i, value in enumerate(values):
            x = left + group_w * (i + .5) + (j - len(series) / 2) * bar_w
            if value is None:
                out.append(f'<text x="{x + bar_w/2:.2f}" y="{top + ph - 10}" text-anchor="middle" font-size="10" fill="{color}">N/A</text>')
                continue
            y = top + ph * (upper - value) / (upper - lower)
            bar_h = max(0, ph * (value - lower) / (upper - lower))
            out += [f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w-2:.2f}" height="{bar_h:.2f}" fill="{color}"/>',
                    f'<text x="{x + bar_w/2:.2f}" y="{y - 5:.2f}" text-anchor="middle" font-size="10">{value:.3f}</text>']
    for i, category in enumerate(categories):
        x = left + group_w * (i + .5)
        out.append(f'<text x="{x:.2f}" y="{top + ph + 25}" text-anchor="middle" font-size="12">{html.escape(category)}</text>')
    out += ['<text x="85" y="490" font-size="12">冻结模型和工作点；描述性点估计，不据此重选模型。N/A保留，不能视为0。</text>', '</g></svg>']
    Path(path).write_text('\n'.join(out), encoding='utf-8')


def charts(directory, point_lookup):
    figures = directory / 'figures'
    figures.mkdir(exist_ok=True)
    svg_grouped_bar(figures / 'primary_AP.svg', 'E_A 主分析：逐标签及固定四标签 Macro AP',
                    [LABEL_ZH[k] for k in LABELS] + ['Macro AP'],
                    {m: [r['ap'] for r in point_lookup[('E_A', m, 'f1')]['labels']] +
                         [point_lookup[('E_A', m, 'f1')]['macro']['ap']] for m in MODELS})
    svg_grouped_bar(figures / 'natural_workload.svg', '328张自然原始页：不同冻结工作点的页面复核触发率',
                    [MODE_ZH[m] for m in MODES],
                    {m: [point_lookup[('E_all_natural', m, mode)]['page']['review_trigger_rate']
                         for mode in MODES] for m in MODELS}, y_label='触发率')
    svg_grouped_bar(figures / 'source_AP.svg', '来源分层：固定四标签 Macro AP（缺类时N/A）',
                    ['旧自然', 'V2图片式', 'V3可编辑', 'V3 GitHub', 'V3本地'],
                    {m: [point_lookup[(s, m, 'f1')]['macro']['ap'] for s in
                         ['A_old_natural', 'C_v2_image', 'D_v3_editable', 'D_github', 'D_local']]
                     for m in MODELS})


def write_report(directory, manifest, rows, y, point_lookup, boot, pairs, control_summary):
    def metric(s, m='C', mode='f1'):
        return point_lookup[(s, m, mode)]
    def bootrow(unit, stratum, model, mode='shared', label='macro', endpoint='ap'):
        return next((r for r in boot if r['cluster_unit'] == unit and r['stratum'] == stratum
                     and r['model'] == model and r['mode'] == mode and r['label'] == label
                     and r['metric'] == endpoint), None)
    primary = bootrow('source_family_id', 'E_A', 'C-A')
    primary_doc = bootrow('document_id', 'E_A', 'C-A')
    a, c, h, c9 = [metric('E_A', m)['macro']['ap'] for m in MODELS]
    delta = None if a is None or c is None else c - a
    if delta is None:
        main_text = '主终点不可定义，不能判断C是否优于A，也不替换主分析人群。'
    else:
        direction = '高于' if delta > 0 else '低于' if delta < 0 else '等于'
        main_text = f'C的主分析Macro AP为{fmt(c)}，{direction}A的{fmt(a)}，C−A={fmt(delta)}。'
        if primary and primary['ci_low'] is not None and primary['ci_low'] <= 0 <= primary['ci_high']:
            main_text += '家族级配对区间包含0，不能据此宣称稳定改善。'
        elif primary and primary['ci_low'] is None:
            main_text += '家族级配对区间因既定有效性门槛不可报告，不能据点差宣称稳定改善。'
        else:
            main_text += '来源家族数量少且样本便利收集，区间仍仅为探索性，不能据此宣称广泛泛化。'
    text = [
        '# 4.3G-2 冻结模型在独立PPTX基准上的正式对照评估', '',
        f"生成时间：{now()}。唯一评分批次：`{manifest['batch_id']}`。本报告只对该批次已保存概率做预注册的确定性统计，不重新推理、不拟合、不选阈值、不改路由或标签。", '',
        main_text, '',
        '## 1. 执行身份、完整性与纳入人群', '',
        f"正式模型评分次数：**{manifest['formal_score_passes']}次**；352/352页，23份文稿、328个group、8个预定来源家族；328张自然原始页与24张可控修改页分别统计。主分析E_A为282张A来源自然原始页。无模型输出驱动的排除。", '',
        '完整冻结校验、模型/CLIP/PNG recipe摘要、标签顺序、三工作点和来源家族映射均在第一次特征推理前通过。详细预检及登记输入SHA保存在执行清单和预检输出；评价程序先通过独立合成fixture。', '',
        f"**后处理技术修正披露**：评分登记时分析源码为`{manifest['script_sha256']['analyze_pptx_g2.py']}`，实际后处理最终源码为`{sha(__file__)}`。只读契约审查指出逐文稿/家族明细若直接汇总会混合旧自然页与可控修改页，因此在未查看任何正式概率或指标前，补充natural_or_controlled拆分，并添加只允许本批次具名修正的SHA校验和披露。主9分层、主比较、指标、Bootstrap、阈值与模型分数均未改变；没有重新评分。准确原源码及逐行diff已保留，具名登记见`postprocessing_amendment.json`；原execution_manifest与score_completion未改。不能将此宣称为完全零偏离的原样后处理。", '',
        table(['身份', 'SHA-256'], [
            ['模型bundle', manifest['model_bundle_sha256']],
            ['预测前协议', manifest['protocol_sha256']],
            ['正式冻结版本索引', manifest['freeze_registry_sha256']],
            ['预检输出', manifest['preflight_sha256']],
            ['本分析脚本（评分前登记）', manifest['script_sha256']['analyze_pptx_g2.py']],
            ['逐页概率CSV', sha(directory / 'probabilities.csv')],
            ['逐页三工作点告警CSV', sha(directory / 'alerts.csv')],
        ]), '',
        f"CLIP固定revision `{manifest['clip_revision']}`；默认中心裁剪512维；C为512+PNG10=522维；C9为512+PNG9=521维；H固定A/C/C/C。CLIP与PNG分支均从冻结原始完整页PNG得到，标准化器来自既有train，不在本基准fit。实际运行环境、GPU/CPU及每次分类器调用见 `execution_manifest.json`、`score_completion.json` 和 `scoring_log.jsonl`。", '',
        table(['分层', '页数', '文稿数', '来源家族数', '正例：字号/遮挡/拥挤/裁切'],
              [[STRATUM_ZH[s], int(metric(s)['page']['n_pages']),
                len({r['document_id'] for r in rows if r['sample_id'] in {rows[i]['sample_id'] for i, k in enumerate(build_strata(rows)[s]) if k}}),
                len({r['source_family_id'] for r, k in zip(rows, build_strata(rows)[s]) if k}),
                '/'.join(str(int(z['n_pos'])) for z in metric(s)['labels'])] for s in EXPECTED]), '',
        '来源A表示既有冻结来源类别，不表示多个生成平台完全独立。旧脚本/重建原稿46页另列E_C；可控修改均保留原始页面配对，绝不混入自然缺陷发生率或主Macro AP。', '',
        '## 2. 固定模型、工作点和指标规则', '',
        table(['模型', 'F1阈值', '高召回阈值', '低误报阈值'],
              [[m, *[', '.join(f'{v:g}' for v in manifest['thresholds'][m][mode]) for mode in MODES]] for m in MODELS]), '',
        '概率明确取class=1，告警为p≥t；AP按连续概率计算，三个工作点共用AP。每标签无正例或无负例时AP/P/R/F1全部N/A；正负齐全而无预测正例时Precision=N/A，Recall/F1仍按协议计算。固定四标签Macro和Micro要求所有标签支持完整，不以available-label均值替代。全阴性子集的页面误报率仍可计算。', '',
        '真实缺陷页检出须命中至少一个对应真实正标签，错误类别告警不算检出。高召回和低误报只是历史OOF冻结工作点名称，不保证本基准达到历史目标。', '',
        '## 3. 主分析E_A：逐标签与模型对照', '',
        table(['模型', '标签', '正/负', 'AP', 'Precision', 'Recall', 'F1', 'TP/FP/TN/FN'],
              [[m, LABEL_ZH[z['label']], f"{int(z['n_pos'])}/{int(z['n_neg'])}",
                fmt(z['ap']), fmt(z['precision']), fmt(z['recall']), fmt(z['f1']),
                '/'.join(str(int(z[k])) for k in ('tp', 'fp', 'tn', 'fn'))]
               for m in MODELS for z in metric('E_A', m)['labels']]), '',
        table(['模型', '固定四标签Macro AP', 'Macro F1（F1工作点）', 'Micro F1'],
              [[m, fmt(metric('E_A', m)['macro']['ap']), fmt(metric('E_A', m)['macro']['f1']),
                fmt(metric('E_A', m)['micro']['f1'])] for m in MODELS]), '',
        table(['预定对照', 'Macro AP差值', '家族级配对95%区间', '文稿级敏感性95%区间'],
              [[comparison, fmt(bootrow('source_family_id', 'E_A', comparison)['point']),
                ci_text(bootrow('source_family_id', 'E_A', comparison)),
                ci_text(bootrow('document_id', 'E_A', comparison))]
               for comparison in ('C-A', 'H-A', 'H-C', 'C9-C')]), '',
        'H只在字号标签使用A，其余概率与C相同，所以其变化只能归于预定字号路由；不能把这次观察转成新的按类路由选择。C9−C是预定去宽高比敏感性候选对照，不据此替换主模型。C9和C还采用各自在历史训练中冻结的不同C/class_weight（如遮挡C为0.1/None、C9为1/balanced；裁切C为1/None、C9为10/None），故差异不能完全因果归于宽高比单项。', '',
        '![主分析AP](../outputs/pptx_g2/' + directory.name + '/figures/primary_AP.svg)', '',
        '## 4. 自然页面的误报与复核负担', '',
        table(['人群', '模型', '工作点', '复核页/总页', '触发率', '全阴性误报页/全阴性页', '页面误报率', '真实缺陷检出页/缺陷页', '检出率', '平均告警/页'],
              [[s, m, MODE_ZH[mode], f"{int(q['n_triggered'])}/{int(q['n_pages'])}", pct(q['review_trigger_rate']),
                f"{int(q['n_false_alarm_pages'])}/{int(q['n_all_negative'])}", pct(q['all_negative_page_fpr']),
                f"{int(q['n_correct_hit_pages'])}/{int(q['n_defective'])}", pct(q['defect_page_hit_rate']), fmt(q['mean_alerts_per_page'])]
               for s in ('E_A', 'E_all_natural') for m in MODELS for mode in MODES
               for q in [metric(s, m, mode)['page']]]), '',
        table(['自然328页上的模型', '高召回相对F1新增复核页', '新增全阴性误报页', '新增真实缺陷检出页'],
              [[m, int(metric('E_all_natural', m, 'high_recall')['page']['n_triggered'] - metric('E_all_natural', m)['page']['n_triggered']),
                int(metric('E_all_natural', m, 'high_recall')['page']['n_false_alarm_pages'] - metric('E_all_natural', m)['page']['n_false_alarm_pages']),
                int(metric('E_all_natural', m, 'high_recall')['page']['n_correct_hit_pages'] - metric('E_all_natural', m)['page']['n_correct_hit_pages'])]
               for m in MODELS]), '',
        '额外复核负担既包含正确命中，也包含全阴性告警及错误类别告警；不能仅用Recall解释成本。完整逐标签及三工作点数值见分层CSV。', '',
        '![自然页面复核负担](../outputs/pptx_g2/' + directory.name + '/figures/natural_workload.svg)', '',
        '## 5. 来源、图片式/可编辑与文稿差异', '',
        table(['分层', '模型', 'Macro AP', 'Macro F1', '页面误报率', '缺陷页检出率', '复核触发率'],
              [[s, m, fmt(metric(s, m)['macro']['ap']), fmt(metric(s, m)['macro']['f1']),
                pct(metric(s, m)['page']['all_negative_page_fpr']), pct(metric(s, m)['page']['defect_page_hit_rate']),
                pct(metric(s, m)['page']['review_trigger_rate'])]
               for s in EXPECTED for m in MODELS]), '',
        '图片式V2与可编辑V3还同时区别于内容、比例（3:2与16:9）、生成来源、公开展示选择和正例数量；两者差异不是单独编辑性因素的因果效应。V2拥挤无正例，固定四标签宏指标为N/A；不得用available-label三类均值与V3四类均值直接比较。V3 GitHub下载与中文本地稿单列，不合称同一随机AI平台分布。旧84页自然原稿没有确认正例，其分类AP/F1为N/A，不能用0表示差模型；仍应观察全阴性告警。', '',
        '逐文稿完整四标签支持、混淆计数、概率AP及各工作点页面负担见 `document_metrics.csv`；8个预定来源家族对应结果见 `source_family_metrics.csv`。两表均按natural_or_controlled进一步拆分，旧文稿的原始页与可控变体不混成同一来源分数。所有文稿均报告，没有按成绩选择展示或重新分组。逐单文稿/单家族只报点值，不将其内部页面当独立来源计算CI；它们不能满足独立来源聚类数量要求。', '',
        '![来源分层AP](../outputs/pptx_g2/' + directory.name + '/figures/source_AP.svg)', '',
        '## 6. 可控修改的配对压力测试', '',
        '24页修改来自6个原始group及3份文稿；逐页找到同group原始页，比较四标签概率与同阈值告警。修改意图仅作描述，实际检出和误报严格使用冻结人工真值；非目标标签也可能是真阳性，不能一律算误报。', '',
        table(['模型', '修改意图', '对应真阳性/真阴性', '均值Δ概率', '概率升高/配对数', '修改前→后对应告警', '对应真阳性命中', '对应真阴性误报'],
              [[q['model'], q['modification_type'], f"{q['n_actual_positive']}/{q['n_actual_negative']}",
                fmt(q['mean_probability_delta']), f"{q['n_probability_increased']}/{q['n_pairs']}",
                f"{q['n_alerts_before']}→{q['n_alerts_after']}", pct(q['actual_positive_hit_rate']), pct(q['actual_negative_fpr'])]
               for q in control_summary if q['mode'] == 'f1' and q['is_intended_target']]), '',
        table(['模型（F1工作点）', '非目标类实际阴性中的误报警/阴性标签对', '可控页中多标签页数'],
              [[m, f"{sum(q['modified_fp'] for q in pairs if q['model']==m and q['mode']=='f1' and not q['is_intended_target'])}/"
                   f"{sum(1-q['modified_true'] for q in pairs if q['model']==m and q['mode']=='f1' and not q['is_intended_target'])}",
                len({q['sample_id'] for q in pairs if q['modified_positive_labels'] >= 2})] for m in MODELS]), '',
        '正的平均概率变化仅表示风险响应方向，不等于每页命中或真实缺陷可被稳定区分；应同时查看 `controlled_pairs.csv` 中每个pair/标签的概率变化、告警变化及真值。全三工作点聚合见 `controlled_summary.csv`。', '',
        '## 7. 统计不确定性与有效重复', '',
        '严格采用来源家族级配对bootstrap，B=2000、seed=42；同一全局聚类抽样次数复用于所有模型、阈值、分层与差值。document级作为预定敏感性，不以较窄区间替代家族结果。每次从全352页的固定家族/文稿单位抽样，再取对应分层，非分层单独重抽。所有无效重复与原因位码完整保存，没有补抽、删页或重调家族。', '',
        table(['单位', '主C−A点差', '95%区间/有效重复', '解释'],
              [['来源家族', fmt(primary['point']), ci_text(primary), '主不确定性分析；小家族数仅探索性'],
               ['文稿', fmt(primary_doc['point']), ci_text(primary_doc), '敏感性；不证明文稿独立于共同生成流程']]), '',
        '任一标签无正/负使该重复的固定四标签Macro AP无效。区间要求分层至少2聚类、至少100有效重复且有效比例≥80%；小于10聚类统一标探索性。若任一门槛未过，只报点值、支持与N/A，不能据无区间声称无差异或显著改善。每一分层/模型/模式/指标的有效与无效数、具体原因及CI状态见 `bootstrap_summary.csv`；两个NPZ保留全部2000个值（含NaN）、reason_codes和实际draw_counts，可逐项重算。', '',
        '## 8. 结论、解释边界与交付', '',
        '1. **C相对A**：' + main_text,
        f"2. **H相对C**：主Macro AP差为{fmt(None if h is None or c is None else h-c)}；家族区间{ci_text(bootrow('source_family_id','E_A','H-C'))}。仅为预注册次要对照，不将一次点差解释为稳定路由收益。",
        f"3. **C9敏感性**：C9−C Macro AP差为{fmt(None if c9 is None or c is None else c9-c)}；家族区间{ci_text(bootrow('source_family_id','E_A','C9-C'))}。反映冻结方案对宽高比特征的敏感性，不据成绩改recipe。",
        '4. **类别差异**：以第3节逐类AP/P/R/F1和真实支持数判断；字号、遮挡、裁切是全局CLIP与非语义统计可能难以描述的细节，差结果不能自动归因于标注错误。',
        '5. **自然误报和工作量**：第4节给出三个冻结模式的实际误报警与复核页数；它们是该便利基准上的操作负担，不是部署总体误报率。',
        '6. **高召回代价**：明确列出相对F1新增的复核页、全阴性误报页和正确检出页，不因漏检仍多而继续降低阈值。',
        '7. **图片式与可编辑**：分别报告；文稿、比例、语种、生成工具和展示筛选混杂，不能据差异宣称图片式/可编辑本身更容易。',
        '8. **可控响应**：第6节的真值配对压力测试记录增/减概率和额外误报；24个变体有原页相关性，只作次要诊断。',
        '9. **稀少正例**：自然页四标签正例分布分散且很少，多项单文稿/来源分层缺类；AP对少量页面排名高度敏感，bootstrap可因缺类失效，N/A均保留。',
        '10. **泛化和后续决策**：23份文稿仅8个保守来源家族；公开展示样例、同工作流/同课程、本地AI加少量编辑、AI初审后用户裁定及选择意图未完整证实等限制仍在。没有真正随机平台抽样或独立多人一致性证据；不能证明广泛部署泛化，也不宣布新“最优”模型。', '',
        '未使用SlideAudit官方test派生信息开发本轮代码；没有重新训练A/C/H/C9、调整阈值/路由、修改人工真值或冻结工件。之前审计阶段已披露的旧清单边界事件不被当作本轮特征或评价输入。本轮数据访问边界和受保护文件摘要由评分日志及完成记录核验。', '',
        '主要交付位于 `' + str(directory) + '`：`probabilities.csv`、`alerts.csv`、`stratified_label_metrics.csv`、`stratified_summary_metrics.csv`、`document_metrics.csv`、`source_family_metrics.csv`、`bootstrap_summary.csv`、两份完整bootstrap NPZ、`controlled_pairs.csv`、`controlled_summary.csv`、`point_metrics.json`、`figures/*.svg`、执行清单、失败记录及SHA清单。', '',
        '本次在唯一保存概率上完成预注册评价后停止。后续若依据这些成绩重新挑模型/阈值/路由，这批基准即参与选择，不能再把同批成绩当作新的无偏最终性能证明；不自动接入或修改智能体。', '',
    ]
    path = Path(manifest['report_path'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(text), encoding='utf-8')
    return path


def analyze(directory):
    manifest, rows, y, probabilities, thresholds = load_scores(directory)
    strata = build_strata(rows)
    protected = {p: sha(directory / p) for p in ['execution_manifest.json', 'probabilities.csv',
                                              'alerts.csv', 'score_completion.json']}
    marker = directory / 'analysis_registration.json'
    if marker.exists():
        raise FileExistsError('Analysis already registered; inspect saved outputs without rerunning.')
    save_json(marker, {'at': now(), 'script_sha256': sha(__file__), 'inputs': protected,
                       'pure_saved_probability_postprocessing': True, 'new_inference_calls': 0})
    points = evaluate_strata(y, probabilities, thresholds, strata)
    label_rows, summary_rows = flatten_points(points, rows, strata)
    save_json(directory / 'point_metrics.json', points)
    csv_write(directory / 'stratified_label_metrics.csv', label_rows)
    csv_write(directory / 'stratified_summary_metrics.csv', summary_rows)
    csv_write(directory / 'document_metrics.csv', partition_metrics(rows, y, probabilities, thresholds, 'document_id'))
    csv_write(directory / 'source_family_metrics.csv', partition_metrics(rows, y, probabilities, thresholds, 'source_family_id'))
    pairs, control_summary = controlled_pairs(rows, y, probabilities, thresholds)
    if len(pairs) != 24 * 4 * 3 * 4:
        raise ValueError('Controlled pair coverage mismatch')
    csv_write(directory / 'controlled_pairs.csv', pairs)
    csv_write(directory / 'controlled_summary.csv', control_summary)
    lookup = {(p['stratum'], p['model'], p['mode']): p['metrics'] for p in points}
    charts(directory, lookup)
    boot, details = bootstrap(directory, rows, y, probabilities, thresholds, strata)
    save_json(directory / 'bootstrap_metadata.json', details)
    report = write_report(directory, manifest, rows, y, lookup, boot, pairs, control_summary)
    if any(sha(directory / p) != digest for p, digest in protected.items()):
        raise RuntimeError('Protected single-pass probabilities/manifest modified during analysis')
    artifact_rows = [{'path': str(p.relative_to(directory)), 'bytes': p.stat().st_size, 'sha256': sha(p)}
                     for p in sorted(directory.rglob('*')) if p.is_file() and p.name not in ('artifact_sha256.csv', 'analysis_completion.json')]
    artifact_rows.append({'path': str(report), 'bytes': report.stat().st_size, 'sha256': sha(report)})
    csv_write(directory / 'artifact_sha256.csv', artifact_rows)
    save_json(directory / 'analysis_completion.json', {'at': now(), 'status': 'complete', 'sample_count': len(rows),
               'point_records': len(points), 'stratified_label_rows': len(label_rows),
               'stratified_summary_rows': len(summary_rows), 'bootstrap_endpoints': len(boot),
               'controlled_pair_label_rows': len(pairs), 'report': str(report), 'report_sha256': sha(report),
               'artifact_ledger_sha256': sha(directory / 'artifact_sha256.csv'), 'protected_score_inputs_unchanged': True,
               'new_inference_calls': 0, 'fit_calls': 0, 'threshold_changes': 0})
    print(json.dumps({'status': 'complete', 'report': str(report), 'run_dir': str(directory)}, ensure_ascii=False), flush=True)


def fixture():
    """Entirely invented, deliberately small truth/probability/metadata fixture."""
    truth = np.array([[0, 0, 0, 0], [1, 0, 1, 0], [0, 1, 0, 1],
                      [1, 1, 1, 1], [0, 0, 0, 0], [1, 1, 0, 0]], dtype=np.int8)
    rows = []
    for i in range(6):
        rows.append({'sample_id': f'fiction_{i}', 'cohort_id': 'v1' if i < 4 else 'v3',
                     'document_id': 'fiction_doc' + str(i // 2), 'source_family_id': 'fiction_family' + str(i // 2),
                     'group_id': 'fiction_group' + str(0 if i == 1 else i),
                     'natural_or_controlled': 'controlled' if i == 1 else 'natural',
                     'source_class': 'A', 'source_scope': 'local', 'variant_id': 'font_small' if i == 1 else 'original',
                     'modification_type': 'font_small' if i == 1 else 'none'})
    a = np.array([[.10, .20, .30, .40], [.70, .60, .50, .40], [.30, .60, .20, .90],
                  [.80, .60, .70, .80], [.02, .03, .04, .05], [.55, .65, .25, .35]])
    c = np.minimum(a + .03, 1)
    probabilities = {'A': a, 'C': c, 'H': np.column_stack([a[:, 0], c[:, 1:]]), 'C9': np.minimum(a + .02, 1)}
    thresholds = {m: {mode: [.5] * 4 for mode in MODES} for m in MODELS}
    strata = {'fixture_all': np.ones(6, dtype=bool), 'fixture_no_positive': np.arange(6) == 0,
              'fixture_empty': np.zeros(6, dtype=bool)}
    points = evaluate_strata(truth, probabilities, thresholds, strata)
    assert len(points) == 3 * 4 * 3
    labels, summaries = flatten_points(points, rows, strata)
    assert len(labels) == len(points) * 4
    assert all(q['ap'] is None for q in labels if q['stratum'] != 'fixture_all')
    pairs, control = controlled_pairs(rows, truth, probabilities, thresholds)
    assert len(pairs) == 4 * 3 * 4 and len(control) == 4 * 3 * 4
    target = [p for p in pairs if p['model'] == 'A' and p['mode'] == 'f1' and p['is_intended_target']][0]
    assert target['modified_true'] == 1 and target['original_true'] == 0 and abs(target['probability_delta'] - .6) < 1e-14
    assert target['original_alert'] == 0 and target['modified_alert'] == 1
    with tempfile.TemporaryDirectory(prefix='pptx_g2_analysis_fixture_') as tmp:
        directory = Path(tmp)
        csv_write(directory / 'labels.csv', labels)
        csv_write(directory / 'summary.csv', summaries)
        csv_write(directory / 'controlled.csv', pairs)
        saved, details = bootstrap(directory, rows, truth, probabilities, thresholds, strata, B=101, seed=42)
        with np.load(directory / 'bootstrap_family_replicates.npz', allow_pickle=False) as archive:
            assert archive['draw_counts'].shape == (101, 3)
            entry = saved[0]
            assert archive[entry['replicate_key'] + '_values'].shape == (101,)
            assert archive[entry['replicate_key'] + '_reason_codes'].shape == (101,)
        assert all(r['n_valid'] == 0 for r in saved if r['stratum'] == 'fixture_empty')
        with (directory / 'labels.csv').open(encoding='utf-8-sig', newline='') as f:
            recovered = list(csv.DictReader(f))
        assert len(recovered) == len(labels) and any(r['ap'] == 'N/A' for r in recovered)
        svg_grouped_bar(directory / 'fixture.svg', '合成示例', ['一', '二'], {'A': [.1, None], 'C': [.2, .3]})
        assert '<svg' in (directory / 'fixture.svg').read_text(encoding='utf-8')
        docs = partition_metrics(rows, truth, probabilities, thresholds, 'document_id')
        assert len(docs) == 4 * 4 * 3 * 4
        assert {r['natural_or_controlled'] for r in docs} == {'natural', 'controlled'}
    result = {'status': 'passed', 'at': now(), 'formal_data_or_model_predictions_used': False, 'formal_data_used': False,
              'script_sha256': sha(__file__), 'checks': ['synthetic point and NA flattening',
              'pure CSV round trip', 'same-group original/controlled probability and alerts',
              'actual truth separate from modification intention', 'whole-cluster shared paired draws',
              'streamed NPZ values and invalid reason preservation', 'empty stratum not redrawn',
              'document summary coverage', 'standalone SVG with NA']}
    destination = ML / '.cache/pptx_g2_fixture'
    destination.mkdir(parents=True, exist_ok=True)
    save_json(destination / 'analysis_fixture_results.json', result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--run-dir', type=Path)
    group.add_argument('--fixture', action='store_true')
    args = parser.parse_args()
    if args.fixture:
        fixture()
    else:
        analyze(args.run_dir.resolve())
