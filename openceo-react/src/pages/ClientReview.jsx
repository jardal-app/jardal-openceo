import { useContext, useEffect, useState } from 'react'
import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import {
  CheckCircleIcon,
  PhotoIcon,
  VideoCameraIcon,
  LinkIcon,
  ChatBubbleLeftIcon,
  ClockIcon,
  TagIcon,
} from '@heroicons/react/24/outline'
import { Dialog } from '@headlessui/react'
import client from '@/api'
import { StoreContext } from '@/context/store'
import { swalError } from '@/utils/swalError'

export default function ClientReview() {
  const store = useContext(StoreContext)
  const [tickets, setTickets] = useState([])
  const [loading, setLoading] = useState(true)
  const [selectedTicket, setSelectedTicket] = useState(null)
  const [observationModal, setObservationModal] = useState(false)
  const [observation, setObservation] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const prefersReducedMotion = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  useGSAP(() => {
    if (prefersReducedMotion) return
    const ctx = gsap.context(() => {
      gsap.from('.review-card', { y: 20, opacity: 0, stagger: 0.1, duration: 0.5, ease: 'power2.out' })
    })
    return () => ctx.revert()
  }, [prefersReducedMotion])

  useEffect(() => { loadTickets() }, [])

  const loadTickets = async () => {
    setLoading(true)
    try {
      const response = await client.post('/api/tasks/search', {
        phase: 'client_signoff',
        status: 'waiting',
        client_visible: true,
        limit: 500,
      })
      setTickets(response.data)
    } catch { /* silent */ } finally { setLoading(false) }
  }

  const handleApprove = async (ticketId) => {
    setSubmitting(true)
    try {
      await client.post(`/api/tasks/${ticketId}/phase-action`, {
        action: 'approve',
        comment: 'Aprobado por el cliente desde el panel de revisión',
      })
      loadTickets()
    } catch (err) {
      swalError(err)
    } finally {
      setSubmitting(false)
    }
  }

  const handleObservation = async () => {
    if (!observation.trim()) return
    setSubmitting(true)
    try {
      await client.post(`/api/tasks/${selectedTicket.task_id}/phase-action`, {
        action: 'request_changes',
        comment: observation.trim(),
      })
      setObservationModal(false)
      setObservation('')
      setSelectedTicket(null)
      loadTickets()
    } catch (err) {
      swalError(err)
    } finally {
      setSubmitting(false)
    }
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
      {/* Header */}
      <div className="section-header">
        <div>
          <h1 className="dashboard-title">Revisión de Entregables</h1>
          <p className="dashboard-subtitle">Revisa los avances y aprueba los trabajos completados</p>
        </div>
      </div>

      {tickets.length === 0 ? (
        <div className="card-oceo empty-review-state">
          <CheckCircleIcon className="w-16 h-16" style={{ color: 'var(--oceo-success)' }} />
          <h3>Todo al día</h3>
          <p>No hay entregables pendientes de revisión en este momento.</p>
        </div>
      ) : (
        <div className="review-cards-list">
          {tickets.map((ticket) => {
            const evidence = ticket.params?.evidence || []
            const rawReviewLinks = Array.isArray(ticket.params?.review_links) ? ticket.params.review_links.filter(Boolean) : []
            const fallbackReviewUrl = ticket.params?.test_url || ticket.params?.client_review_url || ticket.client_review_url || ''
            const reviewLinks = rawReviewLinks.length > 0
              ? rawReviewLinks
              : (fallbackReviewUrl ? [fallbackReviewUrl] : [])
            const clientDesc = ticket.params?.client_description || ticket.description || ''

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
                  <span className="status-badge badge-warning">Pendiente de aprobación</span>
                </div>

                <div className="review-description">
                  <p>{clientDesc}</p>
                </div>

                {reviewLinks.length > 0 && (
                  <div className="review-test-url">
                    <div className="review-test-url-header">
                      <LinkIcon className="w-4 h-4" />
                      <span>{reviewLinks.length > 1 ? 'Links de prueba' : 'URL de prueba'}</span>
                    </div>
                    <div className="flex flex-col gap-2">
                      {reviewLinks.map((url, idx) => (
                        <a key={`${url}-${idx}`} href={url} target="_blank" rel="noopener noreferrer" className="test-url-link">
                          {url}
                        </a>
                      ))}
                    </div>
                  </div>
                )}

                {evidence.length > 0 && (
                  <div className="review-evidence">
                    <div className="review-evidence-header">
                      <PhotoIcon className="w-4 h-4" />
                      <span>Evidencia ({evidence.length})</span>
                    </div>
                    <div className="evidence-scroll">
                      {evidence.map((url, idx) => (
                        <a key={idx} href={url} target="_blank" rel="noopener noreferrer" className="evidence-thumb">
                          {url.match(/\.(jpg|jpeg|png|gif|webp)$/i) ? (
                            <img src={url} alt={`Evidencia ${idx + 1}`} className="evidence-thumb-img" />
                          ) : (
                            <div className="evidence-thumb-placeholder">
                              <VideoCameraIcon className="w-6 h-6" style={{ color: 'var(--oceo-text-3)' }} />
                            </div>
                          )}
                        </a>
                      ))}
                    </div>
                  </div>
                )}

                <div className="review-actions">
                  <button onClick={() => handleApprove(ticket.task_id)} disabled={submitting} className="btn-primary">
                    <CheckCircleIcon className="w-5 h-5" />
                    Aprobar y Cerrar
                  </button>
                  <button onClick={() => { setSelectedTicket(ticket); setObservationModal(true) }} disabled={submitting} className="btn-secondary">
                    <ChatBubbleLeftIcon className="w-5 h-5" />
                    No Aprobar — Observaciones
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* Observation Modal */}
      <Dialog open={observationModal} onClose={() => { if (!submitting) { setObservationModal(false); setObservation('') } }} className="relative z-50">
        <div className="dialog-backdrop" aria-hidden="true" />
        <div className="dialog-container">
          <Dialog.Panel className="dialog-panel card-oceo">
            <Dialog.Title className="dialog-title">Añadir Observaciones</Dialog.Title>
            <p className="dialog-description">Describe qué cambios necesitas o por qué no puedes aprobar este entregable.</p>
            <textarea
              value={observation}
              onChange={(e) => setObservation(e.target.value)}
              rows={5}
              className="input-oceo dialog-textarea"
              placeholder="Escribe tus observaciones aquí..."
            />
            <div className="dialog-actions">
              <button onClick={() => { setObservationModal(false); setObservation('') }} disabled={submitting} className="btn-secondary">
                Cancelar
              </button>
              <button onClick={handleObservation} disabled={submitting || !observation.trim()} className="btn-primary">
                {submitting ? 'Enviando...' : 'Enviar Observaciones'}
              </button>
            </div>
          </Dialog.Panel>
        </div>
      </Dialog>
    </div>
  )
}
