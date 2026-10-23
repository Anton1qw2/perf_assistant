import json
import time

import pytest

from performance_assistant.contracts import Options, RunContext, TaskPatch
from performance_assistant.storage import Conflict
from performance_assistant import workflow
from conftest import drain


def test_context_confirmation_preserves_snapshot_and_archives_scope(system):
    _, store, _, service, worker, cid = system
    service.patch(cid,'task',TaskPatch(expected_version=0,summary='latency',profile_id='old'))
    rid=service.run(cid,'run')['id']
    job=store.claim('old-worker')
    q=service.propose_context(cid,'proposal',rid,RunContext(profile_id='new'))
    assert store.snapshot(cid)['runs'][0]['context']['profile_id']=='old'
    with pytest.raises(Conflict),store.owned(job):
        pass
    service.answer(cid,'confirm',q['id'],'да')
    run=store.snapshot(cid)['runs'][0]
    assert run['context']['profile_id']=='new'
    assert run['context']['revision']==1
    assert run['snapshot']['profile_id']=='old'
    assert run['checkpoint']['evidence_ids']==[]
    assert store.one('SELECT count(*) n FROM context_ledger')['n']==1


def test_context_decline_keeps_previous_checkpoint(system):
    _, store, _, service, _, cid=system
    service.patch(cid,'task',TaskPatch(expected_version=0,summary='x',profile_id='old'))
    rid=service.run(cid,'run')['id']
    before=store.snapshot(cid)['runs'][0]['checkpoint']
    q=service.propose_context(cid,'proposal',rid,RunContext(profile_id='new'))
    service.answer(cid,'answer',q['id'],'no')
    assert store.snapshot(cid)['runs'][0]['checkpoint']==before
    assert store.snapshot(cid)['runs'][0]['context']['profile_id']=='old'


def test_context_change_never_forwards_personal_token(system):
    config,store,_,service,_,cid=system
    config.mcp_url='https://profiler.example/mcp'
    service.save_settings(cid,'settings',0,{'repository':'original'},'TEST-SECRET-TOKEN')
    service.patch(cid,'task',TaskPatch(expected_version=0,summary='x',repository='original'))
    rid=service.run(cid,'run')['id']
    q=service.propose_context(cid,'proposal',rid,RunContext(repository='different'))
    service.answer(cid,'answer',q['id'],'yes')
    settings=json.loads(store.one('SELECT settings FROM runs WHERE id=?',(rid,))['settings'])
    assert settings['credential_ref'] is None


def test_external_question_resume_and_stable_operation_id(system,monkeypatch):
    config,store,_,service,worker,cid=system
    config.executor_url='https://executor.example'
    calls=[]
    def fake(url,payload,headers,deadline):
        calls.append(payload)
        if payload['operation_id'].endswith(':0'):
            return {'event':'question','prompt':'Metric or no selection?', 'checkpoint':{'waiting':True}},{}
        assert payload['answer']['answer']=='без выбора'
        return {'event':'result','checkpoint':{},'claimed_improvement':True},{}
    monkeypatch.setattr(workflow,'bounded_json',fake)
    service.patch(cid,'task',TaskPatch(expected_version=0,summary='x',profile_id='p',options=Options(executor='external')))
    service.run(cid,'run')
    drain(worker)
    waiting=store.snapshot(cid)['runs'][0]
    assert waiting['status']=='waiting'
    assert len(waiting['questions'])==1
    before=waiting['active_seconds']
    service.intake(cid,'answer','без выбора')
    drain(worker)
    run=store.snapshot(cid)['runs'][0]
    assert run['status']=='completed'
    assert run['report']['measured_improvement']=='unknown'
    assert run['active_seconds']-before<2
    assert calls[0]['operation_id'].endswith(':0') and calls[1]['operation_id'].endswith(':1')


def test_consultation_question_is_separate_and_explicit_switch_supersedes(system,monkeypatch):
    config,store,_,service,worker,cid=system
    config.model_url='https://model.example'
    config.model_name='test'
    monkeypatch.setattr(worker.model,'complete',lambda *a,**k: json.dumps({'text':'Need context','question':'Which runtime?'}))
    service.intake(cid,'ask','/ask how to reduce latency?')
    drain(worker)
    assert store.one("SELECT status FROM consultations")['status']=='pending'
    before=store.snapshot(cid)['task']
    service.intake(cid,'capabilities','What can you do?')
    drain(worker)
    assert store.one('SELECT status FROM consultations')['status']=='superseded'
    assert store.snapshot(cid)['task']==before


def test_registered_secret_cannot_be_put_in_task(system):
    config,store,vault,service,_,cid=system
    with store.tx() as db:
        vault.save(db,'REGISTERED-SECRET','https://example.test')
    with pytest.raises(ValueError):
        service.patch(cid,'task',TaskPatch(expected_version=0,summary='REGISTERED-SECRET'))


def test_event_cursor_order_and_snapshot(system):
    _,store,_,service,worker,cid=system
    service.intake(cid,'one','hello')
    drain(worker)
    events=store.rows('SELECT * FROM events WHERE conversation_id=? ORDER BY id',(cid,))
    assert [e['id'] for e in events]==sorted({e['id'] for e in events})
    assert store.snapshot(cid)['event_id']==events[-1]['id']


def test_expired_lease_commit_rolls_back_entire_result(system):
    _,store,_,service,_,cid=system
    service.intake(cid,'one','hello')
    job=store.claim('a')
    with pytest.raises(Conflict),store.owned(job) as db:
        store.message(db,cid,'assistant','must rollback','dialogue')
        db.execute("UPDATE jobs SET lease_until=?,status='done' WHERE id=?",(time.time()-1,job['id']))
    assert len(store.snapshot(cid)['messages'])==1
