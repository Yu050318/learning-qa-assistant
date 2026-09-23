import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api, type Capabilities, type DocumentItem, type Message, type Session } from './api'
import { createUploadQueue } from './uploads'

export const useAppStore=defineStore('app',()=>{
  const userId=ref(localStorage.getItem('zhizhan-user')||'demo-user'),generation=ref(0)
  const sessions=ref<Session[]>([]),documents=ref<DocumentItem[]>([]),capabilities=ref<Capabilities|null>(null),documentTotal=ref(0)
  const messagesBySession=ref<Record<string,Message[]>>({}),runsBySession=ref<Record<string,{busy:boolean;status:string;error:string;attempt:number}>>({})
  const error=ref(''),selectedDocuments=ref<string[]>([])
  const loadAttempts:Record<string,number>={}
  let errorTimer:number|undefined
  function notify(message:string){error.value=message;window.clearTimeout(errorTimer);errorTimer=window.setTimeout(()=>{if(error.value===message)error.value=''},8000)}
  const uploads=createUploadQueue(()=>userId.value,()=>loadDocuments(),notify,()=>({allowed:capabilities.value?.upload.allowed_extensions||['.txt','.md'],max:capabilities.value?.upload.max_file_bytes||20*1024*1024,warning:capabilities.value?.upload.long_running_warning_seconds||900}))
  async function guard<T>(job:()=>Promise<T>){const g=generation.value;try{error.value='';const result=await job();return generation.value===g?result:undefined}catch(e:any){if(generation.value===g)error.value=e.message;throw e}}
  async function initialize(){await guard(async()=>{capabilities.value=await api.capabilities(userId.value);await Promise.all([loadSessions(),loadDocuments()])})}
  function messagesFor(id:string){return messagesBySession.value[id]||(messagesBySession.value[id]=[])}
  function runFor(id:string){return runsBySession.value[id]||(runsBySession.value[id]={busy:false,status:'',error:'',attempt:0})}
  function switchUser(value:string){userId.value=value.trim()||'demo-user';localStorage.setItem('zhizhan-user',userId.value);generation.value++;sessions.value=[];documents.value=[];messagesBySession.value={};runsBySession.value={};selectedDocuments.value=[];initialize()}
  async function loadSessions(q=''){const value=await api.sessions(userId.value,q);sessions.value=value;return value}
  async function loadSession(id:string,replaceWhileBusy=false){const g=generation.value,attempt=loadAttempts[id]=(loadAttempts[id]||0)+1;const value=await guard(()=>api.session(userId.value,id));if(value&&generation.value===g&&loadAttempts[id]===attempt&&(replaceWhileBusy||!runFor(id).busy))messagesBySession.value[id]=value.messages;return value}
  async function createSession(title='新对话'){const value=await guard(()=>api.createSession(userId.value,title));if(value){sessions.value.unshift(value);messagesBySession.value[value.id]=[];runFor(value.id)}return value}
  async function send(id:string,body:any){const run=runFor(id);if(run.busy)return;run.busy=true;run.status='正在连接';run.error='';const attempt=++run.attempt,g=generation.value,now=new Date().toISOString(),key=crypto.randomUUID(),assistantId=`pending-assistant-${key}`,messages=messagesFor(id);messages.push({id:`pending-user-${key}`,role:'user',content:body.question,model_provider:null,model_name:null,citations:[],token_usage:null,run_metadata:null,created_at:now},{id:assistantId,role:'assistant',content:'',model_provider:null,model_name:null,citations:[],token_usage:null,run_metadata:null,created_at:now});try{const value=await api.chatStream(userId.value,id,body,(event,data)=>{if(generation.value!==g||run.attempt!==attempt)return;const assistant=messages.find(message=>message.id===assistantId);if(!assistant)return;if(event==='answer_start')assistant.content='';else if(event==='answer_delta')assistant.content+=data.delta||'';else if(event==='done'){assistant.content=data.answer;assistant.model_provider=data.model_provider;assistant.model_name=data.model_name;assistant.citations=data.citations||[];assistant.token_usage=data.token_usage;assistant.run_metadata=data.run_metadata}run.status=event==='run_started'?'正在检索并生成回答':event==='answer_delta'?'正在生成回答':event==='context_usage'?'正在汇总上下文':event==='usage'?'正在保存回答':run.status});if(generation.value===g&&run.attempt===attempt){await loadSession(id,true);await loadSessions()}return value}catch(caught:any){if(generation.value===g&&run.attempt===attempt){run.error=caught?.message||'请求失败';await loadSession(id,true).catch(()=>{})}throw caught}finally{if(generation.value===g&&run.attempt===attempt){run.busy=false;run.status=''}}}
  async function loadDocuments(q='',status=''){const value=await api.documents(userId.value,q,status);documents.value=value.items;documentTotal.value=value.total;selectedDocuments.value=selectedDocuments.value.filter(id=>value.items.some(d=>d.id===id&&d.can_query));uploads.recover(value.items)}
  async function removeDocuments(ids:string[]){await Promise.allSettled(ids.map(id=>api.deleteDocument(userId.value,id)));await loadDocuments()}
  async function retryDocument(id:string){await api.retry(userId.value,id);await loadDocuments()}
  async function renameSession(id:string,title:string){const value=await api.renameSession(userId.value,id,title);sessions.value=sessions.value.map(x=>x.id===id?value:x)}
  const readyDocuments=computed(()=>documents.value.filter(x=>x.can_query))
  return {userId,sessions,documents,capabilities,documentTotal,error,selectedDocuments,uploads,readyDocuments,messagesFor,runFor,initialize,switchUser,loadSessions,loadSession,createSession,send,loadDocuments,removeDocuments,retryDocument,renameSession}
})
