<template>
  <div class="stack">
    <div class="toolbar">
      <h2>Media library</h2>
      <div class="row">
        <label class="btn">
          <input type="file" hidden @change="onFileChosen" :disabled="uploading" />
          {{ uploading ? 'Uploading…' : 'Upload file' }}
        </label>
      </div>
    </div>
    <p v-if="error" class="error">{{ error }}</p>
    <table>
      <thead>
        <tr>
          <th>Preview</th>
          <th>Name</th>
          <th>Type</th>
          <th>Size</th>
          <th>Default duration</th>
          <th>MD5</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="m in items" :key="m.id">
          <td>
            <img v-if="m.type === 'image'" :src="`/api/media/${m.id}/download`" class="thumb" />
            <span v-else class="type-icon">{{ m.type === 'video' ? '▶' : '◪' }}</span>
          </td>
          <td>
            <input v-model="m.original_name" @change="save(m)" />
          </td>
          <td>{{ m.type }}</td>
          <td class="muted">{{ formatBytes(m.size_bytes) }}</td>
          <td>
            <input type="number" min="1" v-model.number="m.default_duration" @change="save(m)" style="width: 90px;" />
          </td>
          <td class="muted mono">{{ m.md5_hash.slice(0, 10) }}…</td>
          <td>
            <button class="btn danger" @click="remove(m)">Delete</button>
          </td>
        </tr>
        <tr v-if="!items.length">
          <td colspan="7" class="muted" style="text-align: center;">No media uploaded yet.</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const items = ref([])
const error = ref(null)
const uploading = ref(false)

async function refresh() {
  error.value = null
  try { items.value = await api.listMedia() } catch (e) { error.value = e.message }
}

async function onFileChosen(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  uploading.value = true
  error.value = null
  try {
    await api.uploadMedia(file, 10)
    await refresh()
  } catch (e) {
    error.value = e.message
  } finally {
    uploading.value = false
  }
}

async function save(m) {
  try {
    await api.updateMedia(m.id, {
      original_name: m.original_name,
      default_duration: m.default_duration,
    })
  } catch (e) {
    error.value = e.message
  }
}

async function remove(m) {
  if (!confirm(`Delete "${m.original_name}"?`)) return
  try {
    await api.deleteMedia(m.id)
    items.value = items.value.filter((x) => x.id !== m.id)
  } catch (e) {
    error.value = e.message
  }
}

function formatBytes(bytes) {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let v = bytes
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(1)} ${units[i]}`
}

onMounted(refresh)
</script>

<style scoped>
.thumb { width: 64px; height: 48px; object-fit: cover; border-radius: 4px; }
.type-icon { font-size: 1.5rem; color: var(--fg-dim); }
.mono { font-family: monospace; }
.error { color: var(--err); }
</style>
