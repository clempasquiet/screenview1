<template>
  <div class="stack" v-if="schedule">
    <div class="toolbar">
      <div>
        <RouterLink to="/schedules" class="muted">← All schedules</RouterLink>
        <h2>
          <input v-model="schedule.name" @change="saveMeta" style="font-size: inherit; font-weight: inherit; max-width: 420px;" />
        </h2>
      </div>
      <div class="row">
        <button class="btn secondary" @click="openPreview" :disabled="previewLoading">
          {{ previewLoading ? 'Loading…' : '▶ Preview' }}
        </button>
        <button class="btn secondary" @click="publish">Publish to devices</button>
        <button class="btn" @click="saveAll">Save</button>
      </div>
    </div>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="flash" class="flash">{{ flash }}</p>

    <PreviewPlayer
      :open="previewOpen"
      :title="previewTitle"
      :subtitle="previewSubtitle"
      :items="previewItems"
      :loading="previewLoading"
      :error="previewError"
      @close="previewOpen = false"
    />

    <div class="grid">
      <section class="card">
        <h3>Playlist</h3>
        <ol class="items">
          <li v-for="(item, idx) in schedule.items" :key="item._key">
            <div class="item-row">
              <span class="order">{{ idx + 1 }}</span>
              <span class="name">{{ mediaLabel(item.media_id) }}</span>
              <label class="dur">
                <span class="muted">Duration (s)</span>
                <input
                  type="number"
                  min="1"
                  :placeholder="defaultDuration(item.media_id)"
                  v-model.number="item.duration_override"
                  style="width: 90px;"
                />
              </label>
              <div class="row">
                <button class="btn secondary" :disabled="idx === 0" @click="move(idx, -1)">↑</button>
                <button class="btn secondary" :disabled="idx === schedule.items.length - 1" @click="move(idx, 1)">↓</button>
                <button class="btn danger" @click="removeItem(idx)">✕</button>
              </div>
            </div>
          </li>
          <li v-if="!schedule.items.length" class="muted">Empty — add media from the library →</li>
        </ol>
      </section>

      <section class="card">
        <h3>Media library</h3>
        <ul class="library">
          <li v-for="m in library" :key="m.id">
            <span>{{ m.original_name }}</span>
            <span class="muted">{{ m.type }} · {{ m.default_duration }}s</span>
            <button class="btn secondary" @click="addItem(m)">Add →</button>
          </li>
        </ul>
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, RouterLink } from 'vue-router'
import { api } from '../api'
import PreviewPlayer from '../components/PreviewPlayer.vue'

const route = useRoute()
const schedule = ref(null)
const library = ref([])
const error = ref(null)
const flash = ref(null)

// Preview modal state — fetched on demand so the signed URLs stay fresh.
const previewOpen = ref(false)
const previewLoading = ref(false)
const previewError = ref('')
const previewItems = ref([])
const previewTitle = ref('')
const previewSubtitle = ref('')

let keyCounter = 0
const nextKey = () => `k${++keyCounter}`

async function load() {
  error.value = null
  try {
    const [s, lib] = await Promise.all([api.getSchedule(route.params.id), api.listMedia()])
    s.items = s.items
      .sort((a, b) => a.order - b.order)
      .map((it) => ({ ...it, _key: nextKey() }))
    schedule.value = s
    library.value = lib
  } catch (e) { error.value = e.message }
}

const mediaById = computed(() => Object.fromEntries(library.value.map((m) => [m.id, m])))
function mediaLabel(id) { return mediaById.value[id]?.original_name || `Media #${id}` }
function defaultDuration(id) { return mediaById.value[id]?.default_duration ?? 10 }

function addItem(media) {
  schedule.value.items.push({
    _key: nextKey(),
    media_id: media.id,
    order: schedule.value.items.length,
    duration_override: null,
  })
}
function removeItem(idx) {
  schedule.value.items.splice(idx, 1)
}
function move(idx, dir) {
  const items = schedule.value.items
  const j = idx + dir
  if (j < 0 || j >= items.length) return
  ;[items[idx], items[j]] = [items[j], items[idx]]
}

function normalizedItems() {
  return schedule.value.items.map((it, i) => ({
    media_id: it.media_id,
    order: i,
    duration_override: it.duration_override || null,
  }))
}

async function saveMeta() {
  try {
    await api.updateSchedule(schedule.value.id, { name: schedule.value.name })
  } catch (e) { error.value = e.message }
}

async function saveAll() {
  try {
    const updated = await api.updateSchedule(schedule.value.id, {
      name: schedule.value.name,
      description: schedule.value.description,
      items: normalizedItems(),
    })
    updated.items = updated.items
      .sort((a, b) => a.order - b.order)
      .map((it) => ({ ...it, _key: nextKey() }))
    schedule.value = updated
    flash.value = 'Schedule saved.'
    setTimeout(() => { flash.value = null }, 3000)
  } catch (e) { error.value = e.message }
}

async function publish() {
  try {
    await saveAll()
    const res = await api.publishSchedule(schedule.value.id)
    flash.value = `Notified ${res.notified}/${res.devices} device(s).`
    setTimeout(() => { flash.value = null }, 4000)
  } catch (e) { error.value = e.message }
}

async function openPreview() {
  // Save any pending changes first so the preview matches what the
  // players would actually get. This also guarantees the server-side
  // item ordering is in sync with our local drag state.
  previewError.value = ''
  previewLoading.value = true
  try {
    await saveAll()
    const preview = await api.previewSchedule(schedule.value.id)
    previewItems.value = preview.items
    previewTitle.value = `Preview · ${preview.schedule_name}`
    previewSubtitle.value = preview.items.length
      ? `${preview.items.length} item(s) · URLs expire in ~15 min`
      : 'Empty schedule'
    previewOpen.value = true
  } catch (e) {
    previewError.value = e.message
    previewItems.value = []
    previewOpen.value = true
  } finally {
    previewLoading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.grid { display: grid; grid-template-columns: 2fr 1fr; gap: 1rem; }
.items { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 0.5rem; }
.item-row {
  display: grid;
  grid-template-columns: 32px 1fr auto auto;
  gap: 0.75rem;
  align-items: center;
  background: var(--bg-3);
  padding: 0.5rem 0.75rem;
  border-radius: 6px;
}
.order { font-weight: 600; color: var(--fg-dim); }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dur { display: flex; flex-direction: column; font-size: 0.75rem; }
.library { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 0.4rem; }
.library li {
  display: grid;
  grid-template-columns: 1fr auto auto;
  gap: 0.5rem;
  align-items: center;
  background: var(--bg-3);
  padding: 0.4rem 0.6rem;
  border-radius: 6px;
  font-size: 0.85rem;
}
.error { color: var(--err); }
.flash { color: var(--ok); }
@media (max-width: 900px) {
  .grid { grid-template-columns: 1fr; }
}
</style>
