import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.dependencies import current_user
from app.application.run_execution import RunWorker
from app.application.run_trace import read_trace
from app.core.errors import AppError
from app.infrastructure.persistence.models import ChatMessage, ChatRun, ChatRunEvent, DocumentRecord
from app.main import create_app
from test_sandbox_workflow import pipeline, sandbox, control, system


def test_actual_tool_trace_is_durable_timestamped_and_replay_does_not_repeat_execution(pipeline):
    p,s=pipeline,pipeline.system
    assert RunWorker(s.store,s.chat).run_once()
    trace=read_trace(s.store,s.uid,p.run.id)
    started=[item for item in trace['events'] if item['event']=='tool_started']
    finished=[item for item in trace['events'] if item['event']=='tool_finished']
    assert len(started)==len(finished)==1
    assert started[0]['name']=='sandbox_execute'
    assert started[0]['operation_key']==finished[0]['operation_key']
    assert all(item['recorded_at'] for item in trace['events'])
    assert trace['knowledge']['status']=='available' and not trace['knowledge']['sources']
    assert trace['remaining_seconds'] is None
    assert not trace['history_gap'] and not trace['has_more']
    raw=json.dumps(trace,default=str)
    for private in ('print(6 * 7)','password_hash','"tool_approval":','model_messages','stdout','source_sha256'):
        assert private not in raw
    again=read_trace(s.store,s.uid,p.run.id,trace['next_cursor'])
    assert not again['events'] and len(p.calls)==1


def test_legacy_time_and_missing_history_are_explicit(pipeline):
    p,s=pipeline,pipeline.system
    assert RunWorker(s.store,s.chat).run_once()
    with s.database.sessions() as session,session.begin():
        rows=list(session.scalars(select(ChatRunEvent).where(ChatRunEvent.run_id==p.run.id).order_by(ChatRunEvent.sequence)))
        rows[0].payload={key:value for key,value in rows[0].payload.items() if key!='recorded_at'}
        session.delete(rows[1])
    trace=read_trace(s.store,s.uid,p.run.id)
    assert trace['history_gap'] and trace['legacy_timestamps']


def test_tool_failure_is_not_shown_as_success_or_source(pipeline):
    p,s=pipeline,pipeline.system
    p.error=AppError('SANDBOX_CLEANUP_FAILED','private driver details',503)
    assert RunWorker(s.store,s.chat).run_once()
    trace=read_trace(s.store,s.uid,p.run.id)
    assert trace['state']=='failed'
    assert any(item['event']=='tool_started' for item in trace['events'])
    assert not any(item['event']=='tool_finished' for item in trace['events'])
    assert 'private driver details' not in json.dumps(trace,default=str)


def test_trace_http_auth_scope_cursor_and_cache_policy(pipeline):
    p,s=pipeline,pipeline.system
    s.close=lambda:None
    app=create_app(s.settings,s)
    path=f'/api/v2/runs/{p.run.id}/trace'
    with TestClient(app) as client:
        assert client.get(path).status_code==401
        app.dependency_overrides[current_user]=lambda:s.other
        denied=client.get(path)
        assert denied.status_code==404 and denied.headers['cache-control']=='no-store'
        app.dependency_overrides[current_user]=lambda:s.uid
        response=client.get(path)
        assert response.status_code==200 and response.headers['cache-control']=='no-store'
        assert response.json()['session_id']==str(s.sid)
        assert client.get(path,params={'after':-1}).status_code==422
        assert client.get(path,params={'after':99999}).status_code==422


def test_rag_context_uses_saved_evidence_distinguishes_parent_and_hides_revoked_documents(pipeline):
    p,s=pipeline,pipeline.system
    claim=s.store.claim_next(p.run.id)
    did=uuid4()
    with s.database.sessions() as session,session.begin():
        session.add(DocumentRecord(id=did,user_id=s.uid,original_name='guide.txt',file_type='txt',
            file_path='fixture-not-opened',content_hash='a'*64,status='ready',embedding_model=s.settings.embedding_model))
        run=session.get(ChatRun,p.run.id)
        snapshot=dict(run.execution_snapshot)
        snapshot['context']={**snapshot['context'],'allowed_document_ids':[str(did)]}
        run.execution_snapshot=snapshot
        if run.message_id:
            session.get(ChatMessage,run.message_id).citations=[{'number':1,'type':'knowledge'}]
    registry={'entries':[{'number':1,'type':'knowledge','document_id':str(did),'chunk_id':'leaf-1',
        'source_name':'guide.txt','excerpt':'Actual leaf evidence'},
        {'number':2,'type':'knowledge','document_id':str(did),'chunk_id':'leaf-2','excerpt':'Excluded evidence'},
        {'number':3,'type':'tool','excerpt':'PRIVATE HTTP RESULT'}],
        'excluded_numbers':[2], 'token_budget':4000,'used_tokens':100,'context_tokens':20,
        'contexts':{'parent':{'context_id':'parent','title':'guide.txt','for_numbers':[1,2],
            'content':'Context only; not an independent citation'}}}
    with s.database.sessions() as session,session.begin():
        run=s.store.owned(session,claim)
        s.store.save_checkpoint(session,run,{'workflow':{'body':{'registry':registry},'messages':['PRIVATE MODEL MESSAGE']}})
    assert read_trace(s.store,s.uid,p.run.id)['knowledge']['sources'][0]['status']=='retrieved'
    with s.database.sessions() as session,session.begin():
        session.get(ChatRun,p.run.id).state='succeeded'
    knowledge=read_trace(s.store,s.uid,p.run.id)['knowledge']
    assert knowledge['status']=='available'
    assert [item['status'] for item in knowledge['sources']]==['cited','excluded']
    assert knowledge['parents'][0]['for_numbers']==[1]
    assert 'PRIVATE' not in json.dumps(knowledge)
    with s.database.sessions() as session,session.begin():
        session.get(DocumentRecord,did).status='deleting'
    revoked=read_trace(s.store,s.uid,p.run.id)['knowledge']
    assert revoked=={'status':'revoked','sources':[],'parents':[]}
