import test from 'node:test'
import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'
import ts from 'typescript'
const uri=source=>`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const source=await readFile(new URL('../src/uploads.ts',import.meta.url),'utf8')
const rewritten=source.replace("from 'vue'",`from '${import.meta.resolve('vue')}'`)
  .replace("from './api'",`from '${uri('export const api=new Proxy({}, {get:(_,key)=>globalThis.__uploadApiMock[key]})')}'`)
const {createUploadQueue}=await import(uri(ts.transpileModule(rewritten,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText))
const settle=async()=>{for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve))}
const file=name=>new File(['source'],name,{type:'text/markdown'})
const queue=scope=>createUploadQueue(()=>{},message=>assert.fail(message),()=>({allowed:['.md'],max:1000,warning:100}),scope)

test('queued transfers and progress polls keep their original workspace after a switch',async()=>{
  let scope='first',next=0
  const transfers=[],polls=[]
  globalThis.__uploadApiMock={upload:async(file,progress,organization)=>{transfers.push(organization);return {id:String(++next)}},document:async(id,organization)=>{polls.push(organization);return {status:'ready'}}}
  const uploads=queue(()=>scope)
  uploads.add([file('a.md'),file('b.md'),file('c.md')])
  scope='second';await settle()
  assert.deepEqual(transfers,['first','first','first'])
  assert.deepEqual(polls,['first','first','first'])
  assert.equal(uploads.state.running,0)
  assert.ok(uploads.state.tasks.every(task=>task.state==='ready'&&task.organizationId==='first'))
})

test('reset on logout fences old transfers and releases no slots in the new account',async()=>{
  let finish,scope='old'
  const polls=[]
  globalThis.__uploadApiMock={upload:async(file)=>file.name==='old.md'?new Promise(resolve=>finish=resolve):{id:'new'},document:async(id)=>{polls.push(id);return {status:'ready'}}}
  const uploads=queue(()=>scope)
  uploads.add([file('old.md')]);uploads.reset();scope='new';uploads.add([file('new.md')])
  finish({id:'old'});await settle()
  assert.deepEqual(polls,['new'])
  assert.deepEqual(uploads.state.tasks.map(task=>task.name),['new.md'])
  assert.equal(uploads.state.running,0)
})

test('revoked workspace stops polling without claiming that the background upload failed',async()=>{
  let calls=0
  globalThis.__uploadApiMock={upload:async()=>({id:'lost'}),document:async()=>{calls++;throw Object.assign(new Error('revoked'),{status:404})}}
  const uploads=queue(()=>'team')
  uploads.add([file('member.md')]);await settle()
  assert.equal(calls,1);assert.equal(uploads.state.running,0)
  assert.equal(uploads.state.tasks[0].state,'unavailable')
})
