<template>
  <div class="login-wrap">
    <form class="card login-card stack" @submit.prevent="submit">
      <h1>ScreenView CMS</h1>
      <p class="muted">Sign in to manage your digital signage network.</p>
      <label>
        Username
        <input v-model="username" autocomplete="username" autofocus required />
      </label>
      <label>
        Password
        <input v-model="password" type="password" autocomplete="current-password" required />
      </label>
      <button type="submit" :disabled="auth.loading">
        {{ auth.loading ? 'Signing in…' : 'Sign in' }}
      </button>
      <p v-if="auth.error" class="error">{{ auth.error }}</p>
    </form>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const username = ref('admin')
const password = ref('')

async function submit() {
  try {
    await auth.login(username.value, password.value)
    router.push(route.query.redirect || '/devices')
  } catch {}
}
</script>

<style scoped>
.login-wrap {
  display: grid;
  place-items: center;
  height: 100vh;
  padding: 1rem;
}
.login-card {
  width: 100%;
  max-width: 360px;
}
h1 { margin: 0; }
label { display: block; font-size: 0.85rem; color: var(--fg-dim); }
label input { margin-top: 0.25rem; }
.error { color: var(--err); margin: 0; }
</style>
