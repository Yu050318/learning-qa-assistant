export function extractWebUrls(question:string):string[]{
  return [...new Set((question.match(/https?:\/\/[^\s<>"'`，。！？、；（）【】]+/gi)||[]).map(url=>url.replace(/[.,;!?：:）)\]}]+$/g,'')))]
}
export function agentLabel(agent:string):string{
  return ({rag:'RAG Agent',sql:'SQL Agent',web:'Web Agent'} as Record<string,string>)[agent]||agent
}
export function statusLabel(status:string):string{
  return ({running:'执行中',ok:'已完成',empty:'证据不足',clarification:'需要补充条件',partial:'部分完成',failed:'执行失败'} as Record<string,string>)[status]||status
}
export function citationLocation(citation:{excel_source?:{sheet_name:string;row_start:number;row_end:number}|null;section?:string;page_number?:number;document_version?:{version_number:number}|null}):string{
  const source=citation.excel_source
  const location=source?`${source.sheet_name} · 第 ${source.row_start===source.row_end?source.row_start:`${source.row_start}–${source.row_end}`} 行`:citation.section||(citation.page_number?`第 ${citation.page_number} 页`:'')
  return [citation.document_version?`v${citation.document_version.version_number}`:'',location].filter(Boolean).join(' · ')
}
