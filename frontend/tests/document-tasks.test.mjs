import test from 'node:test'
import assert from 'node:assert/strict'
import {taskPercent,taskPhase,taskHint} from '../src/document-tasks.ts'
const task=changes=>({state:'running',phase:'verifying',processed_units:100,total_units:100,...changes})
test('complete chunk writes do not claim task success before verification or file cleanup finishes',()=>{
  assert.equal(taskPercent(task({})),99)
  assert.equal(taskPercent(task({phase:'cleaning_files'})),99)
  assert.equal(taskPercent(task({state:'failed'})),99)
  assert.equal(taskPercent(task({state:'succeeded'})),100)
  assert.equal(taskPercent(task({total_units:null})),null)
  assert.equal(taskPercent(task({total_units:0})),null)
})
test('waiting for pinned runs and automatic retries have distinct actionable descriptions',()=>{
  assert.equal(taskPhase(task({state:'queued',phase:'waiting_runs'})),'等待关联运行结束')
  assert.match(taskHint(task({error_code:'DOCUMENT_INDEX_PINNED'})),/自动继续/)
  assert.match(taskHint(task({error_code:'DOCUMENT_INDEX_UNAVAILABLE'})),/不会清理其他位置/)
  assert.equal(taskPhase(task({phase:'future',state:'queued'})),'等待处理')
})
test('a failed family points to version cleanup rather than suggesting a whole-operation retry',()=>{
  assert.match(taskHint(task({kind:'delete_series',error_code:'DOCUMENT_SERIES_CLEANUP_INCOMPLETE'})),/重试/)
  assert.equal(taskPhase(task({kind:'delete_series',phase:'cleaning_versions'})),'逐版清理')
  assert.equal(taskPercent(task({kind:'delete_series',processed_units:1,total_units:2})),50)
})
