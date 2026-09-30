<script setup lang="ts">
import { onMounted,ref } from 'vue';import { Brain,Plus,Trash2,RefreshCw,Save } from 'lucide-vue-next';import { useAppStore } from '../store'
const store=useAppStore(),draft=ref(''),editing=ref<Record<string,string>>({})
onMounted(()=>store.loadMemories())
async function add(){if(!draft.value.trim())return;await store.createMemory(draft.value);draft.value=''}
async function remove(id:string){if(window.confirm('确定删除这条记忆？'))await store.deleteMemory(id)}
</script>
<template><section class="documents"><header class="page-title"><div><small>PERSONAL MEMORY</small><h1>长期记忆</h1><p>这里只保存你明确要求系统记住的偏好和个人事实。</p></div></header><div class="memory-add"><textarea v-model="draft" maxlength="500" placeholder="例如：我偏好简洁的中文回答。"/><button class="primary" @click="add"><Plus/>添加记忆</button></div><div class="memory-grid"><article v-for="m in store.memories" :key="m.id" class="memory-card"><header><Brain/><span :class="['status',m.status]">{{m.status}}</span></header><textarea v-model="editing[m.id]" maxlength="500" :placeholder="m.content"/><p>{{editing[m.id]??m.content}}</p><small>{{m.created_via==='chat'?'来自对话':'手动添加'}} · v{{m.version}}</small><footer><button v-if="editing[m.id]" class="ghost" @click="store.updateMemory(m.id,editing[m.id]);editing[m.id]=''"><Save/>保存</button><button v-if="m.status==='failed'" class="ghost" @click="store.retryMemory(m.id)"><RefreshCw/>重试</button><button class="danger" @click="remove(m.id)"><Trash2/>删除</button></footer></article></div></section></template>
