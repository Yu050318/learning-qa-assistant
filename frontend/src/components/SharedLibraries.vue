<script setup lang="ts">
import {computed,onMounted,ref,watch} from 'vue'
import {Plus,Users,Library,RefreshCw,Trash2} from 'lucide-vue-next'
import {api,type Organization,type KnowledgeBase,type Member,type SharedDocument} from '../api'
import {useAppStore} from '../store'
const store=useAppStore(),orgs=ref<Organization[]>([]),oid=ref(''),kid=ref(''),members=ref<Member[]>([]),grants=ref<Member[]>([]),documents=ref<SharedDocument[]>([])
const busy=ref(false),loading=ref(false),error=ref(''),orgName=ref(''),baseName=ref(''),username=ref(''),memberRole=ref('member'),grantUser=ref(''),grantRole=ref('viewer'),source=ref('')
const libraries=ref<KnowledgeBase[]>([])
const org=computed(()=>orgs.value.find(row=>row.id===oid.value)),bases=computed(()=>libraries.value.filter(row=>row.organization_id===oid.value)),base=computed(()=>bases.value.find(row=>row.id===kid.value)),admin=computed(()=>org.value&&org.value.role!=='member'),editable=computed(()=>base.value&&base.value.role!=='viewer')
let attempt=0
async function load(){const [organizations,available]=await Promise.all([api.organizations(),api.knowledgeBases()]);orgs.value=store.capabilities?.features.workspaces&&store.organizationId?organizations.filter(row=>row.id===store.organizationId):organizations;libraries.value=available;await Promise.all([store.loadKnowledgeBases(),store.loadWorkspaces()]);if(!orgs.value.some(row=>row.id===oid.value))oid.value=orgs.value[0]?.id||'';if(!bases.value.some(row=>row.id===kid.value))kid.value=bases.value[0]?.id||'';await detail()}
async function detail(){const current=++attempt,organization=oid.value,library=kid.value;loading.value=true;members.value=[];grants.value=[];documents.value=[]
  try{const [people,access,docs]=await Promise.all([admin.value&&organization?api.members(organization):Promise.resolve([]),base.value?.role==='admin'?api.grants(library):Promise.resolve([]),library?api.sharedDocuments(library):Promise.resolve([])]);if(current===attempt){members.value=people;grants.value=access;documents.value=docs}}
  catch(e:any){if(current===attempt)error.value=e.message}finally{if(current===attempt)loading.value=false}}
async function act(job:()=>Promise<unknown>){if(busy.value)return;busy.value=true;error.value='';try{await job();await load()}catch(e:any){error.value=e.message}finally{busy.value=false}}
async function refresh(){error.value='';try{await load()}catch(e:any){error.value=e.message}}
function publish(){
  const document=store.readyDocuments.find(doc=>doc.id===source.value)
  if(!document||loading.value)return
  const series=document.version_info?.series_id||document.id
  const expected=documents.value.find(doc=>doc.series_id===series&&doc.is_current)?.id||null
  const library=kid.value
  void act(()=>api.publish(library,document.id,expected))
}
watch(oid,()=>{grantUser.value='';memberRole.value='member';kid.value=bases.value[0]?.id||'';void detail()});watch(kid,()=>{void detail()});onMounted(refresh)
function removeOrg(){if(confirm('归档团队空间后，成员将无法访问此空间、共享库和团队会话。原始文档和历史记录保留。'))void act(()=>api.deleteOrganization(oid.value))}
function removeBase(){if(confirm('删除这个共享库及其授权？原始文档会保留。'))void act(()=>api.deleteBase(kid.value))}
</script>
<template><section class="shared-library" :aria-busy="busy||loading">
  <header><div><small>SHARED KNOWLEDGE</small><h2>让资料在团队中流动</h2><p>按知识库授权，原始文档仍由发布者管理。</p></div><button class="secondary" :disabled="busy" @click="refresh"><RefreshCw/>刷新</button></header>
  <p v-if="error" class="shared-error" role="alert">{{error}}</p>
  <div class="shared-grid"><aside>
    <label class="field"><span><Users/>当前组织</span><select v-model="oid" :disabled="busy"><option value="" disabled>选择组织</option><option v-for="item in orgs" :key="item.id" :value="item.id">{{item.name}} · {{item.role==='owner'?'所有者':item.role==='admin'?'管理员':'成员'}}</option></select></label>
    <form class="inline-form" @submit.prevent="act(async()=>{await api.createOrganization(orgName);orgName=''})"><label class="sr-only" for="new-organization">新组织名称</label><input id="new-organization" v-model="orgName" required maxlength="100" placeholder="创建一个组织"/><button class="icon" :disabled="busy||!orgName.trim()" aria-label="创建组织"><Plus/></button></form>
    <nav aria-label="共享知识库"><button v-for="item in bases" :key="item.id" :aria-current="kid===item.id?'page':undefined" @click="kid=item.id"><Library/><span>{{item.name}}<small>{{item.role==='admin'?'可管理授权':item.role==='editor'?'可发布资料':'只读'}}</small></span></button><p v-if="!bases.length" class="muted">此组织暂无可访问的共享库。</p></nav>
    <form v-if="admin" class="inline-form" @submit.prevent="act(async()=>{await api.createBase(oid,baseName);baseName=''})"><label class="sr-only" for="new-library">共享库名称</label><input id="new-library" v-model="baseName" required maxlength="100" placeholder="新建共享库"/><button class="icon" :disabled="busy||!baseName.trim()" aria-label="创建共享库"><Plus/></button></form>
    <button v-if="org?.role==='owner'" class="text-danger" :disabled="busy" @click="removeOrg">归档团队空间</button>
  </aside><main>
    <section v-if="base" class="shared-block"><div class="block-heading"><h3>{{base.name}}</h3><button v-if="base.role==='admin'" class="icon" :disabled="busy" aria-label="删除共享库" @click="removeBase"><Trash2/></button></div>
      <p class="muted">团队使用本库明确发布的版本，个人切换版本不会改变团队资料。历史发布版本保留供已有运行使用；取消发布会撤销对该版本的访问。</p>
      <form v-if="editable" class="inline-form" @submit.prevent="publish"><label class="sr-only" for="shared-source">选择要发布的个人资料</label><select id="shared-source" v-model="source" required :disabled="busy||loading"><option value="" disabled>发布我的已就绪资料</option><option v-for="doc in store.readyDocuments" :key="doc.id" :value="doc.id">{{doc.original_name}} · v{{doc.version_info?.version_number||1}}</option></select><button class="secondary" :disabled="busy||loading||!source">发布为团队当前版本</button></form>
      <ul class="shared-rows"><li v-for="doc in documents" :key="doc.id"><span>{{doc.original_name}}<small>v{{doc.version_number}} · {{doc.is_current?'团队当前版本':doc.current_document_id?'历史发布版本':'待选择团队当前版本'}} · {{doc.can_query?'可检索':'暂不可检索'}}</small></span><button v-if="editable" class="secondary" :disabled="busy" @click="act(()=>api.unpublish(kid,doc.id))">取消发布</button></li></ul><p v-if="!documents.length&&!loading" class="muted">还没有发布资料。</p>
    </section>
    <section v-if="base?.role==='admin'" class="shared-block"><h3>知识库访问</h3><p class="muted">组织所有者和管理员可访问所有库。其他成员需单独授予只读或编辑权限。</p>
      <form class="inline-form" @submit.prevent="act(()=>api.setGrant(kid,grantUser,grantRole))"><label class="sr-only" for="grant-member">选择成员</label><select id="grant-member" v-model="grantUser" required><option value="" disabled>选择成员</option><option v-for="person in members.filter(row=>row.role==='member')" :key="person.user_id" :value="person.user_id">{{person.username}}</option></select><label class="sr-only" for="grant-role">知识库角色</label><select id="grant-role" v-model="grantRole"><option value="viewer">只读</option><option value="editor">编辑 · 可发布与取消发布</option></select><button class="secondary" :disabled="busy||!grantUser">授权</button></form>
      <ul class="shared-rows"><li v-for="grant in grants" :key="grant.user_id"><span>{{grant.username}}<small>{{grant.role==='viewer'?'只读':'编辑'}}</small></span><button class="secondary" :disabled="busy" @click="act(()=>api.revokeGrant(kid,grant.user_id))">撤销</button></li></ul>
    </section>
    <details v-if="admin" class="shared-block"><summary>组织成员管理</summary><p class="muted">输入已注册用户名。所有者负责管理员任免；管理员可管理普通成员。</p>
      <form class="inline-form" @submit.prevent="act(async()=>{await api.setMember(oid,username,memberRole);username=''})"><label class="sr-only" for="member-name">注册用户名</label><input id="member-name" v-model="username" required pattern="[a-z0-9_-]{3,32}" placeholder="注册用户名"/><label class="sr-only" for="member-role">组织角色</label><select id="member-role" v-model="memberRole"><option value="member">成员</option><option v-if="org?.role==='owner'" value="admin">管理员</option></select><button class="secondary" :disabled="busy||!username">添加 / 更新</button></form>
      <ul class="shared-rows"><li v-for="person in members" :key="person.user_id"><span>{{person.username}}<small>{{person.role==='owner'?'所有者':person.role==='admin'?'管理员':'成员'}}</small></span><button v-if="person.role!=='owner'&&(org?.role==='owner'||person.role==='member')" class="secondary" :disabled="busy" @click="act(()=>api.removeMember(oid,person.user_id))">移除</button></li></ul>
    </details><p v-if="loading" class="muted" role="status">正在读取共享资料…</p><p v-if="!base&&!admin" class="muted">选择一个共享库，或联系组织管理员授予访问权限。</p>
  </main></div>
</section></template>
<style scoped>
.shared-library{margin-top:24px;border-top:1px solid var(--line);padding-top:26px}.shared-library>header{display:flex;justify-content:space-between;align-items:center;gap:20px}.shared-library header small{color:var(--green);letter-spacing:.16em;font-size:10px}.shared-library h2{font-family:'Noto Serif SC',serif;font-size:26px;margin:9px 0}.shared-library header p,.muted{color:var(--muted);font-size:13px;line-height:1.8}.shared-grid{display:grid;grid-template-columns:255px minmax(0,1fr);gap:32px;margin-top:26px}.shared-grid aside{border-right:1px solid var(--line);padding-right:24px}.field{display:grid;gap:10px;font-size:12px}.field span{display:flex;gap:8px;align-items:center}.field svg{width:16px}input,select{min-width:0;max-width:100%;padding:10px;border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:5px}.inline-form{display:flex;gap:8px;margin:16px 0;flex-wrap:wrap}.inline-form input,.inline-form select{flex:1;min-width:100px}nav{display:grid;gap:6px;margin:22px 0}nav button{display:flex;align-items:center;gap:12px;text-align:left;border:0;padding:14px;background:transparent;color:var(--ink);border-radius:6px}nav button[aria-current]{background:var(--green2);color:var(--green)}nav svg{width:18px}nav small,.shared-rows small{display:block;margin-top:6px;font-size:11px;color:var(--muted)}.shared-block{padding-bottom:24px;margin-bottom:22px;border-bottom:1px solid var(--line)}h3{font-size:17px;margin:0 0 12px}.block-heading{display:flex;justify-content:space-between;align-items:center}.shared-rows{list-style:none;padding:0;margin:0}.shared-rows li{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:14px 0;border-bottom:1px solid var(--line);font-size:13px}.shared-rows span{overflow-wrap:anywhere}.shared-error,.text-danger{color:var(--red);font-size:13px}.text-danger{background:none;border:0;padding:12px 0}summary{cursor:pointer;font-size:15px;font-weight:600}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}button:disabled{opacity:.45;cursor:not-allowed}@media(max-width:780px){.shared-grid{grid-template-columns:1fr;gap:20px}.shared-grid aside{border-right:0;border-bottom:1px solid var(--line);padding:0 0 20px}nav{grid-template-columns:repeat(auto-fit,minmax(170px,1fr))}}
</style>
