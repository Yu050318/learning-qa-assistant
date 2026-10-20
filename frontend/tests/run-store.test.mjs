import test from 'node:test'
import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'
import ts from 'typescript'
import {createPinia,setActivePinia} from 'pinia'

const uri=source=>`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const source=await readFile(new URL('../src/store.ts',import.meta.url),'utf8')
const rewritten=source.replace("from 'pinia'",`from '${import.meta.resolve('pinia')}'`)
  .replace("from 'vue'",`from '${import.meta.resolve('vue')}'`)
  .replace("from './api'",`from '${uri('export const api=new Proxy({}, {get:(_,key)=>globalThis.__runApiMock[key]})')}'`)
  .replace("from './uploads'",`from '${uri('export const createUploadQueue=()=>({recover(){},reset(){}})')}'`)
  .replace("from './agents'",`from '${new URL('../src/agents.ts',import.meta.url).href}'`)
const {useAppStore}=await import(uri(ts.transpileModule(rewritten,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText))

const waiting={question:'请补充地区',checkpoint_revision:1,resume_token:'fixture-token',expires_at:'2099-01-01T00:00:00Z',sequence:4}
const messages=state=>[{id:'user',role:'user',content:'fixture'},{id:'assistant',role:'assistant',content:state==='succeeded'?'境内结果':'请补充地区',citations:[],run_metadata:{run_id:'run',state}}]
function setup(mock){
  globalThis.window=globalThis
  globalThis.__runApiMock=mock
  setActivePinia(createPinia())
  const store=useAppStore()
  store.capabilities={features:{durable_runs:true}}
  return store
}

test('refresh restores human pause; supplement reuses assistant and starts after pause cursor',async()=>{
  let state='waiting',resumeArguments
  const store=setup({
    session:async()=>({messages:messages(state)}),
    runs:async()=>[{run_id:'run',state,message_id:'assistant',waiting}],
    continueRun:async(id,token,input)=>{assert.equal(id,'run');assert.equal(token.resume_token,waiting.resume_token);assert.equal(input,'境内');state='running';return {state}},
    run:async()=>({state,message_id:'assistant'}),
    resumeRun:async(id,onEvent,after)=>{
      resumeArguments={id,after}
      onEvent('agent_started',{run_id:'run',agent:'sql',status:'running'})
      onEvent('answer_start',{run_id:'run'})
      onEvent('answer_delta',{run_id:'run',delta:'境内结果'})
      state='succeeded'
      onEvent('done',{run_id:'run',answer:'境内结果',run_metadata:{state}})
    },
  })
  await store.loadSession('session')
  assert.equal(store.runFor('session').waiting.resume_token,waiting.resume_token)
  assert.equal(store.messagesFor('session').length,2)
  await store.continueRun('session','境内')
  assert.deepEqual(resumeArguments,{id:'run',after:waiting.sequence})
  assert.deepEqual(store.messagesFor('session').map(item=>item.id),['user','assistant'])
  assert.equal(store.messagesFor('session')[1].content,'境内结果')
  assert.equal(store.runFor('session').waiting,null)
  assert.equal(store.runFor('session').error,'')
  assert.equal(store.runFor('session').busy,false)
})

test('stopping a paused run calls cancellation API and removes resume controls',async()=>{
  let cancelled
  const store=setup({
    session:async()=>({messages:messages(cancelled?'cancelled':'waiting')}),
    runs:async()=>[{run_id:'run',state:'waiting',message_id:'assistant',waiting}],
    cancelRun:async id=>{cancelled=id;return {state:'cancelled',waiting:null}},
  })
  await store.loadSession('session')
  await store.cancelRun('session')
  assert.equal(cancelled,'run')
  assert.equal(store.runFor('session').waiting,null)
  assert.equal(store.runFor('session').error,'本轮问答已取消')
  assert.equal(store.messagesFor('session').length,2)
})

test('queued run can be cancelled before the first SSE frame arrives',async()=>{
  let finish,cancelled
  const store=setup({
    chatStream:async(id,body,onEvent,durable,onRun)=>{
      assert.equal(durable,true)
      onRun({run_id:'run',state:'queued'})
      await new Promise(resolve=>finish=resolve)
      onEvent('run_cancelled',{run_id:'run'})
    },
    cancelRun:async id=>{cancelled=id;finish();return {state:'cancelled',waiting:null}},
    session:async()=>({messages:messages('cancelled')}),sessions:async()=>[],memories:async()=>[],
  })
  const sending=store.send('session',{question:'fixture'})
  assert.equal(store.runFor('session').busy,true)
  assert.equal(store.runFor('session').runId,'run')
  await store.cancelRun('session')
  await sending
  assert.equal(cancelled,'run')
  assert.equal(store.runFor('session').busy,false)
  assert.equal(store.runFor('session').error,'本轮问答已取消')
})

const spaces=[{id:null,name:'个人空间',kind:'personal',role:'owner',session_count:1},{id:'team',name:'研发团队',kind:'team',role:'member',session_count:1}]
function enableSpaces(store){store.capabilities={features:{workspaces:true,sharing:true,durable_runs:true}};store.workspaces=spaces.map(space=>({...space}))}

test('space switch drops late document and session responses and clears old selections',async()=>{
  let oldDocuments,oldSession
  const store=setup({
    documents:async(q,status,organization)=>organization?{items:[{id:'team-doc',can_query:true}],total:1}:new Promise(resolve=>oldDocuments=resolve),
    session:async()=>new Promise(resolve=>oldSession=resolve),
    sessions:async(q,organization)=>{assert.equal(organization,'team');return [{id:'team-thread',organization_id:'team'}]},
    knowledgeBases:async()=>[{id:'kb-team',organization_id:'team'},{id:'kb-other',organization_id:'other'}],
  })
  enableSpaces(store)
  store.selectedDocuments=['private-doc'];store.selectedBases=['old-kb'];store.messagesFor('private').push({id:'secret',content:'private'})
  const documents=store.loadDocuments(),session=store.loadSession('private')
  await store.switchWorkspace('team')
  oldDocuments({items:[{id:'private-doc',can_query:true}],total:1});oldSession({organization_id:null,messages:[{id:'secret',content:'private'}]})
  await Promise.all([documents,session])
  assert.deepEqual(store.documents.map(item=>item.id),['team-doc'])
  assert.deepEqual(store.sessions.map(item=>item.id),['team-thread'])
  assert.deepEqual(store.knowledgeBases.map(item=>item.id),['kb-team'])
  assert.deepEqual(store.selectedDocuments,[]);assert.deepEqual(store.selectedBases,[])
  assert.deepEqual(store.messagesFor('private'),[])
})

test('space switch ignores old SSE callbacks and completion cannot reload the old thread',async()=>{
  let emit,finish
  const store=setup({
    chatStream:async(id,body,onEvent)=>{emit=onEvent;await new Promise(resolve=>finish=resolve)},
    session:async()=>assert.fail('old thread must not reload after a space change'),
    sessions:async()=>[],documents:async()=>({items:[],total:0}),knowledgeBases:async()=>[],
  })
  enableSpaces(store)
  const pending=store.send('private',{question:'private question'})
  await store.switchWorkspace('team')
  emit('answer_delta',{run_id:'old',delta:'must remain in old space'})
  finish();await pending
  assert.deepEqual(store.messagesFor('private'),[])
  assert.equal(store.organizationId,'team')
})

test('a thread response for another space cannot populate the active message panel',async()=>{
  const store=setup({session:async()=>({organization_id:'team',messages:[{id:'hidden',content:'team data'}]})})
  enableSpaces(store)
  await assert.rejects(()=>store.loadSession('team-thread'),/其他空间/)
  assert.deepEqual(store.messagesFor('team-thread'),[])
})
