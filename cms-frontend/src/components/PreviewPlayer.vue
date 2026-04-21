<template>
  <div v-if="open" class="preview-backdrop" @click.self="close" role="dialog" aria-modal="true">
    <div class="preview-frame">
      <header class="preview-header">
        <div class="titleblock">
          <h2>{{ title }}</h2>
          <p v-if="subtitle" class="muted">{{ subtitle }}</p>
        </div>
        <div class="header-actions">
          <button class="btn secondary" @click="togglePause" :title="paused ? 'Resume (space)' : 'Pause (space)'">
            {{ paused ? '▶ Resume' : '⏸ Pause' }}
          </button>
          <button class="btn secondary" @click="close" title="Close (Esc)">Close</button>
        </div>
      </header>

      <div class="stage" ref="stage">
        <div v-if="loading" class="stage-placeholder">Loading preview…</div>
        <div v-else-if="error" class="stage-placeholder error">{{ error }}</div>
        <div v-else-if="!current" class="stage-placeholder">Empty playlist — nothing to preview.</div>

        <img
          v-else-if="current.type === 'image'"
          :src="current.url"
          :alt="current.original_name"
          class="stage-media"
          @error="handleMediaError"
        />
        <video
          v-else-if="current.type === 'video'"
          ref="videoEl"
          :src="current.url"
          class="stage-media"
          autoplay
          muted
          playsinline
          @ended="advance"
          @error="handleMediaError"
        />
        <video
          v-else-if="current.type === 'stream' && isBrowserPlayableStream(current.url)"
          ref="videoEl"
          :src="current.url"
          class="stage-media"
          autoplay
          muted
          playsinline
          @ended="advance"
          @error="handleMediaError"
        />
        <div
          v-else-if="current.type === 'stream'"
          class="stage-placeholder stream-note"
        >
          <strong>Live stream preview unavailable in the browser.</strong>
          <p>
            <code>{{ current.url }}</code>
          </p>
          <p class="muted">
            Browsers cannot play RTSP / RTMP / SRT directly. The kiosk player
            (libmpv) handles them natively. Open the URL in VLC for a quick
            sanity check, or schedule it on a player to verify it on the actual
            display.
          </p>
        </div>
        <iframe
          v-else
          :src="current.url"
          class="stage-media"
          sandbox="allow-scripts allow-same-origin"
          referrerpolicy="no-referrer"
        />
      </div>

      <footer class="preview-footer">
        <button class="btn secondary" :disabled="items.length < 2" @click="step(-1)" title="Previous (←)">‹ Prev</button>
        <div class="progress">
          <div class="progress-meta">
            <span v-if="items.length">Item {{ index + 1 }} / {{ items.length }}</span>
            <span v-if="current" class="muted">· {{ current.original_name }}</span>
            <span v-if="current" class="muted">· {{ current.duration }}s</span>
          </div>
          <div class="progress-bar" v-if="current && needsTimer(current)">
            <div class="progress-bar-fill" :style="{ width: progressPct + '%' }"></div>
          </div>
        </div>
        <button class="btn secondary" :disabled="items.length < 2" @click="step(1)" title="Next (→)">Next ›</button>
      </footer>

      <ul v-if="items.length" class="timeline" role="list">
        <li
          v-for="(item, i) in items"
          :key="item.media_id + '-' + i"
          :class="{ active: i === index }"
          @click="goTo(i)"
        >
          <span class="timeline-index">{{ i + 1 }}</span>
          <span class="timeline-name">{{ item.original_name }}</span>
          <span class="timeline-type muted">{{ item.type }} · {{ item.duration }}s</span>
        </li>
      </ul>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, default: 'Preview' },
  subtitle: { type: String, default: '' },
  items: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  error: { type: String, default: '' },
})
const emit = defineEmits(['close'])

const index = ref(0)
const paused = ref(false)
const videoEl = ref(null)

const current = computed(() => props.items[index.value] || null)

// Per-item progress for images / widgets (videos use native ended event).
const elapsedMs = ref(0)
let timerId = null
let lastTick = 0

function stopTimer() {
  if (timerId !== null) {
    cancelAnimationFrame(timerId)
    timerId = null
  }
}

function isBrowserPlayableStream(url) {
  if (!url) return false
  // Browsers can natively play HLS in most cases (Safari directly, others
  // when the server serves the right MIME). RTSP/RTMP/SRT need libmpv.
  const u = url.toLowerCase()
  if (u.startsWith('http://') || u.startsWith('https://')) return true
  return false
}

function needsTimer(item) {
  if (!item) return false
  if (item.type === 'image' || item.type === 'widget') return true
  if (item.type === 'stream') {
    // The note panel and any non-browser-playable stream rely on the timer.
    // For browser-playable HLS we still drive auto-advance via the duration
    // timer because a live stream has no natural ``ended`` event.
    return true
  }
  return false
}

function startTimer() {
  stopTimer()
  // Recorded videos rely on the native ``ended`` event; everything else
  // (images, widgets, live streams of any flavour) is timer-driven.
  if (!current.value || !needsTimer(current.value)) return
  lastTick = performance.now()
  const loop = (t) => {
    if (paused.value) {
      lastTick = t
      timerId = requestAnimationFrame(loop)
      return
    }
    elapsedMs.value += t - lastTick
    lastTick = t
    const duration = (current.value?.duration || 5) * 1000
    if (elapsedMs.value >= duration) {
      advance()
      return
    }
    timerId = requestAnimationFrame(loop)
  }
  timerId = requestAnimationFrame(loop)
}

const progressPct = computed(() => {
  if (!current.value || !needsTimer(current.value)) return 0
  const total = Math.max(1, current.value.duration * 1000)
  return Math.min(100, (elapsedMs.value / total) * 100)
})

function resetProgress() {
  elapsedMs.value = 0
  stopTimer()
  startTimer()
}

function advance() {
  if (!props.items.length) return
  index.value = (index.value + 1) % props.items.length
  resetProgress()
  syncVideo()
}

function step(delta) {
  if (!props.items.length) return
  index.value = (index.value + delta + props.items.length) % props.items.length
  resetProgress()
  syncVideo()
}

function goTo(i) {
  if (i < 0 || i >= props.items.length) return
  index.value = i
  resetProgress()
  syncVideo()
}

function togglePause() {
  paused.value = !paused.value
  if (videoEl.value) {
    if (paused.value) videoEl.value.pause()
    else videoEl.value.play().catch(() => {})
  }
  if (!paused.value) {
    lastTick = performance.now()
  }
}

async function syncVideo() {
  await nextTick()
  if (videoEl.value) {
    try {
      videoEl.value.currentTime = 0
      if (!paused.value) await videoEl.value.play()
    } catch {}
  }
}

function handleMediaError() {
  // Surface a friendly note but keep the preview usable — skip to next after a moment.
  setTimeout(advance, 1500)
}

function onKey(event) {
  if (!props.open) return
  switch (event.key) {
    case 'Escape':
      close()
      break
    case 'ArrowRight':
      step(1)
      break
    case 'ArrowLeft':
      step(-1)
      break
    case ' ': // space
      event.preventDefault()
      togglePause()
      break
  }
}

function close() {
  stopTimer()
  emit('close')
}

watch(
  () => props.open,
  (val) => {
    if (val) {
      index.value = 0
      paused.value = false
      resetProgress()
      syncVideo()
      window.addEventListener('keydown', onKey)
    } else {
      stopTimer()
      window.removeEventListener('keydown', onKey)
    }
  },
)

watch(
  () => props.items,
  () => {
    // Playlist swapped (e.g. admin re-opened preview after editing).
    index.value = 0
    resetProgress()
    syncVideo()
  },
)

onBeforeUnmount(() => {
  stopTimer()
  window.removeEventListener('keydown', onKey)
})
</script>

<style scoped>
.preview-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.75);
  backdrop-filter: blur(4px);
  display: grid;
  place-items: center;
  z-index: 1000;
  padding: 1rem;
}
.preview-frame {
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: 10px;
  width: min(1100px, 100%);
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.preview-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0.9rem 1.2rem;
  border-bottom: 1px solid var(--border);
  gap: 1rem;
}
.preview-header h2 { margin: 0; font-size: 1.05rem; }
.titleblock p { margin: 0.15rem 0 0; font-size: 0.85rem; }
.header-actions { display: flex; gap: 0.5rem; }

.stage {
  flex: 1;
  min-height: 360px;
  background: #000;
  display: grid;
  place-items: center;
  overflow: hidden;
}
.stage-media {
  max-width: 100%;
  max-height: 70vh;
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: #000;
  border: none;
}
.stage-placeholder {
  color: var(--fg-dim);
  font-size: 0.95rem;
  text-align: center;
  padding: 1.5rem;
  max-width: 720px;
}
.stage-placeholder.error { color: var(--err); }
.stream-note p { margin: 0.5rem 0; }
.stream-note code {
  background: var(--bg-3);
  color: var(--fg);
  padding: 0.15rem 0.4rem;
  border-radius: 4px;
  word-break: break-all;
}

.preview-footer {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 0.75rem;
  align-items: center;
  padding: 0.6rem 1.2rem;
  border-top: 1px solid var(--border);
}
.progress { display: flex; flex-direction: column; gap: 0.35rem; }
.progress-meta { font-size: 0.8rem; display: flex; gap: 0.5rem; flex-wrap: wrap; }
.progress-bar {
  height: 3px;
  background: var(--bg-3);
  border-radius: 2px;
  overflow: hidden;
}
.progress-bar-fill {
  height: 100%;
  background: var(--accent);
  transition: width 80ms linear;
}

.timeline {
  list-style: none;
  margin: 0;
  padding: 0.5rem 1rem;
  border-top: 1px solid var(--border);
  display: flex;
  gap: 0.5rem;
  overflow-x: auto;
  max-height: 6rem;
}
.timeline li {
  flex: 0 0 auto;
  display: grid;
  grid-template-columns: auto 1fr;
  grid-template-rows: auto auto;
  column-gap: 0.5rem;
  padding: 0.45rem 0.7rem;
  border-radius: 6px;
  background: var(--bg-3);
  cursor: pointer;
  min-width: 160px;
  border: 1px solid transparent;
}
.timeline li:hover { border-color: var(--border); }
.timeline li.active { border-color: var(--accent); background: rgba(79, 140, 255, 0.1); }
.timeline-index {
  grid-row: 1 / span 2;
  align-self: center;
  font-weight: 600;
  color: var(--fg-dim);
}
.timeline-name { font-size: 0.85rem; white-space: nowrap; max-width: 180px; overflow: hidden; text-overflow: ellipsis; }
.timeline-type { font-size: 0.7rem; }
</style>
