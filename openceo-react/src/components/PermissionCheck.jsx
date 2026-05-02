import { useContext } from 'react'
import { StoreContext } from '@/context/store'

export function PermissionCheck({ perms, mode = 'all', fallback = null, children }) {
  const store = useContext(StoreContext)

  if (mode === 'any') {
    const hasAny = perms.some((p) => store.checkPermissions([p]))
    if (!hasAny) return fallback
  } else {
    if (!store.checkPermissions(perms)) return fallback
  }

  return children
}
