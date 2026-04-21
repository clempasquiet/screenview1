<template>
  <div class="stack">
    <div class="toolbar">
      <h2>Devices</h2>
      <button class="btn secondary" @click="refresh">Refresh</button>
    </div>
    <p v-if="error" class="error">{{ error }}</p>
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>MAC</th>
          <th>Status</th>
          <th>Schedule</th>
          <th>Last ping</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="d in devices" :key="d.id">
          <td>
            <input v-model="d.name" @change="save(d)" />
          </td>
          <td class="muted">{{ d.mac_address }}</td>
          <td><span class="status-pill" :class="d.status">{{ d.status }}</span></td>
          <td>
            <select v-model.number="d.current_schedule_id" @change="save(d)">
              <option :value="null">— None —</option>
              <option v-for="s in schedules" :key="s.id" :value="s.id">{{ s.name }}</option>
            </select>
          </td>
          <td class="muted">{{ d.last_ping ? new Date(d.last_ping).toLocaleString() : '—' }}</td>
          <td class="actions">
            <button v-if="d.status !== 'active'" class="btn" @click="approve(d)">Approve</button>
            <button class="btn danger" @click="remove(d)">Delete</button>
          </td>
        </tr>
        <tr v-if="!devices.length">
          <td colspan="6" class="muted" style="text-align: center;">No devices registered yet.</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const devices = ref([])
const schedules = ref([])
const error = ref(null)

async function refresh() {
  error.value = null
  try {
    const [d, s] = await Promise.all([api.listDevices(), api.listSchedules()])
    devices.value = d
    schedules.value = s
  } catch (e) {
    error.value = e.message
  }
}

async function save(device) {
  try {
    await api.updateDevice(device.id, {
      name: device.name,
      current_schedule_id: device.current_schedule_id,
    })
  } catch (e) {
    error.value = e.message
  }
}

async function approve(device) {
  try {
    const updated = await api.updateDevice(device.id, { status: 'active' })
    Object.assign(device, updated)
  } catch (e) {
    error.value = e.message
  }
}

async function remove(device) {
  if (!confirm(`Delete device "${device.name}"?`)) return
  try {
    await api.deleteDevice(device.id)
    devices.value = devices.value.filter((d) => d.id !== device.id)
  } catch (e) {
    error.value = e.message
  }
}

onMounted(refresh)
</script>

<style scoped>
.actions { display: flex; gap: 0.5rem; }
.error { color: var(--err); }
</style>
