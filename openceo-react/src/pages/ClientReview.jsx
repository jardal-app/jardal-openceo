import { useContext, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import {
  CheckCircleIcon,
  ClockIcon,
  LinkIcon,
  PhotoIcon,
  TagIcon,
  TicketIcon,
} from '@heroicons/react/24/outline'
import client from '@/api'
import { StoreContext } from '@/context/store'

export default function ClientReview() {
  const store = useContext(StoreContext)
  const [tickets, setTickets] = useState([])
  const [loading, setLoading] = useState(true)
  const prefersReducedMotion = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  const detailBackState = { from: '/dashboard/review' }

  useGSAP(() => {
    if (prefersReducedMotion) return
    const ctx = gsap.context(() => {
      gsap.from('.review-card', { y: 20, opacity: 0, stagger: 0.1, duration: 0.5, ease: 'power2.out' })
    })
    return () => ctx.revert()
  }, [tickets, prefersReducedMotion])

  useEffect(() => { loadTickets() }, [store.selectedProject])

  const loadTickets = async () => {
    setLoading(true)
    try {
      const body = {
        phase: 'client_signoff',
        status: 'waiting',
        client_visible: true,
        limit: 500,
      }
      if (store.selectedProject) body.project = store.selectedProject
      const response = await client.post('/api/tasks/search', body)
      setTickets(response.data)
    } catch { /* silent */ } finally { setLoading(false) }
  }

  const formatDate = (dateStr) => {
    if (!dateStr) return '—'
    return new Date(dateStr).toLocaleDateString('es-MX', {
      day: '2-digit', month: 'long', year: 'numeric',
    })
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="animate-spin rounded-full h-10 w-10 border-2 border-oceo-blue-400 border-t-transparent" />
      </div>
    )
  }

  return (
    <div className="client-review-page">
      <div className="section-header">
        <div>
          <h1 className="dashboard-title">Revisión de Entregables</h1>
          <p className="dashboard-subtitle">Tickets listos para tu aprobación</p>
        </div>
      </div>

      {tickets.length === 0 ? (
        <div className="card-oceo empty-review-state">
          <CheckCircleIcon className="w-16 h-16" style={{ color: 'var(--oceo-success)' }} />
          <h3>Todo al día</h3>
          <p>No hay tickets pendientes de revisión en este momento.</p>
        </div>
      ) : (
        <div className="review-cards-list">
          {tickets.map((ticket) => {
            const evidence = ticket.params?.evidence || []
            const qaEvidence = ticket.qa_evidence || []
            const clientEvidence = ticket.client_evidence || []
            const rawReviewLinks = Array.isArray(ticket.params?.review_links) ? ticket.params.review_links.filter(Boolean) : []
            const fallbackReviewUrl = ticket.params?.test_url || ticket.params?.client_review_url || ticket.client_review_url || ''
            const reviewLinks = rawReviewLinks.length > 0
              ? rawReviewLinks
              : (fallbackReviewUrl ? [fallbackReviewUrl] : [])
            const clientDesc = ticket.client_description || ticket.params?.client_description || ticket.description || ''

            return (
              <div key={ticket.task_id} className="review-card card-oceo">
                <div className="review-card-header">
                  <div>
                    <h3 className="review-title">
                      {ticket.ticket_number != null ? `#${ticket.ticket_number} — ` : ''}{ticket.title}
                    </h3>
                    <div className="review-meta-row">
                      <span className="project-badge">
                        <TagIcon className="w-3 h-3" />
                        {ticket.project || 'Sin proyecto'}
                      </span>
                      <span className="review-date">
                        <ClockIcon className="w-3 h-3" />
                        {formatDate(ticket.created_at)}
                      </span>
                    </div>
                  </div>
                  <span className="status-badge badge-client">Cliente</span>
                </div>

                {clientDesc && (
                  <div className="review-description">
                    <p>{clientDesc}</p>
                  </div>
                )}

                <div className="review-meta-row" style={{ marginTop: 12, flexWrap: 'wrap', gap: 8 }}>
                  <span className="status-badge badge-info">
                    <LinkIcon className="w-3 h-3" />
                    {reviewLinks.length} link{reviewLinks.length === 1 ? '' : 's'}
                  </span>
                  <span className="status-badge badge-neutral">
                    <PhotoIcon className="w-3 h-3" />
                    {evidence.length + qaEvidence.length + clientEvidence.length} archivo{(evidence.length + qaEvidence.length + clientEvidence.length) === 1 ? '' : 's'}
                  </span>
                </div>

                <div className="review-actions">
                  <Link
                    to={`/dashboard/tickets/${ticket.task_id}`}
                    state={detailBackState}
                    className="btn-primary"
                  >
                    <TicketIcon className="w-5 h-5" />
                    Abrir ticket
                  </Link>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
