import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api, type KnowledgeBase, type Capabilities, type DocumentItem, type MemoryItem, type Message, type Session, type User } from './api'
import { createUploadQueue } from './uploads'
import { agentLabel,statusLabel } from './agents'

export const useAppStore=defineStore('app',()=>{
  const user=ref<User|null>(null),bootstrapped=ref(false)
  const sessions=ref<Session[]>([]),documents=ref<DocumentItem[]>([]),memories=ref<MemoryItem[]>([]),capabilities=ref<Capabilities|null>(null),documentTotal=ref(0)
  const messagesBySession=ref<Record<string,Message[]>>({}),runsBySession=ref<Record<string,{busy:boolean;status:string;error:string;attempt:number}>>({})
  const knowledgeBases=ref<KnowledgeBase[]>([]),selectedBases=ref<string[]>([])
  const error=ref(''),selectedDocuments=ref<string[]>([]),loadAttempts:Record<string,number>={}
  let errorTimer:number|undefined
  function notify(message:string){error.value=message;window.clearTimeout(errorTimer);errorTimer=window.setTimeout(()=>{if(error.value===message)error.value=''},8000)}
  const uploads=createUploadQueue(()=>loadDocuments(),notify,()=>({allowed:capabilities.value?.upload.allowed_extensions||['.txt','.md'],max:capabilities.value?.upload.max_file_bytes||20*1024*1024,warning:capabilities.value?.upload.long_running_warning_seconds||900}))
  async function guard<T>(job:()=>Promise<T>){try{error.value='';return await job()}catch(e:any){error.value=e.message;throw e}}
  async function bootstrap(){try{const auth=await api.refresh();user.value=auth.user;await initialize()}catch{user.value=null}finally{bootstrapped.value=true}}
  async function initialize(){capabilities.value=await api.capabilities();await Promise.all([loadSessions(),loadDocuments(),loadMemories(),loadKnowledgeBases()])}
  async function login(username:string,password:string){const result=await guard(()=>api.login(username,password));user.value=result.user;await initialize()}
  async function register(username:string,password:string){const result=await guard(()=>api.register(username,password));user.value=result.user;await initialize()}
  async function logout(){await api.logout().catch(()=>{});user.value=null;sessions.value=[];documents.value=[];memories.value=[];messagesBySession.value={};runsBySession.value={};knowledgeBases.value=[];selectedBases.value=[]}
  function messagesFor(id:string){return messagesBySession.value[id]||(messagesBySession.value[id]=[])}
  function runFor(id:string){return runsBySession.value[id]||(runsBySession.value[id]={busy:false,status:'',error:'',attempt:0})}
  async function loadSessions(q=''){const value=await api.sessions(q);sessions.value=value;return value}
  async function loadSession(id:string,replaceWhileBusy=false){
    const attempt=loadAttempts[id]=(loadAttempts[id]||0)+1,value=await guard(()=>api.session(id))
    if(loadAttempts[id]===attempt&&(replaceWhileBusy||!runFor(id).busy))messagesBySession.value[id]=value.messages
    if(!replaceWhileBusy&&!runFor(id).busy&&capabilities.value?.features.durable_runs){
      const recent=await api.runs(id)
      if(loadAttempts[id]===attempt&&!runFor(id).busy){
        const latest=recent[0]
        if(latest?.state==='running')void recoverRun(id,latest.run_id)
        else if(latest?.state==='succeeded'&&latest.message_id&&!messagesFor(id).some(message=>message.id===latest.message_id))messagesBySession.value[id]=(await api.session(id)).messages
        else if(latest?.state==='interrupted')runFor(id).error='上次问答已中断，请查看历史后重新提问'
        else if(latest?.state==='failed')runFor(id).error='上次问答失败，请检查配置或重新提问'
      }
    }
    return value
  }
  async function createSession(title='新对话'){const value=await guard(()=>api.createSession(title));sessions.value.unshift(value);messagesBySession.value[value.id]=[];runFor(value.id);return value}
  function streamInto(id:string,assistantId:string,attempt:number){return(event:string,data:any)=>{
    const run=runFor(id);if(run.attempt!==attempt)return
    const assistant=messagesFor(id).find(message=>message.id===assistantId);if(!assistant)return
    if(event==='answer_start')assistant.content=''
    else if(event==='answer_delta')assistant.content+=data.delta||''
    else if(['agent_started','agent_finished','agent_failed'].includes(event)){
      const metadata=assistant.run_metadata||(assistant.run_metadata={agent_events:[]})
      metadata.agent_events.push({agent:data.agent,status:data.status,error_code:data.error_code})
      run.status=`${agentLabel(data.agent)} · ${statusLabel(data.status)}`
    }else if(event==='done')Object.assign(assistant,{content:data.answer,model_provider:data.model_provider,model_name:data.model_name,citations:data.citations||[],token_usage:data.token_usage,run_metadata:data.run_metadata})
    run.status=event==='run_started'?'Supervisor 正在识别与分发任务':event==='answer_delta'?'正在生成回答':event==='context_usage'?'正在汇总上下文':event==='usage'?'正在保存回答':run.status
  }}
  async function recoverRun(id:string,runId:string){
    const run=runFor(id);if(run.busy)return
    run.busy=true;run.error='';run.status='正在恢复回答';const attempt=++run.attempt,assistantId=`replay-${runId}`
    messagesFor(id).push({id:assistantId,role:'assistant',content:'',model_provider:null,model_name:null,citations:[],token_usage:null,run_metadata:null,created_at:new Date().toISOString()})
    try{await api.resumeRun(runId,streamInto(id,assistantId,attempt));await loadSession(id,true)}
    catch(error:any){run.error=error.message;await loadSession(id,true).catch(()=>{})}
    finally{if(run.attempt===attempt){run.busy=false;run.status=''}}
  }
  async function send(id:string,body:any){
    const run=runFor(id);if(run.busy)return
    run.busy=true;run.status='正在连接';run.error=''
    const attempt=++run.attempt,now=new Date().toISOString(),key=crypto.randomUUID(),assistantId=`pending-assistant-${key}`
    messagesFor(id).push({id:`pending-user-${key}`,role:'user',content:body.question,model_provider:null,model_name:null,citations:[],token_usage:null,run_metadata:null,created_at:now},{id:assistantId,role:'assistant',content:'',model_provider:null,model_name:null,citations:[],token_usage:null,run_metadata:null,created_at:now})
    try{
      const value=await api.chatStream(id,body,streamInto(id,assistantId,attempt),capabilities.value?.features.durable_runs===true)
      if(run.attempt===attempt){await loadSession(id,true);await Promise.all([loadSessions(),loadMemories()])}
      return value
    }catch(caught:any){if(run.attempt===attempt){run.error=caught?.message||'请求失败';await loadSession(id,true).catch(()=>{})}throw caught}
    finally{if(run.attempt===attempt){run.busy=false;run.status=''}}
  }
  async function loadKnowledgeBases(){knowledgeBases.value=capabilities.value?.features.sharing?await api.knowledgeBases():[];selectedBases.value=selectedBases.value.filter(id=>knowledgeBases.value.some(base=>base.id===id))}
  async function loadDocuments(q='',status=''){const value=await api.documents(q,status);documents.value=value.items;documentTotal.value=value.total;selectedDocuments.value=selectedDocuments.value.filter(id=>value.items.some(d=>d.id===id&&d.can_query));uploads.recover(value.items)}
  async function removeDocuments(ids:string[]){await Promise.allSettled(ids.map(id=>api.deleteDocument(id)));await loadDocuments()}
  async function retryDocument(id:string){await api.retry(id);await loadDocuments()}
  async function renameSession(id:string,title:string){const value=await api.renameSession(id,title);sessions.value=sessions.value.map(x=>x.id===id?value:x)}
  async function loadMemories(){memories.value=await api.memories()}
  async function createMemory(content:string){await api.createMemory(content);await loadMemories()}
  async function updateMemory(id:string,content:string){await api.updateMemory(id,content);await loadMemories()}
  async function deleteMemory(id:string){await api.deleteMemory(id);await loadMemories()}
  async function retryMemory(id:string){await api.retryMemory(id);await loadMemories()}
  const readyDocuments=computed(()=>documents.value.filter(x=>x.can_query))
  return {knowledgeBases,selectedBases,loadKnowledgeBases,user,bootstrapped,sessions,documents,memories,capabilities,documentTotal,error,selectedDocuments,uploads,readyDocuments,bootstrap,initialize,login,register,logout,messagesFor,runFor,loadSessions,loadSession,createSession,send,loadDocuments,removeDocuments,retryDocument,renameSession,loadMemories,createMemory,updateMemory,deleteMemory,retryMemory}
})
