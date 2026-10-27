import test from 'node:test'
import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'
import ts from 'typescript'
import {artifactRunId,artifactItems,artifactBytes,artifactSize} from '../src/artifacts.ts'
const run='00000000-0000-0000-0000-000000000001',id='00000000-0000-0000-0000-000000000002'
const sha='73475cb40a568e8da8a045ced110137e159f890ac4da883b6b17dc651b3a8049'
const item={id,run_id:run,user_id:id,session_id:id,organization_id:null,tool_name:'sandbox_execute',tool_version:1,name:'answer.txt',size_bytes:2,sha256:sha,media_type:'text/plain',preview_kind:'text',created_at:'2026-10-21T00:00:00Z'}

test('only confirmed Sandbox receipts in completed or waiting Runs expose the shelf',()=>{
  const metadata={run_id:run,state:'succeeded',tool_events:[{name:'sandbox_execute',status:'ok'}]}
  assert.equal(artifactRunId(metadata),run)
  assert.equal(artifactRunId({...metadata,state:'waiting'}),run)
  for(const state of ['queued','running','cancelling','cancelled','failed','interrupted'])assert.equal(artifactRunId({...metadata,state}),null)
  assert.equal(artifactRunId({...metadata,tool_events:[{name:'sandbox_execute',status:'failed'}]}),null)
  assert.equal(artifactRunId({...metadata,run_id:'unbound'}),null)
  assert.equal(artifactRunId(null),null)
})

test('artifact directory rejects wrong run, unsafe filenames, duplicates, size and media drift',()=>{
  assert.deepEqual(artifactItems({items:[item]},run),[item])
  for(const update of [{run_id:id},{name:'../escape'},{name:'x".html'},{size_bytes:2097153},{sha256:'bad'},{preview_kind:'image'},{tool_name:'public.lookup'}])assert.throws(()=>artifactItems({items:[{...item,...update}]},run))
  assert.throws(()=>artifactItems({items:[item,item]},run))
  assert.throws(()=>artifactItems({items:Array(65).fill(item)},run))
  assert.equal(artifactSize(1024),'1.0 KB')
})

test('original downloads verify body length and digest before creating a binary attachment',async()=>{
  const response=new Response('42',{headers:{'x-artifact-sha256':sha,'content-type':'text/plain'}})
  const value=await artifactBytes(response,item,false)
  assert.equal(await value.blob.text(),'42')
  assert.equal(value.blob.type,'application/octet-stream')
  await assert.rejects(()=>artifactBytes(new Response('43',{headers:{'x-artifact-sha256':sha}}),item,false),/回执不一致/)
  await assert.rejects(()=>artifactBytes(new Response('42'),item,false),/回执不一致/)
  await assert.rejects(()=>artifactBytes(new Response('42',{headers:{'content-length':'99999999'}}),item,false),/超过上限/)
})

test('previews keep text literal and accept only normalized PNG image responses',async()=>{
  const value=await artifactBytes(new Response('<script>alert(1)</script>',{headers:{'content-type':'text/plain','x-preview-truncated':'true'}}),item,true)
  assert.equal(await value.blob.text(),'<script>alert(1)</script>')
  assert.equal(value.truncated,true)
  await assert.rejects(()=>artifactBytes(new Response('<script/>',{headers:{'content-type':'text/html'}}),item,true),/格式无效/)
  await assert.rejects(()=>artifactBytes(new Response('x'.repeat(17000),{headers:{'content-type':'text/plain'}}),item,true),/超过上限/)
  const image={...item,preview_kind:'image',media_type:'image/jpeg'}
  await assert.rejects(()=>artifactBytes(new Response('<svg/>',{headers:{'content-type':'image/png'}}),image,true),/格式无效/)
  const bytes=new Uint8Array([137,80,78,71,13,10,26,10])
  assert.equal((await artifactBytes(new Response(bytes,{headers:{'content-type':'image/png'}}),image,true)).blob.type,'image/png')
  await assert.rejects(()=>artifactBytes(new Response('x'),{...item,preview_kind:'none'},true),/格式无效/)
})

test('chunked downloads enforce the byte limit even without a declared length',async()=>{
  let cancelled=false
  const stream=new ReadableStream({start(controller){controller.enqueue(new Uint8Array(2097153))},cancel(){cancelled=true}})
  await assert.rejects(()=>artifactBytes(new Response(stream),item,false),/超过上限/)
  assert.equal(cancelled,true)
})

test('Run reads retry transient locks but account changes and refreshes never cross identities',async()=>{
  const previousFetch=globalThis.fetch,previousChannel=globalThis.BroadcastChannel
  globalThis.BroadcastChannel=class{addEventListener(){}postMessage(){}}
  const auth=(username,expires=3600)=>({access_token:`fixture-${username}`,expires_in:expires,user:{id:username==='first'?id:run,username,role:'admin'}})
  const source=await readFile(new URL('../src/api.ts',import.meta.url),'utf8')
  const rewritten=source.replace("from './sse'",`from '${new URL('../src/sse.ts',import.meta.url).href}'`).replace("from './artifacts'",`from '${new URL('../src/artifacts.ts',import.meta.url).href}'`)
  const javascript=ts.transpileModule(rewritten,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText
  try{
    const {api}=await import(`data:text/javascript;base64,${Buffer.from(javascript).toString('base64')}`)
    let finish
    globalThis.fetch=async(path,init)=>String(path).endsWith('/login')?Response.json(auth(JSON.parse(init.body).username)):new Promise(resolve=>{finish=resolve})
    await api.login('first','fixture')
    const pending=api.artifactContent(run,item)
    await api.login('second','fixture')
    finish(new Response('42',{headers:{'x-artifact-sha256':sha}}))
    await assert.rejects(pending,error=>error.code==='AUTH_CHANGED')
    let requests=0
    globalThis.fetch=async(path,init)=>{
      if(String(path).endsWith('/login'))return Response.json(auth('first',0))
      if(String(path).endsWith('/refresh'))return Response.json(auth('second'))
      requests++;return Response.json({error:{code:'UNAUTHORIZED',message:'fixture'}},{status:401})
    }
    await api.login('first','fixture')
    await assert.rejects(()=>api.artifactContent(run,item),error=>error.code==='AUTH_CHANGED')
    assert.equal(requests,1)
    let code='RESOURCE_BUSY',successAfter=1
    requests=0
    globalThis.fetch=async(path,init)=>{
      if(String(path).endsWith('/login'))return Response.json(auth(JSON.parse(init.body).username))
      requests++
      return requests>successAfter?Response.json({fixture:true}):Response.json({error:{code,message:'fixture'}},{status:code==='RESOURCE_BUSY'?409:403})
    }
    await api.login('first','fixture')
    assert.equal((await api.runTrace(run)).fixture,true)
    assert.equal(requests,2)
    requests=0;successAfter=Infinity
    await assert.rejects(()=>api.runTrace(run),error=>error.code==='RESOURCE_BUSY')
    assert.equal(requests,3)
    requests=0;code='FORBIDDEN'
    await assert.rejects(()=>api.runTrace(run),error=>error.code==='FORBIDDEN')
    assert.equal(requests,1)
    requests=0;code='RESOURCE_BUSY'
    const waiting=api.runTrace(run)
    await api.login('second','fixture')
    await assert.rejects(waiting,error=>error.code==='AUTH_CHANGED')
    assert.equal(requests,1)
  }finally{globalThis.fetch=previousFetch;globalThis.BroadcastChannel=previousChannel}
})
