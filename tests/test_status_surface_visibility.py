"""Synthetic ordinary status visibility and read-only projection controls."""
import json
import pytest
from agent_sensorium import tools, config
from agent_sensorium.dispatcher import candidate_to_thread
from tests.test_core_material_replay import signal, store_at, ingest

@pytest.mark.parametrize('path', ['signal_id','signal_fingerprint','signal_material','event_id','event_fingerprint','event_material'])
@pytest.mark.parametrize('restriction', ['sensitivity','surface'])
def test_ordinary_forbidden_surface_status_has_no_restricted_prose(tmp_path, monkeypatch, path, restriction):
    store=store_at(tmp_path)
    first=signal()
    first.update(summary='Fictional withheld material marker',sensitivity='public_safe',allowed_surfaces=['local','telegram'])
    ingest(store,first)
    candidate=store.read_jsonl('candidates')[0]
    derived={**candidate,'id':'cand-probe-descendant','event_ids':[], 'source_candidate_ids':[candidate['id']], 'kind':'subconscious_advisory'}
    store.append_jsonl('candidates',derived)
    for row in [candidate,derived]:
        store.append_jsonl('threads',candidate_to_thread(row))
    policy={'allowed_surfaces':['local','telegram'],'max_sensitivity':'public_safe'}
    monkeypatch.setattr(config,'load_instance_config',lambda **kwargs:(policy,{}))
    kw={'instance':store.instance,'state_dir':str(store.root),'surface':'telegram'}
    assert json.loads(tools.handle_sensorium_candidate_open(candidate_id=candidate['id'],**kw))['success']
    for row in store.read_jsonl('threads'):
        assert json.loads(tools.handle_sensorium_thread_open(thread_id=row['id'],**kw))['success']
    before=json.loads(tools.handle_sensorium_status(reference_id=candidate['id'],**kw))
    assert not before['data']['exact_subject'].get('not_found')
    incoming=dict(first if path.startswith('signal') else store.read_jsonl('events')[0])
    if not path.endswith('_id'):
        incoming['id']='probe-replay-wrapper'
    if path.endswith('_material'):
        incoming['summary']='Fictional alternate wrapper'
    if restriction=='sensitivity':
        incoming['sensitivity']='local_only'
    else:
        incoming['allowed_surfaces']=['local']
    handler=tools.handle_sensorium_ingest_signal if path.startswith('signal') else tools.handle_sensorium_ingest_event
    keyword='signal' if path.startswith('signal') else 'event'
    result=json.loads(handler(**{keyword:incoming},instance=store.instance,state_dir=str(store.root),config={}))
    assert result['success'] and result['data']['duplicate']
    opens=[]
    for row in store.read_jsonl('candidates'):
        opened=json.loads(tools.handle_sensorium_candidate_open(candidate_id=row['id'],**kw))
        assert not opened['success'] and opened['data'] is None
        opens.append({'kind':'candidate','denied':True})
    for row in store.read_jsonl('threads'):
        opened=json.loads(tools.handle_sensorium_thread_open(thread_id=row['id'],**kw))
        assert not opened['success'] and opened['data'] is None
        opens.append({'kind':'thread','denied':True})
    before_state = _snapshot(store)
    output=json.loads(tools.handle_sensorium_status(reference_id=candidate['id'],**kw))
    assert _snapshot(store) == before_state
    assert output['success'] and output['data']['exact_subject']['not_found']
    candidates=output['data']['top_candidates']
    threads=output['data']['top_threads']
    assert not candidates and not threads, 'Restricted rows must not enter status top lists'
    assert 'Fictional withheld material marker' not in json.dumps(output)
    for reference in ['', *[r['id'] for r in store.read_jsonl('threads')]]:
        status = json.loads(tools.handle_sensorium_status(reference_id=reference, **kw))
        assert 'Fictional withheld material marker' not in json.dumps(status)
        if reference:
            assert status['data']['exact_subject']['not_found']
    policy['max_sensitivity'] = 'local_only' if restriction == 'sensitivity' else 'public_safe'
    local_kw = {**kw, 'surface': 'local'}
    before_state = _snapshot(store)
    for reference in ['', candidate['id'], *[r['id'] for r in store.read_jsonl('threads')]]:
        local = json.loads(tools.handle_sensorium_status(reference_id=reference, **local_kw))
        assert len(local['data']['top_candidates']) == 2
        assert len(local['data']['top_threads']) == 2
        assert 'Fictional withheld material marker' in json.dumps(local)
        if reference:
            assert not local['data']['exact_subject'].get('not_found')
    assert _snapshot(store) == before_state


def _snapshot(store):
    return {str(path.relative_to(store.root)): path.read_bytes()
            for path in store.root.rglob('*') if path.is_file()}


@pytest.mark.parametrize('restriction', ['sensitivity', 'surface'])
@pytest.mark.parametrize('sibling', ['latest_decision', 'legacy_dispatch_result'])
def test_linked_status_prose_uses_current_subject_visibility(tmp_path, monkeypatch, restriction, sibling):
    store = store_at(tmp_path)
    first = signal()
    marker = 'Fictional withheld sibling marker'
    first.update(summary=marker, sensitivity='public_safe', allowed_surfaces=['local', 'telegram'])
    ingest(store, first)
    candidate = store.read_jsonl('candidates')[0]
    policy = {'allowed_surfaces': ['local', 'telegram'], 'max_sensitivity': 'public_safe'}
    monkeypatch.setattr(config, 'load_instance_config', lambda **kwargs: (policy, {}))
    if sibling == 'latest_decision':
        thread = candidate_to_thread(candidate)
        store.append_jsonl('threads', thread)
        updated = json.loads(tools.handle_sensorium_thread_update(
            thread_id=thread['id'], action='hold', reason=marker,
            instance=store.instance, state_dir=str(store.root)))
        assert updated['success']
    else:
        dispatched = json.loads(tools.handle_sensorium_dispatch_once(
            instance=store.instance, state_dir=str(store.root), dry_run=True))
        assert dispatched['success']
        assert marker in json.dumps(dispatched)
    kw = {'instance': store.instance, 'state_dir': str(store.root), 'surface': 'telegram'}
    assert marker in json.dumps(json.loads(tools.handle_sensorium_status(**kw))['data'][sibling])
    incoming = dict(first)
    if restriction == 'sensitivity':
        incoming['sensitivity'] = 'local_only'
    else:
        incoming['allowed_surfaces'] = ['local']
    assert ingest(store, incoming)['duplicate']
    before = _snapshot(store)
    restricted = json.loads(tools.handle_sensorium_status(**kw))
    assert marker not in json.dumps(restricted)
    assert _snapshot(store) == before
    policy['max_sensitivity'] = 'local_only' if restriction == 'sensitivity' else 'public_safe'
    local = json.loads(tools.handle_sensorium_status(**{**kw, 'surface': 'local'}))
    assert marker in json.dumps(local['data'][sibling])
    assert _snapshot(store) == before


@pytest.mark.parametrize('surface', ['', 'local', 'telegram'])
def test_top_n_filters_before_cap_preserves_ranking_and_global_counts(tmp_path, monkeypatch, surface):
    store = store_at(tmp_path)
    policy = {'allowed_surfaces': ['local', 'telegram'], 'max_sensitivity': 'public_safe'}
    monkeypatch.setattr(config, 'load_instance_config', lambda **kwargs: (policy, {}))
    for index in range(12):
        hidden = index < 6
        candidate = {'id': f'cand-synthetic-{index}', 'status': 'candidate',
                     'summary': 'Fictional hidden high rank' if hidden else f'Visible {index}',
                     'pressure': 1 - index / 100, 'kind': 'creative_pull',
                     'allowed_surfaces': ['discord'] if hidden else ['local', 'telegram'],
                     'sensitivity': 'public_safe'}
        store.append_jsonl('candidates', candidate)
        thread = candidate_to_thread(candidate)
        thread.update(id=f'thread-synthetic-{index}', created_at=f'2026-01-{20-index:02d}T00:00:00Z')
        store.append_jsonl('threads', thread)
    before = _snapshot(store)
    data = json.loads(tools.handle_sensorium_status(
        instance=store.instance, state_dir=str(store.root), surface=surface))['data']
    assert [row['id'] for row in data['top_candidates']] == [f'cand-synthetic-{i}' for i in range(6, 11)]
    assert [row['id'] for row in data['top_threads']] == [f'thread-synthetic-{i}' for i in range(6, 11)]
    assert data['counts']['active_candidates'] == 12
    assert data['counts']['dormant_threads'] == 12
    assert 'Fictional hidden high rank' not in json.dumps(data)
    assert _snapshot(store) == before


@pytest.mark.parametrize('path', ['signal_id', 'signal_fingerprint', 'signal_material',
                                  'event_id', 'event_fingerprint', 'event_material'])
@pytest.mark.parametrize('wider', [False, True])
def test_equal_or_wider_replay_and_status_preserve_canonical_state(tmp_path, monkeypatch, path, wider):
    store = store_at(tmp_path)
    first = signal()
    ingest(store, first)
    candidate = store.read_jsonl('candidates')[0]
    thread = candidate_to_thread(candidate)
    store.append_jsonl('threads', thread)
    store.append_jsonl('decisions', {'type': 'thread.updated', 'thread_id': thread['id'],
                                     'reason': 'Fictional preserved reason'})
    store.append_jsonl('artifacts', {'id': 'artifact-synthetic', 'thread_id': thread['id']})
    store.write_state({'budgets': {}, 'delivered_history': ['Fictional delivered history']})
    policy = {'allowed_surfaces': ['local', 'telegram'], 'max_sensitivity': 'private'}
    monkeypatch.setattr(config, 'load_instance_config', lambda **kwargs: (policy, {}))
    incoming = dict(first if path.startswith('signal') else store.read_jsonl('events')[0])
    if not path.endswith('_id'):
        incoming['id'] = 'synthetic-replay-wrapper'
    if path.endswith('_material'):
        incoming['summary'] = 'Fictional alternate wrapper'
    if wider:
        incoming.update(sensitivity='public_safe', allowed_surfaces=['local', 'telegram'])
    before = _snapshot(store)
    handler = tools.handle_sensorium_ingest_signal if path.startswith('signal') else tools.handle_sensorium_ingest_event
    keyword = 'signal' if path.startswith('signal') else 'event'
    replay = json.loads(handler(**{keyword: incoming}, instance=store.instance,
                                state_dir=str(store.root), config={}))
    assert replay['success'] and replay['data']['duplicate']
    assert _snapshot(store) == before
    for reference in ['', candidate['id'], thread['id']]:
        local = json.loads(tools.handle_sensorium_status(
            instance=store.instance, state_dir=str(store.root), surface='local', reference_id=reference))
        assert len(local['data']['top_candidates']) == 1
        assert len(local['data']['top_threads']) == 1
        assert local['data']['latest_decision']['reason'] == 'Fictional preserved reason'
        if reference:
            assert not local['data']['exact_subject'].get('not_found')
    assert _snapshot(store) == before
