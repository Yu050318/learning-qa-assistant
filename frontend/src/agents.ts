export function extractWebUrls(question:string):string[]{
  return [...new Set((question.match(/https?:\/\/[^\s<>"'`，。！？、；（）【】]+/gi)||[]).map(url=>url.replace(/[.,;!?：:）)\]}]+$/g,'')))]
}
export function agentLabel(agent:string):string{
  return ({rag:'RAG Agent',sql:'SQL Agent',web:'Web Agent'} as Record<string,string>)[agent]||agent
}
export function statusLabel(status:string):string{
  return ({running:'执行中',ok:'已完成',empty:'证据不足',clarification:'需要补充条件',partial:'部分完成',failed:'执行失败'} as Record<string,string>)[status]||status
}
