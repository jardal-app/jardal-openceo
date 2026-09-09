import { useState, useMemo, useContext } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import {
  InboxStackIcon,
  BoltIcon,
  EyeIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  CodeBracketIcon,
  BanknotesIcon,
  EnvelopeIcon,
  ClipboardDocumentListIcon,
  SparklesIcon,
  CpuChipIcon,
  ArrowTopRightOnSquareIcon,
  PlayIcon,
  MagnifyingGlassIcon,
  ViewColumnsIcon,
  TableCellsIcon,
  ArrowPathIcon,
  ClockIcon,
  FolderIcon,
} from '@heroicons/react/24/outline'
import { StoreContext } from '@/context/store'
import { PermissionCheck } from '@/components/PermissionCheck'
import client from '@/api'
import { swalError } from '@/utils/swalError'
import Swal from 'sweetalert2'

export const AGENT_CONFIG = {
  dev: {
    label: 'Dev Agent',
    short: 'Dev',
    key: 'dev',
    badgeClass: 'badge-agent-dev',
    color: '#5CAAF7',
    icon: CodeBracketIcon,
    accent: '#2E86E6',
  },
  finance: {
    label: 'Finanzas Agent',
    short: 'Finanzas',
    key: 'finance',
    badgeClass: 'badge-agent-finance',
    color: '#34d399',
    icon: BanknotesIcon,
    accent: '#10B981',
  },
  secretary: {
    label: 'Secretaría Agent',
    short: 'Secretaría',
    key: 'secretary',
    badgeClass: 'badge-agent-secretary',
    color: '#c084fc',
    icon: EnvelopeIcon,
    accent: '#A855F7',
  },
  pm: {
    label: 'PM Agent',
    short: 'PM',
    key: 'pm',
    badgeClass: 'badge-agent-pm',
    color: '#fbbf24',
    icon: ClipboardDocumentListIcon,
    accent: '#F5A524',
  },
  ceo: {
    label: 'CEO Supervisor',
    short: 'CEO',
    key: 'ceo',
    badgeClass: 'badge-agent-ceo',
    color: '#cbd5e1',
    icon: SparklesIcon,
    accent: '#94A3B8',
  },
}

export const STATUS_CONFIG = {
  queued: {
    key: 'queued',
    label: 'En Cola',
    badgeClass: 'badge-warning',
    accent: '#F5A524',
    icon: InboxStackIcon,
  },
  in_progress: {
    key: 'in_progress',
    label: 'En Progreso',
    badgeClass: 'badge-active',
    accent: '#2E86E6',
    icon: BoltIcon,
  },
  waiting: {
    key: 'waiting',
    label: 'En Espera / Revisión',
    badgeClass: 'badge-client',
    accent: '#2dd4bf',
    icon: EyeIcon,
  },
  completed: {
    key: 'completed',
    label: 'Completado',
    badgeClass: 'badge-success',
    accent: '#12B981',
    icon: CheckCircleIcon,
  },
  failed: {
    key: 'failed',
    label: 'Fallido / Atención',
    badgeClass: 'badge-danger',
    accent: '#F04452',
    icon: ExclamationTriangleIcon,
  },
}

const PRIORITY_CONFIG = {
  critical: { label: 'Crítica', badge: 'badge-danger', dot: '#F04452' },
  high: { label: 'Alta', badge: 'badge-warning', dot: '#F5A524' },
  medium: { label: 'Media', badge: 'badge-info', dot: '#3BA4FF' },
  low: { label: 'Baja', badge: 'badge-neutral', dot: '#7E8B99' },
}

const PHASE_LABELS = {
  qa_review: { label: 'QA Review', badge: 'badge-warning' },
  dev_work: { label: 'Dev Work', badge: 'badge-info' },
  client_signoff: { label: 'Cliente', badge: 'badge-client' },
  pm_review: { label: 'PM Review', badge: 'badge-warning' },
  completed: { label: 'Completado', badge: 'badge-success' },
}

export function getAgentData(ticket) {
  const raw = String(ticket.agent || ticket.agent_hint || 'ceo').toLowerCase().trim()
  return AGENT_CONFIG[raw] || {
    label: raw ? raw.toUpperCase() : 'Agente',
    short: raw || 'Agente',
    key: raw || 'other',
    badgeClass: 'badge-neutral',
    color: 'var(--oceo-text-2)',
    icon: CpuChipIcon,
    accent: '#64748b',
  }
}

export function getWorkflowKey(ticket) {
  if (ticket.workflow_key) return ticket.workflow_key
  if (ticket.params?.workflow) return ticket.params.workflow
  if (ticket.params?.workflow_ref) return ticket.params.workflow_ref
  return null
}

export function formatRelativeDate(dateStr) {
  if (!dateStr) return '—'
  const date = new Date(dateStr)
  if (isNaN(date.getTime())) return '—'
  const now = new Date()
  const diffMs = now - date
  const diffMinutes = Math.floor(diffMs / 60000)
  const diffHours = Math.floor(diffMinutes / 60)
  const diffDays = Math.floor(diffHours / 24)

  if (diffMinutes < 1) return 'Ahora'
  if (diffMinutes < 60) return `${diffMinutes}m`
  if (diffHours < 24) return `${diffHours}h`
  if (diffDays < 7) return `${diffDays}d`
  return date.toLocaleDateString('es-MX', { day: '2-digit', month: 'short' })
}

export default function KanbanBoard({
  tickets = [],
  loading = false,
  onRefresh = null,
  hideViewSwitcher = false,
  initialView = 'kanban',
  initialGroupBy = 'status',
}) {
  const store = useContext(StoreContext)
  const location = useLocation()
  const detailBackState = { from: location.pathname }

  const [viewMode, setViewMode] = useState(initialView)
  const [groupBy, setGroupBy] = useState(initialGroupBy)
  const [selectedAgent, setSelectedAgent] = useState('')
  const [selectedWorkflow, setSelectedWorkflow] = useState('')
  const [selectedPriority, setSelectedPriority] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [runningTaskId, setRunningTaskId] = useState(null)

  // Workflows únicos presentes
  const availableWorkflows = useMemo(() => {
    const set = new Set()
    tickets.forEach((t) => {
      const wf = getWorkflowKey(t)
      if (wf) set.add(wf)
    })
    return Array.from(set).sort()
  }, [tickets])

  // Agentes únicos presentes
  const availableAgents = useMemo(() => {
    const map = new Map()
    Object.keys(AGENT_CONFIG).forEach(k => map.set(k, AGENT_CONFIG[k]))
    tickets.forEach((t) => {
      const a = getAgentData(t)
      if (!map.has(a.key)) map.set(a.key, a)
    })
    return Array.from(map.values())
  }, [tickets])

  // Filtrado reactivo de tickets
  const filteredTickets = useMemo(() => {
    const q = searchQuery.trim().toLowerCase()
    return tickets.filter((ticket) => {
      if (store.selectedProject && ticket.project !== store.selectedProject) {
        return false
      }
      if (selectedAgent) {
        const ag = getAgentData(ticket).key
        if (ag !== selectedAgent) return false
      }
      if (selectedWorkflow) {
        const wf = getWorkflowKey(ticket)
        if (selectedWorkflow === '__none__' && wf) return false
        if (selectedWorkflow !== '__none__' && wf !== selectedWorkflow) return false
      }
      if (selectedPriority && ticket.priority !== selectedPriority) {
        return false
      }
      if (q) {
        const ticketNum = ticket.ticket_number != null ? String(ticket.ticket_number) : ''
        const title = (ticket.title || '').toLowerCase()
        const desc = (ticket.description || '').toLowerCase()
        const project = (ticket.project || '').toLowerCase()
        const agent = (ticket.agent || '').toLowerCase()
        const wf = (getWorkflowKey(ticket) || '').toLowerCase()
        const tags = Array.isArray(ticket.tags) ? ticket.tags.join(' ').toLowerCase() : ''

        const matches =
          ticketNum.includes(q) ||
          title.includes(q) ||
          desc.includes(q) ||
          project.includes(q) ||
          agent.includes(q) ||
          wf.includes(q) ||
          tags.includes(q)

        if (!matches) return false
      }
      return true
    })
  }, [tickets, store.selectedProject, selectedAgent, selectedWorkflow, selectedPriority, searchQuery])

  // Agrupación de columnas según groupBy
  const columns = useMemo(() => {
    if (groupBy === 'agent') {
      const agentCols = [
        { key: 'dev', config: AGENT_CONFIG.dev },
        { key: 'finance', config: AGENT_CONFIG.finance },
        { key: 'secretary', config: AGENT_CONFIG.secretary },
        { key: 'pm', config: AGENT_CONFIG.pm },
        { key: 'ceo', config: AGENT_CONFIG.ceo },
      ]

      const otherTickets = filteredTickets.filter((t) => {
        const k = getAgentData(t).key
        return !['dev', 'finance', 'secretary', 'pm', 'ceo'].includes(k)
      })

      const cols = agentCols.map(({ key, config }) => {
        const items = filteredTickets.filter((t) => getAgentData(t).key === key)
        return {
          id: key,
          title: config.label,
          icon: config.icon,
          accent: config.accent,
          badgeClass: config.badgeClass,
          tickets: items,
        }
      })

      if (otherTickets.length > 0) {
        cols.push({
          id: 'other',
          title: 'Otros Agentes',
          icon: CpuChipIcon,
          accent: '#64748b',
          badgeClass: 'badge-neutral',
          tickets: otherTickets,
        })
      }

      return cols
    }

    if (groupBy === 'workflow') {
      const cols = []
      availableWorkflows.forEach((wf) => {
        const items = filteredTickets.filter((t) => getWorkflowKey(t) === wf)
        cols.push({
          id: wf,
          title: wf,
          icon: BoltIcon,
          accent: '#6366F1',
          badgeClass: 'badge-workflow',
          tickets: items,
        })
      })

      const noWfItems = filteredTickets.filter((t) => !getWorkflowKey(t))
      cols.push({
        id: '__direct__',
        title: 'Tareas Directas (Sin Workflow)',
        icon: InboxStackIcon,
        accent: '#94A3B8',
        badgeClass: 'badge-neutral',
        tickets: noWfItems,
      })

      return cols
    }

    // Default: Group by Status
    const statusOrder = ['queued', 'in_progress', 'waiting', 'completed', 'failed']
    return statusOrder.map((stKey) => {
      const conf = STATUS_CONFIG[stKey]
      let items = []
      if (stKey === 'failed') {
        items = filteredTickets.filter((t) => ['failed', 'escalated', 'interrupted'].includes(t.status))
      } else {
        items = filteredTickets.filter((t) => t.status === stKey)
      }
      return {
        id: stKey,
        title: conf.label,
        icon: conf.icon,
        accent: conf.accent,
        badgeClass: conf.badgeClass,
        tickets: items,
      }
    })
  }, [filteredTickets, groupBy, availableWorkflows])

  // Ejecución rápida
  const handleRunTask = async (e, taskId) => {
    e.preventDefault()
    e.stopPropagation()
    try {
      setRunningTaskId(taskId)
      await client.post(`/api/tasks/${taskId}/run`)
      Swal.fire({
        toast: true,
        position: 'top-end',
        icon: 'success',
        title: 'Ejecución lanzada',
        showConfirmButton: false,
        timer: 2000,
      })
      if (onRefresh) onRefresh()
    } catch (err) {
      swalError(err)
    } finally {
      setRunningTaskId(null)
    }
  }

  // Animaciones GSAP
  const prefersReducedMotion =
    typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  useGSAP(() => {
    if (prefersReducedMotion) return
    const ctx = gsap.context(() => {
      gsap.from('.kanban-card', {
        opacity: 0,
        y: 10,
        stagger: 0.02,
        duration: 0.25,
        ease: 'power2.out',
      })
    })
    return () => ctx.revert()
  }, [columns, viewMode, prefersReducedMotion])

  const activeFiltersCount =
    (selectedAgent ? 1 : 0) +
    (selectedWorkflow ? 1 : 0) +
    (selectedPriority ? 1 : 0) +
    (searchQuery ? 1 : 0)

  const handleResetFilters = () => {
    setSelectedAgent('')
    setSelectedWorkflow('')
    setSelectedPriority('')
    setSearchQuery('')
  }

  return (
    <div className="kanban-view-root">
      {/* Controls & Filter Toolbar */}
      <div className="card-oceo kanban-controls-card">
        <div className="kanban-toolbar">
          {/* Left: Search & Filters */}
          <div className="kanban-filter-group">
            {/* Buscador */}
            <div className="input-icon-wrap" style={{ minWidth: 210 }}>
              <MagnifyingGlassIcon className="input-icon" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Buscar ticket, tag o tarea..."
                className="input-oceo input-sm input-with-icon"
              />
            </div>

            {/* Filtro por Agente */}
            <select
              value={selectedAgent}
              onChange={(e) => setSelectedAgent(e.target.value)}
              className="input-oceo input-sm"
              style={{ width: 165 }}
              title="Filtrar por agente que atiende la tarea"
            >
              <option value="">Todos los Agentes</option>
              {availableAgents.map((ag) => (
                <option key={ag.key} value={ag.key}>
                  {ag.short} ({tickets.filter((t) => getAgentData(t).key === ag.key).length})
                </option>
              ))}
            </select>

            {/* Filtro por Workflow */}
            <select
              value={selectedWorkflow}
              onChange={(e) => setSelectedWorkflow(e.target.value)}
              className="input-oceo input-sm"
              style={{ width: 175 }}
              title="Filtrar por Workflow / Plantilla"
            >
              <option value="">Todos los Workflows</option>
              {availableWorkflows.map((wf) => (
                <option key={wf} value={wf}>
                  wf: {wf}
                </option>
              ))}
              <option value="__none__">Sin Workflow (Directo)</option>
            </select>

            {/* Filtro por Prioridad */}
            <select
              value={selectedPriority}
              onChange={(e) => setSelectedPriority(e.target.value)}
              className="input-oceo input-sm"
              style={{ width: 135 }}
            >
              <option value="">Prioridad</option>
              <option value="critical">🔴 Crítica</option>
              <option value="high">🟠 Alta</option>
              <option value="medium">🔵 Media</option>
              <option value="low">⚪ Baja</option>
            </select>

            {activeFiltersCount > 0 && (
              <button
                onClick={handleResetFilters}
                className="btn-secondary btn-sm"
                title="Limpiar filtros activos"
              >
                Limpiar ({activeFiltersCount})
              </button>
            )}
          </div>

          {/* Right: Grouping Switch & View Mode Switcher */}
          <div className="kanban-filter-group">
            {viewMode === 'kanban' && (
              <div className="kanban-mode-switcher" title="Cambiar agrupación de columnas">
                <button
                  type="button"
                  className={`kanban-mode-btn ${groupBy === 'status' ? 'active' : ''}`}
                  onClick={() => setGroupBy('status')}
                >
                  <InboxStackIcon className="w-3.5 h-3.5" />
                  Por Estado
                </button>
                <button
                  type="button"
                  className={`kanban-mode-btn ${groupBy === 'agent' ? 'active' : ''}`}
                  onClick={() => setGroupBy('agent')}
                >
                  <CpuChipIcon className="w-3.5 h-3.5" />
                  Por Agente
                </button>
                <button
                  type="button"
                  className={`kanban-mode-btn ${groupBy === 'workflow' ? 'active' : ''}`}
                  onClick={() => setGroupBy('workflow')}
                >
                  <BoltIcon className="w-3.5 h-3.5" />
                  Por Workflow
                </button>
              </div>
            )}

            {!hideViewSwitcher && (
              <div className="kanban-mode-switcher" title="Modo de vista">
                <button
                  type="button"
                  className={`kanban-mode-btn ${viewMode === 'kanban' ? 'active' : ''}`}
                  onClick={() => setViewMode('kanban')}
                >
                  <ViewColumnsIcon className="w-4 h-4" />
                  Kanban
                </button>
                <button
                  type="button"
                  className={`kanban-mode-btn ${viewMode === 'table' ? 'active' : ''}`}
                  onClick={() => setViewMode('table')}
                >
                  <TableCellsIcon className="w-4 h-4" />
                  Tabla
                </button>
              </div>
            )}

            {onRefresh && (
              <button
                onClick={onRefresh}
                className="btn-icon btn-icon-sm"
                title="Actualizar datos"
                disabled={loading}
              >
                <ArrowPathIcon className={`w-4 h-4 ${loading ? 'animate-spin text-oceo-blue-400' : ''}`} />
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Main Board: Kanban or Table */}
      {viewMode === 'kanban' ? (
        <div className="kanban-board-container">
          {columns.map((column) => (
            <div
              key={column.id}
              className="kanban-column"
              style={{ '--column-accent': column.accent }}
            >
              {/* Column Header */}
              <div className="kanban-column-header">
                <div className="kanban-column-title-group">
                  <div className="kanban-column-dot" />
                  {column.icon && (
                    <column.icon className="w-4 h-4" style={{ color: column.accent }} />
                  )}
                  <span className="kanban-column-title">{column.title}</span>
                </div>
                <span className="kanban-column-count">{column.tickets.length}</span>
              </div>

              {/* Column Body with Cards */}
              <div className="kanban-column-body">
                {column.tickets.length === 0 ? (
                  <div className="kanban-empty-col">
                    <column.icon className="w-7 h-7 opacity-30" />
                    <span>Sin tareas</span>
                  </div>
                ) : (
                  column.tickets.map((ticket) => {
                    const agent = getAgentData(ticket)
                    const wf = getWorkflowKey(ticket)
                    const priority = PRIORITY_CONFIG[ticket.priority] || PRIORITY_CONFIG.medium
                    const phase = ticket.phase
                      ? PHASE_LABELS[ticket.phase] || { label: ticket.phase, badge: 'badge-info' }
                      : null
                    const status = STATUS_CONFIG[ticket.status] || STATUS_CONFIG.in_progress
                    const canRun =
                      ['queued', 'failed', 'interrupted'].includes(ticket.status) &&
                      !ticket.autorun

                    return (
                      <Link
                        key={ticket.task_id}
                        to={`/dashboard/tickets/${ticket.task_id}`}
                        state={detailBackState}
                        className="kanban-card"
                      >
                        {/* Card Header */}
                        <div className="kanban-card-header">
                          <code className="kanban-ticket-code">
                            {ticket.ticket_number != null
                              ? `#${ticket.ticket_number}`
                              : ticket.task_id.slice(0, 8)}
                          </code>

                          <div className="flex items-center gap-1.5">
                            {ticket.source && (
                              <span className="badge-source">{ticket.source}</span>
                            )}
                            <span
                              className={`status-badge ${status.badgeClass}`}
                              style={{ fontSize: '0.68rem', padding: '0.15rem 0.45rem' }}
                            >
                              {status.label}
                            </span>
                          </div>
                        </div>

                        {/* Title */}
                        <p className="kanban-card-title">{ticket.title}</p>

                        {/* Short snippet if present */}
                        {ticket.description && (
                          <p className="kanban-card-desc">{ticket.description}</p>
                        )}

                        {/* Tag Chips Row */}
                        <div className="kanban-card-badges">
                          {/* Agente Badge */}
                          <span className={`badge-agent ${agent.badgeClass}`} title={`Atendido por ${agent.label}`}>
                            <agent.icon className="w-3 h-3" />
                            {agent.short}
                          </span>

                          {/* Workflow Badge */}
                          {wf && (
                            <span className="badge-workflow" title={`Workflow: ${wf}`}>
                              <BoltIcon className="w-3 h-3" />
                              wf: {wf}
                            </span>
                          )}

                          {/* Phase Badge */}
                          {phase && (
                            <span className={`badge-phase ${phase.badge}`} title={`Fase actual: ${phase.label}`}>
                              {phase.label}
                            </span>
                          )}

                          {/* Coding Tier */}
                          {ticket.coding_tier && (
                            <span className="badge-neutral font-mono text-[0.68rem] px-1.5 py-0.5 rounded">
                              {ticket.coding_tier}
                            </span>
                          )}
                        </div>

                        {/* Card Footer */}
                        <div className="kanban-card-footer">
                          <div className="flex items-center gap-2">
                            {ticket.project && (
                              <span className="kanban-card-project" title={`Proyecto: ${ticket.project}`}>
                                <FolderIcon className="w-3 h-3 text-oceo-blue-400" />
                                {ticket.project}
                              </span>
                            )}
                            <span className="flex items-center gap-1 text-[0.7rem] text-slate-400">
                              <ClockIcon className="w-3 h-3 text-slate-500" />
                              {formatRelativeDate(ticket.updated_at || ticket.created_at)}
                            </span>
                          </div>

                          <div className="flex items-center gap-1">
                            {canRun && (
                              <PermissionCheck perms={['write_tickets']}>
                                <button
                                  type="button"
                                  onClick={(e) => handleRunTask(e, ticket.task_id)}
                                  disabled={runningTaskId === ticket.task_id}
                                  className="btn-secondary btn-sm"
                                  style={{ padding: '0.2rem 0.45rem', fontSize: '0.7rem' }}
                                  title="Lanzar ejecución ahora"
                                >
                                  <PlayIcon className="w-3 h-3 text-oceo-success" />
                                  Run
                                </button>
                              </PermissionCheck>
                            )}
                            <ArrowTopRightOnSquareIcon className="w-3.5 h-3.5 text-oceo-text-3 group-hover:text-oceo-blue-400" />
                          </div>
                        </div>
                      </Link>
                    )
                  })
                )}
              </div>
            </div>
          ))}
        </div>
      ) : (
        /* Table View */
        <div className="card-oceo table-card">
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Ticket</th>
                  <th>Título</th>
                  <th>Estado</th>
                  <th>Agente</th>
                  <th>Workflow / Fase</th>
                  <th>Prioridad</th>
                  <th>Proyecto</th>
                  <th>Fecha</th>
                </tr>
              </thead>
              <tbody>
                {filteredTickets.length === 0 ? (
                  <tr>
                    <td colSpan={8} className="empty-cell">
                      <div className="empty-state">
                        <InboxStackIcon className="w-10 h-10" style={{ color: 'var(--oceo-text-3)' }} />
                        <p>No se encontraron tickets con los filtros actuales</p>
                      </div>
                    </td>
                  </tr>
                ) : (
                  filteredTickets.map((ticket) => {
                    const agent = getAgentData(ticket)
                    const wf = getWorkflowKey(ticket)
                    const status = STATUS_CONFIG[ticket.status] || STATUS_CONFIG.in_progress
                    const priority = PRIORITY_CONFIG[ticket.priority] || PRIORITY_CONFIG.medium

                    return (
                      <tr key={ticket.task_id} className="ticket-row">
                        <td>
                          <code className="ticket-id">
                            {ticket.ticket_number != null ? `#${ticket.ticket_number}` : ticket.task_id.slice(0, 8)}
                          </code>
                        </td>
                        <td>
                          <Link
                            to={`/dashboard/tickets/${ticket.task_id}`}
                            state={detailBackState}
                            className="ticket-title-link"
                          >
                            {ticket.title}
                          </Link>
                        </td>
                        <td>
                          <span className={`status-badge ${status.badgeClass}`}>{status.label}</span>
                        </td>
                        <td>
                          <span className={`badge-agent ${agent.badgeClass}`}>
                            <agent.icon className="w-3 h-3" />
                            {agent.short}
                          </span>
                        </td>
                        <td>
                          {wf ? (
                            <span className="badge-workflow">wf: {wf}</span>
                          ) : ticket.phase ? (
                            <span className="badge-phase badge-info">{ticket.phase}</span>
                          ) : (
                            <span className="text-slate-500 text-xs">—</span>
                          )}
                        </td>
                        <td>
                          <span className={`status-badge ${priority.badge}`}>{priority.label}</span>
                        </td>
                        <td className="project-cell">{ticket.project || '—'}</td>
                        <td className="date-cell">{formatRelativeDate(ticket.created_at)}</td>
                      </tr>
                    )
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
