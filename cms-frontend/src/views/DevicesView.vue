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
          <th>Token</th>
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
          <td class="token-cell">
            <span v-if="d.has_api_token" class="muted mono" :title="d.api_token_issued_at
              ? `Issued ${new Date(d.api_token_issued_at).toLocaleString()}`
              : 'Token set'">
              ●●●●●●●●
            </span>
            <span v-else class="status-pill rejected">none</span>
          </td>
          <td class="muted">{{ d.last_ping ? new Date(d.last_ping).toLocaleString() : '—' }}</td>
          <td class="actions">
            <button v-if="d.status !== 'active'" class="btn" @click="approve(d)">Approve</button>
            <button class="btn secondary" @click="rotateToken(d)" title="Invalidate the current token; the player will automatically re-register on its next call.">
              Rotate token
            </button>
            <button class="btn danger" @click="remove(d)">Delete</button>
          </td>
        </tr>
        <tr v-if="!devices.length">
          <td colspan="7" class="muted" style="text-align: center;">No devices registered yet.</td>
        </tr>
      </tbody>
    </table>

    <div v-if="rotatedCredentials" class="card credentials-banner">
      <h3>New API token for {{ rotatedCredentials.name }}</h3>
      <p class="muted">
        The player will automatically pick up the new token the next time it calls
        the server. Copy this value only if you need to provision the token manually.
        It will not be shown again.
      </p>
      <div class="row token-row">
        <code class="mono token-value">{{ rotatedCredentials.api_token }}</code>
        <button class="btn secondary" @click="copyRotated">Copy</button>
        <button class="btn" @click="dismissRotated">Dismiss</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const devices = ref([])
const schedules = ref([])
const error = ref(null)
const rotatedCredentials = ref(null)

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

async function rotateToken(device) {
  if (
    !confirm(
      `Rotate the API token for "${device.name}"?\n\n` +
      `The current token is invalidated immediately. The player will see a 401 on its ` +
      `next request and will transparently re-register to pick up the new one.`
    )
  ) return
  try {
    const creds = await api.rotateDeviceToken(device.id)
    rotatedCredentials.value = creds
    device.has_api_token = true
    device.api_token_issued_at = creds.api_token_issued_at
  } catch (e) {
    error.value = e.message
  }
}

function dismissRotated() {
  rotatedCredentials.value = null
}

async function copyRotated() {
  if (!rotatedCredentials.value) return
  try {
    await navigator.clipboard.writeText(rotatedCredentials.value.api_token)
  } catch (e) {
    error.value = `Copy failed: ${e.message}`
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
.actions { display: flex; gap: 0.5rem; flex-wrap: wrap; }
.error { color: var(--err); }
.mono { font-family: monospace; font-size: 0.9em; }
.token-cell { text-align: center; }
.credentials-banner {
  margin-top: 1rem;
  border-color: var(--accent);
}
.credentials-banner h3 { margin: 0 0 0.25rem; }
.token-row {
  margin-top: 0.75rem;
  align-items: center;
}
.token-value {
  flex: 1;
  padding: 0.5rem 0.75rem;
  background: var(--bg-3);
  border-radius: 6px;
  word-break: break-all;
}
</style>
