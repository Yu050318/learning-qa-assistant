import test from 'node:test'
import assert from 'node:assert/strict'
import {traceLabel,tracePage,traceState} from '../src/run-trace.ts'
const page={run_id:'run',session_id:'session',user_id:'owner',organization_id:null,sequence:8,next_cursor:8,has_more:false,events:[{sequence:2,event:'run_claimed',recorded_at:null},{sequence:8,event:'tool_started',recorded_at:null}]}
test('filtered event gaps are allowed while duplicate, out of order and stale cursors are rejected',()=>{
  assert.equal(tracePage(page,'run','session','owner',null,0).length,2)
  for(const broken of [{...page,next_cursor:9},{...page,has_more:true},{...page,events:[page.events[1],page.events[0]]},{...page,events:[page.events[0],page.events[0]]}])assert.throws(()=>tracePage(broken,'run','session','owner',null,0))
  assert.throws(()=>tracePage(page,'run','session','owner',null,8))
})
test('trace pages cannot cross account, workspace, session or run boundaries',()=>{
  for(const values of [['other','session','owner',null],['run','other','owner',null],['run','session','other',null],['run','session','owner','team']])assert.throws(()=>tracePage(page,...values,0),/不属于/)
})
test('unconfirmed receipts and cancellation never display a successful answer label',()=>{
  assert.equal(traceLabel({event:'tool_failed',status:'uncertain'}),'工具结果未确认')
  assert.equal(traceLabel({event:'tool_finished'}),'工具回执已确认')
  assert.equal(traceState('cancelled'),'已取消')
  assert.equal(traceState('interrupted'),'已中断')
})
