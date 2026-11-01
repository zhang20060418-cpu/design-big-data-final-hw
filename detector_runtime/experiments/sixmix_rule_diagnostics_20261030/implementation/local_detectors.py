"""Train-developed local D2/D3 candidates over measured geometry only.

No file reads, reference labels, generator names, templates, or text semantics.
The thresholds express an engineering hypothesis; outputs are not human truth.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from itertools import combinations
import math
from statistics import median


VERSION = 'sixmix-local-relations-v1'
DEFAULTS = {
    'D3': {
        'maximum_short_characters': 24,
        'maximum_small_font_at_540': 10.0,
        'maximum_paragraph_height_in_fonts': 2.0,
        'alignment_tolerance_at_540': 0.75,
        'minimum_axis_repetitions': 3,
        'minimum_grid_nodes': 6,
        'dominant_grid_fraction_for_abstention': 0.5,
        'grid_envelope_overlap_fraction_for_abstention': 0.1,
        'minimum_residual_nodes': 8,
        'minimum_body_zone_count': 4,
        'maximum_nearest_gap_in_fonts': 8.0,
        'minimum_local_window_nodes': 5,
        'minimum_normalized_density': 24.0,
    },
    'D2': {
        'minimum_target_characters': 8,
        'minimum_lost_horizontal_font_units': 4.0,
        'minimum_lost_vertical_font_units': 0.75,
        'minimum_vertical_paragraph_fraction': 0.25,
    },
}


def _num(x):
    try:
        x = float(x)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _box(x):
    if not isinstance(x, dict):
        return None
    b = {k: _num(x.get(k)) for k in ('x', 'y', 'w', 'h')}
    return b if all(v is not None for v in b.values()) and b['w'] > 0 and b['h'] > 0 else None


def _area(b):
    return b['w'] * b['h'] if b else 0.


def _inter(a, b):
    if not a or not b:
        return None
    x, y = max(a['x'], b['x']), max(a['y'], b['y'])
    w = min(a['x'] + a['w'], b['x'] + b['w']) - x
    h = min(a['y'] + a['h'], b['y'] + b['h']) - y
    return {'x': x, 'y': y, 'w': w, 'h': h} if w > 0 and h > 0 else None


def _bounds(boxes):
    if not boxes:
        return None
    x, y = min(b['x'] for b in boxes), min(b['y'] for b in boxes)
    return {'x': x, 'y': y,
            'w': max(b['x'] + b['w'] for b in boxes) - x,
            'h': max(b['y'] + b['h'] for b in boxes) - y}


def _font(obj):
    values = [_num(p.get('font_pt')) for p in obj.get('paragraphs', []) if p.get('text_chars', 0)]
    values = [x for x in values if x is not None and x > 0]
    if not values:
        values = [_num(x) for x in obj.get('font_sizes_pt', [])]
        values = [x for x in values if x is not None and x > 0]
    return median(values) if values else None


def _paragraphs(obj):
    return [p for p in obj.get('paragraphs', []) if p.get('text_chars', 0) and _box(p.get('bbox'))]


def _dimensions(context):
    p = context.get('page', {})
    w, h = _num(p.get('width_pt')), _num(p.get('height_pt'))
    return (w, h) if w is not None and h is not None and min(w, h) > 0 else (None, None)


def _unrotated(obj):
    r = _num(obj.get('rotation', 0))
    return r is not None and min(r % 360, 360 - r % 360) < 1e-6


def _cfg(config, key):
    return {**DEFAULTS[key], **((config or {}).get(key) or {})}


def _result(key, evidence, unresolved, features, applicable=True):
    status = ('candidate' if evidence else 'unknown' if unresolved else
              'no_candidate_in_available_scope' if applicable else 'unsupported')
    return dict(detector=key, label={'D2': 'occluded_content', 'D3': 'cluttered_layout'}[key],
                version=VERSION, status=status, coverage='partial' if applicable else 'unsupported',
                score=max((e['ordinal_strength'] for e in evidence), default=None if unresolved or not applicable else 0.),
                score_kind='ordinal_heuristic_not_probability', evidence=evidence,
                unresolved=unresolved, features=features,
                limitations=[
                    'Developed using the sixmix training split only; the source documents overlap splits.',
                    'Measured paragraph envelopes are not glyph segmentation or human readability judgments.',
                    'Sparse disorganized microtext and span loss are candidate mechanisms, not a complete detector.'])


def _clusters(values, tolerance):
    groups = []
    for index, value in sorted(enumerate(values), key=lambda p: p[1]):
        if not groups or value - groups[-1][0] > tolerance:
            groups.append([value, []])
        groups[-1][1].append(index)
    return [indices for _, indices in groups]


def _grid_support(nodes, width, height, config):
    """Detect coherent row/column lattices before local region selection.

    This also covers tables drawn from ordinary textboxes instead of native
    cells. A mixed paragraph+table region cannot destroy the table's evidence.
    """
    tolerance = config['alignment_tolerance_at_540'] * height / 540.
    xs = [n['bbox']['x'] * width for n in nodes]
    ys = [n['bbox']['y'] * height for n in nodes]
    xgroups, ygroups = _clusters(xs, tolerance), _clusters(ys, tolerance)
    min_axis = config['minimum_axis_repetitions']
    x_ok = {i for group in xgroups if len(group) >= min_axis for i in group}
    y_ok = {i for group in ygroups if len(group) >= min_axis for i in group}
    protected = x_ok & y_ok
    if len(protected) < config['minimum_grid_nodes']:
        protected = set()
    # A one-dimensional regularly spaced axis is also a legitimate chart or
    # legend organization. No transitive fuzzy alignment chains are used.
    for groups, secondary in ((xgroups, ys), (ygroups, xs)):
        for group in groups:
            if len(group) < 4:
                continue
            ordered = sorted(secondary[i] for i in group)
            gaps = [b - a for a, b in zip(ordered, ordered[1:])]
            if gaps and min(gaps) > tolerance and max(gaps) - min(gaps) <= 2 * tolerance:
                protected.update(group)
    return protected, dict(x_clusters=[len(x) for x in xgroups], y_clusters=[len(y) for y in ygroups],
                           protected_object_ids=[nodes[i]['id'] for i in sorted(protected)])


def _gap_font(a, b, width, height):
    x = max(0., a['bbox']['x'] - b['bbox']['x'] - b['bbox']['w'],
            b['bbox']['x'] - a['bbox']['x'] - a['bbox']['w']) * width
    y = max(0., a['bbox']['y'] - b['bbox']['y'] - b['bbox']['h'],
            b['bbox']['y'] - a['bbox']['y'] - a['bbox']['h']) * height
    return math.hypot(x, y) / median([a['font_pt'], b['font_pt']])


def detect_clutter(context, config=None):
    """D3 replacement: short-text residuals after actual geometric structure."""
    c = _cfg(config, 'D3'); width, height = _dimensions(context)
    if width is None:
        return _result('D3', [], [{'kind': 'page_dimensions_unavailable'}], {}, False)
    page = {'x': 0., 'y': 0., 'w': 1., 'h': 1.}
    nodes, unresolved, excluded = [], [], Counter()
    for obj in context.get('objects', []):
        chars = obj.get('text_chars', 0)
        if not 0 < chars <= c['maximum_short_characters']:
            continue
        if obj.get('kind') == 'cell' or obj.get('cell'):
            excluded['native_table_cell'] += 1; continue
        if obj.get('text_role') in ('page_number', 'navigation', 'title'):
            excluded['header_or_page_navigation'] += 1; continue
        if not _unrotated(obj):
            unresolved.append({'object_id': obj.get('id'), 'kind': 'rotated_short_text_layout'}); continue
        paragraphs = _paragraphs(obj); font = _font(obj)
        if not paragraphs or font is None:
            unresolved.append({'object_id': obj.get('id'), 'kind': 'actual_short_text_layout_or_font_unavailable'}); continue
        b = _inter(_bounds([_box(p['bbox']) for p in paragraphs]), page)
        if not b or b['y'] >= .9:
            excluded['outside_body_scope'] += 1; continue
        if font * 540 / height > c['maximum_small_font_at_540']:
            continue
        if b['h'] * height > font * c['maximum_paragraph_height_in_fonts']:
            excluded['multiline_paragraph'] += 1; continue
        nodes.append({'id': obj['id'], 'bbox': b, 'font_pt': font,
                      'characters': chars, 'measurement_source': 'measured_nonempty_paragraph_envelope'})
    protected, grid = _grid_support(nodes, width, height, c)
    ambiguous_grid_ids = set()
    for a, b in combinations(sorted(protected), 2):
        hit = _inter(nodes[a]['bbox'], nodes[b]['bbox'])
        if _area(hit) / min(_area(nodes[a]['bbox']), _area(nodes[b]['bbox'])) >= c['grid_envelope_overlap_fraction_for_abstention']:
            ambiguous_grid_ids.update((nodes[a]['id'], nodes[b]['id']))
    if ambiguous_grid_ids:
        unresolved.append(dict(kind='aligned_table_or_chart_envelopes_overlap_requires_glyph_review',
                               object_ids=sorted(ambiguous_grid_ids)))
    residual = [n for i, n in enumerate(nodes) if i not in protected]
    zones = {(min(2, int((n['bbox']['x'] + n['bbox']['w'] / 2) * 3)),
              min(2, int((n['bbox']['y'] + n['bbox']['h'] / 2) * 3))) for n in residual}
    nearest = [min(_gap_font(n, other, width, height) for other in residual if other is not n)
               for n in residual] if len(residual) > 1 else []
    local = []
    for node in residual:
        b = node['bbox']; cx, cy = b['x'] + b['w'] / 2, b['y'] + b['h'] / 2
        window = {'x': max(0., min(.5, cx - .25)), 'y': max(0., min(.5, cy - .25)), 'w': .5, 'h': .5}
        selected = [n for n in residual if _area(_inter(n['bbox'], window)) >= .5 * _area(n['bbox'])]
        local.append({'bbox': window, 'object_ids': [n['id'] for n in selected],
                      'node_count': len(selected), 'nodes_per_normalized_area': len(selected) / .25})
    strongest = max(local, key=lambda x: x['node_count'], default=None)
    triggered = (len(residual) >= c['minimum_residual_nodes'] and len(zones) >= c['minimum_body_zone_count']
                 and nearest and median(nearest) <= c['maximum_nearest_gap_in_fonts']
                 and strongest and strongest['node_count'] >= c['minimum_local_window_nodes']
                 and strongest['nodes_per_normalized_area'] >= c['minimum_normalized_density'])
    features = dict(nodes=nodes, grid=grid, residual_object_ids=[n['id'] for n in residual],
                    residual_node_count=len(residual), occupied_body_zones=sorted(zones),
                    median_nearest_gap_font_units=median(nearest) if nearest else None,
                    strongest_local_window=strongest, excluded_counts=dict(excluded), config=c)
    evidence = []
    if triggered and len(protected) / max(1, len(nodes)) >= c['dominant_grid_fraction_for_abstention']:
        # Remaining compact labels can belong to a neighboring card, legend,
        # or chart in a structured dashboard. Context alone cannot establish
        # that relationship: abstain instead of declaring a normal page.
        unresolved.append(dict(kind='dominant_structured_grid_with_unresolved_residual_labels',
                               object_ids=[n['id'] for n in residual],
                               protected_grid_fraction=len(protected) / len(nodes)))
        triggered = False
    if triggered:
        evidence.append(dict(object_ids=[n['id'] for n in residual],
                             bbox=_bounds([n['bbox'] for n in residual]),
                             kind='distributed_short_text_disorganization_with_local_density',
                             measurement_source='COM_actual_paragraphs+font_normalized_geometric_alignment_and_local_windows',
                             ordinal_strength=.7, metrics={k:features[k] for k in
                                 ['residual_node_count', 'occupied_body_zones', 'median_nearest_gap_font_units', 'strongest_local_window']}))
    return _result('D3', evidence, unresolved, features, bool(nodes) or not unresolved)


def detect_occlusion(context, previous_result=None, config=None):
    """Add D2 physically substantial partial span loss to the unchanged D2 base.

    Pass the locked original D2 result to preserve all its evidence and unknowns.
    A span remains a candidate: no invisible-glyph confirmation is fabricated.
    """
    c = _cfg(config, 'D2'); width, height = _dimensions(context)
    if width is None:
        return _result('D2', [], [{'kind': 'page_dimensions_unavailable'}], {}, False)
    base = deepcopy(previous_result) if previous_result is not None else None
    evidence = deepcopy(base.get('evidence', [])) if base else []
    unresolved = deepcopy(base.get('unresolved', [])) if base else []
    existing = {tuple(e.get('object_ids', [])) for e in evidence}
    measured, additions = [], []
    for obj in context.get('objects', []):
        if obj.get('text_chars', 0) < c['minimum_target_characters']:
            continue
        font = _font(obj)
        if font is None or not _unrotated(obj):
            if base is None:
                unresolved.append({'object_id': obj.get('id'), 'kind': 'target_font_or_rotation_unavailable'})
            continue
        for upper in context.get('objects', []):
            if upper.get('id') == obj.get('id') or upper.get('z_order', -1) <= obj.get('z_order', -1):
                continue
            if (upper.get('opacity') or {}).get('state') != 'fully_opaque' or not _unrotated(upper):
                continue
            # Irregular opaque shapes do not fill their bounding rectangle.
            if upper.get('kind') != 'image' and upper.get('geometry_type') != 'rect':
                continue
            if obj.get('parent_id') is not None and obj.get('parent_id') == upper.get('parent_id'):
                continue
            for paragraph in _paragraphs(obj):
                pb = _box(paragraph['bbox']); hit = _inter(pb, _box(upper.get('bbox')))
                if not hit:
                    continue
                lost_x, lost_y = hit['w'] * width / font, hit['h'] * height / font
                metric = dict(target_object_id=obj['id'], upper_object_id=upper['id'],
                              paragraph_id=paragraph.get('id'), bbox=hit,
                              lost_horizontal_font_units=lost_x, lost_vertical_font_units=lost_y,
                              paragraph_vertical_fraction=hit['h']/pb['h'],
                              paragraph_envelope_coverage=_area(hit)/_area(pb))
                measured.append(metric)
                if (lost_x >= c['minimum_lost_horizontal_font_units'] and
                    lost_y >= c['minimum_lost_vertical_font_units'] and
                    hit['h']/pb['h'] >= c['minimum_vertical_paragraph_fraction']):
                    ids = (obj['id'], upper['id'])
                    if ids not in existing:
                        additions.append(dict(object_ids=list(ids), bbox=hit,
                            kind='opaque_cover_removes_substantial_measured_text_span',
                            measurement_source='COM_actual_paragraph+verified_opacity+z_order+font_normalized_span',
                            ordinal_strength=.7, glyph_coverage_confirmed=False, metrics=metric))
                        existing.add(ids)
    evidence.extend(additions)
    features = deepcopy(base.get('features', {})) if base else {}
    features.update(local_span_measurements=measured, local_span_addition_count=len(additions), local_config=c)
    return _result('D2', evidence, unresolved, features, bool(context.get('objects')))


def detect_local(context, previous_results=None, config=None):
    previous_results = previous_results or {}
    return {'D2': detect_occlusion(context, previous_results.get('D2'), config),
            'D3': detect_clutter(context, config)}
