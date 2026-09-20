import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api, type Capabilities, type DocumentItem, type Message, type Session } from './api'
import { createUploadQueue } from './uploads'

export const useAppStore=defineStore('app',()=>{
  const userId=ref(localStorage.getItem('zhizhan-user')||'demo-user'),generation=ref(0)
  const sessions=ref<Session[]>([]),documents=ref<DocumentItem[]>([]),messages=ref<Message[]>([])
  const currentSession=ref<Session|null>(null),capabilities=ref<Capabilities|null>(null),documentTotal=ref(0)
  const busy=ref(false),error=ref(''),selectedDocuments=ref<string[]>([])
  let errorTimer:number|undefined
  function notify(message:string){error.value=message;window.clearTimeout(errorTimer);errorTimer=window.setTimeout(()=>{if(error.value===message)error.value=''},8000)}
  const uploads=createUploadQueue(()=>userId.value,()=>loadDocuments(),notify,()=>({allowed:capabilities.value?.upload.allowed_extensions||['.txt','.md'],max:capabilities.value?.upload.max_file_bytes||20*1024*1024,warning:capabilities.value?.upload.long_running_warning_seconds||900}))
  async function guard<T>(job:()=>Promise<T>){const g=generation.value;try{error.value='';const result=await job();return generation.value===g?result:undefined}catch(e:any){if(generation.value===g)error.value=e.message;throw e}}
  async function initialize(){await guard(async()=>{capabilities.value=await api.capabilities(userId.value);await Promise.all([loadSessions(),loadDocuments()])})}
  function switchUser(value:string){userId.value=value.trim()||'demo-user';localStorage.setItem('zhizhan-user',userId.value);generation.value++;sessions.value=[];documents.value=[];messages.value=[];currentSession.value=null;selectedDocuments.value=[];initialize()}
  async function loadSessions(q=''){const value=await api.sessions(userId.value,q);sessions.value=value;return value}
  async function loadSession(id:string){const value=await guard(()=>api.session(userId.value,id));if(value){currentSession.value=value;messages.value=value.messages}return value}
  async function createSession(title='新对话'){const value=await guard(()=>api.createSession(userId.value,title));if(value){sessions.value.unshift(value);currentSession.value=value;messages.value=[]}return value}
  async function send(id:string,body:any){busy.value=true;try{const value=await guard(()=>api.chat(userId.value,id,body));if(value)await loadSession(id);await loadSessions();return value}finally{busy.value=false}}
  async function loadDocuments(q='',status=''){const value=await api.documents(userId.value,q,status);documents.value=value.items;documentTotal.value=value.total;selectedDocuments.value=selectedDocuments.value.filter(id=>value.items.some(d=>d.id===id&&d.can_query));uploads.recover(value.items)}
  async function removeDocuments(ids:string[]){await Promise.allSettled(ids.map(id=>api.deleteDocument(userId.value,id)));await loadDocuments()}
  async function retryDocument(id:string){await api.retry(userId.value,id);await loadDocuments()}
  async function renameSession(id:string,title:string){const value=await api.renameSession(userId.value,id,title);sessions.value=sessions.value.map(x=>x.id===id?value:x);if(currentSession.value?.id===id)currentSession.value=value}
  const readyDocuments=computed(()=>documents.value.filter(x=>x.can_query))
  return {userId,sessions,documents,messages,currentSession,capabilities,documentTotal,busy,error,selectedDocuments,uploads,readyDocuments,initialize,switchUser,loadSessions,loadSession,createSession,send,loadDocuments,removeDocuments,retryDocument,renameSession}
})
