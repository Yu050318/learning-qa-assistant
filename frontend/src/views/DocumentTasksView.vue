<script setup lang="ts">
import {onMounted,onBeforeUnmount,ref,watch} from 'vue'
import {RouterLink} from 'vue-router'
import {ArrowLeft,RefreshCw,Search,ListChecks,X} from 'lucide-vue-next'
import {api,type DocumentTask} from '../api'
import {useAppStore} from '../store'
import {taskKinds,taskStates,taskPhase,taskPercent,taskHint} from '../document-tasks'
const store=useAppStore(),scope=store.organizationId,rows=ref<DocumentTask[]>([]),total=ref(0),offset=ref(0)
const state=ref(''),kind=ref(''),q=ref(''),error=ref(''),notice=ref(''),loading=ref(false),retrying=ref(''),selected=ref<DocumentTask|null>(null),dialog=ref<HTMLDialogElement>()
const parentTask=ref('')
let disposed=false,attempt=0,poll:ReturnType<typeof setTimeout>|undefined,searchTimer:ReturnType<typeof setTimeout>|undefined
async function load(){
  if(disposed)return
  clearTimeout(poll);const current=++attempt;loading.value=true
  try{
    const page=await api.documentTasks(scope,offset.value,state.value,kind.value,q.value,parentTask.value)
    let detail=selected.value?page.items.find(row=>row.id===selected.value?.id):null
    if(selected.value&&dialog.value?.open&&!detail)detail=await api.documentTask(selected.value.id,scope)
    if(disposed||current!==attempt)return
    rows.value=page.items;total.value=page.total;if(detail)selected.value=detail;error.value='';poll=setTimeout(()=>{void load()},5000)
  }
  catch(e:any){if(!disposed&&current===attempt){rows.value=[];total.value=0;selected.value=null;dialog.value?.close();error.value=e.message}}
  finally{if(!disposed&&current===attempt)loading.value=false}
}
async function details(task:DocumentTask){
  try{const value=await api.documentTask(task.id,scope);if(disposed)return;selected.value=value;dialog.value?.showModal()}
  catch(e:any){if(!disposed)error.value=e.message}
}
async function retry(task:DocumentTask){
  if(retrying.value||!task.can_retry)return
  retrying.value=task.id;error.value='';notice.value=''
  try{const next=await api.retryDocumentTask(task.id,scope);if(disposed)return;selected.value=next;notice.value='重试已提交，处理结果以任务状态为准。';offset.value=0;state.value='';await load()}
  catch(e:any){if(!disposed){error.value=e.message;await load();error.value=e.message}}
  finally{if(!disposed)retrying.value=''}
}
function page(direction:number){offset.value=Math.max(0,offset.value+direction*20);void load()}
function children(task:DocumentTask){dialog.value?.close();selected.value=null;state.value='';kind.value='';q.value='';parentTask.value=task.id}
watch([state,kind,q,parentTask],()=>{attempt++;clearTimeout(poll);clearTimeout(searchTimer);offset.value=0;searchTimer=setTimeout(()=>{void load()},250)})
onMounted(load)
onBeforeUnmount(()=>{disposed=true;attempt++;clearTimeout(poll);clearTimeout(searchTimer)})
</script>
<template><section class="task-page">
  <header><div><small>DOCUMENT OPERATIONS</small><h1><ListChecks/>文档任务</h1><p>{{store.activeWorkspace.name}} · 仅显示当前账号在此空间的持久任务。</p></div><div class="task-actions"><RouterLink class="secondary" to="/documents"><ArrowLeft/>返回资料</RouterLink><button class="primary" :disabled="loading" @click="load"><RefreshCw/>刷新</button></div></header>
  <div class="task-filters"><label class="search"><Search/><input v-model="q" maxlength="200" placeholder="搜索任务文件名" aria-label="搜索任务文件名"></label><select v-model="kind" aria-label="任务类型"><option value="">全部类型</option><option v-for="(label,key) in taskKinds" :key="key" :value="key">{{label}}</option></select><select v-model="state" aria-label="任务状态"><option value="">全部状态</option><option v-for="(label,key) in taskStates" :key="key" :value="key">{{label}}</option></select><span aria-live="polite">{{error?'—':total}} 项任务</span></div>
  <p v-if="error" class="task-error" role="alert">{{error}}</p><p v-if="notice" class="task-notice" role="status">{{notice}}</p>
  <p class="task-note">进度表示已处理的部分，核验和文件清理结束前不会显示为完成。失败重试会创建新任务，原记录继续保留。</p>
  <p v-if="parentTask" class="task-note">正在查看一份资料的逐版清理记录。<button class="secondary" @click="parentTask=''">返回全部任务</button></p>
  <div class="task-rows" :aria-busy="loading"><article v-for="task in rows" :key="task.id">
    <div class="task-kind">{{taskKinds[task.kind]}}</div><div class="task-main"><h2>{{task.original_name}}</h2><div class="task-meta"><span :data-state="task.state">{{taskStates[task.state]}}</span><span>{{taskPhase(task)}}</span><span v-if="task.kind!=='delete_series'">尝试 {{task.attempts}} / {{task.max_attempts}}</span></div>
      <progress v-if="taskPercent(task)!==null" :value="taskPercent(task)??0" max="100" :aria-label="`${task.original_name}任务进度`"/>
      <p v-if="task.total_units!==null&&task.total_units>0" class="task-units">已处理 {{task.processed_units}} / {{task.total_units}}{{task.kind==='delete_series'?' 个版本':''}}<span v-if="task.state!=='succeeded'"> · 后台尚未完成</span></p>
      <p v-if="taskHint(task)" :class="task.state==='failed'?'task-error':'task-note'">{{taskHint(task)}}</p><small>更新于 {{new Date(task.updated_at).toLocaleString()}}</small>
    </div><div class="task-row-actions"><button v-if="task.kind==='delete_series'" class="secondary" @click="children(task)">查看版本任务</button><button class="secondary" @click="details(task)">详情</button><button v-if="task.can_retry" class="primary" :disabled="!!retrying" @click="retry(task)">{{retrying===task.id?'正在提交':'重试'}}</button></div>
  </article><p v-if="!rows.length&&!error" class="task-empty">{{loading?'正在读取任务…':'此范围没有任务。'}}</p></div>
  <footer><button class="secondary" :disabled="loading||offset===0" @click="page(-1)">上一页</button><span>{{total?offset+1:0}}–{{Math.min(offset+20,total)}} / {{total}}</span><button class="secondary" :disabled="loading||offset+20>=total" @click="page(1)">下一页</button></footer>
  <dialog ref="dialog" class="task-dialog" aria-labelledby="task-detail-title"><header><h2 id="task-detail-title">任务详情</h2><button class="icon" aria-label="关闭任务详情" @click="dialog?.close()"><X/></button></header><template v-if="selected"><h3>{{selected.original_name}}</h3><dl><dt>任务编号</dt><dd>{{selected.id}}</dd><dt>类型与状态</dt><dd>{{taskKinds[selected.kind]}} · {{taskStates[selected.state]}}</dd><dt>当前阶段</dt><dd>{{taskPhase(selected)}}</dd><dt>尝试次数</dt><dd>{{selected.attempts}} / {{selected.max_attempts}}</dd><dt>创建时间</dt><dd>{{new Date(selected.created_at).toLocaleString()}}</dd><dt>最近更新</dt><dd>{{new Date(selected.updated_at).toLocaleString()}}</dd></dl><p v-if="taskHint(selected)" class="task-error">{{taskHint(selected)}}</p><p v-if="selected.error_code" class="task-note">错误代码：{{selected.error_code}}</p><button v-if="selected.can_retry" class="primary" :disabled="!!retrying" @click="retry(selected)">重试此任务</button></template></dialog>
</section></template>
<style scoped>
.task-page{height:100%;overflow:auto;padding:44px 5%;color:var(--ink)}header{display:flex;justify-content:space-between;align-items:center;gap:24px}header small{color:var(--green);font-size:10px;letter-spacing:.17em}h1{display:flex;align-items:center;gap:13px;font-family:'Noto Serif SC',serif;font-size:36px;margin:12px 0}h1 svg{width:29px}header p,.task-note{color:var(--muted);font-size:13px;line-height:1.8}.task-actions,.task-row-actions{display:flex;gap:10px;align-items:center}.task-actions a{display:inline-flex;text-decoration:none;align-items:center;gap:7px}.task-actions svg{width:16px}.task-filters{display:flex;align-items:center;gap:13px;flex-wrap:wrap;padding:24px 0;margin-top:16px;border-bottom:1px solid var(--line)}.task-filters select{padding:10px;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--ink)}.task-filters>span{margin-left:auto;font-size:12px;color:var(--muted)}.task-rows{margin-top:20px;border:1px solid var(--line);border-radius:10px;background:var(--surface);overflow:hidden}.task-rows article{display:grid;grid-template-columns:110px minmax(0,1fr) auto;gap:18px;align-items:start;padding:24px;border-bottom:1px solid var(--line)}.task-rows article:last-child{border-bottom:0}.task-kind{color:var(--green);font-size:12px;padding-top:4px}h2{font-size:16px;margin:0 0 11px;overflow-wrap:anywhere}.task-meta{display:flex;gap:14px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--muted)}.task-meta [data-state]{padding:4px 8px;background:var(--paper);border-radius:4px}.task-meta [data-state=succeeded]{background:var(--green2);color:var(--green)}.task-meta [data-state=failed]{color:var(--red)}.task-main>small{display:block;font-size:11px;color:var(--muted);margin-top:12px}progress{display:block;width:min(420px,100%);height:6px;accent-color:var(--green);margin-top:15px}.task-units{font-size:11px;color:var(--muted)}.task-error{color:var(--red);font-size:13px;line-height:1.7}.task-notice{color:var(--green);background:var(--green2);padding:12px;border-radius:6px;font-size:13px}.task-empty{text-align:center;padding:35px;color:var(--muted)}footer{display:flex;align-items:center;justify-content:flex-end;gap:16px;margin-top:22px;font-size:12px;color:var(--muted)}.task-dialog{width:min(520px,92vw);max-height:82vh;overflow:auto;padding:26px;border:1px solid var(--line);border-radius:12px;background:var(--surface);color:var(--ink)}.task-dialog::backdrop{background:#102c225c}.task-dialog dl{display:grid;grid-template-columns:90px minmax(0,1fr);gap:14px;font-size:13px}.task-dialog dt{color:var(--muted)}.task-dialog dd{margin:0;overflow-wrap:anywhere}button:disabled{opacity:.45;cursor:wait}@media(max-width:800px){.task-page{padding:25px 18px}header{align-items:flex-start;flex-wrap:wrap}h1{font-size:28px}.task-rows article{grid-template-columns:1fr;padding:18px;gap:12px}.task-row-actions{justify-content:flex-end}.task-filters>span{margin-left:0}.task-filters .search{width:100%}}
</style>
