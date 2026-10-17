import test from 'node:test'
import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'
import ts from 'typescript'
import {readEventStream} from '../src/sse.ts'

const rid='fixture-run'
function frame(sequence,event,data={}){return `event: ${event}\nid: ${rid}:${sequence}\ndata: ${JSON.stringify({run_id:rid,sequence,...data})}\n\n`}
const response=(body)=>new Response(body,{headers:{'content-type':'text/event-stream'}})

test('replay ignores duplicates and delivers only new answer deltas',async()=>{
  const cursor={runId:rid,sequence:1},received=[]
  const done=await readEventStream(response(frame(1,'run_started')+frame(2,'answer_delta',{delta:'新'})+frame(2,'answer_delta',{delta:'新'})+frame(3,'done',{answer:'最终'})),(event,data)=>received.push([event,data]),cursor)
  assert.deepEqual(received.map(([event])=>event),['answer_delta','done'])
  assert.equal(done.answer,'最终');assert.equal(cursor.sequence,3)
})

test('partial frames and UTF-8 chunks retain the last delivered cursor',async()=>{
  const bytes=new TextEncoder().encode(frame(1,'answer_delta',{delta:'中文'})),cursor={sequence:0},received=[]
  const stream=new ReadableStream({start(controller){for(const byte of bytes)controller.enqueue(new Uint8Array([byte]));controller.close()}})
  await assert.rejects(readEventStream(new Response(stream),(event,data)=>received.push(data),cursor),/连接中断/)
  assert.equal(received[0].delta,'中文');assert.equal(cursor.sequence,1)
})

test('terminal error stops reconnection and sequence gaps are rejected',async()=>{
  const cursor={sequence:0}
  await assert.rejects(readEventStream(response(frame(1,'error',{code:'RUN_INTERRUPTED',message:'已中断',status:409})),()=>{},cursor),/已中断/)
  assert.equal(cursor.failed,true)
  await assert.rejects(readEventStream(response(frame(2,'answer_delta',{delta:'gap'})),()=>{},{sequence:0}),/不连续/)
  await assert.rejects(readEventStream(response(frame(1,'done')),()=>{},{runId:'other',sequence:0}),/标识无效/)
})

test('current human pause and cancellation finish the stream without reconnect errors',async()=>{
  for(const event of ['run_waiting','run_cancelled']){
    const cursor={sequence:0},received=[]
    const value=await readEventStream(response(frame(1,event,{question:'补充地区',resume_token:'token',checkpoint_revision:2})),(name)=>received.push(name),cursor)
    assert.equal(value.sequence,1)
    assert.equal(cursor.failed,undefined)
    assert.deepEqual(received,[event])
  }
})

test('replay passes historical pauses and continues through the resumed round',async()=>{
  const cursor={sequence:0},received=[]
  const value=await readEventStream(response(frame(1,'run_waiting',{is_current:false})+frame(2,'run_resumed')+frame(3,'done',{answer:'继续后的回答'})),(event)=>received.push(event),cursor)
  assert.deepEqual(received,['run_waiting','run_resumed','done'])
  assert.equal(value.answer,'继续后的回答')
  assert.equal(cursor.sequence,3)
})

test('create retry reuses idempotency key; stream reconnect uses the delivered cursor',async()=>{
  const source=await readFile(new URL('../src/api.ts',import.meta.url),'utf8')
  const sseUrl=new URL('../src/sse.ts',import.meta.url).href
  const javascript=ts.transpileModule(source.replace("from './sse'",`from '${sseUrl}'`),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText
  const originalFetch=globalThis.fetch,originalChannel=globalThis.BroadcastChannel
  const requests=[],events=[];let creates=0,streams=0
  globalThis.BroadcastChannel=class{addEventListener(){}postMessage(){}}
  globalThis.fetch=async(url,init)=>{
    requests.push({url,init})
    if(url.endsWith('/runs')){if(++creates===1)throw new TypeError('network loss');return Response.json({run_id:rid})}
    if(++streams===1)return response(frame(1,'run_started')+frame(2,'answer_delta',{delta:'第一段'}))
    return response(frame(2,'answer_delta',{delta:'第一段'})+frame(3,'answer_delta',{delta:'第二段'})+frame(4,'done',{answer:'最终回答'}))
  }
  try{
    const {api}=await import(`data:text/javascript;base64,${Buffer.from(javascript).toString('base64')}`)
    const done=await api.chatStream('session',{question:'fixture'},(event,data)=>events.push([event,data]),true)
    assert.equal(creates,2);assert.equal(streams,2)
    assert.equal(requests[0].init.headers['Idempotency-Key'],requests[1].init.headers['Idempotency-Key'])
    assert.equal(requests[3].init.headers['Last-Event-ID'],`${rid}:2`)
    assert.deepEqual(events.filter(([event])=>event==='answer_delta').map(([,data])=>data.delta),['第一段','第二段'])
    assert.equal(done.answer,'最终回答')
  }finally{globalThis.fetch=originalFetch;globalThis.BroadcastChannel=originalChannel}
})
