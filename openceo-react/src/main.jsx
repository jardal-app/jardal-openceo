import React from 'react'
import ReactDOM from 'react-dom/client'
import { RouterProvider } from "react-router-dom"
import router from './router'
import { StoreProvider } from './context/store'
import Swal from 'sweetalert2'
import './index.css'
import './pages/pages.css'

/* ── SweetAlert2 Dark Theme Defaults ── */
const swalDefaults = {
  background: '#0F0F0F',
  color: '#f1f5f9',
  backdrop: 'rgba(10, 10, 10, 0.85)',
  confirmButtonColor: '#0F6FD3',
  cancelButtonColor: '#334155',
  confirmButtonText: 'OK',
  cancelButtonText: 'Cancelar',
  allowOutsideClick: false,
  allowEscapeKey: false,
  showClass: { popup: 'swal-dark-show' },
  hideClass: { popup: 'swal-dark-hide' },
}
const originalSwalFire = Swal.fire.bind(Swal)
Swal.fire = (...args) => {
  if (args.length === 1 && args[0] && typeof args[0] === 'object' && !Array.isArray(args[0])) {
    return originalSwalFire({
      ...swalDefaults,
      ...args[0],
      showClass: { ...swalDefaults.showClass, ...args[0].showClass },
      hideClass: { ...swalDefaults.hideClass, ...args[0].hideClass },
    })
  }
  return originalSwalFire(...args)
}

const chunkReloadKey = 'openceo:chunk-reload'
const chunkReloadCooldownMs = 30_000

function isDynamicImportFailure(error) {
  const message = String(error?.message || error || '')
  return (
    message.includes('Failed to fetch dynamically imported module') ||
    message.includes('Importing a module script failed') ||
    message.includes('Failed to load module script')
  )
}

function reloadAfterChunkFailure() {
  if (typeof window === 'undefined') return
  const lastReloadAt = Number(window.sessionStorage.getItem(chunkReloadKey) || 0)
  if (lastReloadAt && Date.now() - lastReloadAt < chunkReloadCooldownMs) return
  window.sessionStorage.setItem(chunkReloadKey, String(Date.now()))
  window.location.reload()
}

if (typeof window !== 'undefined') {
  window.addEventListener('vite:preloadError', (event) => {
    event.preventDefault()
    reloadAfterChunkFailure()
  })
  window.addEventListener('unhandledrejection', (event) => {
    if (!isDynamicImportFailure(event?.reason)) return
    event.preventDefault?.()
    reloadAfterChunkFailure()
  })
  window.setTimeout(() => {
    const lastReloadAt = Number(window.sessionStorage.getItem(chunkReloadKey) || 0)
    if (!lastReloadAt) return
    if (Date.now() - lastReloadAt >= chunkReloadCooldownMs) {
      window.sessionStorage.removeItem(chunkReloadKey)
    }
  }, chunkReloadCooldownMs)
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <StoreProvider>
    <RouterProvider router={router} />
  </StoreProvider>
)
