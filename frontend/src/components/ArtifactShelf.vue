<script setup lang="ts">
import {nextTick,onBeforeUnmount,ref,watch} from 'vue'
import {Download,Eye,FileText,Image as ImageIcon,RefreshCw,X} from 'lucide-vue-next'
import {api} from '../api'
import {artifactSize,type RunArtifact} from '../artifacts'
import {useAppStore} from '../store'
const props=defineProps<{runId:string;sessionId:string}>(),store=useAppStore()
const items=ref<RunArtifact[]>([]),loading=ref(false),error=ref(''),busy=ref(''),dialog=ref<HTMLDialogElement|null>(null),selected=ref<RunArtifact|null>(null),text=ref(''),image=ref(''),truncated=ref(false)
let generation=0,action=0,listRequest:AbortController|undefined,contentRequest:AbortController|undefined
const downloads=new Set<string>()
function closePreview(){++action;contentRequest?.abort();busy.value='';selected.value=null;text.value='';truncated.value=false;if(image.value)URL.revokeObjectURL(image.value);image.value='';dialog.value?.close()}
function clear(){++generation;listRequest?.abort();closePreview();items.value=[];error.value='';for(const url of downloads)URL.revokeObjectURL(url);downloads.clear()}
async function load(){
  clear();const stamp=generation;loading.value=true;listRequest=new AbortController()
  try{const rows=await api.artifacts(props.runId,listRequest.signal);if(stamp===generation){if(rows.some(row=>row.user_id!==store.user?.id||row.organization_id!==store.organizationId||row.session_id!==props.sessionId))throw new Error('产物不属于当前账号、空间或对话');items.value=rows}}
  catch(e:any){if(stamp===generation&&e.name!=='AbortError')error.value=e.message}
  finally{if(stamp===generation)loading.value=false}
}
async function preview(item:RunArtifact){
  closePreview();const stamp=generation,attempt=++action;error.value='';selected.value=item;busy.value=item.id;contentRequest=new AbortController()
  await nextTick();if(stamp!==generation||attempt!==action)return;dialog.value?.showModal()
  try{
    const value=await api.artifactContent(props.runId,item,true,contentRequest.signal)
    if(stamp!==generation||attempt!==action)return
    if(item.preview_kind==='text'){const valueText=await value.blob.text();if(stamp!==generation||attempt!==action)return;text.value=valueText}else image.value=URL.createObjectURL(value.blob)
    truncated.value=value.truncated
  }catch(e:any){if(stamp===generation&&attempt===action&&e.name!=='AbortError')error.value=e.message}
  finally{if(stamp===generation&&attempt===action)busy.value=''}
}
async function download(item:RunArtifact){
  closePreview();const stamp=generation,attempt=++action;error.value='';busy.value=item.id;contentRequest=new AbortController()
  try{
    const value=await api.artifactContent(props.runId,item,false,contentRequest.signal)
    if(stamp!==generation||attempt!==action)return
    const url=URL.createObjectURL(value.blob),link=document.createElement('a');downloads.add(url);link.href=url;link.download=item.name.split('/').pop()||'artifact';document.body.append(link);link.click();link.remove()
    window.setTimeout(()=>{URL.revokeObjectURL(url);downloads.delete(url)},1000)
  }catch(e:any){if(stamp===generation&&attempt===action&&e.name!=='AbortError')error.value=e.message}
  finally{if(stamp===generation&&attempt===action)busy.value=''}
}
watch(()=>[props.runId,props.sessionId,store.user?.id,store.user?.role,store.organizationId],()=>void load(),{immediate:true})
onBeforeUnmount(clear)
</script>

<template>
<section v-if="loading||items.length||error" class="artifact-shelf" aria-label="本轮运行产物" :aria-busy="loading">
  <header><div><small>本轮运行 · 产物</small><h3>已生成的文件 <span v-if="items.length">{{items.length}}</span></h3></div><button class="icon" :disabled="loading||!!busy" aria-label="重新核查产物权限与列表" @click="load"><RefreshCw/></button></header>
  <p v-if="loading" class="artifact-note" role="status">正在核查可访问的文件…</p>
  <ul><li v-for="item in items" :key="item.id"><component :is="item.preview_kind==='image'?ImageIcon:FileText" class="artifact-mark"/><div class="artifact-details"><strong>{{item.name}}</strong><p>{{artifactSize(item.size_bytes)}} <span>· 工具 v{{item.tool_version}}</span></p><code :title="`SHA-256：${item.sha256}`">{{item.sha256.slice(0,12)}}…</code></div><div class="artifact-actions"><button v-if="item.preview_kind!=='none'" class="ghost" :disabled="!!busy" :aria-label="`预览 ${item.name}`" @click="preview(item)"><Eye/><span>预览</span></button><button class="ghost" :disabled="!!busy" :aria-label="`校验并下载 ${item.name}`" @click="download(item)"><Download/><span>{{busy===item.id&&!selected?'校验中':'下载'}}</span></button></div></li></ul>
  <p v-if="error&&!selected" role="alert" class="artifact-error">{{error}}</p>
  <p v-if="items.length" class="artifact-note">下载前再次核对权限与文件摘要；预览仅展示安全文本和图片。</p>
</section>
<dialog ref="dialog" class="artifact-preview" :aria-label="selected?'产物预览：'+selected.name:'产物预览'" @cancel.prevent="closePreview" @click="(event:MouseEvent)=>{if(event.target===dialog)closePreview()}">
  <template v-if="selected"><header><div><small>产物预览</small><h3>{{selected.name}}</h3></div><button class="icon" autofocus aria-label="关闭产物预览" @click="closePreview"><X/></button></header><div class="artifact-preview-body"><p v-if="busy" role="status">正在校验并读取预览…</p><p v-else-if="error" class="artifact-error" role="alert">{{error}}</p><img v-else-if="image" :src="image" :alt="selected.name"/><pre v-else>{{text}}</pre></div><footer>{{truncated?'预览已截断，下载可获取完整文件。':'预览来自本轮已确认的工具回执。'}}</footer></template>
</dialog>
</template>

<style scoped>
.artifact-shelf{margin:20px 0 6px;border:1px solid var(--line);border-left:3px solid var(--green);border-radius:6px;padding:17px 20px;background:var(--surface);max-width:760px}.artifact-shelf header,.artifact-preview header{display:flex;align-items:center;justify-content:space-between;gap:16px}.artifact-shelf small,.artifact-preview small{font-size:10px;letter-spacing:.12em;color:var(--green)}h3{font-family:'Noto Serif SC',serif;font-size:16px;margin:5px 0 0}h3 span{font:12px 'Work Sans',sans-serif;color:var(--muted);margin-left:6px}.artifact-shelf ul{list-style:none;padding:0;margin:12px 0 0}.artifact-shelf li{display:flex;gap:12px;align-items:center;border-top:1px solid var(--line);padding:13px 0}.artifact-mark{flex-shrink:0;width:21px;color:var(--green)}.artifact-details{min-width:0;flex:1}.artifact-details strong{display:block;overflow-wrap:anywhere;font-size:13px}.artifact-details p{font-size:11px;color:var(--muted);margin:4px 0}.artifact-details code{font-size:10px;color:var(--muted)}.artifact-actions{display:flex;gap:4px}.artifact-actions button{font-size:12px;padding:7px 9px;display:flex;align-items:center;gap:5px}.artifact-actions svg{width:14px;height:14px}.artifact-note{font-size:11px;line-height:1.6;color:var(--muted);margin:9px 0 0}.artifact-error{color:var(--red);font-size:13px;line-height:1.7}.artifact-preview{width:min(760px,calc(100vw - 32px));max-height:85vh;box-sizing:border-box;padding:22px;border:1px solid var(--line);border-radius:10px;color:var(--ink);background:var(--surface);box-shadow:var(--shadow)}.artifact-preview::backdrop{background:#17251e80}.artifact-preview h3{overflow-wrap:anywhere}.artifact-preview-body{margin:18px 0;max-height:58vh;overflow:auto;overscroll-behavior:contain;border-block:1px solid var(--line);padding:16px 0}.artifact-preview pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.8;margin:0}.artifact-preview img{display:block;max-width:100%;height:auto;margin:auto}.artifact-preview footer{font-size:12px;color:var(--muted)}@media(max-width:550px){.artifact-shelf{padding:14px 12px}.artifact-actions span{display:none}.artifact-actions button{padding:8px}.artifact-preview{padding:16px}}
</style>
