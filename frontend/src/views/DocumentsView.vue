<script setup lang="ts">
import { onMounted,onBeforeUnmount,ref,watch } from 'vue';import { Search,Upload,FolderUp,Trash2,RefreshCw,FileText,CircleAlert } from 'lucide-vue-next';import { useAppStore } from '../store'
import DocumentVersions from '../components/DocumentVersions.vue'
import SharedLibraries from '../components/SharedLibraries.vue'
import type { DocumentItem } from '../api'
import {RouterLink,useRoute} from 'vue-router'
const route=useRoute(),shared=ref(false)
const store=useAppStore(),q=ref(''),status=ref(''),selected=ref<string[]>([]),fileInput=ref<HTMLInputElement>(),folderInput=ref<HTMLInputElement>(),timer=ref<number>(),versionDocument=ref<DocumentItem|null>(null)
onMounted(()=>{shared.value=!!store.organizationId||route.query.manageSpaces==='1';void store.loadDocuments().catch((e:any)=>store.error=e.message)});watch([q,status],()=>{clearTimeout(timer.value);timer.value=window.setTimeout(()=>{selected.value=[];void store.loadDocuments(q.value,status.value).catch((e:any)=>store.error=e.message)},300)})
watch(()=>route.query.manageSpaces,value=>{if(value==='1')shared.value=true})
onBeforeUnmount(()=>clearTimeout(timer.value))
function choose(input:HTMLInputElement|undefined){input?.click()}function add(e:Event){store.uploads.add((e.target as HTMLInputElement).files);(e.target as HTMLInputElement).value=''}
async function remove(){if(!selected.value.length||!confirm(`确定删除选中的 ${selected.value.length} 项资料？`))return;await store.removeDocuments(selected.value);selected.value=[]}
const stateLabel=(s:string)=>s==='ready'?'上传成功':s==='failed'?'上传失败':s==='deleting'?'退休清理中':s==='retired'?'已退休':'上传中'
const failureHint=(message:string|null)=>message?.includes('200 页')?'请拆分文档后重新上传':message?.includes('MinerU')?'文件已保存在本地，可点击右侧按钮重新处理':'请检查服务状态后重试'
</script>
<template><section class="documents"><header class="page-title"><div><small>KNOWLEDGE LIBRARY</small><h1>资料</h1><p>把散落的文件收进同一处，让每次回答都有迹可循。</p></div><div><RouterLink v-if="store.capabilities?.features.document_tasks" class="secondary task-entry" to="/document-tasks">后台任务</RouterLink><button class="secondary" @click="choose(folderInput)"><FolderUp/>上传文件夹</button><button class="primary" @click="choose(fileInput)"><Upload/>上传文件</button></div></header>
<p v-if="store.organizationId" class="space-source-note">当前团队：{{store.activeWorkspace.name}}。我的上传先保留在本空间；明确发布到共享库后，团队成员才可检索。</p>
<nav v-if="store.capabilities?.features.sharing" aria-label="资料范围" class="library-bar"><button :class="shared?'secondary':'primary'" @click="shared=false">{{store.organizationId?'我的上传':'我的资料'}}</button><button :class="shared?'primary':'secondary'" @click="shared=true">{{store.organizationId?'团队共享库':'组织与共享库'}}</button></nav>
<input ref="fileInput" hidden type="file" multiple @change="add"><input ref="folderInput" hidden type="file" multiple webkitdirectory @change="add">
<SharedLibraries v-if="shared"/><template v-else>
<div class="library-bar"><label class="search"><Search/><input v-model="q" placeholder="搜索全部已上传文件"/></label><select v-model="status"><option value="">全部状态</option><option value="ready">上传成功</option><option value="processing">上传中</option><option value="failed">上传失败</option></select><span>{{store.documentTotal}} 项资料</span><button v-if="selected.length" class="danger" @click="remove"><Trash2/>删除 {{selected.length}} 项</button></div>
<div class="table-wrap"><table><thead><tr><th></th><th>文件</th><th>状态</th><th>大小</th><th>更新时间</th><th></th></tr></thead><tbody><tr v-for="d in store.documents" :key="d.id"><td><input v-model="selected" type="checkbox" :value="d.id"></td><td><div class="file-cell"><span><FileText/></span><div><strong>{{d.original_name}}</strong><small>{{d.file_type.replace('.','').toUpperCase()}} · {{d.chunk_count}} 个片段</small></div></div></td><td><div class="status-stack"><span :class="['status',d.status]">{{stateLabel(d.status)}}</span><div v-if="d.status==='failed'" class="failure-card" role="alert"><CircleAlert/><div><strong>处理未完成</strong><p>{{d.error_message||'文档处理失败'}}</p><small>{{failureHint(d.error_message)}}</small></div></div><small v-else-if="!d.can_query&&d.status==='ready'">{{d.index_info?.state==='verified'?'候选已核验，请在版本记录发布':'当前版本暂不可检索'}}</small></div></td><td>{{d.size_bytes==null?'—':(d.size_bytes/1024/1024).toFixed(1)+' MB'}}</td><td>{{new Date(d.updated_at).toLocaleString()}}</td><td><button v-if="d.version_info" class="secondary" @click="versionDocument=d">v{{d.version_info.version_number}} · 版本</button><button v-if="d.status==='failed'" class="icon retry-button" aria-label="重新处理" title="重新处理" @click="store.retryDocument(d.id)"><RefreshCw/></button></td></tr><tr v-if="!store.documents.length"><td colspan="6" class="empty-row">没有找到资料</td></tr></tbody></table></div>
<DocumentVersions v-if="versionDocument" :document="versionDocument" :allowed="store.capabilities?.upload.allowed_extensions||['.txt','.md']" :max-bytes="store.capabilities?.upload.max_file_bytes||20*1024*1024" @close="versionDocument=null" @changed="store.loadDocuments(q,status)"/>
</template></section></template>
<style scoped>.task-entry{display:inline-flex;align-items:center;text-decoration:none}</style>
