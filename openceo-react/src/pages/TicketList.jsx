import { useContext, useEffect, useState } from 'react'
import { Link, Navigate, useLocation } from 'react-router-dom'
import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import {
  MagnifyingGlassIcon,
  FunnelIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  TicketIcon,
  PlusIcon,
  ViewColumnsIcon,
  TableCellsIcon,
} from '@heroicons/react/24/outline'
import client from '@/api'
import { StoreContext } from '@/context/store'
import OptionsCRUD from '@/components/OptionsCRUD'
import { PermissionCheck } from '@/components/PermissionCheck'
import { swalError } from '@/utils/swalError'
import KanbanBoard from '@/components/KanbanBoard'

const statusLabels = {
  queued: 'Queued',
  in_progress: 'In Progress',
  completed: 'Completed',
  failed: 'Failed',
  escalated: 'Escalated',
  interrupted: 'Interrupted',
  waiting: 'Waiting',
  cancelled: 'Cancelled',
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

const priorityLabels = {
  low: 'Baja',
  medium: 'Media',
  high: 'Alta',
  critical: 'Crítica',
}

const priorityColors = {
  low: 'badge-neutral',
  medium: 'badge-info',
  high: 'badge-warning',
  critical: 'badge-danger',
}

const tierLabels = {
  tier1: 'Tier 1 · Codex',
  tier2: 'Tier 2 · Claude',
  tier3: 'Tier 3 · OpenCode',
  tier4: 'Tier 4 · Kimi',
  tier_s: 'Tier S · Consensus',
}

const tierColors = {
  tier1: 'badge-active',
  tier2: 'badge-info',
  tier3: 'badge-warning',
  tier4: 'badge-danger',
  tier_s: 'badge-danger',
}

const phaseLabels = {
  qa_review: 'QA Review',
  dev_work: 'Dev Work',
  client_signoff: 'Cliente',
  pm_review: 'PM Review',
  kimi_chat: 'Kimi Chat',
  opencode_chat: 'OpenCode Chat',
}

function getPhaseInfo(ticket) {
  const label = phaseLabels[ticket.phase] || ticket.phase

  if (ticket.phase === 'client_signoff' && ticket.status === 'waiting') {
    return { label, color: 'badge-client' }
  }

  if (ticket.phase === 'qa_review' && ticket.status === 'waiting') {
    return { label, color: 'badge-warning' }
  }

  return { label, color: 'badge-info' }
}

function getEngineInfo(ticket) {
  if (ticket.coding_tier) {
    return { label: tierLabels[ticket.coding_tier] || ticket.coding_tier, color: tierColors[ticket.coding_tier] || 'badge-neutral' }
  }
  if (ticket.coding_tool) {
    return { label: ticket.coding_tool, color: 'badge-neutral' }
  }
  if (ticket.workflow_key) {
    return { label: ticket.workflow_key, color: 'badge-info' }
  }
  const agent = ticket.agent_hint || ticket.agent
  if (agent) {
    return { label: agent, color: 'badge-neutral' }
  }
  return { label: '—', color: 'badge-neutral' }
}

function getClientStatusInfo(ticket) {
  if (ticket.status === 'completed') {
    return { key: 'completed', label: 'Completado', color: 'badge-success' }
  }
  if (ticket.status === 'waiting' && ticket.phase === 'client_signoff') {
    return { key: 'waiting_review', label: 'En espera de revisión', color: 'badge-client' }
  }
  return { key: 'in_progress', label: 'En proceso', color: 'badge-active' }
}

export default function TicketList() {
  const store = useContext(StoreContext)
  const location = useLocation()
  const [viewMode, setViewMode] = useState(() => localStorage.getItem('ticketViewMode') || 'kanban')
  const [tickets, setTickets] = useState([])
  const [loading, setLoading] = useState(true)
  const [filterStatus, setFilterStatus] = useState('')
  const [filterProject, setFilterProject] = useState('')
  const [filterAgent, setFilterAgent] = useState('')
  const [filterQAPending, setFilterQAPending] = useState(false)
  const [page, setPage] = useState(1)
  const [itemsPerPage, setItemsPerPage] = useState(50)
  const [showFilters, setShowFilters] = useState(true)

  const handleItemsPerPageChange = (value) => {
    setItemsPerPage(Number(value))
    setPage(1)
  }

  const prefersReducedMotion = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  const detailBackState = { from: location.pathname }
  const isClient = store.user?.is_client === true

  useGSAP(() => {
    if (prefersReducedMotion) return
    const ctx = gsap.context(() => {
      gsap.from('.ticket-row', { opacity: 0, y: 10, stagger: 0.04, duration: 0.3, ease: 'power2.out' })
    })
    return () => ctx.revert()
  }, [tickets, prefersReducedMotion])

  useEffect(() => { loadTickets() }, [store.selectedProject])

  const loadTickets = async () => {
    setLoading(true)
    try {
      const params = { limit: 500 }
      if (store.selectedProject) params.project = store.selectedProject
      const response = await client.get('/api/tasks', { params })
      setTickets(response.data)
    } catch { /* silent */ } finally { setLoading(false) }
  }

  const handleRun = async (taskId) => {
    try {
      await client.post(`/api/tasks/${taskId}/run`)
      loadTickets()
    } catch (err) {
      swalError(err)
    }
  }

  const filtered = tickets.filter((t) => {
    if (filterStatus) {
      if (isClient) {
        if (getClientStatusInfo(t).key !== filterStatus) return false
      } else if (t.status !== filterStatus) {
        return false
      }
    }
    if (filterProject && !t.project?.toLowerCase().includes(filterProject.toLowerCase())) return false
    if (filterAgent && (t.agent || t.agent_hint || '').toLowerCase() !== filterAgent.toLowerCase()) return false
    if (!isClient && filterQAPending) {
      const isQAPhase = t.phase === 'qa_review' && t.status === 'waiting'
      const isQAPending = t.qa_review_required && t.qa_review_state === 'pending'
      if (!isQAPhase && !isQAPending) return false
    }
    return true
  })

  const totalPages = Math.ceil(filtered.length / itemsPerPage)
  const paginated = filtered.slice((page - 1) * itemsPerPage, page * itemsPerPage)

  const formatDate = (dateStr) => {
    if (!dateStr) return '—'
    return new Date(dateStr).toLocaleDateString('es-MX', { day: '2-digit', month: 'short', year: 'numeric' })
  }

  if (isClient) {
    return <Navigate to="/dashboard/inicio" replace />
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="animate-spin rounded-full h-10 w-10 border-2 border-oceo-blue-400 border-t-transparent" />
      </div>
    )
  }

  return (
    <div className="tickets-page">
      {/* Header */}
      <div className="section-header">
        <div>
          <h1 className="dashboard-title">Tickets</h1>
          <p className="dashboard-subtitle">{isClient ? 'Seguimiento de tus tickets' : 'Gestión y seguimiento de tareas'}</p>
        </div>
        <div className="dashboard-header-actions">
          {/* View Switcher */}
          <div className="kanban-mode-switcher">
            <button
              type="button"
              className={`kanban-mode-btn ${viewMode === 'kanban' ? 'active' : ''}`}
              onClick={() => {
                setViewMode('kanban')
                localStorage.setItem('ticketViewMode', 'kanban')
              }}
              title="Vista Kanban"
            >
              <ViewColumnsIcon className="w-4 h-4" />
              Kanban
            </button>
            <button
              type="button"
              className={`kanban-mode-btn ${viewMode === 'table' ? 'active' : ''}`}
              onClick={() => {
                setViewMode('table')
                localStorage.setItem('ticketViewMode', 'table')
              }}
              title="Vista Tabla"
            >
              <TableCellsIcon className="w-4 h-4" />
              Tabla
            </button>
          </div>

          {viewMode === 'table' && (
            <>
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
              <button onClick={() => setShowFilters(!showFilters)} className="btn-secondary btn-sm">
                <FunnelIcon className="w-4 h-4" />
                {showFilters ? 'Ocultar filtros' : 'Filtros'}
              </button>
            </>
          )}

          <PermissionCheck perms={['write_tickets']}>
            <Link to="/dashboard/tickets/create" className="btn-primary btn-sm">
              <PlusIcon className="w-4 h-4" />
              Nuevo Ticket
            </Link>
          </PermissionCheck>
        </div>
      </div>

      {/* Main View: Kanban or Table */}
      {viewMode === 'kanban' ? (
        <KanbanBoard
          tickets={tickets}
          loading={loading}
          onRefresh={loadTickets}
          hideViewSwitcher={true}
          initialView="kanban"
        />
      ) : (
        <>
          {/* Filters for Table */}
          {showFilters && (
            <div className="card-oceo filters-card" style={{ marginBottom: '1.25rem' }}>
              <div className="filters-grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', maxWidth: 800 }}>
                <div className="filter-field">
                  <label className="filter-label">Estado</label>
                  <select
                    value={filterStatus}
                    onChange={(e) => { setFilterStatus(e.target.value); setPage(1) }}
                    className="input-oceo input-sm"
                  >
                    <option value="">Todos</option>
                    {isClient ? (
                      <>
                        <option value="in_progress">En proceso</option>
                        <option value="waiting_review">En espera de revisión</option>
                        <option value="completed">Completado</option>
                      </>
                    ) : (
                      <>
                        <option value="queued">En cola</option>
                        <option value="in_progress">En progreso</option>
                        <option value="completed">Completado</option>
                        <option value="failed">Fallido</option>
                        <option value="waiting">Esperando</option>
                        <option value="escalated">Escalado</option>
                        <option value="interrupted">Interrumpido</option>
                      </>
                    )}
                  </select>
                </div>

                <div className="filter-field">
                  <label className="filter-label">Agente</label>
                  <select
                    value={filterAgent}
                    onChange={(e) => { setFilterAgent(e.target.value); setPage(1) }}
                    className="input-oceo input-sm"
                  >
                    <option value="">Todos los agentes</option>
                    <option value="dev">Dev</option>
                    <option value="finance">Finanzas</option>
                    <option value="secretary">Secretaría</option>
                    <option value="pm">PM</option>
                    <option value="ceo">CEO</option>
                  </select>
                </div>

                <div className="filter-field">
                  <label className="filter-label">Proyecto</label>
                  <div className="input-icon-wrap">
                    <MagnifyingGlassIcon className="input-icon" />
                    <input
                      type="text"
                      value={filterProject}
                      onChange={(e) => { setFilterProject(e.target.value); setPage(1) }}
                      placeholder="Buscar proyecto..."
                      className="input-oceo input-sm input-with-icon"
                    />
                  </div>
                </div>

                {!isClient && (
                  <div className="filter-field">
                    <label className="filter-label">QA</label>
                    <button
                      onClick={() => { setFilterQAPending((v) => !v); setPage(1) }}
                      className={`btn-sm w-full ${filterQAPending ? 'btn-primary' : 'btn-secondary'}`}
                    >
                      {filterQAPending ? 'Pendiente QA ✓' : 'Pendiente QA'}
                    </button>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Table */}
          <div className="card-oceo table-card">
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Ticket</th>
                <th>Título</th>
                <th>Estado</th>
                {!isClient && <th>Prioridad</th>}
                {!isClient && <th>Engine</th>}
                {!isClient && <th>Fase</th>}
                <th>Proyecto</th>
                {!isClient && <th>Fecha</th>}
                <th>Acciones</th>
              </tr>
            </thead>
            <tbody>
              {paginated.length === 0 ? (
                <tr>
                  <td colSpan={isClient ? 5 : 9} className="empty-cell">
                    <div className="empty-state">
                      <TicketIcon className="w-10 h-10" style={{ color: 'var(--oceo-text-3)' }} />
                      <p>No se encontraron tickets</p>
                    </div>
                  </td>
                </tr>
              ) : (
                paginated.map((ticket) => (
                  <tr key={ticket.task_id} className="ticket-row">
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
                      {isClient ? (
                        (() => {
                          const clientStatus = getClientStatusInfo(ticket)
                          return <span className={`status-badge ${clientStatus.color}`}>{clientStatus.label}</span>
                        })()
                      ) : (
                        <>
                          <span className={`status-badge ${statusColors[ticket.status] || 'badge-active'}`}>
                            {statusLabels[ticket.status] || ticket.status}
                          </span>
                          {ticket.qa_review_required && ticket.qa_review_state === 'pending' && (
                            <span className="status-badge badge-warning ml-1">QA</span>
                          )}
                        </>
                      )}
                    </td>
                    {!isClient && (
                      <td>
                        <span className={`status-badge ${priorityColors[ticket.priority] || 'badge-neutral'}`}>
                          {priorityLabels[ticket.priority] || ticket.priority || '—'}
                        </span>
                      </td>
                    )}
                    {!isClient && (
                      <td>
                        {(() => {
                          const engine = getEngineInfo(ticket)
                          return <span className={`status-badge ${engine.color}`}>{engine.label}</span>
                        })()}
                      </td>
                    )}
                    {!isClient && (
                      <td>
                        {ticket.phase ? (
                          (() => {
                            const phase = getPhaseInfo(ticket)
                            return <span className={`status-badge ${phase.color}`}>{phase.label}</span>
                          })()
                        ) : '—'}
                      </td>
                    )}
                    <td className="project-cell">{ticket.project || '—'}</td>
                    {!isClient && <td className="date-cell">{formatDate(ticket.created_at)}</td>}
                    <td>
                      <OptionsCRUD
                        viewHref={`/dashboard/tickets/${ticket.task_id}`}
                        viewState={detailBackState}
                        editHref={`/dashboard/tickets/update/${ticket.task_id}`}
                        onRun={(['queued', 'failed', 'interrupted'].includes(ticket.status) && !ticket.autorun) ? () => handleRun(ticket.task_id) : null}
                        viewPerm="read_tickets"
                        editPerm="write_tickets"
                        runPerm="write_tickets"
                        itemName="ticket"
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
        </>
      )}
    </div>
  )
}
