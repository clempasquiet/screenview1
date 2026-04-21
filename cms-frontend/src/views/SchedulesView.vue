<template>
  <div class="stack">
    <div class="toolbar">
      <h2>Schedules</h2>
      <form class="row" @submit.prevent="create">
        <input v-model="newName" placeholder="New schedule name" required />
        <button type="submit" :disabled="!newName">Create</button>
      </form>
    </div>
    <p v-if="error" class="error">{{ error }}</p>
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>Items</th>
          <th>Updated</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="s in schedules" :key="s.id">
          <td><RouterLink :to="`/schedules/${s.id}`">{{ s.name }}</RouterLink></td>
          <td>{{ s.items.length }}</td>
          <td class="muted">{{ new Date(s.updated_at).toLocaleString() }}</td>
          <td class="actions">
            <button class="btn secondary" @click="publish(s)">Publish</button>
            <button class="btn danger" @click="remove(s)">Delete</button>
          </td>
        </tr>
        <tr v-if="!schedules.length">
          <td colspan="4" class="muted" style="text-align: center;">No schedules yet.</td>
        </tr>
      </tbody>
    </table>
    <p v-if="flash" class="flash">{{ flash }}</p>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const schedules = ref([])
const newName = ref('')
const error = ref(null)
const flash = ref(null)

async function refresh() {
  error.value = null
  try { schedules.value = await api.listSchedules() } catch (e) { error.value = e.message }
}

async function create() {
  try {
    await api.createSchedule({ name: newName.value, items: [] })
    newName.value = ''
    await refresh()
  } catch (e) { error.value = e.message }
}

async function remove(s) {
  if (!confirm(`Delete schedule "${s.name}"?`)) return
  try {
    await api.deleteSchedule(s.id)
    schedules.value = schedules.value.filter((x) => x.id !== s.id)
  } catch (e) { error.value = e.message }
}

async function publish(s) {
  try {
    const res = await api.publishSchedule(s.id)
    flash.value = `Notified ${res.notified}/${res.devices} device(s) to re-sync.`
    setTimeout(() => { flash.value = null }, 4000)
  } catch (e) { error.value = e.message }
}

onMounted(refresh)
</script>

<style scoped>
.actions { display: flex; gap: 0.5rem; }
.error { color: var(--err); }
.flash { color: var(--ok); }
</style>
