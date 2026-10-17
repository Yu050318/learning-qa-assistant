import { createRouter, createWebHistory } from 'vue-router'
import ChatView from './views/ChatView.vue'
import DocumentsView from './views/DocumentsView.vue'
import AuthView from './views/AuthView.vue'
import MemoriesView from './views/MemoriesView.vue'
import ModelsView from './views/ModelsView.vue'
import AdminView from './views/AdminView.vue'
import { useAppStore } from './store'

const router = createRouter({ history: createWebHistory(), routes: [
  { path: '/', redirect: '/chat' },
  { path: '/chat/:sessionId?', component: ChatView },
  { path: '/documents', component: DocumentsView },
  { path: '/memories', component: MemoriesView },
  { path: '/models', component: ModelsView },
  { path: '/admin', component: AdminView, meta: { admin: true } },
  { path: '/login', component: AuthView },
] })

router.beforeEach(async to => {
  const store = useAppStore()
  await store.bootstrap()
  if (!store.user && to.path !== '/login') return '/login'
  if (store.user && to.path === '/login') return '/chat'
  if (to.meta.admin && store.user?.role !== 'admin') return '/chat'
})

export default router
