<script setup lang="ts">
import { computed } from 'vue';import { X, Pause, Play, UploadCloud } from 'lucide-vue-next';import { useAppStore } from '../store'
const q=useAppStore().uploads,state=q.state
const done=computed(()=>state.tasks.filter(x=>['ready','failed','skipped'].includes(x.state)).length)
const label=(s:string)=>({queued:'等待上传',transferring:'上传中',processing:'正在准备资料',ready:'上传成功',failed:'上传失败',skipped:'已跳过'}[s]||s)
</script>
<template><div v-if="state.open" class="drawer-mask" @click.self="state.open=false"><aside class="drawer">
  <header><div><small>资料入库</small><h2>上传任务</h2></div><button class="icon" aria-label="关闭" @click="state.open=false"><X/></button></header>
  <div class="upload-summary"><UploadCloud/><strong>{{done}} / {{state.tasks.length}}</strong><span>项已结束</span></div>
  <div class="task-list"><article v-for="task in state.tasks" :key="task.key"><div class="file-mark">{{task.name.split('.').pop()?.toUpperCase()}}</div><div><strong>{{task.path}}</strong><p :class="task.state">{{label(task.state)}}<span v-if="task.state==='transferring'"> · {{Math.round(task.progress*100)}}%</span></p><small v-if="task.error">{{task.error}}</small></div></article></div>
  <footer><button v-if="!state.paused" class="secondary" @click="q.stop"><Pause/>停止剩余上传</button><button v-else class="secondary" @click="q.resume"><Play/>继续</button></footer>
</aside></div></template>
