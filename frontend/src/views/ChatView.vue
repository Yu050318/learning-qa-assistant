<script setup lang="ts">
import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue';import { useRoute,useRouter } from 'vue-router';import { Plus,Search,Send,Square,Paperclip,ChevronRight,X,Sparkles,Database,Zap,LocateFixed } from 'lucide-vue-next'
import { useAppStore } from '../store';import { api,type Citation,type RunWaiting } from '../api';import MessageContent from '../components/MessageContent.vue'
import { extractWebUrls,agentLabel,statusLabel,citationLocation } from '../agents'
import {modelSelection,selectedModel} from '../model-catalog'
import ToolApprovalDialog from '../components/ToolApprovalDialog.vue'
import ArtifactShelf from '../components/ArtifactShelf.vue'
import RunTrace from '../components/RunTrace.vue'
import {artifactRunId} from '../artifacts'
import {requestedSkill,toolApprovalDraft,type ToolApprovalDraft} from '../tool-approvals'
const store=useAppStore(),route=useRoute(),router=useRouter(),draft=ref(''),publicQuery=ref(''),mode=computed({get:()=>store.chatMode,set:value=>{store.chatMode=value;store.selectSkill(null)}}),provider=ref('deepseek'),search=ref(''),citation=ref<Citation|null>(null),citationBody=ref<HTMLElement|null>(null),messageList=ref<HTMLElement|null>(null),picker=ref(false),estimate=ref<any>(null),timer=ref<number>()
const idleRun={busy:false,status:'',error:'',attempt:0,runId:null,waiting:null as RunWaiting|null,cancelPending:false},sessionId=computed(()=>route.params.sessionId as string|undefined),activeMessages=computed(()=>sessionId.value?store.messagesFor(sessionId.value):[]),activeRun=computed(()=>sessionId.value?store.runFor(sessionId.value):idleRun),currentSession=computed(()=>store.sessions.find(x=>x.id===sessionId.value)),lastMetrics=computed(()=>[...activeMessages.value].reverse().find(x=>x.role==='assistant'&&x.run_metadata?.metrics)?.run_metadata.metrics),webAvailable=computed(()=>store.capabilities?.web_read_enabled===true||store.capabilities?.web_search_enabled===true)
let createdSessionId:string|undefined,viewAlive=true
const preparing=ref(false),pendingApproval=ref<{approval:ToolApprovalDraft;body:Record<string,any>;stamp:string;sessionId:string|undefined}|null>(null)
function formStamp(){return JSON.stringify([store.user?.id,store.user?.role,store.capabilities?.features.tool_approvals,store.organizationId,draft.value,mode.value,provider.value,store.selectedSkill,store.selectedBases,store.selectedDocuments])}
watch([formStamp,sessionId],()=>{pendingApproval.value=null})
watch(()=>store.capabilityDirectory?.revision,value=>{if(pendingApproval.value&&pendingApproval.value.approval.revision!==value){pendingApproval.value=null;store.error='能力目录已变化，请核对后重新发送'}})
async function loadCurrent(id:string){try{await store.loadSession(id)}catch(e:any){store.error=e.message;await router.replace('/chat')}}
onMounted(()=>{if(sessionId.value)void loadCurrent(sessionId.value);if(store.capabilities?.features.capability_center)void store.loadCapabilityDirectory().catch(()=>{})});watch(()=>activeRun.value.waiting?.resume_token,()=>{publicQuery.value=''});watch(sessionId,id=>{citation.value=null;publicQuery.value='';if(id!==createdSessionId)store.selectSkill(null);createdSessionId=undefined;if(id)void loadCurrent(id)})
watch(webAvailable,available=>{if(!available&&mode.value==='web')mode.value='auto'})
watch(()=>store.capabilities,cap=>{if(cap)provider.value=selectedModel(cap,provider.value)},{immediate:true})
watch(()=>activeMessages.value.at(-1)?.content,async()=>{if(activeRun.value.busy){await nextTick();messageList.value?.scrollTo({top:messageList.value.scrollHeight})}})
let estimateAttempt=0
watch([draft,mode,provider,()=>store.selectedSkill,sessionId,()=>store.organizationId,()=>store.selectedBases.join(','),()=>store.selectedDocuments.join(',')],()=>{clearTimeout(timer.value);const attempt=++estimateAttempt;if(!provider.value){estimate.value=null;return}timer.value=window.setTimeout(async()=>{try{const value=await api.estimate({session_id:sessionId.value||null,organization_id:store.organizationId,question:draft.value,...(store.selectedSkill?{skill_name:store.selectedSkill}:{}),knowledge_base_ids:['auto','knowledge'].includes(mode.value)?store.selectedBases:[],web_urls:['auto','web'].includes(mode.value)?extractWebUrls(draft.value):[],...modelSelection(store.capabilities,provider.value),search_mode:mode.value,document_ids:['web','sql'].includes(mode.value)?undefined:(store.selectedDocuments.length?store.selectedDocuments:undefined)});if(attempt===estimateAttempt)estimate.value=value}catch{if(attempt===estimateAttempt)estimate.value=null}},350)},{immediate:true})
onBeforeUnmount(()=>{viewAlive=false;pendingApproval.value=null;clearTimeout(timer.value);estimateAttempt++})
async function deliver(body:Record<string,any>,stamp:string,originalId:string|undefined){
  const stale=()=>!viewAlive||formStamp()!==stamp||sessionId.value!==originalId
  if(stale()||activeRun.value.busy)return
  let id=originalId
  preparing.value=true
  try{
    if(!id){const session=await store.createSession(body.question.slice(0,24));if(!session||stale())return;id=session.id;createdSessionId=id;await router.replace(`/chat/${id}`)}
    if(!viewAlive||formStamp()!==stamp||sessionId.value!==id)return
  }catch(e:any){store.error=e.message;return}finally{preparing.value=false}
  const owner=JSON.stringify([store.user?.id,store.organizationId])
  draft.value=''
  try{await store.send(id!,body)}catch{if(viewAlive&&sessionId.value===id&&JSON.stringify([store.user?.id,store.organizationId])===owner&&!draft.value)draft.value=body.question}
}
async function submit(){
  if(!draft.value.trim()||activeRun.value.busy||preparing.value||pendingApproval.value)return
  const question=draft.value
  if(activeRun.value.waiting&&sessionId.value){try{await store.continueRun(sessionId.value,question,publicQuery.value.trim()||undefined);draft.value='';publicQuery.value=''}catch{};return}
  if(!provider.value){store.error='暂无可用的回答模型，请联系管理员检查配置';return}
  const stamp=formStamp(),id=sessionId.value
  preparing.value=true
  let body:Record<string,any>
  try{
    const skillName=store.capabilities?.features.capability_center?requestedSkill(question,store.selectedSkill):store.selectedSkill
    if(skillName&&store.capabilities?.features.capability_center){await store.loadCapabilityDirectory();if(!viewAlive||stamp!==formStamp()||id!==sessionId.value)return}
    const webUrls=['auto','web'].includes(mode.value)?extractWebUrls(question):[]
    if(webUrls.length>3)throw new Error('每轮最多指定 3 个网页')
    body={question,...(skillName?{skill_name:skillName}:{}),knowledge_base_ids:['auto','knowledge'].includes(mode.value)?[...store.selectedBases]:[],web_urls:webUrls,search_mode:mode.value,...modelSelection(store.capabilities,provider.value),document_ids:['web','sql'].includes(mode.value)?undefined:(store.selectedDocuments.length?[...store.selectedDocuments]:undefined)}
    const approval=store.capabilities?.features.capability_center?toolApprovalDraft(store.capabilityDirectory,store.user?.role,store.capabilities.features.tool_approvals===true,skillName):null
    if(approval){if(approval.tools.some(tool=>tool.network_policy==='public-https'))body.web_query=question.trim();pendingApproval.value={approval,body,stamp,sessionId:id};return}
  }catch(e:any){if(viewAlive&&stamp===formStamp()&&id===sessionId.value)store.error=e.message;return}finally{preparing.value=false}
  await deliver(body!,stamp,id)
}
async function confirmApproval(){
  const pending=pendingApproval.value
  pendingApproval.value=null
  if(!pending||preparing.value)return
  if(pending.approval.tools.some(tool=>tool.network_policy==='public-https')&&(!pending.body.web_query?.trim()||pending.body.web_query.trim().length>400)){store.error='请核对本轮允许发送给公共工具的查询（1–400 字）';return}
  await deliver({...pending.body,approved_tools:pending.approval.tools.map(tool=>tool.name),approval_catalog_revision:pending.approval.revision},pending.stamp,pending.sessionId)
}
async function compact(){if(sessionId.value&&estimate.value?.context_version){await api.compact(sessionId.value,estimate.value.context_version,provider.value,store.capabilities?.features.model_profiles?provider.value:undefined);await store.loadSession(sessionId.value)}}
async function openCitation(value:Citation){citation.value=value;await nextTick();citationBody.value?.scrollTo({top:0})}
async function locateCitation(){if(!citation.value)return;const number=citation.value.number;citation.value=null;await nextTick();const target=document.querySelector<HTMLElement>(`.citation-ref[data-citation="${number}"]`);target?.scrollIntoView({behavior:'smooth',block:'center'});target?.focus();target?.classList.add('located');window.setTimeout(()=>target?.classList.remove('located'),1600)}
</script>
<template><div class="chat-layout"><aside class="sessions"><button class="new-chat" @click="router.push('/chat')"><Plus/>新建对话</button><label class="search"><Search/><input v-model="search" placeholder="搜索对话" @input="store.loadSessions(search)"/></label><div class="session-list"><RouterLink v-for="s in store.sessions" :key="s.id" :to="`/chat/${s.id}`"><span>{{s.title}}<i v-if="store.runFor(s.id).busy" class="session-running" title="正在生成"/></span><small>{{new Date(s.updated_at).toLocaleDateString()}}</small></RouterLink></div></aside>
<section class="conversation"><header class="chat-head"><div><small>KNOWLEDGE WORKSPACE</small><h1>{{currentSession?.title||'与资料开始一场对话'}}</h1></div><div class="head-controls"><select v-model="provider" :disabled="activeRun.busy||!!activeRun.waiting" aria-label="回答模型"><option v-if="!provider" value="" disabled>暂无可用模型</option><option v-for="m in store.capabilities?.models.filter(x=>x.available)" :key="m.profile_id||m.provider" :value="store.capabilities?.features.model_profiles?m.profile_id:m.provider">{{m.display_name||m.model_name}} · {{m.model_name}}</option></select><button class="ghost" :disabled="activeRun.busy||!!activeRun.waiting" @click="picker=true"><Paperclip/>{{store.selectedDocuments.length?`${store.selectedDocuments.length} 项资料`:'全部资料'}}</button></div></header>
<div ref="messageList" class="messages" :class="{empty:!activeMessages.length}"><div v-if="!activeMessages.length" class="welcome"><div class="orb"><Sparkles/></div><small>资料会成为回答的依据</small><h2>今天想弄清楚什么？</h2><p>上传项目文档、表格或演示稿，然后让 Learning Q&A 替你检索、梳理和引用。</p><div><button @click="draft='总结当前资料的核心内容'">总结资料</button><button @click="draft='找出资料中相互矛盾的观点'">检查矛盾</button></div></div>
<article v-for="m in activeMessages" :key="m.id" :class="['message',m.role]"><div class="role">{{m.role==='user'?'你':'Learning Q&A'}}</div><MessageContent :content="m.content" :citations="m.citations||[]" :active-citation="citation?.number" @citation="openCitation"/><ol v-if="m.run_metadata?.agent_events?.length" class="agent-progress" aria-label="Agent 执行过程"><li v-for="(event,index) in m.run_metadata.agent_events" :key="index" :data-status="event.status">{{agentLabel(event.agent)}} · {{statusLabel(event.status)}}<span v-if="event.error_code">（{{event.error_code}}）</span></li></ol><RunTrace v-if="m.role==='assistant'&&m.run_metadata?.run_id&&sessionId" :run-id="m.run_metadata.run_id" :session-id="sessionId" :run-state="m.run_metadata.state"/><ArtifactShelf v-if="artifactRunId(m.run_metadata)" :run-id="artifactRunId(m.run_metadata)!" :session-id="sessionId||''"/><p v-if="m.run_metadata?.failures?.length" class="stream-error">部分任务未完成：{{m.run_metadata.failures.map((failure:any)=>failure.code).join('、')}}</p><div v-if="m.role==='assistant'&&m.model_name" class="message-meta">{{m.model_name}} · {{m.token_usage?.total_tokens??'—'}} tokens</div></article><div v-if="activeRun.busy" class="thinking"><i/><i/><i/> {{activeRun.status||'正在梳理资料'}}</div><div v-else-if="activeRun.error" class="stream-error" role="alert">{{activeRun.error}}</div></div>
<footer class="composer">
  <div v-if="activeRun.waiting" class="human-input" role="status"><strong>等待你补充信息</strong><p>{{activeRun.waiting.question}}</p><small>补充后继续当前任务，保留已完成步骤。有效期至 {{new Date(activeRun.waiting.expires_at).toLocaleTimeString()}}</small><label v-if="activeRun.waiting.public_input_allowed" class="public-input">允许发送给公共工具的完整查询（可选）<input v-model="publicQuery" maxlength="400" autocomplete="off" placeholder="例如：查询北京今天的天气"/><small>仅此处内容会作为新的公开查询；下方补充信息不会自动发送。留空继续使用原公开查询。</small></label></div>
  <div v-if="store.capabilities?.features.capability_center" class="active-capability"><button class="ghost" :disabled="activeRun.busy||!!activeRun.waiting" @click="store.paletteOpen=true">选择能力 <kbd>Ctrl K</kbd></button><span v-if="store.selectedSkill">/{{store.selectedSkill}}<button class="icon" aria-label="清除当前 Skill" :disabled="activeRun.busy||!!activeRun.waiting" @click="store.selectSkill(null)"><X :size="13"/></button></span><small v-if="store.capabilityError">{{store.capabilityError}}</small></div>
  <div class="mode-tabs"><button v-for="m in ['auto','knowledge','web','sql']" :class="{active:mode===m}" :disabled="activeRun.busy||!!activeRun.waiting||m==='web'&&!webAvailable||m==='sql'&&!store.capabilities?.sql_enabled" :title="m==='web'&&!webAvailable?'联网搜索尚未启用':undefined" @click="mode=m as any">{{{auto:'自动调度',knowledge:'知识库',web:'联网 / 网页',sql:'业务 SQL'}[m]}}</button></div>
  <div class="composer-input"><textarea data-chat-composer v-model="draft" :maxlength="activeRun.waiting?4000:8000" :placeholder="activeRun.waiting?'补充信息，Enter 继续当前任务':'输入问题，Enter 发送，Shift + Enter 换行'" @keydown.enter.exact.prevent="submit"/><button class="send" :aria-label="activeRun.waiting?'补充并继续':'发送问题'" :disabled="!draft.trim()||activeRun.busy||preparing" @click="submit"><Send/></button></div>
  <button v-if="store.capabilities?.features.durable_runs&&activeRun.runId&&(activeRun.busy||activeRun.waiting)" class="stop-run" :disabled="activeRun.cancelPending" @click="sessionId&&store.cancelRun(sessionId)"><Square :size="14"/>{{activeRun.cancelPending?'正在取消':'停止执行'}}</button>
  <p v-if="estimate?.recommendation==='skill_over_budget'" class="error" role="alert">Skill 指令已超出上下文预算，请调整能力配置或选择其他 Skill。</p>
  <div class="usage"><div><Database/>缓存命中 <strong>{{lastMetrics?.cache?.by_model?.[0]?.hit_rate!=null?Math.round(lastMetrics.cache.by_model[0].hit_rate*100)+'%':'—'}}</strong></div><div><Zap/>预计上下文 <strong>{{estimate?`${Math.round(estimate.usage_ratio*100)}%`:'—'}}</strong><button v-if="estimate?.recommendation?.includes('compaction')&&sessionId&&!activeRun.busy&&!activeRun.waiting" @click="compact">压缩</button></div></div>
</footer></section>
<div v-if="picker" class="modal-mask" @click.self="picker=false"><div class="modal"><header><h3>{{store.organizationId?'选择团队共享库':'选择资料与共享库'}}</h3><button class="icon" @click="picker=false"><X/></button></header><p v-if="store.organizationId">未勾选时使用本空间全部已授权共享库；上传的原始资料需先明确发布到共享库。</p><template v-else><label v-for="d in store.readyDocuments" :key="d.id"><input v-model="store.selectedDocuments" type="checkbox" :value="d.id"/>{{d.original_name}}</label></template><label v-for="base in store.knowledgeBases" :key="base.id"><input v-model="store.selectedBases" type="checkbox" :value="base.id" :disabled="store.selectedBases.length>=20&&!store.selectedBases.includes(base.id)"/>共享库 · {{base.name}}</label><button class="primary" @click="picker=false">完成</button></div></div>
<aside v-if="citation" class="citation-panel" role="dialog" :aria-label="`引用 ${citation.number}`"><header><small>引用 {{citation.number}}</small><button class="icon" aria-label="关闭引用" @click="citation=null"><X/></button></header><div class="citation-heading"><h3>{{citation.type==='memory'?'长期记忆':citation.tool_name||citation.source_name||citation.title}}</h3><p>{{citationLocation(citation)}}</p></div><div ref="citationBody" class="citation-scroll"><p class="citation-location">高亮引用片段</p><blockquote><mark>{{citation.excerpt}}</mark></blockquote><template v-if="citation.type==='sql'"><p>返回 {{citation.row_count}} 行{{citation.truncated?' · 结果已截断':''}}</p><pre class="sql-query">{{citation.query}}</pre></template></div><footer><button class="ghost" @click="locateCitation"><LocateFixed/>定位回答中的引用</button><a v-if="citation.url" :href="citation.url" target="_blank" rel="noopener noreferrer">查看网页 <ChevronRight/></a></footer></aside>
</div><ToolApprovalDialog :draft="pendingApproval?.approval||null" :session-name="currentSession?.title||''" :public-query="pendingApproval?.body.web_query" @public-query="pendingApproval&&(pendingApproval.body.web_query=$event)" @cancel="pendingApproval=null" @confirm="confirmApproval"/></template>

<style scoped>
.human-input{padding:12px 16px;margin-bottom:12px;border:1px solid var(--border,#e5e7eb);border-left:3px solid #9b7545;border-radius:8px;background:var(--surface,#f6f6f6)}
.human-input p{margin:6px 0}.human-input small{color:var(--muted,#6b7280)}
.composer-input{position:relative}.composer-input .send{top:9px}
.stop-run{display:inline-flex;align-items:center;gap:6px;padding:7px 12px;margin:4px 8px 4px 0;border:1px solid var(--border,#e5e7eb);border-radius:8px;background:transparent;color:inherit}
.agent-progress{list-style:none;padding:0;display:flex;gap:8px;flex-wrap:wrap;font-size:12px;color:var(--muted,#6b7280)}
.agent-progress li{border:1px solid var(--border,#e5e7eb);border-radius:6px;padding:5px 9px}
.agent-progress li[data-status="failed"],.agent-progress li[data-status="partial"]{color:#a33d30}
.sql-query{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;line-height:1.6;padding:12px;background:var(--surface,#f6f6f6);border-radius:6px}
.active-capability{display:flex;align-items:center;flex-wrap:wrap;gap:12px;margin:0 24px 10px;font-size:11px;color:var(--green)}.active-capability>span{display:flex;gap:6px;align-items:center;background:var(--green2);padding:3px 8px;border-radius:5px}.active-capability small{color:var(--red)}
.public-input{display:grid;gap:8px;margin-top:14px;font-size:12px}.public-input input{width:100%;padding:10px;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--ink)}
</style>
