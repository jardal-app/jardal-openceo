import { useContext, useEffect, useMemo, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import {
  MagnifyingGlassIcon,
  CheckCircleIcon,
  XCircleIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
} from '@heroicons/react/24/outline'
import client from '@/api'
import { StoreContext } from '@/context/store'
import { swalError } from '@/utils/swalError'

// Bandeja central "Requiere humano" (TASK-2026-00041): unifica en una sola vista
// todo lo bloqueado por un humano — gate QA, fases HITL (qa_review, pm_approval,
// client_review, admin_approval) — con la accion contextual de cada tipo.
// El servidor ya decide el permiso por fase (`phase-action` exige involved +
// permission de la fase + client_visible); aqui solo se filtra por tipo.
// QADashboard.jsx queda como vista filtrada (tipo QA), no como lugar separado.
const statusLabels = {
  queued: 'En cola',
  in_progress: 'En progreso',
  completed: 'Completado',
  failed: 'Fallido',
  escalated: 'Escalado',
  interrupted: 'Interrumpido',
  waiting: 'En espera',
  cancelled: 'Cancelado',
}

const statusColors = {
  queued: 'badge-warning',
  in_progress: 'badge-active',
  completed: 'badge-success',
  failed: 'badge-danger',
  escalated: 'badge-danger',
  interrupted: 'badge-warning',
  waiting: 'badge-active',
  cancelled: 'badge-danger',
}

function inboxType(ticket) {
  const qaState = ticket.qa_review_state
  if (qaState === 'pending' || qaState === 'changes_requested' || ticket.phase === 'qa_review') {
    return { key: 'qa', label: 'QA', badge: 'badge-review' }
  }
  if (ticket.phase === 'client_signoff') {
    return { key: 'cliente', label: 'Cliente', badge: 'badge-client' }
  }
  return { key: 'aprobacion', label: 'Aprobación', badge: 'badge-phase' }
}

const filters = [
  { key: 'todos', label: 'Todos' },
  { key: 'qa', label: 'QA' },
  { key: 'aprobacion', label: 'Aprobaciones' },
  { key: 'cliente', label: 'Cliente' },
]

export default function HumanInbox() {
  const store = useContext(StoreContext)
  const location = useLocation()
  const [tickets, setTickets] = useState([])
  const [loading, setLoading] = useState(true)
  const [activeFilter, setActiveFilter] = useState('todos')
  const [actionLoading, setActionLoading] = useState(null)
  const [page, setPage] = useState(1)
  const [itemsPerPage, setItemsPerPage] = useState(10)
  const detailBackState = { from: location.pathname }

  useEffect(() => { loadTickets() }, [store.selectedProject]) // eslint-disable-line react-hooks/exhaustive-deps

  const loadTickets = async () => {
    setLoading(true)
    try {
      const base = { limit: 200 }
      if (store.selectedProject) base.project = store.selectedProject
      // Tres consultas sobre el mismo endpoint existente: waiting (cualquier fase),
      // mas QA pendiente/solicitada aunque su estado aun no sea waiting.
      const [waitingRes, qaPendingRes, qaChangesRes] = await Promise.all([
        client.post('/api/tasks/search', { ...base, status: 'waiting' }),
        client.post('/api/tasks/search', { ...base, qa_review_state: 'pending' }),
        client.post('/api/tasks/search', { ...base, qa_review_state: 'changes_requested' }),
      ])
      const byId = new Map()
      for (const row of [...waitingRes.data, ...qaPendingRes.data, ...qaChangesRes.data]) {
        if (row?.task_id && !byId.has(row.task_id)) byId.set(row.task_id, row)
      }
      const rows = [...byId.values()].sort(
        (a, b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0),
      )
      setTickets(rows)
      setPage(1)
    } catch { /* silent */ } finally { setLoading(false) }
  }

  const visible = useMemo(() => {
    if (activeFilter === 'todos') return tickets
    return tickets.filter((t) => inboxType(t).key === activeFilter)
  }, [tickets, activeFilter])

  const counts = useMemo(() => {
    const c = { todos: tickets.length, qa: 0, aprobacion: 0, cliente: 0 }
    for (const t of tickets) c[inboxType(t).key] += 1
    return c
  }, [tickets])

  const handleAction = async (taskId, action, comment) => {
    setActionLoading(`${taskId}-${action}`)
    try {
      await client.post(`/api/tasks/${taskId}/phase-action`, { action, comment })
      loadTickets()
    } catch (err) {
      swalError(err)
    } finally {
      setActionLoading(null)
    }
  }

  const handleApprove = (taskId) => handleAction(taskId, 'approve', 'Aprobado desde la bandeja')
  const handleRequestChanges = (taskId) => {
    const comment = window.prompt('Describe lo que debe corregirse:')
    if (!comment?.trim()) return
    handleAction(taskId, 'request_changes', comment.trim())
  }

  const formatDate = (dateStr) => {
    if (!dateStr) return '—'
    return new Date(dateStr).toLocaleDateString('es-MX', {
      day: '2-digit',
      month: 'short',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  }

  const totalPages = Math.ceil(visible.length / itemsPerPage)
  const paginated = visible.slice((page - 1) * itemsPerPage, page * itemsPerPage)
  const canAct = store.checkPermissions(['qa_review'])

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="animate-spin rounded-full h-10 w-10 border-2 border-oceo-blue-400 border-t-transparent" />
      </div>
    )
  }

  return (
    <div className="qa-dashboard-page">
      <div className="section-header">
        <div>
          <h1 className="dashboard-title">Requiere humano</h1>
          <p className="dashboard-subtitle">QA, aprobaciones y cliente en un solo lugar, con tu permiso</p>
        </div>
        <div className="dashboard-header-actions">
          <span className="results-count">{visible.length} pendiente{visible.length !== 1 ? 's' : ''}</span>
          <select
            value={itemsPerPage}
            onChange={(e) => { setItemsPerPage(Number(e.target.value)); setPage(1) }}
            className="input-oceo input-sm"
            style={{ width: 90 }}
          >
            <option value={10}>10 / pág</option>
            <option value={50}>50 / pág</option>
            <option value={100}>100 / pág</option>
          </select>
        </div>
      </div>

      <div className="flex items-center gap-2 flex-wrap" style={{ marginBottom: 12 }}>
        {filters.map((f) => (
          <button
            key={f.key}
            onClick={() => { setActiveFilter(f.key); setPage(1) }}
            className={activeFilter === f.key ? 'btn-primary btn-sm' : 'btn-secondary btn-sm'}
          >
            {f.label} ({counts[f.key]})
          </button>
        ))}
      </div>

      <div className="card-oceo table-card">
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Ticket</th>
                <th>Título</th>
                <th>Tipo</th>
                <th>Estado</th>
                <th>Proyecto</th>
                <th>Actualizado</th>
                <th>Acciones</th>
              </tr>
            </thead>
            <tbody>
              {visible.length === 0 ? (
                <tr>
                  <td colSpan={7} className="empty-cell">
                    <div className="empty-state">
                      <MagnifyingGlassIcon className="w-10 h-10" style={{ color: 'var(--oceo-text-3)' }} />
                      <p>Nada esperando humano. Buen momento para seguir construyendo.</p>
                    </div>
                  </td>
                </tr>
              ) : (
                paginated.map((ticket) => {
                  const type = inboxType(ticket)
                  return (
                    <tr key={ticket.task_id}>
                      <td>
                        <code className="ticket-id">
                          {ticket.ticket_number != null ? `#${ticket.ticket_number}` : ticket.task_id.slice(0, 8)}
                        </code>
                      </td>
                      <td>
                        <Link to={`/dashboard/tickets/${ticket.task_id}`} state={detailBackState} className="ticket-title-link">
                          {ticket.title}
                        </Link>
                      </td>
                      <td>
                        <span className={`status-badge ${type.badge}`}>
                          {type.label}
                        </span>
                      </td>
                      <td>
                        <span className={`status-badge ${statusColors[ticket.status] || 'badge-active'}`}>
                          {statusLabels[ticket.status] || ticket.status}
                        </span>
                      </td>
                      <td className="project-cell">{ticket.project || '—'}</td>
                      <td className="date-cell">{formatDate(ticket.updated_at)}</td>
                      <td>
                        <div className="flex items-center gap-2 flex-wrap">
                          {canAct && (
                            <button
                              onClick={() => handleApprove(ticket.task_id)}
                              disabled={actionLoading === `${ticket.task_id}-approve`}
                              className="btn-primary btn-sm"
                              title="Aprobar"
                            >
                              <CheckCircleIcon className="w-4 h-4" />
                              {actionLoading === `${ticket.task_id}-approve` ? '...' : 'Aprobar'}
                            </button>
                          )}
                          {canAct && (
                            <button
                              onClick={() => handleRequestChanges(ticket.task_id)}
                              disabled={actionLoading === `${ticket.task_id}-request_changes`}
                              className="btn-danger btn-sm"
                              title="Solicitar cambios"
                            >
                              <XCircleIcon className="w-4 h-4" />
                              {actionLoading === `${ticket.task_id}-request_changes` ? '...' : 'Cambios'}
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  )
                })
              )}
            </tbody>
          </table>
        </div>

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
