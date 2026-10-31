"""Public trace projection; never return raw model messages or checkpoint inputs."""
from app.manager.transactions import transaction_scope, as_transaction
from app.dao.queries import run_trace as queries
import math
import re
from uuid import UUID


from app.core.errors import AppError
from app.entity.time import aware
from app.entity.do.models import ChatMessage, ChatRunCheckpoint, ChatRunOperation
from app.dao.postgres.repository import Repository

EVENT_FIELDS = {
    'run_started': (), 'run_claimed': ('generation',), 'run_resumed': ('checkpoint_revision',),
    'run_waiting': (), 'cancel_requested': (), 'run_cancelled': (), 'done': (),
    'error': ('code',), 'agent_started': ('agent','status'),
    'agent_finished': ('agent','status','elapsed_ms'), 'agent_failed': ('agent','status','error_code','elapsed_ms'),
    'tool_started': ('operation_key','name','generation','readonly'),
    'tool_finished': ('operation_key','status','elapsed_ms'),
    'tool_failed': ('operation_key','status','error_code','elapsed_ms'),
    'tool_approval_granted': (), 'artifacts_ready': (), 'context_usage': (), 'usage': (),
}


def public_event(row):
    if row.event not in EVENT_FIELDS:
        return None
    payload = row.payload
    result = {'sequence':row.sequence, 'event':row.event, 'recorded_at':payload.get('recorded_at')}
    for key in EVENT_FIELDS[row.event]:
        value = payload.get(key)
        if isinstance(value,str):
            result[key] = value[:128]
        elif isinstance(value,(int,float)) and math.isfinite(value):
            result[key] = value
    if row.event == 'tool_approval_granted':
        result['tool_names'] = [name for name in payload.get('tool_names',[])[:129]
            if isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',name)]
    if row.event == 'artifacts_ready':
        result['file_count'] = len(payload.get('items',[]))
    return result


def knowledge_context(store, session, run, checkpoint, message):
    context = (run.execution_snapshot or {}).get('context',{})
    registry = (((checkpoint.payload if checkpoint else {}).get('workflow') or {}).get('body') or {}).get('registry')
    # Completed tools already persist the evidence body in their receipt. This
    # also supports live traces and older Runs without a final workflow checkpoint.
    receipts = queries.knowledge_tool_receipts(run, session)
    for receipt,updated in receipts:
        if checkpoint and aware(checkpoint.updated_at)>=aware(updated) and registry:
            break
        value = receipt.get('value') if isinstance(receipt,dict) else None
        candidate = ((value.get('body') or {}).get('registry')) if isinstance(value,dict) else None
        if candidate:
            registry=candidate
            break
    if not registry:
        return {'status':'unavailable','sources':[],'parents':[]}
    try:
        frozen = [UUID(value) for value in context.get('allowed_document_ids',[])]
        organization = UUID(context['organization_id']) if context.get('organization_id') else None
        if context.get('knowledge_base_ids') or organization:
            from app.service.sharing import SharingService
            SharingService(store.settings,store.database).scope(run.user_id,
                [UUID(value) for value in context.get('knowledge_base_ids',[])], frozen,
                session=session, organization_id=organization, include_owned=not context.get('shared_only',False))
        else:
            current = Repository(session).ready_document_ids(run.user_id,frozen,store.settings.embedding_model,
                require_index=store.settings.document_publication_enabled)
            if set(current)!=set(frozen):
                raise AppError('DOCUMENT_SCOPE_CHANGED','资料授权已变化',409)
    except (AppError,ValueError,TypeError):
        return {'status':'revoked','sources':[],'parents':[]}
    cited = {item.get('number') for item in (message.citations if message and run.state=='succeeded' else []) or [] if item.get('type')=='knowledge'}
    excluded = set(registry.get('excluded_numbers',[]))
    allowed = {str(value) for value in frozen}
    sources = []
    for entry in registry.get('entries',[]):
        if entry.get('type')!='knowledge' or entry.get('document_id') not in allowed:
            continue
        item = {key:entry.get(key) for key in ('number','document_id','chunk_id','source_name','page_number','section','score','rerank_score')}
        item.update(excerpt=str(entry.get('excerpt',''))[:500],
            status='excluded' if entry['number'] in excluded else 'cited' if entry['number'] in cited else 'retrieved')
        sources.append(item)
    numbers = {item['number'] for item in sources if item['status']!='excluded'}
    parents = [{'context_id':str(item.get('context_id',''))[:128], 'title':str(item.get('title',''))[:500],
        'for_numbers':[number for number in item.get('for_numbers',[]) if number in numbers],
        'excerpt':str(item.get('content',''))[:1200], 'truncated':len(str(item.get('content','')))>1200}
        for item in registry.get('contexts',{}).values() if any(number in numbers for number in item.get('for_numbers',[]))]
    return {'status':'available','sources':sources[:100],'parents':parents[:32],
        'truncated':len(sources)>100 or len(parents)>32,
        'budget_tokens':registry.get('token_budget'), 'used_tokens':registry.get('used_tokens'),
        'parent_context_tokens':registry.get('context_tokens')}


def read_trace(store, user_id, run_id, after=0):
    run, rows = store.read(user_id,run_id,after)
    with transaction_scope(store.database) as session:
        owned = store.get_row(session,user_id,run_id)
        parent = Repository(session).chat_session(user_id,owned.session_id)
        checkpoint = as_transaction(session).load(ChatRunCheckpoint,run_id)
        message = as_transaction(session).load(ChatMessage,owned.message_id) if owned.message_id else None
        knowledge = knowledge_context(store,session,owned,checkpoint,message)
        metrics = ((message.run_metadata or {}).get('metrics') or {}).get('context') if message else None
        calls = (metrics or {}).get('calls',[])
        model_usage = [{key:item[key] for key in ('attempt_index','input_tokens','input_budget_tokens','usage_ratio','measurement','model_role') if key in item}
            for item in calls[-128:]]
        remaining = owned.remaining_seconds
        if remaining is not None and owned.active_since is not None and owned.state in {'running','cancelling'}:
            end = min(store.now(session),aware(owned.lease_expires_at)) if owned.lease_expires_at else store.now(session)
            remaining = max(0,remaining-max(0,(end-aware(owned.active_since)).total_seconds()))
        if owned.state in {'succeeded','failed','cancelled','interrupted'}:
            remaining = None  # Older terminal Runs did not settle the final time debit.
    events = [value for row in rows if (value:=public_event(row)) is not None]
    cursor = rows[-1].sequence if rows else after
    gap = [row.sequence for row in rows]!=list(range(after+1,cursor+1)) or not rows and after<run.sequence
    return {'run_id':run.id,'session_id':run.session_id,'user_id':user_id,'organization_id':parent.organization_id,
        'state':run.state,'sequence':run.sequence,
        'next_cursor':cursor,'has_more':cursor<run.sequence,'history_gap':bool(gap),
        'legacy_timestamps':any(value['recorded_at'] is None for value in events),
        'remaining_seconds':remaining,'created_at':run.created_at,
        'events':events,'knowledge':knowledge,'model_context':model_usage}
