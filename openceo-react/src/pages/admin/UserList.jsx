import { useContext, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import { PlusIcon, UsersIcon, MagnifyingGlassIcon, ChevronLeftIcon, ChevronRightIcon } from '@heroicons/react/24/outline'
import client from '@/api'
import { StoreContext } from '@/context/store'
import OptionsCRUD from '@/components/OptionsCRUD'
import { swalConfirm } from '@/utils/swalConfirm'
import { swalError } from '@/utils/swalError'

export default function UserList() {
  const store = useContext(StoreContext)
  const [users, setUsers] = useState([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [itemsPerPage, setItemsPerPage] = useState(10)

  const handleItemsPerPageChange = (value) => {
    setItemsPerPage(Number(value))
    setPage(1)
  }

  const prefersReducedMotion = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  useGSAP(() => {
    if (prefersReducedMotion) return
    const ctx = gsap.context(() => {
      gsap.from('.user-row', { opacity: 0, y: 10, stagger: 0.05, duration: 0.3, ease: 'power2.out' })
    })
    return () => ctx.revert()
  }, [users, prefersReducedMotion])

  useEffect(() => { loadUsers() }, [])

  const loadUsers = async () => {
    setLoading(true)
    try {
      const res = await client.get('/api/users')
      setUsers(res.data.data || res.data || [])
    } catch { /* silent */ } finally { setLoading(false) }
  }

  const handleDelete = async (userId) => {
    const confirmed = await swalConfirm({
      title: '¿Eliminar usuario?',
      text: 'Esta acción no se puede deshacer.',
      icon: 'warning',
      confirmText: 'Eliminar',
    })
    if (!confirmed) return
    try {
      await client.delete(`/api/users/${userId}`)
      loadUsers()
    } catch (err) {
      swalError(err)
    }
  }

  const filtered = users.filter(u => {
    const q = search.toLowerCase()
    if (!q) return true
    return (
      u.username?.toLowerCase().includes(q) ||
      u.display_name?.toLowerCase().includes(q) ||
      u.email?.toLowerCase().includes(q)
    )
  })

  const totalPages = Math.ceil(filtered.length / itemsPerPage)
  const paginated = filtered.slice((page - 1) * itemsPerPage, page * itemsPerPage)

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="animate-spin rounded-full h-10 w-10 border-2 border-oceo-blue-400 border-t-transparent" />
      </div>
    )
  }

  return (
    <div className="users-page">
      {/* Header */}
      <div className="section-header">
        <div>
          <h1 className="dashboard-title">Usuarios</h1>
          <p className="dashboard-subtitle">Gestión de usuarios del sistema</p>
        </div>
        <div className="dashboard-header-actions">
          <span className="results-count">{filtered.length} resultado{filtered.length !== 1 ? 's' : ''}</span>
          <select
            value={itemsPerPage}
            onChange={(e) => handleItemsPerPageChange(e.target.value)}
            className="input-oceo input-sm"
            style={{ width: 90 }}
          >
            <option value={10}>10 / pág</option>
            <option value={50}>50 / pág</option>
            <option value={100}>100 / pág</option>
            <option value={200}>200 / pág</option>
            <option value={500}>500 / pág</option>
          </select>
          <div className="input-icon-wrap search-inline">
            <MagnifyingGlassIcon className="input-icon" />
            <input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Buscar..."
              className="input-oceo input-with-icon input-sm"
            />
          </div>
          <Link to="/dashboard/admin/users/create" className="btn-primary btn-sm">
            <PlusIcon className="w-4 h-4" />
            Nuevo usuario
          </Link>
        </div>
      </div>

      {/* Table */}
      <div className="card-oceo table-card">
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>
                  <div className="th-content">
                    <UsersIcon className="w-4 h-4" />
                    Usuario
                  </div>
                </th>
                <th>Nombre</th>
                <th>Email</th>
                <th>Rol</th>
                <th style={{ width: '60px' }}></th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={5} className="empty-cell">
                    <div className="empty-state">
                      <UsersIcon className="w-10 h-10" style={{ color: 'var(--oceo-text-3)' }} />
                      <p>No se encontraron usuarios</p>
                    </div>
                  </td>
                </tr>
              ) : (
                paginated.map((user) => (
                  <tr key={user.user_id || user._id || user.id} className="user-row">
                    <td>
                      <div className="user-cell">
                        <div className="user-avatar">{(user.display_name || user.username || 'U').charAt(0).toUpperCase()}</div>
                        <span className="user-username">{user.username}</span>
                      </div>
                    </td>
                    <td className="user-display-name">{user.display_name || '—'}</td>
                    <td className="user-email">{user.email || '—'}</td>
                    <td>
                      {user.is_admin ? (
                        <span className="status-badge badge-active">Admin</span>
                      ) : (
                        <span className="status-badge badge-active" style={{ background: 'rgba(126,139,153,0.12)', color: 'var(--oceo-text-3)', borderColor: 'rgba(126,139,153,0.25)' }}>Usuario</span>
                      )}
                    </td>
                    <td>
                      <OptionsCRUD
                        editHref={`/dashboard/admin/users/update/${user.user_id || user._id || user.id}`}
                        onDelete={() => handleDelete(user.user_id || user._id || user.id)}
                        editPerm="manage_users"
                        deletePerm="manage_users"
                        itemName="usuario"
                      />
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="pagination">
            <span className="pagination-info">
              Página {page} de {totalPages}
            </span>
            <div className="pagination-buttons">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page === 1}
                className="btn-secondary btn-sm"
              >
                <ChevronLeftIcon className="w-4 h-4" />
                Anterior
              </button>
              <button
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page === totalPages}
                className="btn-secondary btn-sm"
              >
                Siguiente
                <ChevronRightIcon className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
