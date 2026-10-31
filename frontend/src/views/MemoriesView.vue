<script setup lang="ts">
import { ref,watch } from 'vue';import { Brain,Plus,Trash2,RefreshCw,Save } from 'lucide-vue-next';import { useAppStore } from '../store'
const store=useAppStore(),draft=ref(''),editing=ref<Record<string,string>>({})
import { api } from '../api'
const settings=ref<Awaited<ReturnType<typeof api.memorySettings>>|null>(null),automatic=ref<Awaited<ReturnType<typeof api.automaticMemories>>>([]),busy=ref(false),error=ref('')
let generation=0
async function refresh(){
  const attempt=generation,uid=store.user?.id
  if(!uid)return
  const [value,items]=await Promise.all([api.memorySettings(),api.automaticMemories()])
  if(attempt===generation&&uid===store.user?.id){settings.value=value;automatic.value=items}
}
async function act(action:()=>Promise<unknown>){
  if(busy.value)return
  const attempt=generation
  busy.value=true;error.value=''
  try{await action();if(attempt===generation)await refresh()}
  catch(e){if(attempt===generation)error.value=e instanceof Error?e.message:'操作失败，请重试'}
  finally{if(attempt===generation)busy.value=false}
}
async function toggle(){if(!settings.value)return;const enabled=!settings.value.automatic_enabled;if(!enabled&&!window.confirm('关闭后停止自动采集与召回，并清空已有自动记忆。手工记忆保留。'))return;await act(()=>api.setAutomaticMemory(enabled))}
async function forget(id?:string){if(window.confirm(id?'删除后立即停止使用，后台继续清理。':'清空自动记忆？后续个人对话仍会继续提取。'))await act(()=>id?api.deleteAutomaticMemory(id):api.clearAutomaticMemories())}
watch(()=>store.user?.id,()=>{
  generation++;settings.value=null;automatic.value=[];editing.value={};draft.value='';busy.value=false;error.value=''
  if(store.user)void act(()=>store.loadMemories())
},{immediate:true})
async function add(){if(!draft.value.trim())return;await store.createMemory(draft.value);draft.value=''}
async function remove(id:string){if(window.confirm('确定删除这条记忆？'))await store.deleteMemory(id)}
</script>
<template><section class="documents"><header class="page-title"><div><small>PERSONAL MEMORY</small><h1>长期记忆</h1><p>管理手工记忆和你选择开启的个人自动记忆。</p></div></header><p v-if="error" role="alert">{{error}}</p><section class="memory-add" aria-labelledby="automatic-memory-heading"><div><h2 id="automatic-memory-heading">自动记忆</h2><p>开启后，从后续个人对话提取偏好和稳定事实。团队会话、模型回答和工具结果不会采集。</p><p v-if="settings && !settings.available">服务端尚未启用自动记忆。</p><p v-if="settings?.pending_jobs" role="status">{{settings.pending_jobs}} 项后台同步中，完成后才发布。</p><p v-if="settings?.failed_jobs" role="status">{{settings.failed_jobs}} 项同步失败或结果待核查。</p></div><button class="primary" role="switch" :aria-checked="settings?.automatic_enabled??false" :disabled="busy||!settings?.available" @click="toggle">{{settings?.automatic_enabled?'关闭自动记忆':'开启自动记忆'}}</button><button class="ghost" :disabled="busy" @click="act(refresh)"><RefreshCw/>刷新</button><button v-if="settings?.available" class="danger" :disabled="busy" @click="forget()">清空自动记忆</button></section><div class="memory-grid"><article v-for="item in automatic" :key="item.id" class="memory-card"><header><Brain/><span>自动提取 · v{{item.version}}</span></header><p style="white-space:pre-wrap">{{item.content}}</p><small>来自个人对话 · {{new Date(item.updated_at).toLocaleString()}}</small><footer><button class="danger" :disabled="busy" @click="forget(item.id)"><Trash2/>删除</button></footer></article></div><h2>手工记忆</h2><div class="memory-add"><textarea v-model="draft" maxlength="500" placeholder="例如：我偏好简洁的中文回答。"/><button class="primary" @click="add"><Plus/>添加记忆</button></div><div class="memory-grid"><article v-for="m in store.memories" :key="m.id" class="memory-card"><header><Brain/><span :class="['status',m.status]">{{m.status}}</span></header><textarea v-model="editing[m.id]" maxlength="500" :placeholder="m.content"/><p>{{editing[m.id]??m.content}}</p><small>{{m.created_via==='chat'?'来自对话':'手动添加'}} · v{{m.version}}</small><footer><button v-if="editing[m.id]" class="ghost" @click="store.updateMemory(m.id,editing[m.id]);editing[m.id]=''"><Save/>保存</button><button v-if="m.status==='failed'" class="ghost" @click="store.retryMemory(m.id)"><RefreshCw/>重试</button><button class="danger" @click="remove(m.id)"><Trash2/>删除</button></footer></article></div></section></template>
