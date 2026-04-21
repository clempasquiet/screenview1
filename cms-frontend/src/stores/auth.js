import { defineStore } from 'pinia'
import { api, getToken, setToken } from '../api'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: getToken(),
    username: null,
    error: null,
    loading: false,
  }),
  getters: {
    isAuthenticated: (state) => !!state.token,
  },
  actions: {
    async login(username, password) {
      this.loading = true
      this.error = null
      try {
        const res = await api.login(username, password)
        setToken(res.access_token)
        this.token = res.access_token
        this.username = username
      } catch (e) {
        this.error = e.message
        throw e
      } finally {
        this.loading = false
      }
    },
    logout() {
      setToken(null)
      this.token = null
      this.username = null
    },
  },
})
