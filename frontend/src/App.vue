<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, RouterView } from 'vue-router'
import { MessageSquareText, Library, Leaf, UserRound } from 'lucide-vue-next'
import { useAppStore } from './store'
import UploadDrawer from './components/UploadDrawer.vue'
const store=useAppStore(),identity=ref(store.userId)
onMounted(()=>store.initialize())
</script>
<template><div class="shell">
  <aside class="rail"><div class="brand"><Leaf :size="23"/><span>Learning Q&A</span></div><nav>
    <RouterLink to="/chat"><MessageSquareText/><span>对话</span></RouterLink>
    <RouterLink to="/documents"><Library/><span>资料</span></RouterLink>
  </nav><div class="identity"><UserRound :size="17"/><input v-model="identity" aria-label="开发用户标识" @change="store.switchUser(identity)"/></div></aside>
  <main><div v-if="store.error" class="global-error">{{store.error}}</div><RouterView/></main>
  <UploadDrawer/>
</div></template>
