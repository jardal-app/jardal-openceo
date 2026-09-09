import { useContext, useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import {
  TicketIcon,
  EyeIcon,
  UserIcon,
  CheckCircleIcon,
  ArrowRightIcon,
  ChartBarIcon,
  BoltIcon,
  ClipboardDocumentCheckIcon,
  ClockIcon,
  ExclamationTriangleIcon,
  PlusIcon,
  InboxStackIcon,
} from '@heroicons/react/24/outline'
import client from '@/api'
import { StoreContext } from '@/context/store'
import KanbanBoard from '@/components/KanbanBoard'
import { PermissionCheck } from '@/components/PermissionCheck'

const statusLabels = {
  queued: 'En cola',
  in_progress: 'En progreso',
  completed: 'Completado',
  failed: 'Fallido',
  escalated: 'Escalado',
  interrupted: 'Interrumpido',
  waiting: 'Esperando',
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

function formatShortDate(dateStr) {
  if (!dateStr) return 'Sin fecha'
  return new Date(dateStr).toLocaleDateString('es-MX', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  })
}

function getClientTicketState(ticket) {
  if (ticket.status === 'completed') {
    return { key: 'completed', label: 'Completado', color: 'badge-success' }
  }
  if (ticket.status === 'waiting' && ticket.phase === 'client_signoff') {
    return { key: 'waiting_review', label: 'En espera de revisión', color: 'badge-client' }
  }
  return { key: 'in_progress', label: 'En proceso', color: 'badge-active' }
}

export default function Dashboard() {
  const store = useContext(StoreContext)
  const location = useLocation()
  const isClient = store.user?.is_client === true
  const isQAHome = store.checkPermissions(['qa_review']) && !store.user?.is_admin && !isClient
  const detailBackState = { from: location.pathname }

  const [stats, setStats] = useState({
    total: 0,
    inProgress: 0,
    queued: 0,
    inReview: 0,
    clientWaiting: 0,
    completed: 0,
    failed: 0,
  })
  const [allTickets, setAllTickets] = useState([])
  const [recentTickets, setRecentTickets] = useState([])
  const [qaPendingTickets, setQaPendingTickets] = useState([])
  const [qaClientTickets, setQaClientTickets] = useState([])
  const [clientTickets, setClientTickets] = useState([])
  const [loading, setLoading] = useState(true)

  const prefersReducedMotion = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  useGSAP(() => {
    if (prefersReducedMotion) return
    const ctx = gsap.context(() => {
      gsap.from('.stat-card', { y: 20, opacity: 0, stagger: 0.08, duration: 0.45, ease: 'power2.out' })
      gsap.from('.section-header', { y: -10, opacity: 0, duration: 0.4, ease: 'power2.out' })
      gsap.from('.completion-card, .qa-stream-card', { y: 16, opacity: 0, stagger: 0.1, duration: 0.45, delay: 0.15, ease: 'power2.out' })
    })
    return () => ctx.revert()
  }, [prefersReducedMotion, isQAHome, isClient, allTickets])

  useEffect(() => {
    loadDashboard()
  }, [store.selectedProject, isQAHome, isClient])

  const loadDashboard = async () => {
    setLoading(true)
    try {
      if (isClient) {
        const response = await client.get('/api/tasks', { params: { limit: 500 } })
        const rows = Array.isArray(response.data) ? response.data : []
        const derived = rows.reduce((acc, ticket) => {
          const state = getClientTicketState(ticket).key
          if (state === 'waiting_review') acc.clientWaiting += 1
          else if (state === 'completed') acc.completed += 1
          else acc.inProgress += 1
          return acc
        }, { inProgress: 0, clientWaiting: 0, completed: 0 })

        setStats({
          total: rows.length,
          inProgress: derived.inProgress,
          queued: 0,
          inReview: 0,
          clientWaiting: derived.clientWaiting,
          completed: derived.completed,
          failed: 0,
        })
        setClientTickets(rows)
        setAllTickets(rows)
        setRecentTickets([])
        setQaPendingTickets([])
        setQaClientTickets([])
        setLoading(false)
        return
      }

      const projectParams = store.selectedProject ? { project: store.selectedProject } : {}

      const requests = [
        client.get('/api/tasks/stats', { params: projectParams }),
        client.get('/api/tasks', { params: { ...projectParams, limit: 500 } }),
      ]

      if (isQAHome) {
        requests.push(
          client.post('/api/tasks/search', {
            ...projectParams,
            phase: 'qa_review',
            status: 'waiting',
            limit: 4,
          }),
          client.post('/api/tasks/search', {
            ...projectParams,
            phase: 'client_signoff',
            status: 'waiting',
            limit: 4,
          }),
        )
      }

      const responses = await Promise.all(requests)
      const statsResponse = responses[0]
      const tasksResponse = responses[1]
      const rows = Array.isArray(tasksResponse?.data) ? tasksResponse.data : []

      setAllTickets(rows)

      const counts = rows.reduce((acc, t) => {
        acc.total++
        if (t.status === 'in_progress') acc.inProgress++
        else if (t.status === 'queued') acc.queued++
        else if (t.status === 'waiting') acc.waiting++
        else if (t.status === 'completed') acc.completed++
        else if (['failed', 'escalated', 'interrupted'].includes(t.status)) acc.failed++
        return acc
      }, { total: 0, inProgress: 0, queued: 0, waiting: 0, completed: 0, failed: 0 })

      setStats({
        total: statsResponse.data?.total || counts.total,
        inProgress: counts.inProgress,
        queued: counts.queued,
        inReview: statsResponse.data?.in_review || counts.waiting,
        clientWaiting: statsResponse.data?.client_waiting || 0,
        completed: statsResponse.data?.completed || counts.completed,
        failed: counts.failed,
      })

      if (isQAHome) {
        setQaPendingTickets(Array.isArray(responses[2]?.data) ? responses[2].data : [])
        setQaClientTickets(Array.isArray(responses[3]?.data) ? responses[3].data : [])
        setRecentTickets([])
        setClientTickets([])
      } else {
        setRecentTickets(rows.slice(0, 5))
        setQaPendingTickets([])
        setQaClientTickets([])
        setClientTickets([])
      }
    } catch {
      /* silent */
    } finally {
      setLoading(false)
    }
  }

  const renderQaStream = ({ title, icon: Icon, tickets, emptyText, badgeClass, badgeLabel }) => (
    <div className="card-oceo recent-section qa-stream-card">
      <div className="section-header-inner">
        <div className="flex items-center gap-2">
          {Icon && <Icon className="w-5 h-5" style={{ color: 'var(--oceo-blue-400)' }} />}
          <h2>{title}</h2>
        </div>
      </div>

      {tickets.length === 0 ? (
        <div className="empty-state qa-stream-empty">
          <TicketIcon className="w-10 h-10" style={{ color: 'var(--oceo-text-3)' }} />
          <p>{emptyText}</p>
        </div>
      ) : (
        <div className="recent-list">
          {tickets.map((ticket) => (
            <Link
              key={ticket.task_id}
              to={`/dashboard/tickets/${ticket.task_id}`}
              state={detailBackState}
              className="recent-ticket qa-stream-ticket"
            >
              <div className="recent-ticket-info">
                <div className="qa-stream-topline">
                  <code className="ticket-id">
                    {ticket.ticket_number != null ? `#${ticket.ticket_number}` : ticket.task_id.slice(0, 8)}
                  </code>
                  <span className={`status-badge ${badgeClass}`}>{badgeLabel}</span>
                </div>
                <p className="recent-ticket-title">{ticket.title}</p>
                <p className="recent-ticket-meta">
                  {ticket.project || 'Sin proyecto'} · {formatShortDate(ticket.updated_at || ticket.created_at)}
                </p>
              </div>
              <ArrowRightIcon className="w-4 h-4 qa-stream-arrow" />
            </Link>
          ))}
        </div>
      )}
    </div>
  )

  const clientSections = [
    {
      key: 'waiting_review',
      title: 'En espera de revisión',
      badgeClass: 'badge-client',
      tickets: clientTickets.filter((ticket) => getClientTicketState(ticket).key === 'waiting_review'),
      emptyText: 'No hay tickets esperando tu revisión.',
    },
    {
      key: 'in_progress',
      title: 'En proceso',
      badgeClass: 'badge-active',
      tickets: clientTickets.filter((ticket) => getClientTicketState(ticket).key === 'in_progress'),
      emptyText: 'No hay tickets en proceso.',
    },
    {
      key: 'completed',
      title: 'Completados',
      badgeClass: 'badge-success',
      tickets: clientTickets.filter((ticket) => getClientTicketState(ticket).key === 'completed'),
      emptyText: 'Todavía no hay tickets completados.',
    },
  ]

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="animate-spin rounded-full h-10 w-10 border-2 border-oceo-blue-400 border-t-transparent" />
      </div>
    )
  }

  const completionRate = stats.total > 0 ? Math.round((stats.completed / stats.total) * 100) : 0
  const qaProgressBase = stats.inReview + stats.clientWaiting + stats.completed
  const qaProgress = qaProgressBase > 0
    ? Math.round(((stats.clientWaiting + stats.completed) / qaProgressBase) * 100)
    : 0

  if (isClient) {
    return (
      <div className="dashboard-page client-dashboard-page">
        <div className="section-header">
          <div>
            <h1 className="dashboard-title">Tus tickets</h1>
            <p className="dashboard-subtitle">Todo tu seguimiento en una sola vista.</p>
          </div>
        </div>

        <div className="stats-grid client-stats-grid">
          <div className="stat-card card-oceo">
            <div className="stat-icon-wrap" style={{ background: 'rgba(46,134,230,0.15)' }}>
              <TicketIcon className="stat-icon" style={{ color: 'var(--oceo-blue-400)' }} />
            </div>
            <div className="stat-content">
              <p className="stat-value">{stats.total}</p>
              <p className="stat-label">Total</p>
            </div>
          </div>

          <div className="stat-card card-oceo">
            <div className="stat-icon-wrap" style={{ background: 'rgba(168,85,247,0.14)' }}>
              <EyeIcon className="stat-icon" style={{ color: '#c084fc' }} />
            </div>
            <div className="stat-content">
              <p className="stat-value">{stats.clientWaiting}</p>
              <p className="stat-label">En espera de revisión</p>
            </div>
          </div>

          <div className="stat-card card-oceo">
            <div className="stat-icon-wrap" style={{ background: 'rgba(46,134,230,0.12)' }}>
              <UserIcon className="stat-icon" style={{ color: 'var(--oceo-blue-300)' }} />
            </div>
            <div className="stat-content">
              <p className="stat-value">{clientSections[1].tickets.length}</p>
              <p className="stat-label">En proceso</p>
            </div>
          </div>

          <div className="stat-card card-oceo">
            <div className="stat-icon-wrap" style={{ background: 'rgba(18,185,129,0.15)' }}>
              <CheckCircleIcon className="stat-icon" style={{ color: 'var(--oceo-success)' }} />
            </div>
            <div className="stat-content">
              <p className="stat-value">{stats.completed}</p>
              <p className="stat-label">Completados</p>
            </div>
          </div>
        </div>

        <div className="client-ticket-sections">
          {clientSections.map((section) => (
            <section key={section.key} className="card-oceo client-ticket-section">
              <div className="section-header-inner">
                <h2>{section.title}</h2>
                <span className={`status-badge ${section.badgeClass}`}>{section.tickets.length}</span>
              </div>

              {section.tickets.length === 0 ? (
                <div className="empty-state client-ticket-empty">
                  <TicketIcon className="w-10 h-10" style={{ color: 'var(--oceo-text-3)' }} />
                  <p>{section.emptyText}</p>
                </div>
              ) : (
                <div className="client-ticket-list">
                  {section.tickets.map((ticket) => {
                    const state = getClientTicketState(ticket)
                    return (
                      <Link
                        key={ticket.task_id}
                        to={`/dashboard/tickets/${ticket.task_id}`}
                        state={detailBackState}
                        className="client-ticket-card"
                      >
                        <div className="client-ticket-card-main">
                          <div className="client-ticket-card-top">
                            <code className="ticket-id">
                              {ticket.ticket_number != null ? `#${ticket.ticket_number}` : ticket.task_id.slice(0, 8)}
                            </code>
                            <span className={`status-badge ${state.color}`}>{state.label}</span>
                          </div>
                          <p className="client-ticket-card-title">{ticket.title}</p>
                          {ticket.project && (
                            <div className="client-ticket-card-meta">
                              <span className="project-badge">{ticket.project}</span>
                            </div>
                          )}
                        </div>
                        <ArrowRightIcon className="w-4 h-4 qa-stream-arrow" />
                      </Link>
                    )
                  })}
                </div>
              )}
            </section>
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="dashboard-page">
      <div className="section-header">
        <div>
          <h1 className="dashboard-title">Centro de Operaciones</h1>
          <p className="dashboard-subtitle">Monitoreo en tiempo real de agentes, flujos y estado de tareas</p>
        </div>
        <div className="dashboard-header-actions">
          <PermissionCheck perms={['write_tickets']}>
            <Link to="/dashboard/tickets/create" className="btn-primary btn-sm">
              <PlusIcon className="w-4 h-4" />
              Nuevo Ticket
            </Link>
          </PermissionCheck>
          <Link to="/dashboard/tickets/list" className="btn-secondary btn-sm">
            <TicketIcon className="w-4 h-4" />
            Tabla de Tickets
          </Link>
        </div>
      </div>

      {/* Stats Cards Grid - 6 Key Indicators */}
      <div className="stats-grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))' }}>
        <div className="stat-card card-oceo">
          <div className="stat-icon-wrap" style={{ background: 'rgba(46,134,230,0.15)' }}>
            <InboxStackIcon className="stat-icon" style={{ color: 'var(--oceo-blue-400)' }} />
          </div>
          <div className="stat-content">
            <p className="stat-value">{stats.total}</p>
            <p className="stat-label">Total Tareas</p>
          </div>
        </div>

        <div className="stat-card card-oceo">
          <div className="stat-icon-wrap" style={{ background: 'rgba(46,134,230,0.15)' }}>
            <BoltIcon className="stat-icon" style={{ color: 'var(--oceo-blue-300)' }} />
          </div>
          <div className="stat-content">
            <p className="stat-value">{stats.inProgress}</p>
            <p className="stat-label">En Progreso</p>
          </div>
        </div>

        <div className="stat-card card-oceo">
          <div className="stat-icon-wrap" style={{ background: 'rgba(245,165,36,0.15)' }}>
            <EyeIcon className="stat-icon" style={{ color: 'var(--oceo-warning)' }} />
          </div>
          <div className="stat-content">
            <p className="stat-value">{stats.inReview}</p>
            <p className="stat-label">{isQAHome ? 'Pendientes QA' : 'En Espera / Rev.'}</p>
          </div>
        </div>

        <div className="stat-card card-oceo">
          <div className="stat-icon-wrap" style={{ background: 'rgba(245,165,36,0.12)' }}>
            <ClockIcon className="stat-icon" style={{ color: '#fbbf24' }} />
          </div>
          <div className="stat-content">
            <p className="stat-value">{stats.queued}</p>
            <p className="stat-label">En Cola</p>
          </div>
        </div>

        <div className="stat-card card-oceo">
          <div className="stat-icon-wrap" style={{ background: 'rgba(18,185,129,0.15)' }}>
            <CheckCircleIcon className="stat-icon" style={{ color: 'var(--oceo-success)' }} />
          </div>
          <div className="stat-content">
            <p className="stat-value">{stats.completed}</p>
            <p className="stat-label">Completadas</p>
          </div>
        </div>

        <div className="stat-card card-oceo">
          <div className="stat-icon-wrap" style={{ background: 'rgba(240,68,82,0.15)' }}>
            <ExclamationTriangleIcon className="stat-icon" style={{ color: 'var(--oceo-danger)' }} />
          </div>
          <div className="stat-content">
            <p className="stat-value">{stats.failed}</p>
            <p className="stat-label">Fallidos / Atención</p>
          </div>
        </div>
      </div>

      {/* Completion bar */}
      <div className="card-oceo completion-card" style={{ marginBottom: '1.25rem' }}>
        <div className="completion-header">
          <ChartBarIcon className="w-5 h-5" style={{ color: 'var(--oceo-blue-400)' }} />
          <h3>Tasa de Completitud</h3>
        </div>
        <div className="completion-bar-wrap">
          <div className="completion-bar">
            <div className="completion-bar-fill" style={{ width: `${completionRate}%` }} />
          </div>
          <span className="completion-percentage">{completionRate}%</span>
        </div>
      </div>

      {/* QA Stream if isQAHome */}
      {isQAHome && (
        <div className="qa-stream-grid" style={{ marginBottom: '1.5rem' }}>
          {renderQaStream({
            title: 'Pendientes QA',
            icon: ClipboardDocumentCheckIcon,
            tickets: qaPendingTickets,
            emptyText: 'Sin tickets pendientes',
            badgeClass: 'badge-warning',
            badgeLabel: 'QA',
          })}
          {renderQaStream({
            title: 'Esperando Cliente',
            icon: UserIcon,
            tickets: qaClientTickets,
            emptyText: 'Sin tickets esperando cliente',
            badgeClass: 'badge-client',
            badgeLabel: 'Cliente',
          })}
        </div>
      )}

      {/* Interactive Kanban Board */}
      <KanbanBoard
        tickets={allTickets}
        loading={loading}
        onRefresh={loadDashboard}
        initialView="kanban"
        initialGroupBy="status"
      />
    </div>
  )
}
