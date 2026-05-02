import { useContext, useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import {
  MagnifyingGlassIcon,
  CheckCircleIcon,
  XCircleIcon,
  ArrowTopRightOnSquareIcon,
  TicketIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
} from '@heroicons/react/24/outline'
import client from '@/api'
import { StoreContext } from '@/context/store'
import { swalError } from '@/utils/swalError'

const statusLabels = {
  queued: 'En cola',
  in_progress: 'En progreso',
  completed: 'Completado',
  failed: 'Fallido',
  escalated: 'Escalado',
  interrupted: 'Interrumpido',
  waiting: 'Esperando QA',
}

const statusColors = {
  queued: 'badge-warning',
  in_progress: 'badge-active',
  completed: 'badge-success',
  failed: 'badge-danger',
  escalated: 'badge-danger',
  interrupted: 'badge-warning',
  waiting: 'badge-active',
}

export default function QADashboard() {
  const store = useContext(StoreContext)
  const location = useLocation()
  const [tickets, setTickets] = useState([])
  const [loading, setLoading] = useState(true)
  const [actionLoading, setActionLoading] = useState(null)
  const [page, setPage] = useState(1)
  const [itemsPerPage, setItemsPerPage] = useState(10)
  const prefersReducedMotion = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  const detailBackState = { from: location.pathname }

  const handleItemsPerPageChange = (value) => {
    setItemsPerPage(Number(value))
    setPage(1)
  }

  useGSAP(() => {
    if (prefersReducedMotion) return
    const ctx = gsap.context(() => {
      gsap.from('.qa-ticket-row', { opacity: 0, y: 10, stagger: 0.04, duration: 0.3, ease: 'power2.out' })
    })
    return () => ctx.revert()
  }, [tickets, prefersReducedMotion])

  useEffect(() => { loadTickets() }, [store.selectedProject])

  const loadTickets = async () => {
    setLoading(true)
    try {
      const body = {
        phase: 'qa_review',
        status: 'waiting',
        limit: 200,
      }
      if (store.selectedProject) body.project = store.selectedProject
      const response = await client.post('/api/tasks/search', body)
      setTickets(response.data)
    } catch { /* silent */ } finally { setLoading(false) }
  }

  const handleApprove = async (taskId) => {
    setActionLoading(taskId + '-approve')
    try {
      await client.post(`/api/tasks/${taskId}/phase-action`, {
        action: 'approve',
        comment: 'Aprobado por QA desde el dashboard',
      })
      loadTickets()
    } catch (err) {
      swalError(err)
    } finally {
      setActionLoading(null)
    }
  }

  const handleRequestChanges = async (taskId) => {
    const comment = window.prompt('Describe los cambios que debe corregir DEV:')
    if (!comment?.trim()) return
    setActionLoading(taskId + '-changes')
    try {
      await client.post(`/api/tasks/${taskId}/phase-action`, {
        action: 'request_changes',
        comment: comment.trim(),
      })
      loadTickets()
    } catch (err) {
      swalError(err)
    } finally {
      setActionLoading(null)
    }
  }

  const handleOpenSession = async (tool, taskId) => {
    // Open blank window synchronously (user gesture) to avoid popup blocker
    const newWindow = window.open('about:blank', '_blank', 'noopener,noreferrer')
    setActionLoading(`${taskId}-${tool}`)
    try {
      const response = await client.post(`/api/tasks/${taskId}/phase-${tool}`, { phase: 'qa_review' })
      const { url } = response.data
      if (url && newWindow) {
        newWindow.location.href = url
      } else {
        newWindow?.close()
      }
    } catch (err) {
      newWindow?.close()
      swalError(err)
    } finally {
      setActionLoading(null)
    }
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

  const totalPages = Math.ceil(tickets.length / itemsPerPage)
  const paginated = tickets.slice((page - 1) * itemsPerPage, page * itemsPerPage)

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="animate-spin rounded-full h-10 w-10 border-2 border-oceo-blue-400 border-t-transparent" />
      </div>
    )
  }

  return (
    <div className="qa-dashboard-page">
      {/* Header */}
      <div className="section-header">
        <div>
          <h1 className="dashboard-title">Revisión QA</h1>
          <p className="dashboard-subtitle">Tickets en fase qa_review asignados a tu usuario o visibles para admin</p>
        </div>
        <div className="dashboard-header-actions">
          <span className="results-count">{tickets.length} pendiente{tickets.length !== 1 ? 's' : ''}</span>
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
        </div>
      </div>

      {/* Table */}
      <div className="card-oceo table-card" style={{ position: 'relative' }}>
        {actionLoading && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              zIndex: 10,
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              backgroundColor: 'rgba(10, 10, 10, 0.7)',
              borderRadius: 'inherit',
            }}
          >
            <div className="animate-spin rounded-full h-10 w-10 border-2 border-oceo-blue-400 border-t-transparent mb-3" />
            <p style={{ color: '#e5e5e5', fontSize: '0.875rem' }}>Creando sesión de revisión…</p>
          </div>
        )}
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Ticket</th>
                <th>Título</th>
                <th>Estado</th>
                <th>QA</th>
                <th>Proyecto</th>
                <th>Actualizado</th>
                <th>Acciones</th>
              </tr>
            </thead>
            <tbody>
              {tickets.length === 0 ? (
                <tr>
                  <td colSpan={7} className="empty-cell">
                    <div className="empty-state">
                      <MagnifyingGlassIcon className="w-10 h-10" style={{ color: 'var(--oceo-text-3)' }} />
                      <p>No hay tickets pendientes de revisión QA</p>
                    </div>
                  </td>
                </tr>
              ) : (
                paginated.map((ticket) => (
                  <tr key={ticket.task_id} className="qa-ticket-row">
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
                      <span className={`status-badge ${statusColors[ticket.status] || 'badge-active'}`}>
                        {statusLabels[ticket.status] || ticket.status}
                      </span>
                    </td>
                    <td>
                      <span className="status-badge badge-warning">
                        {ticket.phase || 'qa_review'}
                      </span>
                      {ticket.involved_user_ids?.length > 0 && (
                        <span className="status-badge badge-neutral ml-1">
                          {ticket.involved_user_ids.slice(0, 2).join(', ')}
                          {ticket.involved_user_ids.length > 2 && ` +${ticket.involved_user_ids.length - 2}`}
                        </span>
                      )}
                    </td>
                    <td className="project-cell">{ticket.project || '—'}</td>
                    <td className="date-cell">{formatDate(ticket.updated_at)}</td>
                    <td>
                      <div className="flex items-center gap-2 flex-wrap">
                        <button
                          onClick={() => handleOpenSession('kimi', ticket.task_id)}
                          disabled={actionLoading && actionLoading.startsWith(ticket.task_id)}
                          className="btn-secondary btn-sm"
                          title="Abrir en Kimi Web"
                        >
                          <ArrowTopRightOnSquareIcon className="w-4 h-4" />
                          {actionLoading === `${ticket.task_id}-kimi` ? 'Creando…' : 'Kimi'}
                        </button>
                        <button
                          onClick={() => handleOpenSession('opencode', ticket.task_id)}
                          disabled={actionLoading && actionLoading.startsWith(ticket.task_id)}
                          className="btn-secondary btn-sm"
                          title="Abrir en OpenCode Web (kimi-k2.6)"
                        >
                          <ArrowTopRightOnSquareIcon className="w-4 h-4" />
                          {actionLoading === `${ticket.task_id}-opencode` ? 'Creando…' : 'OpenCode'}
                        </button>
                        {store.checkPermissions(['qa_review']) && (
                          <button
                            onClick={() => handleApprove(ticket.task_id)}
                            disabled={actionLoading === ticket.task_id + '-approve'}
                            className="btn-primary btn-sm"
                            title="Aprobar y entregar al cliente"
                          >
                            <CheckCircleIcon className="w-4 h-4" />
                            {actionLoading === ticket.task_id + '-approve' ? '...' : 'Aprobar'}
                          </button>
                        )}
                        {store.checkPermissions(['qa_review']) && (
                          <button
                            onClick={() => handleRequestChanges(ticket.task_id)}
                            disabled={actionLoading === ticket.task_id + '-changes'}
                            className="btn-danger btn-sm"
                            title="Solicitar cambios y devolver a DEV"
                          >
                            <XCircleIcon className="w-4 h-4" />
                            {actionLoading === ticket.task_id + '-changes' ? '...' : 'Cambios'}
                          </button>
                        )}
                      </div>
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
