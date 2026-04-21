<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">ScreenView</div>
      <nav>
        <RouterLink to="/devices">Devices</RouterLink>
        <RouterLink to="/media">Media</RouterLink>
        <RouterLink to="/schedules">Schedules</RouterLink>
      </nav>
      <div class="spacer" />
      <button class="btn secondary" @click="logout">Sign out</button>
    </aside>
    <main class="content">
      <RouterView />
    </main>
  </div>
</template>

<script setup>
import { RouterView, RouterLink, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()

function logout() {
  auth.logout()
  router.push('/login')
}
</script>

<style scoped>
.shell {
  display: grid;
  grid-template-columns: 220px 1fr;
  height: 100vh;
}
.sidebar {
  background: var(--bg-2);
  border-right: 1px solid var(--border);
  padding: 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.brand {
  font-weight: 700;
  font-size: 1.25rem;
  margin-bottom: 1rem;
  color: var(--accent-2);
}
nav {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
nav a {
  padding: 0.5rem 0.75rem;
  border-radius: 6px;
  color: var(--fg);
}
nav a.router-link-active {
  background: var(--bg-3);
  color: var(--accent-2);
}
nav a:hover { text-decoration: none; background: var(--bg-3); }
.spacer { flex: 1; }
.content {
  padding: 1.5rem 2rem;
  overflow: auto;
}
</style>
