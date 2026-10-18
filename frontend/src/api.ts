import {readEventStream,type StreamCursor} from './sse'
export type Organization = {id:string;name:string;role:'owner'|'admin'|'member'}
export type KnowledgeBase = {id:string;name:string;organization_id:string;role:'admin'|'editor'|'viewer'}
export type Member = {user_id:string;username:string;role:string}
export type SharedDocument = {id:string;original_name:string;status:string;can_query:boolean}
export type ModelProfile = {model_name:string;context_window:number;enabled:boolean}
export type ModelRole='answer'|'fast'|'grader'|'evaluator'
export type RegisteredProfile={id:string;version:number;display_name:string;provider:'deepseek'|'ollama';connection_id:string;model_name:string;context_window:number;max_output_tokens:number;timeout_seconds:number;supports_stream:boolean;supports_tools:boolean;supports_structured_output:boolean;enabled:boolean;fallback_profile_id:string|null}
export type ModelCatalog={schema_version:2;profiles:Record<string,RegisteredProfile>;assignments:Record<ModelRole,string>;provider_defaults:Partial<Record<'deepseek'|'ollama',string>>}
export type CatalogModel={profile_id:string;profile_version:number;display_name:string;provider:string;model_name:string;enabled:boolean;available:boolean;health:string;checked_at:string|null;error_code:string|null;input_budget_tokens:number;cache_usage_supported:boolean}
export type ModelCatalogInfo={revision:number;catalog:ModelCatalog;can_manage:boolean;models:CatalogModel[];connections:{id:string;provider:string;configured:boolean}[]}
export type ModelVersionPage={total:number;items:{revision:number;created_by:string|null;created_at:string;configuration:unknown}[]}
export type ModelConfiguration = {profiles:Record<'deepseek'|'ollama',ModelProfile>;default_provider:'deepseek'|'ollama';allow_fallback:boolean;timeout_seconds:number;max_output_tokens:number}
export type ModelCenterInfo = {revision:number;configuration:ModelConfiguration;can_manage:boolean;models:{provider:'deepseek'|'ollama';model_name:string;enabled:boolean;available:boolean;health:string;checked_at:string|null;error_code:string|null;input_budget_tokens:number}[]}
export type ModelStatistics = {scope:string;days:number;models:{provider:string;model_name:string;model_role?:ModelRole|null;model_profile_id?:string|null;model_profile_version?:number|null;attempts:number;succeeded:number;failed:number;incomplete:number;fallback_attempts:number;reported_calls:number;reported_total_tokens:number|null;total_tokens:number|null;average_ms:number|null}[]}
export type User = { id:string; username:string; role:'user'|'admin' }
export type PreflightReport = {ready:boolean;checks:{name:string;status:'pass'|'fail'|'warn'|'disabled';code:string;action:string}[];features:Record<string,boolean>}
export type DocumentVersionInfo = {series_id:string;version_number:number;current_document_id:string|null;is_current:boolean;content_hash:string}
export type AuthResult = { access_token:string; token_type:'bearer'; expires_in:number; user:User }
export type Session = { id:string; title:string; summary:string; summarized_through:string|null; created_at:string; updated_at:string }
export type Citation = { type:'knowledge'|'web'|'memory'|'sql'; number:number; excerpt:string; source_name?:string; title?:string; url?:string; page_number?:number; section?:string; memory_id?:string; query?:string; columns?:string[]; row_count?:number; truncated?:boolean; excel_source?:{sheet_name:string;row_start:number;row_end:number;source_anchor:string}|null;document_version?:Pick<DocumentVersionInfo,'series_id'|'version_number'|'content_hash'>|null }
export type Message = { id:string; role:string; content:string; model_provider:string|null; model_name:string|null; citations:Citation[]; token_usage:Record<string,number>|null; run_metadata:any; created_at:string }
export type DocumentItem = { id:string; original_name:string; file_type:string; status:'processing'|'ready'|'failed'|'deleting'; chunk_count:number; embedding_model:string; error_message:string|null; size_bytes:number|null; can_query:boolean; unavailable_reason:string|null; created_at:string; updated_at:string;version_info?:DocumentVersionInfo|null }
export type MemoryItem = { id:string; content:string; status:'processing'|'ready'|'failed'|'deleting'; version:number; created_via:'chat'|'management_ui'; source_session_id:string|null; source_message_id:string|null; vector_updated_at:string|null; last_error:string|null; created_at:string; updated_at:string }
export type Capabilities = { default_model_provider:string|null;default_model_profile_id?:string|null; models:{profile_id?:string;display_name?:string;provider:string;model_name:string;available:boolean;input_budget_tokens:number|null;cache_usage_supported:boolean}[]; web_search_enabled:boolean; web_read_enabled:boolean; sql_enabled:boolean; upload:{allowed_extensions:string[];max_file_bytes:number;long_running_warning_seconds:number}; features:Record<string,boolean> }
export type StreamEvent = { run_id:string; sequence:number; [key:string]:any }
export class ApiError extends Error { constructor(public code:string, message:string, public status:number, public requestId?:string){ super(message) } }

let accessToken='',expiresAt=0,currentAuth:AuthResult|null=null,refreshing:Promise<AuthResult>|null=null
const channel=typeof BroadcastChannel==='undefined'?null:new BroadcastChannel('rag-auth')
channel?.addEventListener('message',event=>{if(event.data?.type==='token')setToken(event.data.result)})
function setToken(result:AuthResult|null){currentAuth=result;accessToken=result?.access_token||'';expiresAt=result?Date.now()+result.expires_in*1000:0}
async function parse<T>(response:Response):Promise<T>{if(response.status===204)return undefined as T;const body=await response.json().catch(()=>null);if(!response.ok)throw new ApiError(body?.error?.code||'REQUEST_FAILED',body?.error?.message||'请求失败',response.status,body?.error?.request_id);return body}
async function refresh():Promise<AuthResult>{
  if(refreshing)return refreshing
  const job=async()=>{if(currentAuth&&expiresAt>Date.now()+30_000)return currentAuth;const response=await fetch('/api/v2/auth/refresh',{method:'POST',credentials:'include'});const result=await parse<AuthResult>(response);setToken(result);channel?.postMessage({type:'token',result});return result}
  refreshing=(async()=>{const locks=(navigator as any).locks;return locks?locks.request('rag-auth-refresh',job):job()})().catch(error=>{setToken(null);throw error}).finally(()=>refreshing=null)
  return refreshing
}
async function request<T>(path:string,init:RequestInit={},retry=true):Promise<T>{
  const headers:Record<string,string>={...(init.body instanceof FormData?{}:{'Content-Type':'application/json'}),...(init.headers as Record<string,string>||{})}
  if(accessToken)headers.Authorization=`Bearer ${accessToken}`
  const response=await fetch(path,{...init,headers,credentials:'same-origin'})
  if(response.status===401&&retry&&!path.startsWith('/api/v2/auth/')){await refresh();return request<T>(path,init,false)}
  return parse<T>(response)
}
async function authRequest(path:string,username:string,password:string,adminCode?:string){const result=await request<AuthResult>(path,{method:'POST',body:JSON.stringify({username,password,...(adminCode?{admin_code:adminCode}:{})})},false);setToken(result);channel?.postMessage({type:'token',result});return result}
async function streamRequest(path:string,body:any,onEvent:(event:string,data:StreamEvent)=>void,retry=true):Promise<StreamEvent>{
  const response=await fetch(path,{method:'POST',headers:{Authorization:`Bearer ${accessToken}`,'Content-Type':'application/json','Accept':'text/event-stream'},body:JSON.stringify(body)})
  if(response.status===401&&retry){await refresh();return streamRequest(path,body,onEvent,false)}
  if(!response.ok)await parse(response);if(!response.body)throw new ApiError('STREAM_UNAVAILABLE','浏览器无法读取流式响应',0)
  return readEventStream(response,onEvent,{sequence:0})
}
async function upload(file:File,onProgress:(n:number)=>void,retry=true):Promise<DocumentItem>{return new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest(),data=new FormData();data.append('file',file);xhr.open('POST','/api/v2/documents');xhr.setRequestHeader('Authorization',`Bearer ${accessToken}`);xhr.upload.onprogress=e=>e.lengthComputable&&onProgress(e.loaded/e.total);xhr.onload=async()=>{if(xhr.status===401&&retry){try{await refresh();resolve(await upload(file,onProgress,false))}catch(e){reject(e)}return}let body:any;try{body=JSON.parse(xhr.responseText)}catch{};xhr.status<300?resolve(body):reject(new ApiError(body?.error?.code||'UPLOAD_FAILED',body?.error?.message||'上传失败',xhr.status))};xhr.onerror=()=>reject(new ApiError('NETWORK_ERROR','网络连接失败',0));xhr.send(data)})}
export type RunWaiting={question:string;checkpoint_revision:number;resume_token:string;expires_at:string;sequence:number}
export type RunInfo={run_id:string;session_id:string;state:'queued'|'running'|'waiting'|'cancelling'|'cancelled'|'succeeded'|'failed'|'interrupted';sequence:number;message_id:string|null;waiting:RunWaiting|null}
async function resumeRun(runId:string,onEvent:(event:string,data:StreamEvent)=>void,after=0){
  const cursor:StreamCursor={runId,sequence:after}
  for(let attempt=0;;attempt++){
    try{
      let response=await fetch(`/api/v2/runs/${runId}/events`,{headers:{Authorization:`Bearer ${accessToken}`,'Last-Event-ID':`${runId}:${cursor.sequence}`},cache:'no-store'})
      if(response.status===401){await refresh();response=await fetch(`/api/v2/runs/${runId}/events`,{headers:{Authorization:`Bearer ${accessToken}`,'Last-Event-ID':`${runId}:${cursor.sequence}`},cache:'no-store'})}
      if(!response.ok)await parse(response)
      return await readEventStream(response,onEvent,cursor)
    }catch(error:any){
      if(cursor.failed||attempt>=3||(error.status&&error.status<500))throw error
      await new Promise(resolve=>setTimeout(resolve,500*2**attempt))
    }
  }
}
async function durableChat(id:string,body:any,onEvent:(event:string,data:StreamEvent)=>void,onRun?:(run:RunInfo)=>void){
  const key=crypto.randomUUID();let run:RunInfo
  for(let attempt=0;;attempt++){
    try{run=await request<RunInfo>(`/api/v2/sessions/${id}/runs`,{method:'POST',headers:{'Idempotency-Key':key},body:JSON.stringify(body)});break}
    catch(error:any){if(attempt>=3||(error.status&&error.status<500&&error.status!==429))throw error;await new Promise(resolve=>setTimeout(resolve,500*2**attempt))}
  }
  onRun?.(run)
  return resumeRun(run.run_id,onEvent)
}
export const api={
  modelCatalog:()=>request<ModelCatalogInfo>('/api/v2/model-center/catalog'),
  saveModelCatalog:(expected_revision:number,catalog:ModelCatalog)=>request<ModelCatalogInfo>('/api/v2/model-center/catalog',{method:'PUT',body:JSON.stringify({expected_revision,catalog})}),
  probeProfile:(id:string)=>request<ModelCatalogInfo>(`/api/v2/model-center/catalog/profiles/${id}/probe`,{method:'POST'}),
  modelVersions:(offset=0)=>request<ModelVersionPage>(`/api/v2/model-center/versions?offset=${offset}&limit=10`),
  modelCenter:()=>request<ModelCenterInfo>('/api/v2/model-center'),saveModelConfiguration:(expected_revision:number,configuration:ModelConfiguration)=>request<ModelCenterInfo>('/api/v2/model-center',{method:'PUT',body:JSON.stringify({expected_revision,configuration})}),probeModel:(provider:string)=>request<ModelCenterInfo>(`/api/v2/model-center/probe/${provider}`,{method:'POST',body:'{}'}),modelStatistics:(days:number,globalScope=false)=>request<ModelStatistics>(`/api/v2/model-center/statistics?days=${days}&global_scope=${globalScope}`),
  adminOverview:()=>request<{features:Record<string,boolean>;ingestion_mode:string;notice:string}>('/api/v2/admin/overview'),
  adminPreflight:()=>request<PreflightReport>('/api/v2/admin/preflight'),
  organizations:()=>request<Organization[]>('/api/v2/organizations'),createOrganization:(name:string)=>request<Organization>('/api/v2/organizations',{method:'POST',body:JSON.stringify({name})}),deleteOrganization:(id:string)=>request<void>(`/api/v2/organizations/${id}`,{method:'DELETE'}),
  members:(id:string)=>request<Member[]>(`/api/v2/organizations/${id}/members`),setMember:(id:string,username:string,role:string)=>request<void>(`/api/v2/organizations/${id}/members`,{method:'PUT',body:JSON.stringify({username,role})}),removeMember:(id:string,user:string)=>request<void>(`/api/v2/organizations/${id}/members/${user}`,{method:'DELETE'}),
  knowledgeBases:()=>request<KnowledgeBase[]>('/api/v2/knowledge-bases'),createBase:(id:string,name:string)=>request<KnowledgeBase>(`/api/v2/organizations/${id}/knowledge-bases`,{method:'POST',body:JSON.stringify({name})}),deleteBase:(id:string)=>request<void>(`/api/v2/knowledge-bases/${id}`,{method:'DELETE'}),
  grants:(id:string)=>request<Member[]>(`/api/v2/knowledge-bases/${id}/grants`),setGrant:(id:string,user:string,role:string)=>request<void>(`/api/v2/knowledge-bases/${id}/grants/${user}`,{method:'PUT',body:JSON.stringify({role})}),revokeGrant:(id:string,user:string)=>request<void>(`/api/v2/knowledge-bases/${id}/grants/${user}`,{method:'DELETE'}),
  sharedDocuments:(id:string)=>request<SharedDocument[]>(`/api/v2/knowledge-bases/${id}/documents`),publish:(id:string,document:string)=>request<void>(`/api/v2/knowledge-bases/${id}/documents/${document}`,{method:'PUT',body:'{}'}),unpublish:(id:string,document:string)=>request<void>(`/api/v2/knowledge-bases/${id}/documents/${document}`,{method:'DELETE'}),
  resumeRun,runs:(id:string)=>request<RunInfo[]>(`/api/v2/sessions/${id}/runs`),
  run:(id:string)=>request<RunInfo>(`/api/v2/runs/${id}`),
  cancelRun:(id:string)=>request<RunInfo>(`/api/v2/runs/${id}/cancel`,{method:'POST'}),
  continueRun:(id:string,waiting:RunWaiting,supplement:string)=>request<RunInfo>(`/api/v2/runs/${id}/resume`,{method:'POST',body:JSON.stringify({checkpoint_revision:waiting.checkpoint_revision,resume_token:waiting.resume_token,supplement})}),

  versions:(id:string,offset=0)=>request<{items:DocumentItem[];total:number}>(`/api/v2/documents/${id}/versions?offset=${offset}&limit=20`),
  uploadVersion:(id:string,file:File)=>{const body=new FormData();body.append('file',file);return request<DocumentItem>(`/api/v2/documents/${id}/versions`,{method:'POST',body})},
  activateVersion:(id:string,expected:string|null)=>request<DocumentItem>(`/api/v2/documents/${id}/activate`,{method:'POST',body:JSON.stringify({expected_current_document_id:expected})}),
  refresh,login:(u:string,p:string)=>authRequest('/api/v2/auth/login',u,p),register:(u:string,p:string,code?:string)=>authRequest('/api/v2/auth/register',u,p,code),me:()=>request<User>('/api/v2/auth/me'),logout:async()=>{try{if(!accessToken||expiresAt<=Date.now())await refresh();await request('/api/v2/auth/logout',{method:'POST'},false)}finally{setToken(null);channel?.postMessage({type:'token',result:null})}},
  capabilities:()=>request<Capabilities>('/api/v2/capabilities'),sessions:(q='')=>request<Session[]>(`/api/v2/sessions?limit=100&q=${encodeURIComponent(q)}`),createSession:(title:string)=>request<Session>('/api/v2/sessions',{method:'POST',body:JSON.stringify({title})}),session:(id:string)=>request<Session&{messages:Message[]}>(`/api/v2/sessions/${id}?message_limit=100`),renameSession:(id:string,title:string)=>request<Session>(`/api/v2/sessions/${id}`,{method:'PATCH',body:JSON.stringify({title})}),deleteSession:(id:string)=>request<void>(`/api/v2/sessions/${id}`,{method:'DELETE'}),chatStream:(id:string,body:any,onEvent:(event:string,data:StreamEvent)=>void,durable=false,onRun?:(run:RunInfo)=>void)=>durable?durableChat(id,body,onEvent,onRun):streamRequest(`/api/v2/sessions/${id}/messages/stream`,body,onEvent),
  documents:(q='',status='')=>request<{items:DocumentItem[];total:number}>(`/api/v2/documents?limit=100&q=${encodeURIComponent(q)}${status?`&status=${status}`:''}`),document:(id:string)=>request<DocumentItem>(`/api/v2/documents/${id}`),retry:(id:string)=>request<DocumentItem>(`/api/v2/documents/${id}/retry`,{method:'POST',body:'{}'}),deleteDocument:(id:string)=>request<void>(`/api/v2/documents/${id}`,{method:'DELETE'}),estimate:(body:any)=>request<any>('/api/v2/context-estimate',{method:'POST',body:JSON.stringify(body)}),compact:(id:string,version:string,provider:string,profileId?:string)=>request<any>(`/api/v2/sessions/${id}/compact`,{method:'POST',body:JSON.stringify({expected_context_version:version,...(profileId?{model_profile_id:profileId}:{model_provider:provider})})}),upload,
  memories:()=>request<MemoryItem[]>('/api/v2/memories'),createMemory:(content:string)=>request<MemoryItem>('/api/v2/memories',{method:'POST',body:JSON.stringify({content})}),updateMemory:(id:string,content:string)=>request<MemoryItem>(`/api/v2/memories/${id}`,{method:'PATCH',body:JSON.stringify({content})}),deleteMemory:(id:string)=>request<void>(`/api/v2/memories/${id}`,{method:'DELETE'}),retryMemory:(id:string)=>request<MemoryItem>(`/api/v2/memories/${id}/retry`,{method:'POST',body:'{}'}),
}
