export type Session = { id:string; title:string; summary:string; summarized_through:string|null; created_at:string; updated_at:string }
export type Citation = { type:'knowledge'|'web'; number:number; excerpt:string; source_name?:string; title?:string; url?:string; page_number?:number; section?:string }
export type Message = { id:string; role:string; content:string; model_provider:string|null; model_name:string|null; citations:Citation[]; token_usage:Record<string,number>|null; run_metadata:any; created_at:string }
export type DocumentItem = { id:string; original_name:string; file_type:string; status:'processing'|'ready'|'failed'|'deleting'; chunk_count:number; embedding_model:string; error_message:string|null; size_bytes:number|null; can_query:boolean; unavailable_reason:string|null; created_at:string; updated_at:string }
export type Capabilities = { default_model_provider:string|null; models:{provider:string;model_name:string;available:boolean;input_budget_tokens:number|null;cache_usage_supported:boolean}[]; web_search_enabled:boolean; upload:{allowed_extensions:string[];max_file_bytes:number;long_running_warning_seconds:number}; features:Record<string,boolean> }
export type StreamEvent = { run_id:string; sequence:number; [key:string]:any }
export class ApiError extends Error { constructor(public code:string, message:string, public status:number, public requestId?:string){ super(message) } }

async function request<T>(path:string, userId:string, init:RequestInit={}):Promise<T>{
  const response=await fetch(path,{...init,headers:{'X-User-ID':userId,...(init.body instanceof FormData?{}:{'Content-Type':'application/json'}),...init.headers}})
  if(response.status===204) return undefined as T
  const body=await response.json().catch(()=>null)
  if(!response.ok) throw new ApiError(body?.error?.code||'REQUEST_FAILED',body?.error?.message||'请求失败',response.status,body?.error?.request_id)
  return body
}
async function streamRequest(path:string,userId:string,body:any,onEvent:(event:string,data:StreamEvent)=>void):Promise<StreamEvent>{
  const response=await fetch(path,{method:'POST',headers:{'X-User-ID':userId,'Content-Type':'application/json','Accept':'text/event-stream'},body:JSON.stringify(body)})
  if(!response.ok){const value=await response.json().catch(()=>null);throw new ApiError(value?.error?.code||'REQUEST_FAILED',value?.error?.message||'请求失败',response.status,value?.error?.request_id)}
  if(!response.body)throw new ApiError('STREAM_UNAVAILABLE','浏览器无法读取流式响应',0)
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',doneEvent:StreamEvent|undefined
  const consume=(block:string)=>{let event='message';const data:string[]=[];for(const line of block.split('\n')){if(line.startsWith('event:'))event=line.slice(6).trim();else if(line.startsWith('data:'))data.push(line.slice(5).trimStart())}if(!data.length)return;const value=JSON.parse(data.join('\n')) as StreamEvent;onEvent(event,value);if(event==='error')throw new ApiError(value.code||'STREAM_ERROR',value.message||'流式请求失败',value.status||200,value.request_id);if(event==='done')doneEvent=value}
  while(true){const result=await reader.read();buffer+=decoder.decode(result.value||new Uint8Array(),{stream:!result.done}).replace(/\r\n/g,'\n');let boundary;while((boundary=buffer.indexOf('\n\n'))>=0){const block=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2);if(block.trim())consume(block)}if(result.done)break}
  if(buffer.trim())consume(buffer)
  if(!doneEvent)throw new ApiError('STREAM_INTERRUPTED','流式响应意外中断，请刷新会话确认结果',0)
  return doneEvent
}
export const api={
  capabilities:(u:string)=>request<Capabilities>('/api/v2/capabilities',u),
  sessions:(u:string,q='')=>request<Session[]>(`/api/v2/sessions?limit=100&q=${encodeURIComponent(q)}`,u),
  createSession:(u:string,title:string)=>request<Session>('/api/v2/sessions',u,{method:'POST',body:JSON.stringify({title})}),
  session:(u:string,id:string)=>request<Session&{messages:Message[]}>(`/api/v2/sessions/${id}?message_limit=100`,u),
  renameSession:(u:string,id:string,title:string)=>request<Session>(`/api/v2/sessions/${id}`,u,{method:'PATCH',body:JSON.stringify({title})}),
  deleteSession:(u:string,id:string)=>request<void>(`/api/v2/sessions/${id}`,u,{method:'DELETE'}),
  chat:(u:string,id:string,body:any)=>request<any>(`/api/v2/sessions/${id}/messages`,u,{method:'POST',body:JSON.stringify(body)}),
  chatStream:(u:string,id:string,body:any,onEvent:(event:string,data:StreamEvent)=>void)=>streamRequest(`/api/v2/sessions/${id}/messages/stream`,u,body,onEvent),
  documents:(u:string,q='',status='')=>request<{items:DocumentItem[];total:number}>(`/api/v2/documents?limit=100&q=${encodeURIComponent(q)}${status?`&status=${status}`:''}`,u),
  document:(u:string,id:string)=>request<DocumentItem>(`/api/v2/documents/${id}`,u),
  retry:(u:string,id:string)=>request<DocumentItem>(`/api/v2/documents/${id}/retry`,u,{method:'POST',body:'{}'}),
  deleteDocument:(u:string,id:string)=>request<void>(`/api/v2/documents/${id}`,u,{method:'DELETE'}),
  estimate:(u:string,body:any)=>request<any>('/api/v2/context-estimate',u,{method:'POST',body:JSON.stringify(body)}),
  compact:(u:string,id:string,version:string,provider:string)=>request<any>(`/api/v2/sessions/${id}/compact`,u,{method:'POST',body:JSON.stringify({expected_context_version:version,model_provider:provider})}),
  upload:(u:string,file:File,onProgress:(n:number)=>void)=>new Promise<DocumentItem>((resolve,reject)=>{ const xhr=new XMLHttpRequest(); const data=new FormData(); data.append('file',file); xhr.open('POST','/api/v2/documents'); xhr.setRequestHeader('X-User-ID',u); xhr.upload.onprogress=e=>e.lengthComputable&&onProgress(e.loaded/e.total); xhr.onload=()=>{let body:any;try{body=JSON.parse(xhr.responseText)}catch{}; xhr.status<300?resolve(body):reject(new ApiError(body?.error?.code||'UPLOAD_FAILED',body?.error?.message||'上传失败',xhr.status))}; xhr.onerror=()=>reject(new ApiError('NETWORK_ERROR','网络连接失败',0)); xhr.send(data) }),
}
