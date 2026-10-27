export type TraceEvent={sequence:number;event:string;recorded_at:string|null;name?:string;agent?:string;operation_key?:string;status?:string;code?:string;error_code?:string;elapsed_ms?:number;tool_names?:string[];file_count?:number;generation?:number}
export function traceLabel(event:TraceEvent){return ({run_started:'任务入队',run_claimed:'开始执行',run_resumed:'补充信息后恢复',run_waiting:'等待补充信息',cancel_requested:'请求停止',run_cancelled:'已取消',done:'回答已完成',error:'运行失败',agent_started:'专职任务开始',agent_finished:'专职任务结束',agent_failed:'专职任务未完成',tool_started:'工具开始',tool_finished:'工具回执已确认',tool_failed:event.status==='uncertain'?'工具结果未确认':'工具失败',tool_approval_granted:'本次工具授权已确认',artifacts_ready:'文件已保存',context_usage:'上下文预算记录',usage:'模型用量已记录'} as Record<string,string>)[event.event]||'事件记录'}
export function traceState(state:string){return ({queued:'等待执行',running:'正在执行',waiting:'等待补充',cancelling:'正在停止',cancelled:'已取消',succeeded:'已完成',failed:'失败',interrupted:'已中断'} as Record<string,string>)[state]||'状态未知'}
export function tracePage(page:any,runId:string,sessionId:string,userId:string,organizationId:string|null,after:number):TraceEvent[]{
  if(page?.run_id!==runId||page.session_id!==sessionId||page.user_id!==userId||page.organization_id!==organizationId)throw new Error('执行记录不属于当前账号、空间或对话')
  if(!Array.isArray(page.events)||page.events.length>100||!Number.isSafeInteger(page.sequence)||!Number.isSafeInteger(page.next_cursor)||page.next_cursor<after||page.next_cursor>page.sequence||page.has_more!== (page.next_cursor<page.sequence))throw new Error('执行记录游标无效')
  let previous=after
  for(const item of page.events){if(!Number.isSafeInteger(item.sequence)||item.sequence<=previous||item.sequence>page.next_cursor||typeof item.event!=='string')throw new Error('执行记录序号不连续或重复');previous=item.sequence}
  return page.events
}
