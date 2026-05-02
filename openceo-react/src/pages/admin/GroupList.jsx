import { useContext, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import { PlusIcon, UserGroupIcon, MagnifyingGlassIcon, ChevronLeftIcon, ChevronRightIcon } from '@heroicons/react/24/outline'
import client from '@/api'
import { StoreContext } from '@/context/store'
import OptionsCRUD from '@/components/OptionsCRUD'
import { swalConfirm } from '@/utils/swalConfirm'
import { swalError } from '@/utils/swalError'

const PERM_CATEGORIES = {
  tickets: {
    label: 'Tickets',
    perms: ['read_tickets', 'write_tickets', 'approve_tickets', 'reject_tickets', 'review_tickets'],
    className: '',
  },
  admin: {
    label: 'Administración',
    perms: ['manage_users', 'manage_groups'],
    className: 'perm-badge-admin',
  },
  dashboard: {
    label: 'Dashboard',
    perms: ['admin_dashboard'],
    className: 'perm-badge-review',
  },
  client: {
    label: 'Cliente',
    perms: ['client_review'],
    className: 'perm-badge-client',
  },
}

function groupPermissions(permissions) {
  const groups = {}
  const unmatched = []

  for (const perm of permissions || []) {
    let found = false
    for (const [key, cat] of Object.entries(PERM_CATEGORIES)) {
      if (cat.perms.includes(perm)) {
        if (!groups[key]) groups[key] = []
        groups[key].push(perm)
        found = true
        break
      }
    }
    if (!found) unmatched.push(perm)
  }

  if (unmatched.length) {
    groups['other'] = unmatched
  }

  return groups
}

function renderGroupedPermissions(permissions) {
  const groups = groupPermissions(permissions)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
      {Object.entries(groups).map(([key, perms]) => {
        if (key === 'other') {
          return (
            <div key={key} className="perm-group">
              <div className="perm-group-label">Otros</div>
              <div className="perm-badges">
                {perms.map(p => (
                  <span key={p} className="perm-badge">{p}</span>
                ))}
              </div>
            </div>
          )
        }
        const cat = PERM_CATEGORIES[key]
        return (
          <div key={key} className="perm-group">
            <div className="perm-group-label">{cat.label}</div>
            <div className="perm-badges">
              {perms.map(p => (
                <span key={p} className={`perm-badge ${cat.className}`}>
                  {PERM_LABELS[p] || p}
                </span>
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}

const PERM_LABELS = {
  read_tickets: 'Ver',
  write_tickets: 'Crear/editar',
  approve_tickets: 'Aprobar',
  reject_tickets: 'Rechazar',
  review_tickets: 'Revisar',
  manage_users: 'Usuarios',
  manage_groups: 'Grupos',
  admin_dashboard: 'Dashboard',
  client_review: 'Revisión Cliente',
}

export default function GroupList() {
  const store = useContext(StoreContext)
  const [groups, setGroups] = useState([])
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
      gsap.from('.group-row', { opacity: 0, y: 10, stagger: 0.05, duration: 0.3, ease: 'power2.out' })
    })
    return () => ctx.revert()
  }, [groups, prefersReducedMotion])

  useEffect(() => { loadGroups() }, [])

  const loadGroups = async () => {
    setLoading(true)
    try {
      const res = await client.get('/api/groups')
      setGroups(res.data)
    } catch { /* silent */ } finally { setLoading(false) }
  }

  const handleDelete = async (groupId) => {
    const confirmed = await swalConfirm({
      title: '¿Eliminar grupo?',
      text: 'Esta acción no se puede deshacer.',
      icon: 'warning',
      confirmText: 'Eliminar',
    })
    if (!confirmed) return
    try {
      await client.delete(`/api/groups/${groupId}`)
      loadGroups()
    } catch (err) {
      swalError(err)
    }
  }

  const filtered = groups.filter(g => {
    const q = search.toLowerCase()
    if (!q) return true
    return g.name?.toLowerCase().includes(q) || g.display_name?.toLowerCase().includes(q)
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
    <div className="groups-page">
      {/* Header */}
      <div className="section-header">
        <div>
          <h1 className="dashboard-title">Grupos</h1>
          <p className="dashboard-subtitle">Gestión de grupos y permisos</p>
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
          <Link to="/dashboard/admin/groups/create" className="btn-primary btn-sm">
            <PlusIcon className="w-4 h-4" />
            Nuevo grupo
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
                    <UserGroupIcon className="w-4 h-4" />
                    Nombre
                  </div>
                </th>
                <th>Display</th>
                <th>Permisos</th>
                <th style={{ width: '60px' }}></th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={4} className="empty-cell">
                    <div className="empty-state">
                      <UserGroupIcon className="w-10 h-10" style={{ color: 'var(--oceo-text-3)' }} />
                      <p>No se encontraron grupos</p>
                    </div>
                  </td>
                </tr>
              ) : (
                paginated.map((group) => (
                  <tr key={group.group_id || group._id || group.id} className="group-row">
                    <td className="group-name-cell" style={{ fontWeight: 500, color: 'var(--oceo-text-1)' }}>
                      {group.name}
                    </td>
                    <td className="group-desc-cell">{group.display_name || group.description || '—'}</td>
                    <td>
                      {renderGroupedPermissions(group.permissions)}
                    </td>
                    <td>
                      <OptionsCRUD
                        editHref={`/dashboard/admin/groups/update/${group.group_id || group._id || group.id}`}
                        onDelete={() => handleDelete(group.group_id || group._id || group.id)}
                        editPerm="manage_groups"
                        deletePerm="manage_groups"
                        itemName="grupo"
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
