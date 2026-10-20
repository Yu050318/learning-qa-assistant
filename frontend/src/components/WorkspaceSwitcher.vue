<script setup lang="ts">
import {ref} from 'vue'
import {useRoute,useRouter} from 'vue-router'
import {Building2,House,Check,ChevronDown,RefreshCw,X} from 'lucide-vue-next'
import {useAppStore} from '../store'
const store=useAppStore(),router=useRouter(),route=useRoute(),dialog=ref<HTMLDialogElement>(),opened=ref(false),error=ref('')
async function refresh(){error.value='';try{await store.loadWorkspaces()}catch(e:any){error.value=e.message}}
function open(){opened.value=true;dialog.value?.showModal();void refresh()}
async function select(id:string|null){
  error.value=''
  if(id===store.organizationId){dialog.value?.close();return}
  try{await store.switchWorkspace(id);if(route.path.startsWith('/chat/'))await router.replace('/chat');dialog.value?.close()}
  catch(e:any){error.value=e.message}
}
async function manage(){dialog.value?.close();await router.push('/documents?manageSpaces=1')}
</script>
<template>
  <button class="workspace-trigger" aria-haspopup="dialog" :aria-expanded="opened" :aria-label="`当前空间：${store.activeWorkspace.name}，切换空间`" :title="store.activeWorkspace.name" @click="open">
    <Building2 v-if="store.organizationId" :size="19"/><House v-else :size="19"/>
    <strong>{{store.activeWorkspace.name}}</strong><small>{{store.activeWorkspace.session_count}} 个对话 <ChevronDown :size="10"/></small>
  </button>
  <dialog ref="dialog" class="workspace-dialog" aria-labelledby="workspace-title" @close="opened=false" @cancel="store.workspaceChanging&&$event.preventDefault()">
    <header><div><small>YOUR KNOWLEDGE SPACES</small><h2 id="workspace-title">选择知识空间</h2></div><button class="icon" aria-label="关闭空间列表" :disabled="store.workspaceChanging" @click="dialog?.close()"><X/></button></header>
    <p>团队共享知识库；每个人的对话记录独立保留。</p>
    <p v-if="error" class="workspace-error" role="alert">{{error}}</p>
    <ul><li v-for="space in store.workspaces" :key="space.id||'personal'"><button :disabled="store.workspaceChanging" :aria-pressed="space.id===store.organizationId" @click="select(space.id)">
      <Building2 v-if="space.id"/><House v-else/><span><strong>{{space.name}}</strong><small>{{space.id?'团队空间':'私有空间'}} · {{space.session_count}} 个对话<span v-if="space.id"> · {{space.role==='owner'?'所有者':space.role==='admin'?'管理员':'成员'}}</span></small></span><Check v-if="space.id===store.organizationId" :size="18"/>
    </button></li></ul>
    <p class="workspace-note">切换不会取消已提交的后台运行或上传，它们继续使用原空间。</p>
    <footer><button class="secondary" :disabled="store.workspaceChanging" @click="refresh"><RefreshCw :size="16"/>刷新列表</button><button class="primary" :disabled="store.workspaceChanging" @click="manage">管理团队</button></footer>
  </dialog>
</template>
<style scoped>
.workspace-trigger{width:100%;display:grid;justify-items:center;gap:6px;margin-top:24px;padding:12px 2px;border:1px solid #e8f1e128;border-radius:9px;background:#ffffff0b;color:inherit}.workspace-trigger strong{max-width:64px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;font-weight:500}.workspace-trigger small{display:flex;gap:3px;align-items:center;font-size:10px;color:#cddbd3}.workspace-trigger:focus-visible{outline:2px solid #e6d8ae;outline-offset:3px}
.workspace-dialog{width:min(500px,92vw);max-height:82vh;overflow:auto;border:1px solid var(--line);border-radius:14px;background:var(--surface);color:var(--ink);padding:28px;box-shadow:var(--shadow)}.workspace-dialog::backdrop{background:#102c225c;backdrop-filter:blur(3px)}header,footer{display:flex;align-items:center;justify-content:space-between;gap:14px}header small{font-size:10px;letter-spacing:.15em;color:var(--green)}h2{font-family:'Noto Serif SC',serif;font-size:26px;margin:9px 0}p{font-size:13px;line-height:1.8;color:var(--muted)}ul{list-style:none;margin:20px 0;padding:0;display:grid;gap:9px}li button{width:100%;display:flex;align-items:center;gap:13px;text-align:left;padding:16px;border:1px solid var(--line);border-radius:9px;background:transparent;color:var(--ink)}li button[aria-pressed=true]{background:var(--green2);border-color:#b4c8b8;color:var(--green)}li button>span{flex:1;min-width:0}li strong{display:block;font-size:15px;overflow-wrap:anywhere}li small{display:block;font-size:11px;color:var(--muted);margin-top:7px}.workspace-note{font-size:11px;border-top:1px solid var(--line);padding-top:15px}.workspace-error{color:var(--red)}footer{margin-top:18px}button:disabled{opacity:.5;cursor:wait}
</style>
