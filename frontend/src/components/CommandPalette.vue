<script setup lang="ts">
import {computed,nextTick,onBeforeUnmount,onMounted,ref,watch} from 'vue'
import {useRoute,useRouter} from 'vue-router'
import {Search,X,ArrowUpRight,Command as CommandIcon} from 'lucide-vue-next'
import {useAppStore} from '../store'
import {commands,nextCommand,type Command} from '../capability-ui'
const store=useAppStore(),route=useRoute(),router=useRouter(),dialog=ref<HTMLDialogElement>(),input=ref<HTMLInputElement>(),query=ref(''),active=ref(-1)
let opener:HTMLElement|null=null
const items=computed(()=>commands(store.capabilityDirectory,store.capabilities,query.value).map(item=>locked.value&&item.kind!=='tool'?{...item,disabled:true,reason:'当前任务结束后可切换'}:item))
const locked=computed(()=>{const id=route.params.sessionId;return typeof id==='string'&&route.path.startsWith('/chat')&&(store.runFor(id).busy||!!store.runFor(id).waiting)})
watch(()=>store.paletteOpen,async open=>{
  if(open){opener=document.activeElement as HTMLElement;query.value='';dialog.value?.showModal();await nextTick();input.value?.focus();void store.loadCapabilityDirectory().catch(()=>{})}
  else{dialog.value?.close();if(opener?.isConnected)opener.focus()}
})
watch(items,()=>{active.value=nextCommand(items.value,-1,1)})
async function move(direction:number){active.value=nextCommand(items.value,active.value,direction);await nextTick();document.getElementById(`cap-command-${active.value}`)?.scrollIntoView({block:'nearest'})}
async function select(item:Command|undefined){
  if(!item||item.disabled||store.capabilityLoading||locked.value&&item.kind!=='tool')return
  if(item.kind==='tool'){store.paletteOpen=false;await router.push({path:'/capabilities',query:{tab:'tools',q:item.name}});return}
  store.selectSkill(item.kind==='skill'?item.name:null)
  store.chatMode=item.kind==='mode'?item.name as typeof store.chatMode:'auto'
  store.paletteOpen=false
  if(!route.path.startsWith('/chat'))await router.push('/chat')
  await nextTick();document.querySelector<HTMLTextAreaElement>('[data-chat-composer]')?.focus()
}
function key(event:KeyboardEvent){if((event.ctrlKey||event.metaKey)&&!event.altKey&&event.key.toLowerCase()==='k'&&store.user&&store.capabilities?.features.capability_center){event.preventDefault();store.paletteOpen=!store.paletteOpen}}
onMounted(()=>window.addEventListener('keydown',key));onBeforeUnmount(()=>{window.removeEventListener('keydown',key);dialog.value?.close()})
</script>
<template>
  <dialog ref="dialog" class="command-dialog" aria-labelledby="command-title" @close="store.paletteOpen=false" @keydown.down.prevent="move(1)" @keydown.up.prevent="move(-1)">
    <header><CommandIcon :size="20"/><h2 id="command-title">能力命令</h2><button class="icon" aria-label="关闭命令面板" @click="store.paletteOpen=false"><X/></button></header>
    <label class="command-search"><Search :size="18"/><input ref="input" v-model="query" @keydown.enter.prevent="select(items[active])" placeholder="搜索模式、Skill 或工具…" role="combobox" aria-label="搜索能力命令" aria-autocomplete="list" aria-expanded="true" aria-controls="cap-command-results" :aria-activedescendant="active>=0?`cap-command-${active}`:undefined"/></label>
    <p v-if="locked" class="command-note">当前问答仍在执行或等待补充，可查看工具；完成后再切换能力。</p>
    <p v-if="store.capabilityLoading" role="status" class="command-state">正在读取能力目录…</p>
    <div v-else-if="store.capabilityError" class="command-state" role="alert">{{store.capabilityError}}<button @click="store.loadCapabilityDirectory().catch(()=>{})">重新加载</button></div>
    <div v-else id="cap-command-results" class="command-results" role="listbox" aria-label="能力命令结果">
      <button v-for="(item,index) in items" :id="`cap-command-${index}`" :key="item.key" role="option" tabindex="-1" :aria-selected="active===index" :aria-disabled="item.disabled||locked&&item.kind!=='tool'" :disabled="item.disabled||locked&&item.kind!=='tool'" :class="{active:active===index}" @mousemove="!item.disabled&&(active=index)" @click="select(item)"><span><small>{{{mode:'问答模式',skill:'Skill',tool:'Tool'}[item.kind]}}</small><strong>{{item.label}}</strong><em>{{item.description}}</em></span><small v-if="item.disabled">{{item.reason}}</small><ArrowUpRight v-else :size="16"/></button>
      <p v-if="!items.length" class="command-state">没有匹配的能力，试试名称或关键词。</p>
    </div>
    <footer><span>↑ ↓ 选择 · Enter 确认 · Esc 关闭</span><RouterLink to="/capabilities" @click="store.paletteOpen=false">打开能力中心</RouterLink></footer>
  </dialog>
</template>
<style scoped>
.command-dialog{width:min(630px,94vw);max-height:82vh;padding:0;border:1px solid var(--line);border-radius:14px;background:var(--surface);color:var(--ink);box-shadow:var(--shadow)}.command-dialog::backdrop{background:#102c2266;backdrop-filter:blur(4px)}header{display:flex;align-items:center;gap:12px;padding:20px 24px;color:var(--green)}h2{font-family:'Noto Serif SC',serif;font-size:20px;margin:0;flex:1}.command-search{display:flex;gap:12px;align-items:center;margin:0 24px 16px;padding:12px 0;border-bottom:1px solid var(--line)}input{width:100%;border:0;outline:0;background:transparent;color:var(--ink);font:inherit;font-size:15px}.command-results{max-height:49vh;overflow:auto;padding:0 12px 12px}.command-results button{display:flex;align-items:center;justify-content:space-between;gap:16px;width:100%;padding:14px 12px;border:1px solid transparent;border-radius:7px;background:transparent;color:var(--ink);text-align:left}.command-results button.active{background:var(--green2);border-color:var(--line)}.command-results strong,.command-results em{display:block}.command-results strong{font-size:14px;margin:5px 0;overflow-wrap:anywhere}.command-results em{font-size:12px;font-style:normal;color:var(--muted);overflow-wrap:anywhere}.command-results small{font-size:10px;color:var(--muted)}.command-results button:disabled{opacity:.5;cursor:not-allowed}.command-note,.command-state{padding:12px 24px;font-size:12px;color:var(--muted);line-height:1.8}.command-state button{display:block;margin-top:12px}.command-note{margin:0;background:var(--green2)}footer{display:flex;justify-content:space-between;gap:12px;padding:15px 24px;border-top:1px solid var(--line);font-size:11px;color:var(--muted)}footer a{color:var(--green)}
</style>
