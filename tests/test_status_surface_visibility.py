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


@pytest.mark.parametrize('action', ['DROP', 'SAVE', 'CREATE_CONSCIOUS_TASK'])
@pytest.mark.parametrize('dry_run', [True, False])
@pytest.mark.parametrize('restriction', ['surface', 'sensitivity'])
def test_bound_advisory_reason_obeys_current_source(tmp_path, monkeypatch, action, dry_run, restriction):
    store = store_at(tmp_path)
    marker = 'Fictional source-bound advisory withheld marker'
    first = signal()
    first.update(summary=marker, sensitivity='public_safe', allowed_surfaces=['local', 'telegram'])
    ingest(store, first)
    candidate = store.read_jsonl('candidates')[0]
    policy = {'allowed_surfaces': ['local', 'telegram'], 'max_sensitivity': 'public_safe'}
    monkeypatch.setattr(config, 'load_instance_config', lambda **kwargs: (policy, {}))
    output = {'action': action, 'rationale': marker, 'candidate_ids': [candidate['id']],
              'event_ids': candidate['event_ids']}
    if action == 'CREATE_CONSCIOUS_TASK':
        output['conscious_task'] = {'request_type': 'THINK', 'title': marker, 'why': marker,
                                    'expected_decision': 'Hold or engage'}
    produced = json.loads(tools.handle_sensorium_subconscious_advisory(
        instance=store.instance, state_dir=str(store.root), enabled=True, dry_run=dry_run,
        advisory_output=output, config={'model_enabled': False}, record_receipt=True))
    assert produced['success'], produced
    receipt = store.read_jsonl('decisions')[-1]
    assert receipt['type'] == 'subconscious.advisory'
    assert receipt['admission_binding']['source_candidate_id'] == candidate['id']
    assert (receipt['candidate_id'] is None) == (dry_run or action != 'CREATE_CONSCIOUS_TASK')
    kw = {'instance': store.instance, 'state_dir': str(store.root), 'surface': 'telegram'}
    assert marker in json.dumps(json.loads(tools.handle_sensorium_status(**kw)))
    incoming = dict(first)
    if restriction == 'surface':
        incoming['allowed_surfaces'] = ['local']
    else:
        incoming['sensitivity'] = 'local_only'
    assert ingest(store, incoming)['duplicate']
    before = _snapshot(store)
    forbidden = json.loads(tools.handle_sensorium_status(reference_id=candidate['id'], **kw))
    assert forbidden['data']['exact_subject']['not_found']
    assert forbidden['data']['top_candidates'] == []
    assert not json.loads(tools.handle_sensorium_candidate_open(candidate_id=candidate['id'], **kw))['success']
    policy['max_sensitivity'] = 'local_only' if restriction == 'sensitivity' else 'public_safe'
    local = json.loads(tools.handle_sensorium_status(**{**kw, 'surface': 'local'}))
    assert marker in json.dumps(local)
    assert _snapshot(store) == before
    assert marker not in json.dumps(forbidden)


@pytest.mark.parametrize('action', ['DROP', 'SAVE'])
@pytest.mark.parametrize('dry_run', [True, False])
@pytest.mark.parametrize('restriction', ['surface', 'sensitivity'])
def test_unbound_multi_source_reason_is_not_global(tmp_path, monkeypatch, action, dry_run, restriction):
    store = store_at(tmp_path)
    marker = 'Fictional multi-source rationale marker'
    sources = []
    for item in ['a', 'b']:
        row = signal(identifier=f'sig-{item}', item=item)
        row.update(summary=marker, sensitivity='public_safe', allowed_surfaces=['local', 'telegram'])
        ingest(store, row)
        sources.append(row)
    candidates = store.read_jsonl('candidates')
    assert len(candidates) == 2
    policy = {'allowed_surfaces': ['local', 'telegram'], 'max_sensitivity': 'public_safe'}
    monkeypatch.setattr(config, 'load_instance_config', lambda **kwargs: (policy, {}))
    response = json.loads(tools.handle_sensorium_subconscious_advisory(
        instance=store.instance, state_dir=str(store.root), enabled=True, dry_run=dry_run,
        advisory_output={'action': action, 'rationale': marker,
                         'candidate_ids': [c['id'] for c in candidates],
                         'event_ids': [e for c in candidates for e in c['event_ids']]},
        config={'model_enabled': False}, record_receipt=True))
    assert response['success'], response
    decision = store.read_jsonl('decisions')[-1]
    assert decision['type'] == 'subconscious.advisory'
    assert decision['admission_binding'] is None and decision['candidate_id'] is None
    for row in sources:
        incoming = dict(row)
        if restriction == 'surface':
            incoming['allowed_surfaces'] = ['local']
        else:
            incoming['sensitivity'] = 'local_only'
        assert ingest(store, incoming)['duplicate']
    before = _snapshot(store)
    status = json.loads(tools.handle_sensorium_status(
        instance=store.instance, state_dir=str(store.root), surface='telegram'))
    assert status['data']['top_candidates'] == [] and status['data']['top_threads'] == []
    assert _snapshot(store) == before
    assert marker not in json.dumps(status)
    local = json.loads(tools.handle_sensorium_status(
        instance=store.instance, state_dir=str(store.root), surface='local'))
    assert 'latest_decision' not in local['data']
    assert _snapshot(store) == before


@pytest.mark.parametrize('sibling', ['latest_decision', 'legacy_dispatch_result'])
@pytest.mark.parametrize('binding', [{}, [], '', False, {'source_candidate_id': ''},
                                    {'source_candidate_id': []}, {'source_candidate_id': 'missing'}])
def test_explicit_invalid_or_missing_binding_fails_closed(tmp_path, monkeypatch, sibling, binding):
    store = store_at(tmp_path)
    monkeypatch.setattr(config, 'load_instance_config', lambda **kwargs: ({}, {}))
    record = {'type': 'subconscious.advisory', 'action': 'save', 'output_action': 'SAVE',
              'reason': 'Fictional invalid subject marker', 'admission_binding': binding}
    if sibling == 'latest_decision':
        store.append_jsonl('decisions', record)
    else:
        store.write_state({'last_dispatch_result': record})
    before = _snapshot(store)
    status = json.loads(tools.handle_sensorium_status(instance=store.instance, state_dir=str(store.root)))
    assert sibling not in status['data']
    assert 'Fictional invalid subject marker' not in json.dumps(status)
    assert _snapshot(store) == before


@pytest.mark.parametrize('direct', ['candidate_id', 'origin_candidate_id', 'thread_id'])
@pytest.mark.parametrize('hidden', ['direct', 'nested', 'neither'])
def test_all_declared_status_subjects_gate_historical_prose(tmp_path, monkeypatch, direct, hidden):
    store = store_at(tmp_path)
    policy = {'allowed_surfaces': ['local', 'telegram'], 'max_sensitivity': 'public_safe'}
    monkeypatch.setattr(config, 'load_instance_config', lambda **kwargs: (policy, {}))
    for name in ['direct', 'nested']:
        row = {'id': f'cand-{name}', 'status': 'archived', 'sensitivity': 'public_safe',
               'allowed_surfaces': ['local'] if hidden == name else ['local', 'telegram']}
        store.append_jsonl('candidates', row)
        if name == 'direct':
            thread = candidate_to_thread(row)
            thread.update(id='thread-direct', status='closed')
            store.append_jsonl('threads', thread)
    record = {'type': 'subconscious.advisory', 'action': 'save', 'output_action': 'SAVE',
              direct: 'thread-direct' if direct == 'thread_id' else 'cand-direct',
              'admission_binding': {'source_candidate_id': 'cand-nested', 'item_key': 'historical'},
              'reason': 'Fictional historical subject marker'}
    store.append_jsonl('decisions', record)
    store.append_jsonl('artifacts', {'id': 'artifact-synthetic', 'content': 'Preserved artifact'})
    store.write_state({'last_dispatch_result': record, 'budgets': {'synthetic': 1},
                       'delivered_history': ['Preserved delivery']})
    before = _snapshot(store)
    status = json.loads(tools.handle_sensorium_status(
        instance=store.instance, state_dir=str(store.root), surface='telegram'))
    for sibling in ['latest_decision', 'legacy_dispatch_result']:
        assert (sibling in status['data']) == (hidden == 'neither')
    assert ('Fictional historical subject marker' in json.dumps(status)) == (hidden == 'neither')
    local = json.loads(tools.handle_sensorium_status(
        instance=store.instance, state_dir=str(store.root), surface='local'))
    assert 'Fictional historical subject marker' in json.dumps(local)
    assert _snapshot(store) == before


@pytest.mark.parametrize('output_action', ['DROP', 'SAVE', 'CREATE_CONSCIOUS_TASK'])
def test_unlinked_typed_advisory_choice_is_not_global(tmp_path, monkeypatch, output_action):
    store = store_at(tmp_path)
    monkeypatch.setattr(config, 'load_instance_config', lambda **kwargs: ({}, {}))
    store.append_jsonl('decisions', {'type': 'subconscious.advisory', 'output_action': output_action,
                                    'reason': 'Fictional lost source marker'})
    before = _snapshot(store)
    status = json.loads(tools.handle_sensorium_status(instance=store.instance, state_dir=str(store.root)))
    assert 'latest_decision' not in status['data']
    assert 'Fictional lost source marker' not in json.dumps(status)
    assert _snapshot(store) == before


def test_ordinary_unlinked_operational_diagnostics_remain_global(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    monkeypatch.setattr(config, 'load_instance_config', lambda **kwargs: ({}, {}))
    disabled = json.loads(tools.handle_sensorium_subconscious_advisory(
        instance=store.instance, state_dir=str(store.root), enabled=False,
        config={'model_enabled': False}, record_receipt=True))
    assert disabled['success'] and disabled['data']['action'] == 'disabled'
    receipt = store.read_jsonl('decisions')[-1]
    assert receipt['admission_binding'] is None and receipt['output_action'] is None
    dispatch = json.loads(tools.handle_sensorium_dispatch_once(
        instance=store.instance, state_dir=str(store.root), dry_run=True, config={}))
    assert dispatch['success'] and dispatch['data']['action'] == 'no_candidate'
    before = _snapshot(store)
    status = json.loads(tools.handle_sensorium_status(instance=store.instance, state_dir=str(store.root)))
    assert status['data']['latest_decision']['action'] == 'disabled'
    assert status['data']['legacy_dispatch_result']['action'] == 'no_candidate'
    assert _snapshot(store) == before


@pytest.mark.parametrize('sibling', ['latest_decision', 'legacy_dispatch_result'])
@pytest.mark.parametrize('mode', ['missing', 'historical', 'global'])
def test_missing_historical_and_global_status_controls(tmp_path, monkeypatch, sibling, mode):
    store = store_at(tmp_path)
    first = signal()
    first.update(sensitivity='public_safe', allowed_surfaces=['local', 'telegram'])
    ingest(store, first)
    candidate = store.read_jsonl('candidates')[0]
    policy = {'allowed_surfaces': ['local', 'telegram'], 'max_sensitivity': 'public_safe'}
    monkeypatch.setattr(config, 'load_instance_config', lambda **kwargs: (policy, {}))
    marker = 'Fictional historical operational receipt'
    if sibling == 'latest_decision':
        thread = candidate_to_thread(candidate)
        store.append_jsonl('threads', thread)
        result = json.loads(tools.handle_sensorium_thread_update(
            thread_id=thread['id'], action='close', reason=marker,
            instance=store.instance, state_dir=str(store.root)))
        assert result['success']
        if mode == 'missing':
            store.paths['threads'].write_text('')
        elif mode == 'global':
            store.append_jsonl('decisions', {'type': 'synthetic.operational', 'reason': marker})
    else:
        dispatched = json.loads(tools.handle_sensorium_dispatch_once(
            instance=store.instance, state_dir=str(store.root), dry_run=True, config={}))
        assert dispatched['success']
        if mode == 'missing':
            store.paths['candidates'].write_text('')
        elif mode == 'historical':
            reviewed = json.loads(tools.handle_sensorium_candidate_update(
                candidate_id=candidate['id'], action='mark_reviewed',
                instance=store.instance, state_dir=str(store.root)))
            assert reviewed['success']
        else:
            state = store.read_state()
            state['last_dispatch_result'] = {'action': 'no_candidate',
                                             'reason': 'No eligible candidate above threshold.'}
            store.write_state(state)
    before = _snapshot(store)
    status = json.loads(tools.handle_sensorium_status(
        instance=store.instance, state_dir=str(store.root), surface='telegram'))
    assert (sibling in status['data']) == (mode != 'missing')
    assert _snapshot(store) == before


@pytest.mark.parametrize('action', ['drop', 'save', 'would_create_conscious_task'])
def test_legacy_advisory_action_without_output_link_is_not_global(action):
    assert not tools._status_record_visible(
        {'type': 'subconscious.advisory', 'action': action, 'reason': 'Fictional source prose'},
        candidates=[], threads=[], surface='local', instance_config={})


@pytest.mark.parametrize('key', ['candidate_id', 'origin_candidate_id', 'thread_id'])
@pytest.mark.parametrize('value', ['', [], False])
def test_malformed_direct_subject_is_not_global(key, value):
    assert not tools._status_record_visible(
        {key: value, 'reason': 'Fictional subject prose'},
        candidates=[], threads=[], surface='local', instance_config={})
