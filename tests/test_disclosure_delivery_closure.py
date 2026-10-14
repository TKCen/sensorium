"""Synthetic current-owner delivery and bounded disclosure regressions."""
import json
from agent_sensorium import tools
from agent_sensorium.config import DisclosureVisibility
from agent_sensorium.dispatcher import candidate_to_thread
from agent_sensorium.outbox import prepare_outbox_request, dispatch_outbox_request, FakeDiscordAdapter
from agent_sensorium.volunteer_cards import build_volunteer_cards
from agent_sensorium.pointers import select_attention_pointer
from tests.test_core_material_replay import store_at, signal, ingest
from tests.test_canonical_ingress_replay import create_role_advisory

POLICY = {'max_sensitivity': 'public_safe', 'allowed_surfaces': ['local','discord']}
DIRECT = {'enabled': True, 'direct_modes_enabled': True, 'allowed_delivery_modes': ['discord_channel_thread'], 'discord': {'enabled': True}}

def seed(tmp_path):
    store = store_at(tmp_path)
    (store.root / 'instance.config.json').write_text(json.dumps(POLICY))
    row = signal('fictional-source')
    row.update(sensitivity='public_safe', allowed_surfaces=['local', 'discord'])
    ingest(store, row)
    return store, row, store.read_jsonl('candidates')[0]

def test_outbox_stale_policy_and_inverse(tmp_path):
    store, row, root = seed(tmp_path)
    thread = candidate_to_thread(root)
    store.append_jsonl('threads', thread)
    args = dict(thread_id=thread['id'], request_type='THINK', surface='discord', delivery_mode='discord_channel_thread', target={'channel_id':'fictional-channel'}, message_preview='Fictional retained summary', config=DIRECT)
    prepared = prepare_outbox_request(store, **args)
    assert prepared['success'], prepared
    assert prepare_outbox_request(store, **args)['idempotent_hit']
    oid = prepared['data']['id']
    adapter = FakeDiscordAdapter()
    assert not dispatch_outbox_request(store, outbox_id=oid, adapter=adapter, config=DIRECT)['success']
    assert not dispatch_outbox_request(store, outbox_id=oid, adapter=adapter, execute=True)['success']
    assert adapter.calls == []
    strict = dict(row, allowed_surfaces=[], sensitivity='local_only')
    assert ingest(store, strict)['duplicate']
    candidates, threads = store.read_jsonl('candidates'), store.read_jsonl('threads')
    assert all(r['allowed_surfaces'] == [] for r in candidates + threads)
    assert not DisclosureVisibility(candidates, threads, 'discord', POLICY).subject('thread', thread['id'])
    queued = store.read_jsonl('outbox')[0]
    assert queued['allowed_surfaces'] == ['discord','local']
    actual = dispatch_outbox_request(store, outbox_id=oid, adapter=adapter, config=DIRECT, execute=True)
    assert not actual['success']
    assert adapter.calls == []
    assert store.read_jsonl('outbox')[0]['status'] == 'prepared'
    assert not dispatch_outbox_request(store, outbox_id=oid, adapter=adapter, config=DIRECT, execute=True)['success']
    assert adapter.calls == []
    # Current source and legacy standalone thread remain accepted inverses.
    other, _, current = seed(tmp_path / 'current')
    for thread in (candidate_to_thread(current), dict(candidate_to_thread(current), id='fictional-standalone-thread', origin_candidate_id='')):
        other.append_jsonl('threads', thread)
        p = prepare_outbox_request(other, **dict(args, thread_id=thread['id']))
        a = FakeDiscordAdapter()
        assert p['success'] and dispatch_outbox_request(other, outbox_id=p['data']['id'], adapter=a, config=DIRECT, execute=True)['success']
        assert len(a.calls) == 1
    print('REGRESSION 4273370996 strict replay hid source/thread; prepared policy remained broad; fake sent once; current/standalone inverses and guards held')

def test_deep_json_loader_then_consumer(tmp_path):
    store = store_at(tmp_path)
    (store.root / 'instance.config.json').write_text(json.dumps(POLICY))
    base = {'id':'fictional-deep', 'status':'candidate', 'kind':'creative_pull', 'summary':'Fictional shallow summary', 'pressure':.9, 'sensitivity':'public_safe','allowed_surfaces':['local']}
    # Construct bytes without invoking recursive encoder or changing recursion limits.
    encoded = json.dumps(base)[:-1] + ',"metadata":' + '{"nested":' * 965 + '{}' + '}' * 965 + '}\n'
    store.paths['candidates'].write_text(encoded)
    loaded = store.read_jsonl('candidates')
    assert len(loaded) == 1 and loaded[0]['id'] == base['id']
    neighbor = dict(base, id='fictional-neighbor', pressure=.8)
    store.append_jsonl('candidates', neighbor)
    loaded = store.read_jsonl('candidates')
    disclosure = DisclosureVisibility(loaded, [], 'local', POLICY)
    assert not disclosure.subject('candidate', base['id'])
    assert disclosure.subject('candidate', neighbor['id'])
    assert not any(surfaces(store, base['id']).values())
    assert all(surfaces(store, neighbor['id']).values())
    base['metadata'] = {'nested':{}}
    store.rewrite_jsonl('candidates', [base])
    assert DisclosureVisibility(store.read_jsonl('candidates'), [], 'local', POLICY).subject('candidate', base['id'])
    assert json.loads(tools.handle_sensorium_status(instance=store.instance, state_dir=str(store.root), surface='local', config_path=str(store.root / 'instance.config.json')))['success']
    print('REGRESSION 4273371006 JSONL accepted depth965 bytes; visibility/status raised RecursionError; shallow inverse held')

def surfaces(store, identifier):
    status = json.loads(tools.handle_sensorium_status(instance=store.instance, state_dir=str(store.root), surface='local', config_path=str(store.root / 'instance.config.json')))
    opened = json.loads(tools.handle_sensorium_candidate_open(candidate_id=identifier, instance=store.instance, state_dir=str(store.root), surface='local', config_path=str(store.root / 'instance.config.json')))
    cards = build_volunteer_cards(store, surface='local', instance_config=POLICY, limit=20)
    pointer = select_attention_pointer(store, surface='local', instance_config=POLICY)
    return {'status': any(c['id']==identifier for c in status['data']['top_candidates']), 'open': opened['success'], 'cards': any(c['subject_ref']['id']==identifier for c in cards), 'pointer':pointer.get('candidate_id')==identifier}

def test_unbound_real_advisory_and_inverses(tmp_path):
    store, _, root = seed(tmp_path)
    derived = create_role_advisory(store, root)
    assert derived['kind'] == 'subconscious_advisory'
    derived['pressure'] = .99
    rows = store.read_jsonl('candidates')
    rows[-1] = derived
    store.rewrite_jsonl('candidates', rows)
    assert all(surfaces(store, derived['id']).values())
    # Retain source-derived prose, remove source linkage only, hide actual source.
    rows[0].update(allowed_surfaces=[], sensitivity='local_only')
    store.rewrite_jsonl('candidates', rows)
    assert not any(surfaces(store, derived['id']).values())
    for key in ('source_candidate_ids', 'admission_binding'):
        derived.pop(key, None)
    derived['advisory_meta'].pop('admission_binding', None)
    rows[-1] = derived
    store.rewrite_jsonl('candidates', rows)
    actual = surfaces(store, derived['id'])
    assert not any(actual.values()), actual
    # Normal standalone candidate remains legitimate, unlike source-derived advisory.
    standalone = dict(derived, id='fictional-standalone-candidate', kind='creative_pull')
    store.rewrite_jsonl('candidates', [standalone])
    assert all(surfaces(store, standalone['id']).values())
    print('REGRESSION 4273371011 real bound advisory visible; hidden bound source suppressed all4; removed bindings exposed all4; ordinary standalone inverse held')


def test_current_owner_validation_serializes_replay_through_fake_dispatch(tmp_path):
    """A real replay cannot commit between owner admission and fake effect."""
    from threading import Event, Thread
    from agent_sensorium.store import _candidate_process_lock

    store, row, root = seed(tmp_path)
    thread = candidate_to_thread(root)
    store.append_jsonl("threads", thread)
    args = dict(thread_id=thread["id"], request_type="THINK", surface="discord",
                delivery_mode="discord_channel_thread", target={"channel_id": "fictional-channel"},
                message_preview="Fictional concurrent delivery", config=DIRECT)
    first = prepare_outbox_request(store, **args)
    second = prepare_outbox_request(store, **dict(args, message_preview="Fictional later delivery"))
    assert first["success"] and second["success"]
    probe_ready, replay_done = Event(), Event()
    observed, errors = {}, []
    lock = _candidate_process_lock(str((store.root / "locks" / "candidates.lock").resolve()))

    def replay():
        try:
            acquired = lock.acquire(blocking=False)
            observed["blocked"] = not acquired
            if acquired:
                lock.release()
            probe_ready.set()
            observed["replay"] = ingest(store, dict(row, allowed_surfaces=[], sensitivity="local_only"))
        except BaseException as exc:
            errors.append(exc)
        finally:
            replay_done.set()

    class CoordinatedFake(FakeDiscordAdapter):
        def create_thread(self, **kwargs):
            worker.start()
            assert probe_ready.wait(5), "Replay lock probe did not run"
            if not observed["blocked"]:
                assert replay_done.wait(5), "Unserialized real replay did not finish"
            observed["policy_at_effect"] = store.read_jsonl("candidates")[0]["allowed_surfaces"]
            return super().create_thread(**kwargs)

    worker = Thread(target=replay, daemon=True)
    adapter = CoordinatedFake()
    try:
        result = dispatch_outbox_request(store, outbox_id=first["data"]["id"],
                                         adapter=adapter, config=DIRECT, execute=True)
    finally:
        if worker.ident is not None:
            worker.join(5)
    assert not worker.is_alive()
    assert not errors, errors
    assert result["success"], result
    assert observed["replay"]["duplicate"]
    assert observed["blocked"], "Real candidate replay acquired ownership during admitted dispatch"
    assert observed["policy_at_effect"] == row["allowed_surfaces"]
    assert len(adapter.calls) == 1
    history = next(r for r in store.read_jsonl("outbox") if r["id"] == first["data"]["id"])
    assert history["status"] == "dispatched"
    before = {name: store.paths[name].read_bytes() for name in ("outbox", "threads", "decisions")}
    denied = dispatch_outbox_request(store, outbox_id=second["data"]["id"],
                                     adapter=adapter, config=DIRECT, execute=True)
    assert denied["error"] == "current_owner_not_allowed"
    assert not dispatch_outbox_request(store, outbox_id=first["data"]["id"],
                                       adapter=adapter, config=DIRECT, execute=True)["success"]
    assert len(adapter.calls) == 1
    assert {name: store.paths[name].read_bytes() for name in before} == before
    assert next(r for r in store.read_jsonl("outbox") if r["id"] == history["id"]) == history
