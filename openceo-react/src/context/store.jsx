import { createContext, useReducer } from "react"
import Swal from 'sweetalert2'
import client from '@/api'

function normalizeUserPayload(user) {
  if (!user || typeof user !== 'object') return null
  const normalizedUser = user?._doc
    ? { ...user._doc, ...user }
    : { ...user }
  delete normalizedUser._doc
  if (normalizedUser.password) delete normalizedUser.password
  if (!Array.isArray(normalizedUser.all_permissions) && Array.isArray(normalizedUser.permissions_cache)) {
    normalizedUser.all_permissions = [...normalizedUser.permissions_cache]
  }
  return normalizedUser
}

const initialState = {
  user: null,
  loading: false,
  query: null,
  selectedProject: localStorage.getItem("selectedProject") || "",
  toasts: [],
  notifications: [],
}

if (localStorage.getItem('heaven') && localStorage.getItem("user")) {
  initialState.user = normalizeUserPayload(JSON.parse(localStorage.getItem("user")))
}

function makeHeaven(length) {
  var result = ''
  var characters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz012345678'
  var charactersLength = characters.length
  for (var i = 0; i < length; i++)
    result += characters.charAt(Math.floor(Math.random() * charactersLength))
  return result
}

function storeReducer(state, action) {
  switch (action.type) {
    case 'LOGIN':
      return { ...state, user: action.payload }
    case 'LOGOUT':
      return { ...state, user: null }
    case 'LOADING':
      return { ...state, loading: action.payload }
    case 'SET_QUERY':
      return { ...state, query: action.payload }
    case 'SET_PROJECT':
      return { ...state, selectedProject: action.payload }
    case 'ADD_TOAST':
      return { ...state, toasts: [...state.toasts, action.payload] }
    case 'REMOVE_TOAST':
      return { ...state, toasts: state.toasts.filter(t => t.id !== action.payload) }
    case 'ADD_NOTIFICATION':
      return { ...state, notifications: [action.payload, ...state.notifications] }
    case 'CLEAR_NOTIFICATIONS':
      return { ...state, notifications: [] }
    case 'UPDATE_PROFILE':
      return { ...state, user: { ...state.user, ...action.payload } }
    default:
      return state
  }
}

export const StoreContext = createContext({
  user: null,
  loading: false,
  selectedProject: "",
  login(userData) { },
  logout() { },
  updateProfile(data) { },
  setLoading(value) { },
  refreshToken(value) { },
  setQuery(value) { },
  setProject(value) { },
  checkPermissions(permissions) { },
  checkPermissionsMenu(permissions) { },
  showErrors(errors) { },
  showSuccess(values) { },
  addToast(toast) { },
  removeToast(id) { },
  toasts: [],
  addNotification(notification) { },
  clearNotifications() { },
  notifications: [],
})

export function StoreProvider(props) {
  const [state, dispatch] = useReducer(storeReducer, initialState)

  function login(data) {
    const safeUser = normalizeUserPayload(data?.user) ?? {}
    localStorage.setItem("heaven", data.token)
    localStorage.setItem("token", makeHeaven(data.token.length))
    localStorage.setItem("sesionId", makeHeaven(data.token.length))
    localStorage.setItem("user", JSON.stringify(safeUser))
    dispatch({ type: 'LOGIN', payload: safeUser })
  }

  function logout(options = {}) {
    localStorage.clear()
    if (!options?.skipRequest) client.post("/api/auth/logout").catch(() => { })
    dispatch({ type: 'LOGOUT' })
  }

  function updateProfile(data) {
    var tempUser = JSON.parse(localStorage.getItem("user"))
    tempUser = { ...tempUser, ...data }
    localStorage.setItem("user", JSON.stringify(tempUser))
    dispatch({ type: 'UPDATE_PROFILE', payload: data })
  }

  function setLoading(value) {
    dispatch({ type: 'LOADING', payload: value })
  }

  function setQuery(value) {
    dispatch({ type: 'SET_QUERY', payload: value })
  }

  function setProject(value) {
    localStorage.setItem("selectedProject", value || "")
    dispatch({ type: 'SET_PROJECT', payload: value })
  }

  function checkPermissions(permissions) {
    if (state.user?.is_admin) return true
    const userPerms = state.user?.all_permissions
    if (!Array.isArray(userPerms)) return false
    for (var i in permissions)
      if (!userPerms.includes(permissions[i]))
        return false
    return true
  }

  function checkPermissionsMenu(permissions) {
    if (state.user?.is_admin) return true
    const userPerms = state.user?.all_permissions
    if (!Array.isArray(userPerms)) return false
    var one = false
    for (var i of permissions)
      if (userPerms.includes(i[0]))
        one = true
    return one
  }

  function refreshToken(data) {
    if (!data?.token) return
    localStorage.setItem("heaven", data.token)
    localStorage.setItem("token", makeHeaven(data.token.length))
    localStorage.setItem("sesionId", makeHeaven(data.token.length))
  }

  function showSuccess({ title = "Éxito!", message, redirect, navigate }) {
    Swal.fire({
      title,
      text: message,
      icon: 'success',
      confirmButtonColor: "#0F6FD3",
      allowOutsideClick: false
    }).then((result) => {
      if (result.isConfirmed && redirect) {
        navigate(redirect)
      }
    }).finally(() => {
      document.querySelectorAll('.swal2-container').forEach(el => {
        if (!el.querySelector('.swal2-popup')) {
          el.remove()
        }
      })
    })
  }

  function showErrors(errors) {
    const errorMessage = errors.join("\n")
    Swal.fire({
      title: 'Error!',
      text: errorMessage,
      icon: 'error',
      confirmButtonColor: "#ef4444",
    }).finally(() => {
      document.querySelectorAll('.swal2-container').forEach(el => {
        if (!el.querySelector('.swal2-popup')) {
          el.remove()
        }
      })
    })
  }

  function addToast(toast) {
    const id = Date.now() + Math.random()
    const payload = { id, duration: 5000, ...toast }
    dispatch({ type: 'ADD_TOAST', payload })
    if (payload.duration > 0) {
      setTimeout(() => removeToast(id), payload.duration)
    }
  }

  function removeToast(id) {
    dispatch({ type: 'REMOVE_TOAST', payload: id })
  }

  function addNotification(notification) {
    const id = Date.now() + Math.random()
    dispatch({ type: 'ADD_NOTIFICATION', payload: { id, timestamp: new Date().toISOString(), type: "info", ...notification } })
  }

  function clearNotifications() {
    dispatch({ type: 'CLEAR_NOTIFICATIONS' })
  }

  return (
    <StoreContext.Provider
      value={{
        user: state.user,
        loading: state.loading,
        selectedProject: state.selectedProject,
        login,
        logout,
        updateProfile,
        setLoading,
        refreshToken,
        setQuery,
        setProject,
        checkPermissions,
        checkPermissionsMenu,
        showSuccess,
        showErrors,
        addToast,
        removeToast,
        toasts: state.toasts,
        addNotification,
        clearNotifications,
        notifications: state.notifications,
      }}
      {...props}
    />
  )
}
