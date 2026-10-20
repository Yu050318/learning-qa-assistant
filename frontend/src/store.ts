import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api, type Workspace, type RunWaiting, type KnowledgeBase, type Capabilities, type DocumentItem, type MemoryItem, type Message, type Session, type User } from './api'
import { createUploadQueue } from './uploads'
import { agentLabel,statusLabel } from './agents'

export const useAppStore=defineStore('app',()=>{
  const user=ref<User|null>(null),bootstrapped=ref(false)
  const sessions=ref<Session[]>([]),documents=ref<DocumentItem[]>([]),memories=ref<MemoryItem[]>([]),capabilities=ref<Capabilities|null>(null),documentTotal=ref(0)
  const messagesBySession=ref<Record<string,Message[]>>({}),runsBySession=ref<Record<string,{busy:boolean;status:string;error:string;attempt:number;runId:string|null;waiting:RunWaiting|null;cancelPending:boolean}>>({})
  const knowledgeBases=ref<KnowledgeBase[]>([]),selectedBases=ref<string[]>([])
  const workspaces=ref<Workspace[]>([]),organizationId=ref<string|null>(null),workspaceChanging=ref(false)
  const activeWorkspace=computed(()=>workspaces.value.find(item=>item.id===organizationId.value)||{id:null,kind:'personal',name:'个人空间',role:'owner',session_count:0})
  let workspaceEpoch=0,documentsAttempt=0,sessionsAttempt=0
  const error=ref(''),selectedDocuments=ref<string[]>([]),loadAttempts:Record<string,number>={}
  let errorTimer:number|undefined
  function notify(message:string){error.value=message;window.clearTimeout(errorTimer);errorTimer=window.setTimeout(()=>{if(error.value===message)error.value=''},8000)}
  const uploads=createUploadQueue(()=>{void loadDocuments().catch(()=>{})},notify,()=>({allowed:capabilities.value?.upload.allowed_extensions||['.txt','.md'],max:capabilities.value?.upload.max_file_bytes||20*1024*1024,warning:capabilities.value?.upload.long_running_warning_seconds||900}),()=>organizationId.value)
  async function guard<T>(job:()=>Promise<T>){try{error.value='';return await job()}catch(e:any){error.value=e.message;throw e}}
  let bootstrapPromise:Promise<void>|undefined
  function bootstrap(){return bootstrapPromise ||= restoreAuthentication()}
  async function restoreAuthentication(){try{const auth=await api.refresh();user.value=auth.user;await initialize()}catch{user.value=null}finally{bootstrapped.value=true}}
  function clearWorkspace(){sessions.value=[];documents.value=[];documentTotal.value=0;messagesBySession.value={};runsBySession.value={};knowledgeBases.value=[];selectedBases.value=[];selectedDocuments.value=[]}
  function rememberWorkspace(){try{if(user.value)sessionStorage.setItem(`rag-workspace:${user.value.id}`,organizationId.value||'')}catch{}}
  async function loadWorkspaces(){
    if(!capabilities.value?.features.workspaces){workspaces.value=[];return}
    const epoch=workspaceEpoch,rows=await api.workspaces();if(epoch!==workspaceEpoch)return
    workspaces.value=rows
    if(organizationId.value&&!rows.some(row=>row.id===organizationId.value)){await switchWorkspace(null);notify('原团队空间已不可访问，已返回个人空间')}
  }
  async function switchWorkspace(id:string|null){
    if(id===organizationId.value&&!workspaceChanging.value)return
    if(id!==null&&!workspaces.value.some(item=>item.id===id))throw new Error('空间不可访问，请刷新空间列表')
    const epoch=++workspaceEpoch;workspaceChanging.value=true;organizationId.value=id;clearWorkspace();error.value='';rememberWorkspace()
    try{await Promise.all([loadSessions(),loadDocuments(),loadKnowledgeBases()])}
    catch(e:any){if(epoch===workspaceEpoch){clearWorkspace();error.value=e.message}throw e}
    finally{if(epoch===workspaceEpoch)workspaceChanging.value=false}
  }
  async function initialize(){
    ++workspaceEpoch;clearWorkspace();organizationId.value=null;capabilities.value=await api.capabilities();await loadWorkspaces()
    try{const saved=user.value?sessionStorage.getItem(`rag-workspace:${user.value.id}`):null;if(saved&&workspaces.value.some(item=>item.id===saved))organizationId.value=saved}catch{}
    await Promise.all([loadSessions(),loadDocuments(),loadMemories(),loadKnowledgeBases()])
  }
  async function login(username:string,password:string){const result=await guard(()=>api.login(username,password));user.value=result.user;await initialize()}
  async function register(username:string,password:string,code?:string){const result=await guard(()=>api.register(username,password,code));user.value=result.user;await initialize()}
  async function logout(){++workspaceEpoch;uploads.reset();clearWorkspace();organizationId.value=null;workspaces.value=[];workspaceChanging.value=false;await api.logout().catch(()=>{});user.value=null;memories.value=[]}
  function messagesFor(id:string){return messagesBySession.value[id]||(messagesBySession.value[id]=[])}
  function runFor(id:string){return runsBySession.value[id]||(runsBySession.value[id]={busy:false,status:'',error:'',attempt:0,runId:null,waiting:null,cancelPending:false})}
  async function loadSessions(q=''){const epoch=workspaceEpoch,attempt=++sessionsAttempt,value=await api.sessions(q,organizationId.value);if(epoch===workspaceEpoch&&attempt===sessionsAttempt)sessions.value=value;return value}
  async function loadSession(id:string,replaceWhileBusy=false){
    const epoch=workspaceEpoch,scope=organizationId.value,attempt=loadAttempts[id]=(loadAttempts[id]||0)+1,value=await guard(()=>api.session(id))
    if(epoch!==workspaceEpoch)return value
    if((value.organization_id??null)!==scope)throw new Error('该会话属于其他空间，请切换空间后查看')
    if(loadAttempts[id]===attempt&&(replaceWhileBusy||!runFor(id).busy))messagesBySession.value[id]=value.messages
    if(!replaceWhileBusy&&!runFor(id).busy&&capabilities.value?.features.durable_runs){
      const recent=await api.runs(id)
      if(epoch!==workspaceEpoch)return value
      if(loadAttempts[id]===attempt&&!runFor(id).busy){
        const latest=recent[0]
        if(latest){runFor(id).runId=latest.run_id;runFor(id).waiting=latest.waiting}
        if(latest&&['queued','running','cancelling'].includes(latest.state))void recoverRun(id,latest.run_id)
        else if(latest?.state==='succeeded'&&latest.message_id&&!messagesFor(id).some(message=>message.id===latest.message_id)){const restored=await api.session(id);if(epoch===workspaceEpoch)messagesBySession.value[id]=restored.messages}
        else if(latest?.state==='interrupted')runFor(id).error='上次问答已中断，请查看历史后重新提问'
        else if(latest?.state==='failed')runFor(id).error='上次问答失败，请检查配置或重新提问'
        else if(latest?.state==='cancelled')runFor(id).error='本轮问答已取消'
      }
    }
    return value
  }
  async function createSession(title='新对话'){const epoch=workspaceEpoch,value=await guard(()=>api.createSession(title,organizationId.value));if(epoch!==workspaceEpoch)return;sessions.value.unshift(value);messagesBySession.value[value.id]=[];runFor(value.id);activeWorkspace.value.session_count++;return value}
  function streamInto(id:string,assistantId:string,attempt:number){const epoch=workspaceEpoch;return(event:string,data:any)=>{
    if(epoch!==workspaceEpoch)return
    const run=runFor(id);if(run.attempt!==attempt)return
    run.runId=data.run_id
    const assistant=messagesFor(id).find(message=>message.id===assistantId);if(!assistant)return
    if(event==='answer_start')assistant.content=''
    else if(event==='answer_delta')assistant.content+=data.delta||''
    else if(['agent_started','agent_finished','agent_failed'].includes(event)){
      const metadata=assistant.run_metadata||(assistant.run_metadata={})
      metadata.agent_events||=[]
      metadata.agent_events.push({agent:data.agent,status:data.status,error_code:data.error_code})
      run.status=`${agentLabel(data.agent)} · ${statusLabel(data.status)}`
    }else if(event==='done'){run.waiting=null;Object.assign(assistant,{content:data.answer,model_provider:data.model_provider,model_name:data.model_name,citations:data.citations||[],token_usage:data.token_usage,run_metadata:data.run_metadata})}
    else if(event==='run_waiting'&&data.is_current!==false){run.waiting=data;assistant.content=data.question;run.status='等待补充信息'}
    else if(event==='run_cancelled'){run.waiting=null;run.error='本轮问答已取消'}
    else if(event==='cancel_requested'){run.status='正在停止执行';run.waiting=null}
    else if(event==='run_claimed')run.status='正在执行任务'
    run.status=event==='run_started'?'Supervisor 正在识别与分发任务':event==='answer_delta'?'正在生成回答':event==='context_usage'?'正在汇总上下文':event==='usage'?'正在保存回答':run.status
  }}
  async function recoverRun(id:string,runId:string,after=0){
    const epoch=workspaceEpoch
    const run=runFor(id);if(run.busy)return
    run.busy=true;run.error='';run.runId=runId;run.status='正在恢复回答';const attempt=++run.attempt
    try{
      const info=await api.run(runId)
      if(epoch!==workspaceEpoch)return
      if(info.state==='waiting'){run.waiting=info.waiting;await loadSession(id,true);return}
      const assistantId=messagesFor(id).find(message=>message.id===info.message_id)?.id||`replay-${runId}`
      if(!messagesFor(id).some(message=>message.id===assistantId))messagesFor(id).push({id:assistantId,role:'assistant',content:'',model_provider:null,model_name:null,citations:[],token_usage:null,run_metadata:null,created_at:new Date().toISOString()})
      run.waiting=null
      await api.resumeRun(runId,streamInto(id,assistantId,attempt),after);if(epoch===workspaceEpoch)await loadSession(id,true)
    }
    catch(error:any){if(epoch===workspaceEpoch){run.error=error.message;await loadSession(id,true).catch(()=>{})}}
    finally{if(run.attempt===attempt){run.busy=false;run.status=''}}
  }
  async function send(id:string,body:any){
    const epoch=workspaceEpoch
    const run=runFor(id);if(run.busy||run.waiting)return
    run.busy=true;run.status='正在连接';run.error='';run.runId=null
    const attempt=++run.attempt,now=new Date().toISOString(),key=crypto.randomUUID(),assistantId=`pending-assistant-${key}`
    messagesFor(id).push({id:`pending-user-${key}`,role:'user',content:body.question,model_provider:null,model_name:null,citations:[],token_usage:null,run_metadata:null,created_at:now},{id:assistantId,role:'assistant',content:'',model_provider:null,model_name:null,citations:[],token_usage:null,run_metadata:null,created_at:now})
    try{
      const value=await api.chatStream(id,body,streamInto(id,assistantId,attempt),capabilities.value?.features.durable_runs===true,info=>{if(epoch===workspaceEpoch&&run.attempt===attempt){run.runId=info.run_id;run.status='已入队，等待执行'}})
      if(epoch===workspaceEpoch&&run.attempt===attempt){await loadSession(id,true);if(epoch===workspaceEpoch)await Promise.all([loadSessions(),loadMemories()])}
      return value
    }catch(caught:any){if(epoch===workspaceEpoch&&run.attempt===attempt){run.error=caught?.message||'请求失败';await loadSession(id,true).catch(()=>{})}throw caught}
    finally{if(run.attempt===attempt){run.busy=false;run.status=''}}
  }
  async function cancelRun(id:string){
    const epoch=workspaceEpoch
    const run=runFor(id);if(!run.runId||run.cancelPending)return
    run.cancelPending=true
    try{const info=await api.cancelRun(run.runId);if(epoch!==workspaceEpoch)return;run.waiting=info.waiting;run.status=info.state==='cancelling'?'正在停止执行':'';if(info.state==='cancelled'){run.waiting=null;run.error='本轮问答已取消';await loadSession(id,true)}}
    catch(error:any){run.error=error.message}
    finally{run.cancelPending=false}
  }
  async function continueRun(id:string,supplement:string){
    const epoch=workspaceEpoch
    const run=runFor(id);if(run.busy||!run.waiting||!run.runId)return
    const waiting=run.waiting,runId=run.runId
    run.busy=true;run.error='';run.status='正在提交补充信息'
    try{await api.continueRun(runId,waiting,supplement);run.waiting=null}
    catch(error:any){run.error=error.message;throw error}
    finally{run.busy=false}
    if(epoch===workspaceEpoch)await recoverRun(id,runId,waiting.sequence)
  }
  async function loadKnowledgeBases(){const epoch=workspaceEpoch,scope=organizationId.value,rows=capabilities.value?.features.sharing?await api.knowledgeBases():[];if(epoch!==workspaceEpoch)return;knowledgeBases.value=capabilities.value?.features.workspaces?rows.filter(row=>row.organization_id===scope):rows;selectedBases.value=selectedBases.value.filter(id=>knowledgeBases.value.some(base=>base.id===id))}
  async function loadDocuments(q='',status=''){const epoch=workspaceEpoch,attempt=++documentsAttempt,value=await api.documents(q,status,organizationId.value);if(epoch!==workspaceEpoch||attempt!==documentsAttempt)return;documents.value=value.items;documentTotal.value=value.total;selectedDocuments.value=selectedDocuments.value.filter(id=>value.items.some(d=>d.id===id&&d.can_query));uploads.recover(value.items)}
  async function removeDocuments(ids:string[]){const scope=organizationId.value,epoch=workspaceEpoch;const results=await Promise.allSettled(ids.map(id=>api.deleteDocument(id,scope)));if(epoch!==workspaceEpoch)return;if(results.some(result=>result.status==='rejected'))notify('部分资料未能退休，请刷新任务和权限后重试');await loadDocuments()}
  async function retryDocument(id:string){const scope=organizationId.value,epoch=workspaceEpoch;await api.retry(id,scope);if(epoch===workspaceEpoch)await loadDocuments()}
  async function renameSession(id:string,title:string){const value=await api.renameSession(id,title);sessions.value=sessions.value.map(x=>x.id===id?value:x)}
  async function loadMemories(){const epoch=workspaceEpoch,rows=await api.memories();if(epoch===workspaceEpoch)memories.value=rows}
  async function createMemory(content:string){await api.createMemory(content);await loadMemories()}
  async function updateMemory(id:string,content:string){await api.updateMemory(id,content);await loadMemories()}
  async function deleteMemory(id:string){await api.deleteMemory(id);await loadMemories()}
  async function retryMemory(id:string){await api.retryMemory(id);await loadMemories()}
  const readyDocuments=computed(()=>documents.value.filter(x=>x.can_query))
  return {workspaces,organizationId,activeWorkspace,workspaceChanging,loadWorkspaces,switchWorkspace,knowledgeBases,selectedBases,loadKnowledgeBases,user,bootstrapped,sessions,documents,memories,capabilities,documentTotal,error,selectedDocuments,uploads,readyDocuments,bootstrap,initialize,login,register,logout,messagesFor,runFor,loadSessions,loadSession,createSession,send,cancelRun,continueRun,loadDocuments,removeDocuments,retryDocument,renameSession,loadMemories,createMemory,updateMemory,deleteMemory,retryMemory}
})
