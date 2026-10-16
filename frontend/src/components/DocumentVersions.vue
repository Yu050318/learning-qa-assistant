<script setup lang="ts">
import { onMounted,onBeforeUnmount,ref } from 'vue'
import { X,Upload,RefreshCw } from 'lucide-vue-next'
import { api,type DocumentItem } from '../api'
const props=defineProps<{document:DocumentItem;allowed:string[];maxBytes:number}>()
const emit=defineEmits<{close:[];changed:[]}>()
const dialog=ref<HTMLDialogElement>(),input=ref<HTMLInputElement>(),rows=ref<DocumentItem[]>([]),total=ref(0),offset=ref(0)
const loading=ref(false),busy=ref(false),error=ref(''),notice=ref('')
let timer:ReturnType<typeof setTimeout>|undefined,disposed=false
async function load(){
  clearTimeout(timer);loading.value=true
  try{const page=await api.versions(props.document.id,offset.value);if(disposed)return;rows.value=page.items;total.value=page.total
    if(rows.value.some(row=>row.status==='processing'||row.status==='deleting'))timer=setTimeout(()=>{void load()},5000)
  }catch(e:any){error.value=e.message}finally{loading.value=false}
}
async function refresh(){error.value='';await load();emit('changed')}
async function page(direction:number){offset.value=Math.max(0,offset.value+direction*20);await load()}
async function upload(event:Event){
  const field=event.target as HTMLInputElement,file=field.files?.[0];field.value='';if(!file)return
  const extension='.'+(file.name.split('.').pop()||'').toLowerCase()
  error.value='';notice.value=''
  if(!props.allowed.includes(extension)||file.size===0||file.size>props.maxBytes){error.value='请选择支持格式、非空且未超过大小限制的文件';return}
  busy.value=true;clearTimeout(timer)
  try{await api.uploadVersion(props.document.id,file);offset.value=0;notice.value='新版本正在入库。就绪后请明确设为当前，原版本继续提供默认检索。';await load();emit('changed')}
  catch(e:any){error.value=`${e.message}。若连接中断，请刷新历史核对结果，不要立即重复上传。`}
  finally{busy.value=false}
}
async function activate(row:DocumentItem){
  if(!row.version_info||!row.can_query)return
  busy.value=true;error.value='';notice.value='';clearTimeout(timer)
  try{await api.activateVersion(row.id,row.version_info.current_document_id);notice.value=`v${row.version_info.version_number} 已设为当前；历史版本与已有引用继续保留。`;await load();emit('changed')}
  catch(e:any){error.value=e.message;await load()}
  finally{busy.value=false}
}
onMounted(()=>{dialog.value?.showModal();void load()})
onBeforeUnmount(()=>{disposed=true;clearTimeout(timer)})
</script>
<template>
<dialog ref="dialog" class="version-dialog" aria-labelledby="version-title" @close="emit('close')" @cancel="busy&&$event.preventDefault()">
  <header><div><small>DOCUMENT HISTORY</small><h2 id="version-title">版本记录</h2><p>{{document.original_name}}</p></div><button class="icon" aria-label="关闭版本记录" :disabled="busy" @click="dialog?.close()"><X/></button></header>
  <p class="version-hint">全部资料默认查询各文档的当前版本。上传新版本不会覆盖旧文件，也不会自动切换；在聊天中指定旧版资料仍可查询历史内容。</p>
  <div class="version-actions"><button class="primary" :disabled="busy||loading||rows.some(row=>row.status==='processing')" @click="input?.click()"><Upload/>上传新版本</button><button class="secondary" :disabled="loading||busy" @click="refresh"><RefreshCw/>刷新</button><span aria-live="polite">共 {{total}} 个版本</span></div>
  <input ref="input" hidden type="file" :accept="allowed.join(',')" @change="upload">
  <p v-if="error" class="version-error" role="alert">{{error}}</p><p v-if="notice" class="version-notice" role="status">{{notice}}</p>
  <p v-if="loading&&!rows.length" role="status">正在读取版本记录…</p>
  <ol class="version-list"><li v-for="row in rows" :key="row.id"><div><div class="version-row-title"><strong>v{{row.version_info?.version_number}} · {{row.original_name}}</strong><span v-if="row.version_info?.is_current" class="status ready">当前</span><span v-else class="status">历史 / 待切换</span></div><small>{{new Date(row.created_at).toLocaleString()}} · {{row.status==='ready'?'已就绪':row.status==='failed'?'处理失败':row.status==='deleting'?'删除中':'处理中'}} · {{row.chunk_count}} 个片段</small><code :title="row.version_info?.content_hash">SHA-256 {{row.version_info?.content_hash.slice(0,12)}}…</code><p v-if="row.error_message" class="version-error">{{row.error_message}}</p></div><button v-if="!row.version_info?.is_current" class="secondary" :disabled="busy||loading||!row.can_query" @click="activate(row)">设为当前</button></li></ol>
  <footer><button class="secondary" :disabled="loading||busy||offset===0" @click="page(-1)">上一页</button><span>{{offset+1}}–{{Math.min(offset+20,total)}} / {{total}}</span><button class="secondary" :disabled="loading||busy||offset+20>=total" @click="page(1)">下一页</button></footer>
</dialog>
</template>
<style scoped>
.version-dialog{width:min(760px,calc(100vw - 32px));max-height:86vh;overflow:auto;border:1px solid var(--line);border-radius:12px;background:var(--surface);color:var(--ink);padding:28px;box-shadow:var(--shadow)}
.version-dialog::backdrop{background:#18271f75}.version-dialog header{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}.version-dialog header small{font-size:10px;letter-spacing:.14em;color:var(--green)}.version-dialog h2{font-family:'Noto Serif SC',serif;margin:8px 0;font-size:27px}.version-dialog header p{margin:0;color:var(--muted);overflow-wrap:anywhere}.version-hint{font-size:13px;line-height:1.8;color:var(--muted);padding:16px 0;border-bottom:1px solid var(--line)}.version-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.version-actions>span{margin-left:auto;font-size:12px;color:var(--muted)}.version-list{list-style:none;padding:0;margin:18px 0}.version-list li{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:20px 0;border-bottom:1px solid var(--line)}.version-row-title{display:flex;align-items:center;flex-wrap:wrap;gap:10px}.version-row-title strong{font-size:14px;overflow-wrap:anywhere}.version-list small{display:block;margin-top:9px;color:var(--muted);line-height:1.7}.version-list code{font-size:11px;color:var(--muted)}.version-list button{flex-shrink:0}.version-error{font-size:13px;color:var(--red);line-height:1.7}.version-notice{padding:12px;background:var(--green2);color:var(--green);font-size:13px;line-height:1.7}.version-dialog footer{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--muted)}button:disabled{opacity:.45;cursor:not-allowed}@media(max-width:600px){.version-dialog{padding:18px}.version-list li{align-items:flex-start;flex-direction:column}.version-actions>span{width:100%;margin:0}}
</style>
