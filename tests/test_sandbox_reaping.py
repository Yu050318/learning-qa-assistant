from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.errors import AppError
from app.infrastructure.persistence.models import ChatRun
from app.infrastructure.sandbox import DockerSandbox, MANAGED_LABEL
from test_sandbox_workflow import pipeline, sandbox, control, system


def item():
    return {'Id':'c'*64, 'Name':'/learningqa-sandbox-'+'d'*32,
        'Created':(datetime.now(timezone.utc)-timedelta(seconds=120)).isoformat(),
        'Config':{'Labels':{MANAGED_LABEL:'true','learningqa.sandbox.scope':'a'*64,
            'learningqa.sandbox.owner':'b'*32,'learningqa.sandbox.run':str(uuid4())}}}


def driver(value):
    client=object.__new__(DockerSandbox)
    client.namespace='a'*64
    client.readiness=lambda **kwargs: {'ready':True}
    calls=[]
    def command(args, **kwargs):
        calls.append(args)
        if args[0]=='ps':return 0, b'c'*64+b'\n', b''
        if args[0]=='container':return 0, json.dumps([value]).encode(), b''
        if args[0]=='rm':return 0, b'', b''
        raise AssertionError(args)
    client._command=command
    return client,calls


def test_only_old_scoped_container_ids_are_removed_inside_a_run_guard():
    value=item();client,calls=driver(value)
    held=[]
    @contextmanager
    def guard(run_id):
        assert str(run_id)==value['Config']['Labels']['learningqa.sandbox.run']
        held.append(True)
        yield True
        assert calls[-1]==['rm','--force',value['Id']]
    assert client.reap(guard)==1 and held
    assert 'label='+MANAGED_LABEL+'=true' in calls[0]
    assert 'label=learningqa.sandbox.scope='+'a'*64 in calls[0]


@pytest.mark.parametrize('change', ['fresh','foreign','unscoped','wrong-id','wrong-name','wrong-owner','bad-run','bad-date','unmanaged'])
def test_foreign_fresh_or_unverifiable_containers_are_never_removed(change):
    value=deepcopy(item());labels=value['Config']['Labels']
    if change=='fresh':value['Created']=datetime.now(timezone.utc).isoformat()
    if change=='foreign':labels['learningqa.sandbox.scope']='e'*64
    if change=='unscoped':del labels['learningqa.sandbox.scope']
    if change=='wrong-id':value['Id']='f'*64
    if change=='wrong-name':value['Name']='/other-project'
    if change=='wrong-owner':labels['learningqa.sandbox.owner']='foreign'
    if change=='bad-run':labels['learningqa.sandbox.run']='invalid'
    if change=='bad-date':value['Created']='2026-01-01'
    if change=='unmanaged':labels[MANAGED_LABEL]='false'
    client,calls=driver(value)
    def guard(run_id):raise AssertionError('should not acquire a Run lock')
    assert client.reap(guard)==0
    assert not any(args[0]=='rm' for args in calls)


def test_live_lease_guard_and_cleanup_error_are_respected():
    client,calls=driver(item())
    @contextmanager
    def guard(run_id):yield False
    assert client.reap(guard)==0 and not any(args[0]=='rm' for args in calls)
    original=client._command
    client._command=lambda args,**kwargs: (1,b'',b'') if args[0]=='rm' else original(args,**kwargs)
    @contextmanager
    def expired(run_id):yield True
    with pytest.raises(AppError) as error:client.reap(expired)
    assert error.value.code=='SANDBOX_CLEANUP_FAILED'


def test_run_guard_protects_running_and_cancelling_leases_and_allows_expired_or_deleted(pipeline):
    runtime=pipeline.runtime;store=pipeline.system.store
    claim=store.claim_next()
    for state in ('running','cancelling'):
        with store.database.sessions() as session,session.begin():
            row=session.scalar(select(ChatRun).where(ChatRun.id==claim.run_id));row.state=state
        with runtime.cleanup_guard(claim.run_id) as allowed:assert not allowed
    with store.database.sessions() as session,session.begin():
        row=session.get(ChatRun,claim.run_id);row.lease_expires_at=store.now(session)-timedelta(seconds=1)
    with runtime.cleanup_guard(claim.run_id) as allowed:assert allowed
    with runtime.cleanup_guard(uuid4()) as allowed:assert allowed


def test_namespace_ignores_credentials_but_distinguishes_databases(pipeline):
    from sqlalchemy.engine import make_url
    runtime=pipeline.runtime
    db=runtime.artifacts.store.database
    original=db.engine
    class Engine:
        url=make_url('postgresql://first:secret@db:5432/one?sslmode=require')
    db.__dict__['engine']=Engine()
    try:
        first=runtime.namespace
        del runtime.namespace
        Engine.url=make_url('postgresql://second:rotated@db:5432/one?sslmode=verify-full')
        assert runtime.namespace==first
        del runtime.namespace
        Engine.url=make_url('postgresql://second:rotated@db:5432/two')
        assert runtime.namespace!=first
    finally:db.__dict__['engine']=original
