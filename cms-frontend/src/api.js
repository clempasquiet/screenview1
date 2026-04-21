const TOKEN_KEY = 'screenview.token'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {})
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const res = await fetch(path, { ...options, headers })
  if (res.status === 401) {
    setToken(null)
    if (!path.endsWith('/login-json')) window.location.hash = '#/login'
    throw new Error('Unauthorized')
  }
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`
    try {
      const err = await res.json()
      if (err.detail) message = err.detail
    } catch {}
    throw new Error(message)
  }
  if (res.status === 204) return null
  const ct = res.headers.get('content-type') || ''
  if (ct.includes('application/json')) return res.json()
  return res.text()
}

export const api = {
  login(username, password) {
    return request('/api/auth/login-json', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    })
  },
  health() { return request('/api/health') },

  listDevices() { return request('/api/devices') },
  getDevice(id) { return request(`/api/devices/${id}`) },
  updateDevice(id, patch) {
    return request(`/api/devices/${id}`, { method: 'PATCH', body: JSON.stringify(patch) })
  },
  deleteDevice(id) { return request(`/api/devices/${id}`, { method: 'DELETE' }) },

  listMedia() { return request('/api/media') },
  uploadMedia(file, default_duration = 10) {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('default_duration', String(default_duration))
    return request('/api/media', { method: 'POST', body: fd })
  },
  updateMedia(id, patch) {
    return request(`/api/media/${id}`, { method: 'PATCH', body: JSON.stringify(patch) })
  },
  deleteMedia(id) { return request(`/api/media/${id}`, { method: 'DELETE' }) },

  listSchedules() { return request('/api/schedules') },
  getSchedule(id) { return request(`/api/schedules/${id}`) },
  createSchedule(data) {
    return request('/api/schedules', { method: 'POST', body: JSON.stringify(data) })
  },
  updateSchedule(id, data) {
    return request(`/api/schedules/${id}`, { method: 'PATCH', body: JSON.stringify(data) })
  },
  deleteSchedule(id) { return request(`/api/schedules/${id}`, { method: 'DELETE' }) },
  publishSchedule(id) {
    return request(`/api/schedules/${id}/publish`, { method: 'POST' })
  },
}
