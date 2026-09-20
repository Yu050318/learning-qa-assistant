export type Session = { id:string; title:string; summary:string; summarized_through:string|null; created_at:string; updated_at:string }
export type Citation = { type:'knowledge'|'web'; number:number; excerpt:string; source_name?:string; title?:string; url?:string; page_number?:number; section?:string }
export type Message = { id:string; role:string; content:string; model_provider:string|null; model_name:string|null; citations:Citation[]; token_usage:Record<string,number>|null; run_metadata:any; created_at:string }
export type DocumentItem = { id:string; original_name:string; file_type:string; status:'processing'|'ready'|'failed'|'deleting'; chunk_count:number; embedding_model:string; error_message:string|null; size_bytes:number|null; can_query:boolean; unavailable_reason:string|null; created_at:string; updated_at:string }
export type Capabilities = { default_model_provider:string|null; models:{provider:string;model_name:string;available:boolean;input_budget_tokens:number|null;cache_usage_supported:boolean}[]; web_search_enabled:boolean; upload:{allowed_extensions:string[];max_file_bytes:number;long_running_warning_seconds:number}; features:Record<string,boolean> }
export class ApiError extends Error { constructor(public code:string, message:string, public status:number, public requestId?:string){ super(message) } }

async function request<T>(path:string, userId:string, init:RequestInit={}):Promise<T>{
  const response=await fetch(path,{...init,headers:{'X-User-ID':userId,...(init.body instanceof FormData?{}:{'Content-Type':'application/json'}),...init.headers}})
  if(response.status===204) return undefined as T
  const body=await response.json().catch(()=>null)
  if(!response.ok) throw new ApiError(body?.error?.code||'REQUEST_FAILED',body?.error?.message||'请求失败',response.status,body?.error?.request_id)
  return body
}
export const api={
  capabilities:(u:string)=>request<Capabilities>('/api/v2/capabilities',u),
  sessions:(u:string,q='')=>request<Session[]>(`/api/v2/sessions?limit=100&q=${encodeURIComponent(q)}`,u),
  createSession:(u:string,title:string)=>request<Session>('/api/v2/sessions',u,{method:'POST',body:JSON.stringify({title})}),
  session:(u:string,id:string)=>request<Session&{messages:Message[]}>(`/api/v2/sessions/${id}?message_limit=100`,u),
  renameSession:(u:string,id:string,title:string)=>request<Session>(`/api/v2/sessions/${id}`,u,{method:'PATCH',body:JSON.stringify({title})}),
  deleteSession:(u:string,id:string)=>request<void>(`/api/v2/sessions/${id}`,u,{method:'DELETE'}),
  chat:(u:string,id:string,body:any)=>request<any>(`/api/v2/sessions/${id}/messages`,u,{method:'POST',body:JSON.stringify(body)}),
  documents:(u:string,q='',status='')=>request<{items:DocumentItem[];total:number}>(`/api/v2/documents?limit=100&q=${encodeURIComponent(q)}${status?`&status=${status}`:''}`,u),
  document:(u:string,id:string)=>request<DocumentItem>(`/api/v1/documents/${id}`,u),
  retry:(u:string,id:string)=>request<DocumentItem>(`/api/v1/documents/${id}/retry`,u,{method:'POST',body:'{}'}),
  deleteDocument:(u:string,id:string)=>request<void>(`/api/v1/documents/${id}`,u,{method:'DELETE'}),
  estimate:(u:string,body:any)=>request<any>('/api/v2/context-estimate',u,{method:'POST',body:JSON.stringify(body)}),
  compact:(u:string,id:string,version:string,provider:string)=>request<any>(`/api/v2/sessions/${id}/compact`,u,{method:'POST',body:JSON.stringify({expected_context_version:version,model_provider:provider})}),
  upload:(u:string,file:File,onProgress:(n:number)=>void)=>new Promise<DocumentItem>((resolve,reject)=>{ const xhr=new XMLHttpRequest(); const data=new FormData(); data.append('file',file); xhr.open('POST','/api/v1/documents'); xhr.setRequestHeader('X-User-ID',u); xhr.upload.onprogress=e=>e.lengthComputable&&onProgress(e.loaded/e.total); xhr.onload=()=>{let body:any;try{body=JSON.parse(xhr.responseText)}catch{}; xhr.status<300?resolve(body):reject(new ApiError(body?.error?.code||'UPLOAD_FAILED',body?.error?.message||'上传失败',xhr.status))}; xhr.onerror=()=>reject(new ApiError('NETWORK_ERROR','网络连接失败',0)); xhr.send(data) }),
}
