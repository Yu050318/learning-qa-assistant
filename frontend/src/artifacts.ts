export type RunArtifact={id:string;run_id:string;user_id:string;session_id:string;organization_id:string|null;tool_name:string;tool_version:number;name:string;size_bytes:number;sha256:string;media_type:string;preview_kind:'text'|'image'|'none';created_at:string}
const uuid=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i
export function artifactRunId(metadata:any):string|null{
  return metadata&&uuid.test(metadata.run_id)&&['succeeded','waiting'].includes(metadata.state)&&Array.isArray(metadata.tool_events)&&metadata.tool_events.some((event:any)=>event?.name==='sandbox_execute'&&event.status==='ok'&&!event.error_code)?metadata.run_id:null
}
export function artifactItems(value:any,runId:string):RunArtifact[]{
  if(!Array.isArray(value?.items)||value.items.length>64)throw new Error('产物列表格式无效')
  const seen=new Set<string>()
  return value.items.map((item:any)=>{
    if(!item||!uuid.test(item.id)||seen.has(item.id)||item.run_id!==runId||!uuid.test(item.user_id)||!uuid.test(item.session_id)||item.organization_id!==null&&!uuid.test(item.organization_id)||typeof item.name!=='string'||item.name.length>256||item.name.split('/').length>5||item.name.split('/').some((part:string)=>!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(part))||!Number.isInteger(item.size_bytes)||item.size_bytes<0||item.size_bytes>2097152||!/^[a-f0-9]{64}$/.test(item.sha256)||!Number.isInteger(item.tool_version)||item.tool_version<1||item.tool_name!=='sandbox_execute'||!['text','image','none'].includes(item.preview_kind)||!['text/plain','image/png','image/jpeg','image/webp','application/octet-stream'].includes(item.media_type))throw new Error('产物回执无法通过校验，请刷新后重试')
    if(item.preview_kind==='text'&&item.media_type!=='text/plain'||item.preview_kind==='image'&&!item.media_type.startsWith('image/')||item.preview_kind==='none'&&item.media_type!=='application/octet-stream')throw new Error('产物预览类型不一致')
    seen.add(item.id);return item as RunArtifact
  })
}
export function artifactSize(bytes:number){return bytes<1024?`${bytes} B`:bytes<1048576?`${(bytes/1024).toFixed(1)} KB`:`${(bytes/1048576).toFixed(1)} MB`}
export async function artifactBytes(response:Response,item:RunArtifact,preview:boolean){
  const maximum=preview?12*1024*1024:2097152
  const length=response.headers.get('content-length')
  if(length!==null&&(!/^\d+$/.test(length)||Number(length)>maximum))throw new Error('产物响应超过上限')
  const reader=response.body?.getReader();if(!reader)throw new Error('浏览器无法读取产物')
  const chunks:Uint8Array[]=[];let size=0
  try{while(true){const part=await reader.read();if(part.done)break;size+=part.value.byteLength;if(size>maximum)throw new Error('产物响应超过上限');chunks.push(part.value)}}finally{await reader.cancel().catch(()=>{})}
  const content=new Uint8Array(size);let offset=0;for(const chunk of chunks){content.set(chunk,offset);offset+=chunk.length}
  const media=(response.headers.get('content-type')||'').split(';')[0]?.trim()
  if(preview){
    if(item.preview_kind==='none'||item.preview_kind==='text'&&media!=='text/plain'||item.preview_kind==='image'&&(media!=='image/png'||![137,80,78,71,13,10,26,10].every((n,i)=>content[i]===n)))throw new Error('产物预览格式无效')
    if(item.preview_kind==='text'&&size>16387)throw new Error('文本预览超过上限')
  }else{
    const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',content))).map(n=>n.toString(16).padStart(2,'0')).join('')
    if(size!==item.size_bytes||digest!==item.sha256||response.headers.get('x-artifact-sha256')!==item.sha256)throw new Error('产物内容与回执不一致，已停止下载')
  }
  return {blob:new Blob([content],{type:preview?(item.preview_kind==='text'?'text/plain;charset=utf-8':'image/png'):'application/octet-stream'}),truncated:response.headers.get('x-preview-truncated')==='true'}
}
