import { createRouter, createWebHashHistory } from 'vue-router'
import { useAuthStore } from './stores/auth'

const routes = [
  { path: '/', redirect: '/devices' },
  { path: '/login', name: 'login', component: () => import('./views/LoginView.vue') },
  {
    path: '/',
    component: () => import('./views/AppShell.vue'),
    meta: { requiresAuth: true },
    children: [
      { path: 'devices', name: 'devices', component: () => import('./views/DevicesView.vue') },
      { path: 'media', name: 'media', component: () => import('./views/MediaView.vue') },
      { path: 'schedules', name: 'schedules', component: () => import('./views/SchedulesView.vue') },
      { path: 'schedules/:id', name: 'schedule-edit', component: () => import('./views/ScheduleEditView.vue') },
    ],
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.meta.requiresAuth && !auth.isAuthenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && auth.isAuthenticated) {
    return { name: 'devices' }
  }
  return true
})

export default router
