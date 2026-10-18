<script setup lang="ts">
import {computed,onMounted,ref} from 'vue'
import {Activity,Plus,Save,Trash2} from 'lucide-vue-next'
import {api,type ModelCatalog,type ModelCatalogInfo,type ModelVersionPage,type RegisteredProfile} from '../api'
import {catalogProblem,modelRoles,profileReferences,roleLabels,supportsRole} from '../model-catalog'

const emit=defineEmits<{saved:[]}>()
const info=ref<ModelCatalogInfo|null>(null),draft=ref<ModelCatalog|null>(null),busy=ref(false),error=ref(''),notice=ref('')
const versions=ref<ModelVersionPage|null>(null),historyOffset=ref(0),historyOpen=ref(false)
const profiles=computed(()=>Object.values(draft.value?.profiles||{})),problem=computed(()=>draft.value?catalogProblem(draft.value):'')
const dirty=computed(()=>!!info.value&&JSON.stringify(draft.value)!==JSON.stringify(info.value.catalog))
const health=(state:string)=>({available:'目录检测通过',unknown:'尚未检测',unavailable:'目录检测失败',disabled:'已停用',not_configured:'凭据未配置'}[state]||state)
function accept(value:ModelCatalogInfo){info.value=value;draft.value=structuredClone(value.catalog)}
async function load(){if(busy.value)return;busy.value=true;error.value='';try{accept(await api.modelCatalog())}catch(e:any){error.value=e.message}finally{busy.value=false}}
async function save(){if(!draft.value||!info.value||problem.value||busy.value)return;busy.value=true;error.value='';notice.value='';try{accept(await api.saveModelCatalog(info.value.revision,draft.value));versions.value=null;notice.value='配置已保存。新任务使用新版本，已入队任务保留原模型与角色。';if(historyOpen.value)await history();emit('saved')}catch(e:any){error.value=e.message}finally{busy.value=false}}
function add(){if(!draft.value||profiles.value.length>=64)return;const id=`model-${crypto.randomUUID().replaceAll('-','')}`,source=profiles.value.find(profile=>profile.enabled)||profiles.value[0];draft.value.profiles[id]={...source,id,version:1,display_name:'新模型',model_name:source?.model_name||'deepseek-chat',fallback_profile_id:null}}
function remove(profile:RegisteredProfile){if(!draft.value||profileReferences(draft.value,profile.id).length)return;delete draft.value.profiles[profile.id]}
function changeProvider(profile:RegisteredProfile){profile.connection_id=profile.provider}
function alias(provider:'deepseek'|'ollama',event:Event){if(!draft.value)return;const id=(event.target as HTMLSelectElement).value;if(id)draft.value.provider_defaults[provider]=id;else delete draft.value.provider_defaults[provider]}
async function probe(id:string){if(busy.value||dirty.value)return;busy.value=true;error.value='';try{accept(await api.probeProfile(id));notice.value='仅检测模型目录，不证明生成、工具调用或声明能力已经通过验证。'}catch(e:any){error.value=e.message}finally{busy.value=false}}
async function history(offset=0){busy.value=true;error.value='';try{versions.value=await api.modelVersions(offset);historyOffset.value=offset}catch(e:any){error.value=e.message}finally{busy.value=false}}
defineExpose({load})
onMounted(load)
</script>

<template>
  <section class="catalog-editor" :aria-busy="busy">
    <p v-if="error" role="alert" class="catalog-error">{{error}}</p><p v-if="notice" role="status" class="catalog-notice">{{notice}}</p>
    <p v-if="!info&&!error" role="status">正在读取模型目录…</p>
    <template v-if="info&&draft">
      <header class="catalog-heading"><div><small>MODEL CATALOG · REV {{info.revision}}</small><h2>让每个模型各司其职</h2></div><span>{{info.can_manage?'平台管理员':'只读访问'}} · {{profiles.length}} 个模型</span></header>
      <form @submit.prevent="save">
        <fieldset :disabled="busy||!info.can_manage"><legend class="sr-only">模型目录与角色分配</legend>
          <div class="roles-grid"><label v-for="role in modelRoles" :key="role"><span>{{roleLabels[role]}}</span><select v-model="draft.assignments[role]" :aria-label="roleLabels[role]"><option v-for="profile in profiles.filter(item=>supportsRole(item,role))" :key="profile.id" :value="profile.id">{{profile.display_name}} · {{profile.model_name}}</option></select></label></div>
          <div class="catalog-toolbar"><div><h3>模型 Profile</h3><p>一个连接可配置多个模型。地址与凭据由服务端管理。</p></div><button v-if="info.can_manage" type="button" class="secondary" :disabled="profiles.length>=64" @click="add"><Plus :size="16"/>新增模型</button></div>
          <div class="profiles-grid"><article v-for="profile in profiles" :key="profile.id" class="profile-card">
            <header><div><strong>{{profile.display_name}}</strong><small>{{profile.id}} · v{{profile.version}}</small></div><label class="check"><input v-model="profile.enabled" type="checkbox" :aria-label="`启用 ${profile.display_name}`"/>启用</label></header>
            <label>显示名称<input v-model="profile.display_name" required maxlength="120"/></label>
            <div class="fields-row"><label>服务端连接<select v-model="profile.provider" @change="changeProvider(profile)"><option v-for="connection in info.connections" :key="connection.id" :value="connection.provider">{{connection.provider}}{{connection.configured?'':' · 凭据未配置'}}</option></select></label><label>模型名称<input v-model="profile.model_name" required maxlength="200" pattern="[a-zA-Z0-9._:/-]+"/></label></div>
            <div class="fields-row"><label>上下文窗口 / Token<input v-model.number="profile.context_window" type="number" min="4096" max="2000000" required/></label><label>最大输出 / Token<input v-model.number="profile.max_output_tokens" type="number" min="1" max="16384" required/></label></div>
            <div class="fields-row"><label>单次超时 / 秒<input v-model.number="profile.timeout_seconds" type="number" min="0.001" step="any" max="120" required/></label><label>降级模型<select v-model="profile.fallback_profile_id"><option :value="null">不降级</option><option v-for="candidate in profiles.filter(item=>item.id!==profile.id&&item.enabled&&!item.fallback_profile_id)" :key="candidate.id" :value="candidate.id">{{candidate.display_name}}</option></select></label></div>
            <div class="declared"><span>声明能力</span><label><input v-model="profile.supports_stream" type="checkbox"/>流式输出</label><label><input v-model="profile.supports_tools" type="checkbox"/>工具调用</label><label><input v-model="profile.supports_structured_output" type="checkbox"/>结构化输出</label></div>
            <footer><span>{{health(info.models.find(item=>item.profile_id===profile.id)?.health||'unknown')}}</span><div v-if="info.can_manage"><button type="button" class="icon" :disabled="dirty||!profile.enabled" :aria-label="`检测 ${profile.display_name} 目录`" :title="dirty?'请先保存配置':'仅检测目录'" @click="probe(profile.id)"><Activity :size="17"/></button><button type="button" class="icon" :disabled="profileReferences(draft,profile.id).length>0" :title="profileReferences(draft,profile.id).join('、')||'删除后标识不能重用'" :aria-label="`删除 ${profile.display_name}`" @click="remove(profile)"><Trash2 :size="17"/></button></div></footer>
            <p v-if="profileReferences(draft,profile.id).length" class="profile-uses">用于：{{profileReferences(draft,profile.id).join('、')}}</p>
          </article></div>
          <details class="compatibility"><summary>旧客户端的默认模型</summary><p>使用提供商名称选择模型的旧客户端会使用以下默认值。</p><label v-for="provider in (['deepseek','ollama'] as const)" :key="provider">{{provider}}<select :value="draft.provider_defaults[provider]||''" @change="alias(provider,$event)"><option value="">未设置</option><option v-for="profile in profiles.filter(item=>item.provider===provider&&supportsRole(item,'answer'))" :key="profile.id" :value="profile.id">{{profile.display_name}}</option></select></label></details>
        </fieldset>
        <p v-if="problem" role="alert" class="catalog-error">{{problem}}</p><p class="catalog-help">能力勾选是配置声明，仍需通过实际调用验证。删除模型前请移除角色、默认模型及降级引用。</p>
        <button v-if="info.can_manage" class="primary" :disabled="busy||!dirty||!!problem"><Save :size="17"/>保存目录与角色</button>
      </form>
      <details v-if="info.can_manage" class="catalog-history" @toggle.self="historyOpen=($event.target as HTMLDetailsElement).open;historyOpen&&!versions&&history()"><summary>配置修订历史</summary><ol v-if="versions"><li v-for="version in versions.items" :key="version.revision"><strong>修订 {{version.revision}}</strong><time>{{new Date(version.created_at).toLocaleString()}}</time><details><summary>查看该版本配置</summary><pre>{{JSON.stringify(version.configuration,null,2)}}</pre></details></li></ol><div v-if="versions" class="history-pages"><button type="button" class="secondary" :disabled="busy||historyOffset===0" @click="history(Math.max(0,historyOffset-10))">上一页</button><span>共 {{versions.total}} 个修订</span><button type="button" class="secondary" :disabled="busy||historyOffset+10>=versions.total" @click="history(historyOffset+10)">下一页</button></div></details>
    </template>
  </section>
</template>

<style scoped>
.profile-uses{font-size:11px;color:var(--muted);line-height:1.7;margin:9px 0 0;overflow-wrap:anywhere}.profile-card header small{overflow-wrap:anywhere}
.catalog-heading,.catalog-toolbar,.profile-card header,.profile-card footer{display:flex;align-items:center;justify-content:space-between;gap:16px}.catalog-heading{margin:24px 0}.catalog-heading small{color:var(--green);letter-spacing:.12em;font-size:10px}.catalog-heading h2{font:600 25px 'Noto Serif SC',serif;margin:9px 0}.catalog-heading>span,.catalog-toolbar p,.catalog-help,.compatibility p{font-size:12px;color:var(--muted);line-height:1.7}.catalog-toolbar{margin:28px 0 18px}.catalog-toolbar h3{margin:0}.catalog-toolbar p{margin:6px 0}.roles-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;padding:22px;background:var(--green2);border-radius:9px}.roles-grid label,.profile-card>label,.fields-row label{display:grid;gap:8px;font-size:12px;min-width:0}.profiles-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}.profile-card{background:var(--surface);border:1px solid var(--line);border-radius:9px;padding:22px}.profile-card header{margin-bottom:20px}.profile-card header strong{font-family:'Noto Serif SC',serif}.profile-card header small{display:block;color:var(--muted);font-size:10px;margin-top:6px}.check{font-size:12px;white-space:nowrap}.fields-row{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:16px}input:not([type=checkbox]),select{width:100%;min-width:0;border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:6px;padding:10px;font:inherit}.declared{display:flex;flex-wrap:wrap;gap:10px;font-size:11px;padding:19px 0;color:var(--muted)}.declared label{white-space:nowrap}.profile-card footer{padding-top:12px;border-top:1px solid var(--line);font-size:11px;color:var(--muted)}.profile-card footer>div{display:flex;gap:9px}.catalog-error{color:var(--red);font-size:13px}.catalog-notice{padding:12px;background:var(--green2);color:var(--green);font-size:13px}.compatibility,.catalog-history{margin:24px 0;padding:18px;border:1px solid var(--line);border-radius:8px;font-size:13px}.compatibility label{display:inline-grid;gap:8px;margin:10px 16px 0 0;min-width:180px}.catalog-history ol{list-style:none;margin:16px 0;padding:0}.catalog-history li{padding:13px 0;border-top:1px solid var(--line)}.catalog-history time{margin-left:16px;color:var(--muted);font-size:11px}.catalog-history li details{margin-top:10px}.catalog-history pre{overflow:auto;max-height:260px;font-size:11px}.history-pages{display:flex;gap:14px;align-items:center}fieldset{border:0;margin:0;padding:0;min-width:0}button:disabled{opacity:.45;cursor:not-allowed}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}@media(max-width:760px){.catalog-heading{align-items:flex-start;flex-direction:column}.roles-grid,.profiles-grid{grid-template-columns:1fr}.roles-grid{padding:16px}.profile-card{padding:17px}.fields-row{gap:10px}.catalog-toolbar{align-items:flex-start}.catalog-history time{display:block;margin:5px 0}.history-pages{flex-wrap:wrap}}
</style>
