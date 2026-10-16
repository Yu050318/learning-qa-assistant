import { createRouter, createWebHistory } from 'vue-router'
import ChatView from './views/ChatView.vue'
import DocumentsView from './views/DocumentsView.vue'
import AuthView from './views/AuthView.vue'
import MemoriesView from './views/MemoriesView.vue'
import ModelsView from './views/ModelsView.vue'

export default createRouter({ history: createWebHistory(), routes: [
  { path: '/', redirect: '/chat' },
  { path: '/chat/:sessionId?', component: ChatView },
  { path: '/documents', component: DocumentsView },
  { path: '/memories', component: MemoriesView },
  { path: '/models', component: ModelsView },
  { path: '/login', component: AuthView },
] })
