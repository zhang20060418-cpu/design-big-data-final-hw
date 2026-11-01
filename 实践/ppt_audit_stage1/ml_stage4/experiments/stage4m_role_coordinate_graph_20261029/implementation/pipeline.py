"""M composes isolated adapters with the unchanged L measurement/detector core."""
from collections import defaultdict
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys

EXP=Path(__file__).resolve().parents[1]
L=EXP.parent/'stage4l_six_detectors_20261029'


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec)
    sys.modules[name]=m;spec.loader.exec_module(m);return m


SHARED=module('M_reuse_L_shared',L/'detector_implementation/shared_features.py')
SPECIAL=module('M_reuse_L_special',L/'detector_implementation/specialized.py')
WHITE=module('M_reuse_L_white',L/'detector_implementation/whitespace.py')
GEN=module('M_reuse_L_gen',L/'detector_implementation/generative.py')
GATE=module('M_reuse_L_contract',L/'scripts/independent_verify.py')
ROLE=module('M_roles',EXP/'implementation/role_adapter.py')
GRAPH=module('M_graph',EXP/'implementation/graph_clutter.py')
COORD=module('M_coordinates',EXP/'implementation/coordinate_adapter.py')


def validate_inventory(contexts):
    GATE.validate_contexts(contexts,require_spatial=False)
    if len(contexts)!=130 or {c['document_id'] for c in contexts}!={f'C{i:02d}' for i in range(1,12)}:
        raise ValueError('Only the explicit constructed130 cohort is allowed')


def prepare_contexts(contexts,fresh,config):
    validate_inventory(contexts)
    cs,coordinate_stats=COORD.apply_fresh_coordinates(contexts,fresh)
    bydoc=defaultdict(list)
    for c in cs:
        c['spatial']={};c['relations']={};bydoc[c['document_id']].append(c)
    # Build initial info/decor/native-envelope definitions using the same L rules.
    for pages in bydoc.values():SHARED.classify_document(pages,config['shared'])
    roles=ROLE.infer_roles(cs,config['roles'])
    cs=ROLE.apply_role_updates(cs,roles)
    # Respect role adapter's local_annotation=information rather than blindly
    # deleting every footnote from the information mask.
    for c in cs:
        for o in c['objects']:
            if o['kind'] in {'text','cell'}:
                o['content_regions']=SHARED.text_boxes(o) if o['content_class']=='information' else []
        SHARED.build_spatial(c,config['shared'])
        c['availability']['coordinate_adapter']='M_fresh_per_object_proof_or_unknown'
    GATE.validate_contexts(cs)
    return cs,roles,coordinate_stats


def run_detectors(contexts,config):
    bydoc=defaultdict(list)
    for c in contexts:bydoc[c['document_id']].append(c)
    d6={};documents=[]
    gen=GEN.GenerativeDesignAnomalyDetector(config['generative'])
    for doc,pages in sorted(bydoc.items()):
        result=gen.detect_document(pages);d6.update(result['page_results'])
        documents.append({'document_id':doc,**result['document_result']})
    white=WHITE.WhitespaceDetector(config['whitespace'])
    results=[]
    for c in contexts:
        detectors=SPECIAL.detect_specialized(c,config['specialized'])
        detectors['D3']=GRAPH.detect(c,config['graph'])
        detectors.update(D5=white.detect(c),D6=d6[c['sample_id']])
        results.append({'sample_id':c['sample_id'],'document_id':c['document_id'],
            'page_index':c['page_index'],'page_role':c['page']['role'],'detectors':detectors})
    GATE.validate_results(contexts,{r['sample_id']:r['detectors'] for r in results})
    return results,documents
