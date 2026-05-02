import Swal from 'sweetalert2'
import axios from 'axios'

function resolveBackendUrl() {
  const configuredUrl = String(import.meta.env.VITE_API_URL || '').trim()
  if (configuredUrl) return configuredUrl
  return 'http://localhost:18765'
}

const api_uri = resolveBackendUrl()

const client = axios.create({
  baseURL: api_uri,
  timeout: 90000,
  headers: {
    'Content-Type': 'application/json',
  },
  withCredentials: true,
})

const cleanEmptyStrings = (obj) => {
  Object.keys(obj).forEach(key => {
    if (obj[key] && typeof obj[key] === 'object') {
      cleanEmptyStrings(obj[key])
    } else if (obj[key] === '') {
      delete obj[key]
    }
  })
}

client.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('heaven')
    if (token) config.headers['Authorization'] = 'heaven'

    // Let the browser set multipart boundary when uploading FormData
    if (config.data instanceof FormData) {
      delete config.headers['Content-Type']
    }

    if (config.data && config.headers['Content-Type'] === 'application/json') {
      let data = typeof config.data === 'string' ? JSON.parse(config.data) : config.data
      cleanEmptyStrings(data)
      config.data = (typeof config.data === 'string') ? JSON.stringify(data) : data
    }

    return config
  },
  (error) => Promise.reject(error)
)

client.interceptors.response.use(
  (response) => response,
  (error) => {
    const requestUrl = String(error?.config?.url || '')
    const isLoginRequest = requestUrl.includes('/auth/login')
    const isOnLoginPage = window.location.pathname === '/login'

    if (error?.response?.status === 401) {
      if (isOnLoginPage || isLoginRequest) {
        return Promise.reject(error)
      }
      localStorage.clear()
      window.location.href = '/login'
      return Promise.reject(error)
    }

    if (error.code === 'ECONNABORTED' && error.message.includes('timeout')) {
      Swal.fire({
        title: 'Problema de conexión',
        text: 'No hay conexión a internet o el servidor es muy lento. Intenta de nuevo más tarde.',
        icon: 'warning',
        confirmButtonColor: '#0F6FD3',
        background: '#1a1a1a',
        color: '#f1f5f9',
        confirmButtonText: 'Entendido'
      }).finally(() => {
        document.querySelectorAll('.swal2-container').forEach(el => {
          if (!el.querySelector('.swal2-popup')) {
            el.remove()
          }
        })
      })
    } else if (import.meta.env.VITE_NODE_ENV !== 'production') {
      console.log(error)
      console.error('Error Axios:', error.config.url, error.response)
    }

    return Promise.reject(error)
  }
)

export default client
