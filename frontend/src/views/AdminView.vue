<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { ShieldCheck, RefreshCw, ArrowUpRight } from 'lucide-vue-next'
import { api } from '../api'

const overview = ref<Awaited<ReturnType<typeof api.adminOverview>> | null>(null)
const error = ref(''), loading = ref(false)
const labels: Record<string, string> = {
  model_center: '模型中心', sharing: '组织与共享知识库', document_versions: '文档版本',
  durable_runs: 'Run / Event 持久化', hierarchy: '三级分块', knowledge_quality: '知识规划与证据判断', rerank: '检索精排',
}
async function load() {
  loading.value = true; error.value = ''
  try { overview.value = await api.adminOverview() }
  catch (e: any) { error.value = e.message; overview.value = null }
  finally { loading.value = false }
}
onMounted(load)
</script>

<template>
  <section class="documents admin-page" :aria-busy="loading">
    <header class="page-title">
      <div><small>PLATFORM ADMINISTRATION</small><h1>平台管理</h1><p>检查能力配置，进入已开放的管理功能。</p></div>
      <button class="secondary" :disabled="loading" @click="load"><RefreshCw :size="16" />刷新配置</button>
    </header>
    <p v-if="error" class="stream-error" role="alert">{{ error }}</p>
    <template v-if="overview">
      <div class="admin-note"><ShieldCheck :size="22" /><div><strong>平台管理员</strong><p>管理权限不改变私人资料、会话与组织知识库的访问边界。</p></div></div>
      <section class="admin-section"><h2>能力配置</h2><p class="muted">{{ overview.notice }}</p>
        <dl class="feature-list"><div v-for="(enabled, key) in overview.features" :key="key"><dt>{{ labels[key] || key }}</dt><dd :class="enabled ? 'enabled' : 'disabled'">{{ enabled ? '已启用' : '未启用' }}</dd></div>
          <div><dt>入库执行方式</dt><dd>{{ overview.ingestion_mode === 'durable' ? '独立持久 Worker' : 'API 进程内执行' }}</dd></div>
        </dl>
      </section>
      <section class="admin-section"><h2>管理入口</h2><div class="admin-links">
        <RouterLink v-if="overview.features.model_center" to="/models"><span><strong>模型中心</strong><small>模型配置、目录检测与调用统计</small></span><ArrowUpRight :size="20" /></RouterLink>
        <RouterLink v-if="overview.features.sharing" to="/documents"><span><strong>组织与共享知识库</strong><small>在资料页面切换至“组织与共享库”管理成员与授权</small></span><ArrowUpRight :size="20" /></RouterLink>
        <p v-if="!overview.features.model_center && !overview.features.sharing" class="muted">管理能力尚未启用。完成数据库迁移与部署预检后，再由服务端配置开启。</p>
      </div></section>
    </template>
  </section>
</template>

<style scoped>
.admin-page{max-width:1200px;margin:auto}.admin-note{display:flex;gap:16px;padding:22px 24px;background:var(--green2);border:1px solid #c8d8cc;border-radius:8px;color:var(--green);margin:30px 0}.admin-note p{margin:7px 0 0;font-size:13px;line-height:1.7}.admin-section{margin:32px 0}.admin-section h2{font-family:'Noto Serif SC',serif;font-size:21px}.muted{color:var(--muted);font-size:13px;line-height:1.7}.feature-list{margin:18px 0;border:1px solid var(--line);border-radius:8px;background:var(--surface);overflow:hidden}.feature-list>div{display:flex;justify-content:space-between;gap:18px;padding:16px 22px;border-bottom:1px solid var(--line);font-size:14px}.feature-list>div:last-child{border:0}.feature-list dd{margin:0}.enabled{color:var(--green);font-weight:600}.disabled{color:var(--muted)}.admin-links{display:grid;gap:12px}.admin-links a{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:20px 22px;border:1px solid var(--line);border-radius:8px;color:var(--green);background:var(--surface);text-decoration:none}.admin-links small{display:block;margin-top:7px;color:var(--muted);line-height:1.6}.admin-links a:hover{border-color:var(--green)}
</style>
