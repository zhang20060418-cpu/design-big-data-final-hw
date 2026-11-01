"""Measured-text relation graphs for local clutter hypotheses.

This detector neither parses PPTX nor reads labels. Distances use physical page
points and measured line heights (or an explicitly reported font-height proxy).
An ordinal candidate is not an automatic human annotation or a probability.
"""
from __future__ import annotations

from collections import Counter
from itertools import combinations
import json
import math
from pathlib import Path
from statistics import median


def _finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _box(value):
    if not isinstance(value, dict):
        return None
    b = {k: _finite(value.get(k)) for k in ('x', 'y', 'w', 'h')}
    return b if all(v is not None for v in b.values()) and b['w'] > 0 and b['h'] > 0 else None


def _area(b):
    return b['w'] * b['h'] if b else 0.


def _intersection(a, b):
    x, y = max(a['x'], b['x']), max(a['y'], b['y'])
    return {'x': x, 'y': y,
            'w': max(0., min(a['x'] + a['w'], b['x'] + b['w']) - x),
            'h': max(0., min(a['y'] + a['h'], b['y'] + b['h']) - y)}


def _bounds(boxes):
    x, y = min(b['x'] for b in boxes), min(b['y'] for b in boxes)
    return {'x': x, 'y': y,
            'w': max(b['x'] + b['w'] for b in boxes) - x,
            'h': max(b['y'] + b['h'] for b in boxes) - y}


def _union_area(boxes):
    """Exact axis-aligned rectangle union by an x sweep, without raster rounding."""
    xs = sorted(set(v for b in boxes for v in (b['x'], b['x'] + b['w'])))
    result = 0.
    for left, right in zip(xs, xs[1:]):
        spans = sorted((b['y'], b['y'] + b['h']) for b in boxes
                       if b['x'] < right and b['x'] + b['w'] > left)
        height, low, high = 0., None, None
        for a, b in spans:
            if low is None:
                low, high = a, b
            elif a > high:
                height += high - low
                low, high = a, b
            else:
                high = max(high, b)
        if low is not None:
            height += high - low
        result += (right - left) * height
    return result


def _norm(b, w, h):
    return {'x': b['x'] / w, 'y': b['y'] / h, 'w': b['w'] / w, 'h': b['h'] / h}


def _physical(b, w, h):
    return {'x': b['x'] * w, 'y': b['y'] * h, 'w': b['w'] * w, 'h': b['h'] * h}


def _gap(a, b):
    dx = max(0., a['x'] - b['x'] - b['w'], b['x'] - a['x'] - a['w'])
    dy = max(0., a['y'] - b['y'] - b['h'], b['y'] - a['y'] - a['h'])
    return math.hypot(dx, dy)


def _axis_labels(values, tolerance):
    """Complete-range clusters avoid transitive alignment of a long drift."""
    groups = []
    labels = {}
    for index, value in sorted(enumerate(values), key=lambda t: t[1]):
        if not groups or value - groups[-1][0] > tolerance:
            groups.append([value, []])
        groups[-1][1].append(index)
    for group_id, (_, indices) in enumerate(groups):
        for index in indices:
            labels[index] = group_id
    return labels, Counter(labels.values())


def _regular_grid(nodes, c):
    line_height = median(n['line_height_pt'] for n in nodes)
    tolerance = line_height * c['grid_alignment_tolerance_line_heights']
    # A ragged text column normally aligns at its left edge rather than center.
    # Choose one coherent pair of axes for the whole region, never per-node
    # combinations. This is geometric regularity, not label-dependent tuning.
    choices = []
    for x_mode, x_factor in (('left', 0.), ('center', .5), ('right', 1.)):
        xs = [n['box_pt']['x'] + n['box_pt']['w'] * x_factor for n in nodes]
        x_labels, x_counts = _axis_labels(xs, tolerance)
        for y_mode, y_factor in (('top', 0.), ('center', .5), ('bottom', 1.)):
            ys = [n['box_pt']['y'] + n['box_pt']['h'] * y_factor for n in nodes]
            y_labels, y_counts = _axis_labels(ys, tolerance)
            one_row_or_column = len(x_counts) == 1 or len(y_counts) == 1
            aligned = sum(one_row_or_column or (x_counts[x_labels[i]] >= 2 and
                          y_counts[y_labels[i]] >= 2) for i in range(len(nodes))) / len(nodes)
            choices.append((aligned, -(len(x_counts) + len(y_counts)), x_mode,
                            y_mode, x_counts, y_counts))
    aligned, _, x_mode, y_mode, x_counts, y_counts = max(choices, key=lambda v: (v[0], v[1]))
    all_pairs = list(combinations(nodes, 2))
    overlap = any(_area(_intersection(a['box_pt'], b['box_pt'])) > 1e-9
                  for a, b in all_pairs)
    minimum_gap = min((_gap(a['box_pt'], b['box_pt']) /
                       median([a['line_height_pt'], b['line_height_pt']])
                       for a, b in all_pairs), default=None)
    protected = (aligned >= c['minimum_regular_grid_node_fraction'] and not overlap
                 and minimum_gap is not None
                 and minimum_gap >= c['regular_grid_minimum_gap_line_heights'])
    return {'aligned_node_fraction': aligned, 'row_clusters': len(y_counts),
            'column_clusters': len(x_counts), 'minimum_pair_gap_line_heights': minimum_gap,
            'protected_regular_grid': protected,
            'alignment_axes': {'horizontal': x_mode, 'vertical': y_mode},
            'alignment_source': 'one_coherent_actual_envelope_edge_or_center_grid_with_line_height_tolerance'}


def _line_height(obj, paragraphs, page_height, c):
    actual = []
    for p in paragraphs:
        for line in p.get('line_boxes') or []:
            b = _box(line.get('bbox', line) if isinstance(line, dict) else None)
            if b:
                actual.append(b['h'] * page_height)
    if actual:
        return median(actual), 'COM_actual_line_envelope'
    fonts = [_finite(p.get('font_pt')) for p in paragraphs]
    fonts = [v for v in fonts if v is not None and v > 0]
    if not fonts:
        fonts = [_finite(v) for v in obj.get('font_sizes_pt', [])]
        fonts = [v for v in fonts if v is not None and v > 0]
    if fonts:
        return median(fonts) * c['paragraph_font_to_line_height_factor'], 'effective_font_times_1.2_typographic_proxy'
    return None, 'unavailable'


def _get_config(config):
    if config is None:
        path = Path(__file__).resolve().parents[1] / 'config' / 'graph_detector.json'
        return json.loads(path.read_text(encoding='utf-8'))
    if isinstance(config, (str, Path)):
        return json.loads(Path(config).read_text(encoding='utf-8'))
    return dict(config)


def _result(c, evidence, unresolved, features, *, unsupported=False):
    status = ('candidate' if evidence else 'unsupported' if unsupported else
              'unknown' if unresolved else 'no_candidate_in_available_scope')
    return {'detector': 'D3', 'label': 'cluttered_layout', 'version': c['version'],
            'score': max((e['ordinal_strength'] for e in evidence), default=None if unresolved or unsupported else 0.),
            'score_kind': 'ordinal_heuristic_not_probability', 'status': status,
            'coverage': 'unsupported' if unsupported else 'partial',
            'features': features, 'evidence': evidence, 'unresolved': unresolved,
            'limitations': [
                'Actual paragraph/line rectangles are envelope evidence, not visible glyph segmentation or human readability truth.',
                'Font-times-1.2 line heights are explicitly marked proxies; no guessed default font size is used.',
                'Native table cells and explicit flow-node labels are not independent crowding nodes.',
                'Normal-grid protection is an engineering control and may miss an intentionally aligned but unreadably dense layout.',
                'Candidate thresholds were fixed from engineering hypotheses and mechanical fixtures; no reference-label fitting.']}


def detect(context, config=None):
    """Return a standard D3 result; immutable input context is sufficient."""
    c = _get_config(config)
    page = context.get('page', {})
    w, h = _finite(page.get('width_pt')), _finite(page.get('height_pt'))
    if w is None or h is None or w <= 0 or h <= 0:
        return _result(c, [], [{'kind': 'page_dimensions_unavailable'}], {}, unsupported=True)
    objects = context.get('objects', [])
    object_ids = [o.get('id') for o in objects]
    if len(set(object_ids)) != len(object_ids) or any(x is None for x in object_ids):
        raise ValueError('Unique nonempty object IDs are required')
    by_id = {o['id']: o for o in objects}
    nodes, unresolved, excluded = [], [], Counter()
    for obj in objects:
        if not obj.get('text_chars', 0):
            continue
        parent = by_id.get(obj.get('parent_id'), {})
        if obj.get('kind') == 'cell' or obj.get('cell') or parent.get('kind') == 'table':
            excluded['native_table_cell'] += 1
            continue
        if obj.get('content_class') in ('auxiliary', 'background', 'decoration') or obj.get('text_role') in ('page_number', 'navigation', 'footnote'):
            excluded['auxiliary_or_noninformation'] += 1
            continue
        if obj.get('decoration_role') in ('flow_node', 'legend', 'bullet') or obj.get('text_role') == 'flow_label':
            excluded['explicit_flow_or_legend_label'] += 1
            continue
        rotation = _finite(obj.get('rotation', 0))
        if rotation is None or min(rotation % 360, 360 - rotation % 360) > 1e-6:
            unresolved.append({'object_id': obj['id'], 'kind': 'rotated_or_unresolved_layout'})
            continue
        paragraphs = [p for p in obj.get('paragraphs', []) if p.get('text_chars', 0) > 0 and _box(p.get('bbox'))]
        if not paragraphs:
            unresolved.append({'object_id': obj['id'], 'kind': 'actual_nonempty_layout_unavailable'})
            continue
        line_height, height_source = _line_height(obj, paragraphs, h, c)
        if line_height is None or line_height <= 0:
            unresolved.append({'object_id': obj['id'], 'kind': 'line_height_and_effective_font_unavailable'})
            continue
        paragraph_boxes = [_intersection(_box(p['bbox']), {'x': 0., 'y': 0., 'w': 1., 'h': 1.}) for p in paragraphs]
        paragraph_boxes = [b for b in paragraph_boxes if _area(b) > 0]
        if not paragraph_boxes:
            excluded['text_entirely_outside_page_D4_scope'] += 1
            continue
        bbox = _bounds(paragraph_boxes)
        module_id = obj.get('module_id')
        if module_id is None and parent.get('kind') == 'group':
            module_id = parent['id']
        nodes.append({'id': obj['id'], 'bbox': bbox, 'box_pt': _physical(bbox, w, h),
                      'paragraph_boxes_pt': [_physical(b, w, h) for b in paragraph_boxes],
                      'chars': obj['text_chars'], 'line_height_pt': line_height,
                      'line_height_source': height_source, 'native_parent_id': obj.get('parent_id'),
                      'module_id': module_id, 'text_role': obj.get('text_role', 'unknown')})
    if not nodes:
        return _result(c, [], unresolved, {'nodes': [], 'edges': [], 'regions': [],
                       'excluded_counts': dict(excluded)}, unsupported=not unresolved)
    graph = {i: set() for i in range(len(nodes))}
    pairs, edges = {}, []
    for i, j in combinations(range(len(nodes)), 2):
        a, b = nodes[i], nodes[j]
        line_height = median([a['line_height_pt'], b['line_height_pt']])
        gap = _gap(a['box_pt'], b['box_pt']) / line_height
        overlap = _area(_intersection(a['box_pt'], b['box_pt']))
        cover = overlap / min(_area(a['box_pt']), _area(b['box_pt']))
        pair = {'a': a['id'], 'b': b['id'], 'gap_line_heights': gap,
                'intersection_smaller_envelope_fraction': cover,
                'same_native_parent': a['native_parent_id'] is not None and a['native_parent_id'] == b['native_parent_id'],
                'different_declared_module': a['module_id'] is not None and b['module_id'] is not None and a['module_id'] != b['module_id']}
        pairs[i, j] = pair
        if gap <= c['edge_gap_line_heights'] or cover >= c['edge_overlap_smaller_fraction']:
            graph[i].add(j)
            graph[j].add(i)
            edges.append(pair)
    components, seen = [], set()
    for start in graph:
        if start in seen:
            continue
        component, stack = set(), [start]
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            stack.extend(graph[node] - component)
        seen.update(component)
        components.append(frozenset(component))
    regions = set(components)
    for center in graph:
        selected = {center}
        frontier = {center}
        for _ in range(c['neighborhood_hops']):
            frontier = set().union(*(graph[i] for i in frontier)) - selected if frontier else set()
            selected.update(frontier)
        regions.add(frozenset(selected))
    metrics, evidence = [], []
    for selected in sorted(regions, key=lambda v: (len(v), tuple(sorted(v)))):
        if len(selected) < c['minimum_nodes']:
            continue
        selected = sorted(selected)
        local_nodes = [nodes[i] for i in selected]
        boxes = [b for n in local_nodes for b in n['paragraph_boxes_pt']]
        bound = _bounds(boxes)
        occupancy = _union_area(boxes) / _area(bound)
        local_pairs = [pairs[min(i, j), max(i, j)] for i, j in combinations(selected, 2)]
        tight_ids, overlap_ids, interfered_ids = set(), set(), set()
        for pair in local_pairs:
            if pair['gap_line_heights'] <= c['tight_gap_line_heights']:
                tight_ids.update((pair['a'], pair['b']))
                if pair['different_declared_module']:
                    interfered_ids.update((pair['a'], pair['b']))
            if pair['intersection_smaller_envelope_fraction'] >= c['edge_overlap_smaller_fraction']:
                overlap_ids.update((pair['a'], pair['b']))
        n = len(selected)
        grid = _regular_grid(local_nodes, c)
        tight_fraction = len(tight_ids) / n
        overlap_fraction = len(overlap_ids) / n
        interference_fraction = len(interfered_ids) / n
        module_count = len({node['module_id'] for node in local_nodes if node['module_id'] is not None})
        median_chars = median(node['chars'] for node in local_nodes)
        dense_tight = (median_chars <= c['maximum_median_characters']
                       and occupancy >= c['minimum_local_union_occupancy']
                       and tight_fraction >= c['minimum_tight_neighbor_fraction'])
        overlap_reason = overlap_fraction >= c['minimum_overlap_incident_fraction']
        irregular_reason = grid['aligned_node_fraction'] < c['minimum_regular_grid_node_fraction']
        module_reason = (module_count >= c['minimum_declared_module_count_for_interference']
                         and interference_fraction >= c['minimum_interfered_node_fraction'])
        triggered = dense_tight and not grid['protected_regular_grid'] and (overlap_reason or irregular_reason or module_reason)
        metric = {'object_ids': [node['id'] for node in local_nodes], 'bbox': _norm(bound, w, h),
                  'node_count': n, 'local_union_occupancy': occupancy,
                  'median_characters': median_chars, 'tight_neighbor_fraction': tight_fraction,
                  'overlap_incident_fraction': overlap_fraction,
                  'cross_module_interfered_node_fraction': interference_fraction,
                  'declared_module_count': module_count, 'grid': grid,
                  'reasons': {'envelope_overlap': overlap_reason, 'irregular_organization': irregular_reason,
                              'cross_module_interference': module_reason}, 'triggered': triggered,
                  'evidence_scope': 'same_induced_node_set_and_actual_content_bounds'}
        metrics.append(metric)
        if triggered:
            evidence.append({'object_ids': metric['object_ids'], 'bbox': metric['bbox'],
                             'kind': 'actual_text_relation_cluster_congestion',
                             'measurement_source': 'COM_actual_nonempty_paragraph_envelopes+line_height_normalized_native_relations',
                             'ordinal_strength': .8 if overlap_reason else .7, 'metrics': metric})
    # Keep maximal witnesses while preserving all tested region metrics above.
    keep = []
    for e in sorted(evidence, key=lambda v: (-len(v['object_ids']), tuple(v['object_ids']))):
        if not any(set(e['object_ids']) <= set(k['object_ids']) for k in keep):
            keep.append(e)
    features = {'nodes': nodes, 'edges': edges, 'components': [[nodes[i]['id'] for i in sorted(v)] for v in components],
                'regions': metrics, 'excluded_counts': dict(excluded),
                'eligible_node_count': len(nodes), 'unresolved_object_count': len(unresolved),
                'native_group_semantics': 'Only explicit native groups/module IDs, no rhetorical inference',
                'unit': 'physical_points_and_local_line_heights',
                'layout_union': 'exact_axis_aligned_nonempty_paragraph_envelope_union'}
    return _result(c, keep, unresolved, features)
