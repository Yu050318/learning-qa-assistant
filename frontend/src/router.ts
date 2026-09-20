import { createRouter, createWebHistory } from 'vue-router'
import ChatView from './views/ChatView.vue'
import DocumentsView from './views/DocumentsView.vue'

export default createRouter({ history: createWebHistory(), routes: [
  { path: '/', redirect: '/chat' },
  { path: '/chat/:sessionId?', component: ChatView },
  { path: '/documents', component: DocumentsView },
] })
