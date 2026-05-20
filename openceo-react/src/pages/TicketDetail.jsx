import { useContext, useEffect, useRef, useState } from 'react'
import { useParams, Link, useLocation, useNavigate } from 'react-router-dom'
import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import {
  ArrowLeftIcon,
  CheckCircleIcon,
  XCircleIcon,
  ClockIcon,
  PhotoIcon,
  VideoCameraIcon,
  LinkIcon,
  TagIcon,
  UserIcon,
  GlobeAltIcon,
  CalendarIcon,
  MagnifyingGlassIcon,
  ArrowTopRightOnSquareIcon,
  CpuChipIcon,
  WrenchIcon,
  DocumentTextIcon,
  ListBulletIcon,
  BeakerIcon,
  RocketLaunchIcon,
  CogIcon,
  ExclamationTriangleIcon,
  PencilIcon,
  PlusIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline'
import client from '@/api'
import { StoreContext } from '@/context/store'
import { swalError } from '@/utils/swalError'
import { swalConfirm } from '@/utils/swalConfirm'
import Swal from 'sweetalert2'
import FilesUpload from '@/components/FilesUpload'

const statusLabels = {
  queued: 'Queued',
  in_progress: 'In Progress',
  completed: 'Completed',
  failed: 'Failed',
  escalated: 'Escalated',
  interrupted: 'Interrupted',
  waiting: 'Waiting',
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

const approvalLabels = {
  not_required: 'No requerida',
  pending: 'Pendiente',
  approved: 'Aprobada',
  changes_requested: 'Cambios solicitados',
  rejected: 'Rechazada',
}

const approvalColors = {
  not_required: 'badge-active',
  pending: 'badge-warning',
  approved: 'badge-success',
  changes_requested: 'badge-warning',
  rejected: 'badge-danger',
}

const qaStateLabels = {
  not_required: 'No requerida',
  pending: 'Pendiente de QA',
  approved: 'Aprobado por QA',
  changes_requested: 'Cambios solicitados por QA',
}

const qaStateColors = {
  not_required: 'badge-active',
  pending: 'badge-warning',
  approved: 'badge-success',
  changes_requested: 'badge-danger',
}

function cleanupSwalBackdrops() {
  document.querySelectorAll('.swal2-container').forEach(el => {
    if (!el.querySelector('.swal2-popup')) {
      el.remove()
    }
  })
}

export default function TicketDetail() {
  const { id } = useParams()
  const location = useLocation()
  const navigate = useNavigate()
  const store = useContext(StoreContext)
  const [ticket, setTicket] = useState(null)
  const [reviews, setReviews] = useState([])
  const [loading, setLoading] = useState(true)
  const [savingEvidence, setSavingEvidence] = useState(false)
  const [openingSession, setOpeningSession] = useState(false)
  const [qaEvidence, setQaEvidence] = useState([])
  const [clientEvidence, setClientEvidence] = useState([])
  const [qaReviewLinks, setQaReviewLinks] = useState([])
  const [newLink, setNewLink] = useState('')
  const [usersMap, setUsersMap] = useState({})
  const [requestingChanges, setRequestingChanges] = useState(false)
  const [requestChangesComment, setRequestChangesComment] = useState('')
  const filesUploadRef = useRef(null)
  const clientFilesUploadRef = useRef(null)
  const containerRef = useRef(null)
  const isClient = store.user?.is_client === true
  const backHref = typeof location.state?.from === 'string'
    ? location.state.from
    : (isClient ? '/dashboard/inicio' : '/dashboard/tickets/list')
  const backLabel = backHref === '/dashboard/inicio' ? 'Volver al dashboard' : 'Volver a tickets'
  const isAdmin = store.user?.is_admin === true
  const isQA = store.checkPermissions(['qa_review']) && !isClient

  const prefersReducedMotion = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  useGSAP(() => {
    if (prefersReducedMotion) return
    const scope = containerRef.current
    if (!scope) return
    const ctx = gsap.context(() => {
      gsap.from('.detail-section', { y: 15, opacity: 0, stagger: 0.08, duration: 0.4, ease: 'power2.out' })
    }, scope)
    return () => ctx.revert()
  }, [prefersReducedMotion, ticket])

  useEffect(() => {
    loadTicket(); loadUsers()
    return () => {
      cleanupSwalBackdrops()
    }
  }, [id])

  const loadUsers = async () => {
    try {
      const res = await client.get('/api/users/names')
      setUsersMap(res.data)
    } catch { /* silent */ }
  }

  const loadTicket = async () => {
    setLoading(true)
    try {
      const [ticketRes, reviewsRes] = await Promise.all([
        client.get(`/api/tasks/${id}`),
        client.get(`/api/tasks/${id}/reviews`),
      ])
      const data = ticketRes.data
      setTicket(data)
      setReviews(reviewsRes.data)
      setQaEvidence(data.qa_evidence || [])
      setClientEvidence(data.client_evidence || [])
      setQaReviewLinks(Array.isArray(data.params?.review_links) ? data.params.review_links.filter(Boolean) : [])
      filesUploadRef.current?.resetCommitted?.(data.qa_evidence || [])
      clientFilesUploadRef.current?.resetCommitted?.(data.client_evidence || [])
    } catch { /* silent */ } finally { setLoading(false) }
  }

  const handleApprove = async () => {
    const confirmed = await swalConfirm({
      title: '¿Aprobar?',
      text: 'Esta acción aprueba la fase actual y continúa el flujo.',
      icon: 'question',
      confirmText: 'Aprobar',
      cancelText: 'Cancelar',
    })
    if (!confirmed) return
    try {
      await client.post(`/api/tasks/${id}/phase-action`, {
        action: 'approve',
        comment: 'Aprobado desde el panel web',
      })
      if (isClient) {
        navigate('/dashboard/inicio', { replace: true })
        return
      }
      loadTicket()
    } catch (err) {
      await swalError(err)
    }
  }

  const [sessionUrls, setSessionUrls] = useState({})

  const handleOpenSession = async (tool) => {
    setOpeningSession(true)
    try {
      const response = await client.post(`/api/tasks/${id}/phase-${tool}`, { phase: ticket?.phase || 'qa_review' })
      const { url } = response.data
      if (url) {
        setSessionUrls(prev => ({ ...prev, [tool]: url }))
      }
    } catch (err) {
      swalError(err)
    } finally {
      setOpeningSession(false)
    }
  }

  const handlePhaseRequestChanges = () => {
    setRequestingChanges(true)
  }

  const handleCancelRequestChanges = () => {
    setRequestingChanges(false)
    setRequestChangesComment('')
  }

  const handleSubmitRequestChanges = async () => {
    const comment = requestChangesComment.trim()
    if (!comment) {
      await Swal.fire({
        title: 'Faltan observaciones',
        text: 'Escribe al menos una observación antes de enviar.',
        icon: 'warning',
        confirmButtonText: 'Entendido',
      })
      return
    }
    setSavingEvidence(true)
    let evidenceSaved = false
    try {
      const savedEvidence = await persistClientEvidence()
      evidenceSaved = true
      await client.post(`/api/tasks/${id}/phase-action`, {
        action: 'request_changes',
        comment,
        attachments: savedEvidence,
        metadata: { client_evidence_count: savedEvidence.length },
      })
      setRequestingChanges(false)
      setRequestChangesComment('')
      if (isClient) {
        navigate('/dashboard/inicio', { replace: true })
        return
      }
      await loadTicket()
    } catch (err) {
      if (evidenceSaved) {
        await loadTicket()
      }
      await swalError(err)
    } finally {
      setSavingEvidence(false)
    }
  }

  const persistQAEvidence = async () => {
    const prepared = await filesUploadRef.current?.prepareSave?.() || {
      urls: qaEvidence,
      uploadedKeys: [],
      removedKeys: [],
    }

    try {
      await client.patch(`/api/tasks/${id}`, {
        qa_evidence: prepared.urls,
        params: { review_links: qaReviewLinks },
      })
    } catch (err) {
      try {
        await filesUploadRef.current?.rollbackPreparedSave?.(prepared.uploadedKeys)
      } catch { /* silent */ }
      throw err
    }

    setQaEvidence(prepared.urls)
    await filesUploadRef.current?.finalizePreparedSave?.(prepared)
    return prepared.urls
  }

  const persistClientEvidence = async () => {
    const prepared = await clientFilesUploadRef.current?.prepareSave?.() || {
      urls: clientEvidence,
      uploadedKeys: [],
      removedKeys: [],
    }

    try {
      await client.patch(`/api/tasks/${id}`, {
        client_evidence: prepared.urls,
      })
    } catch (err) {
      try {
        await clientFilesUploadRef.current?.rollbackPreparedSave?.(prepared.uploadedKeys)
      } catch { /* silent */ }
      throw err
    }

    setClientEvidence(prepared.urls)
    await clientFilesUploadRef.current?.finalizePreparedSave?.(prepared)
    return prepared.urls
  }

  const handleSaveQAEvidence = async () => {
    setSavingEvidence(true)
    try {
      await persistQAEvidence()
      await loadTicket()
    } catch (err) {
      await swalError(err)
    } finally {
      setSavingEvidence(false)
    }
  }

  const handleSaveAndApprove = async () => {
    const confirmed = await swalConfirm({
      title: '¿Guardar y aprobar?',
      text: 'Esta acción guarda la evidencia y entrega el ticket a la siguiente fase.',
      icon: 'warning',
      confirmText: 'Sí, guardar y aprobar',
      cancelText: 'Cancelar',
    })
    if (!confirmed) return

    setSavingEvidence(true)
    let evidenceSaved = false
    try {
      await persistQAEvidence()
      evidenceSaved = true
      await client.post(`/api/tasks/${id}/phase-action`, {
        action: 'approve',
        comment: 'Aprobado desde el panel web',
      })
      await loadTicket()
    } catch (err) {
      if (evidenceSaved) {
        await loadTicket()
      }
      await swalError(err)
    } finally {
      setSavingEvidence(false)
    }
  }

  const handleSaveClientEvidence = async () => {
    setSavingEvidence(true)
    try {
      await persistClientEvidence()
      await loadTicket()
    } catch (err) {
      await swalError(err)
    } finally {
      setSavingEvidence(false)
    }
  }

  const handleSaveClientAndApprove = async () => {
    const confirmed = await swalConfirm({
      title: '¿Guardar y aprobar?',
      text: 'Esta acción guarda tu evidencia y aprueba el ticket.',
      icon: 'warning',
      confirmText: 'Sí, guardar y aprobar',
      cancelText: 'Cancelar',
    })
    if (!confirmed) return

    setSavingEvidence(true)
    let evidenceSaved = false
    try {
      await persistClientEvidence()
      evidenceSaved = true
      await client.post(`/api/tasks/${id}/phase-action`, {
        action: 'approve',
        comment: 'Aprobado por el cliente desde el detalle del ticket',
      })
      await loadTicket()
    } catch (err) {
      if (evidenceSaved) {
        await loadTicket()
      }
      await swalError(err)
    } finally {
      setSavingEvidence(false)
    }
  }

  const handleRevertApproval = async () => {
    const confirmed = await swalConfirm({
      title: '¿Revertir aprobación?',
      text: 'Esto devolverá el ticket a QA para seguir revisándolo y quitará la entrega al cliente.',
      icon: 'warning',
      confirmText: 'Sí, revertir',
      cancelText: 'Cancelar',
    })
    if (!confirmed) return

    try {
      await client.post(`/api/tasks/${id}/phase-revert`, {
        comment: 'Aprobación revertida desde QA por envío accidental al cliente',
      })
      loadTicket()
    } catch (err) {
      await swalError(err)
    }
  }

  const handleAddLink = () => {
    const trimmed = newLink.trim()
    if (!trimmed) return
    if (qaReviewLinks.includes(trimmed)) {
      setNewLink('')
      return
    }
    setQaReviewLinks([...qaReviewLinks, trimmed])
    setNewLink('')
  }

  const handleRemoveLink = async (index) => {
    const nextLinks = qaReviewLinks.filter((_, i) => i !== index)
    setQaReviewLinks(nextLinks)
    try {
      await client.patch(`/api/tasks/${id}`, { params: { review_links: nextLinks } })
    } catch { /* best effort */ }
  }

  const formatDate = (dateStr) => {
    if (!dateStr) return '—'
    return new Date(dateStr).toLocaleDateString('es-MX', {
      day: '2-digit', month: 'long', year: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  }

  const formatDateShort = (dateStr) => {
    if (!dateStr) return '—'
    return new Date(dateStr).toLocaleDateString('es-MX', {
      day: '2-digit', month: 'short', year: 'numeric',
    })
  }

  const MediaGrid = ({ urls, title, icon: Icon }) => {
    if (!urls?.length) return null
    return (
      <div className="detail-section card-oceo">
        <h2 className="section-title">{Icon && <Icon className="w-4 h-4" />} {title}</h2>
        <div className="evidence-grid">
          {urls.map((url, idx) => (
            <a key={idx} href={url} target="_blank" rel="noopener noreferrer" className="evidence-item">
              {url.match(/\.(jpg|jpeg|png|gif|webp)$/i) ? (
                <img src={url} alt={`${title} ${idx + 1}`} className="evidence-image" />
              ) : url.match(/\.(mp4|webm|mov)$/i) ? (
                <div className="evidence-placeholder"><VideoCameraIcon className="w-8 h-8" style={{ color: 'var(--oceo-text-3)' }} /></div>
              ) : (
                <div className="evidence-placeholder"><LinkIcon className="w-8 h-8" style={{ color: 'var(--oceo-text-3)' }} /></div>
              )}
            </a>
          ))}
        </div>
      </div>
    )
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="animate-spin rounded-full h-10 w-10 border-2 border-oceo-blue-400 border-t-transparent" />
      </div>
    )
  }

  if (!ticket) {
    return (
      <div className="empty-state-page">
        <p>Ticket no encontrado</p>
        <Link to={backHref} className="action-link">{backLabel}</Link>
      </div>
    )
  }

  const pmTicket = ticket.params?.pm_ticket || {}
  const devEvidence = ticket.params?.evidence || []
  const attachments = ticket.attachments || []
  const rawReviewLinks = Array.isArray(ticket.params?.review_links)
    ? ticket.params.review_links.filter(Boolean)
    : (typeof ticket.params?.review_links === 'string' && ticket.params.review_links.trim()
      ? [ticket.params.review_links.trim()]
      : [])
  const fallbackReviewLinks = [
    ticket.params?.test_url,
    ticket.params?.client_review_url,
    ticket.params?.review_url,
    ticket.params?.external_url,
    ticket.client_review_url,
    ticket.review_url,
    ticket.external_url,
    pmTicket.external_url,
  ].filter(Boolean)
  const reviewLinks = rawReviewLinks.length > 0
    ? [...new Set(rawReviewLinks)]
    : [...new Set(fallbackReviewLinks)]
  const acceptanceCriteria = pmTicket.acceptance_criteria || []
  const clientDesc = ticket.client_description || pmTicket.description || ticket.description || ''

  // Resolve required permission for current workflow phase
  const currentPhase = (ticket.workflow_phases || []).find(p => p.key === ticket.phase)
  const phasePermission = currentPhase?.permission || null
  const canClientActOnPhase = isClient
    && ticket.client_visible === true
    && ticket.status === 'waiting'
    && ticket.phase === 'client_signoff'
    && store.checkPermissions(['client_review'])
  const canActOnPhase = isClient
    ? canClientActOnPhase
    : (ticket.status === 'waiting' && phasePermission && store.checkPermissions([phasePermission]))

  // ─── CLIENT VIEW ──────────────────────────────────────────────
  if (isClient) {
    return (
      <div className="ticket-detail-page client-view" ref={containerRef}>
        {/* Header */}
        <div className="detail-section detail-header">
          <Link to={backHref} className="btn-icon">
            <ArrowLeftIcon className="w-5 h-5" />
          </Link>
          <div className="detail-header-content">
            <h1 className="detail-title">{ticket.title}</h1>
            <div className="detail-badges">
              {ticket.ticket_number != null && (
                <span className="status-badge badge-neutral">#{ticket.ticket_number}</span>
              )}
              <span className={`status-badge ${statusColors[ticket.status] || 'badge-active'}`}>
                {statusLabels[ticket.status] || ticket.status}
              </span>
              {ticket.approval_required && (
                <span className={`status-badge ${approvalColors[ticket.approval_state] || 'badge-active'}`}>
                  {approvalLabels[ticket.approval_state] || ticket.approval_state}
                </span>
              )}
              <span className="project-badge">
                <TagIcon className="w-3 h-3" />
                {ticket.project || 'Sin proyecto'}
              </span>
            </div>
          </div>
        </div>

        {/* Client Description */}
        {clientDesc && (
          <div className="detail-section card-oceo">
            <h2 className="section-title">
              <DocumentTextIcon className="w-4 h-4" />
              Descripción
            </h2>
            <p className="description-text">{clientDesc}</p>
          </div>
        )}

        {/* Acceptance Criteria */}
        {acceptanceCriteria.length > 0 && (
          <div className="detail-section card-oceo">
            <h2 className="section-title">
              <ListBulletIcon className="w-4 h-4" />
              Criterios de aceptación
            </h2>
            <ul className="acceptance-list">
              {acceptanceCriteria.map((item, idx) => (
                <li key={idx} className="acceptance-item">
                  <CheckCircleIcon className="w-4 h-4 acceptance-check" />
                  <span>{item}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Info Grid */}
        <div className="detail-section info-grid client-info-grid">
          <div className="card-oceo info-card">
            <h2 className="section-title">
              <CalendarIcon className="w-4 h-4" />
              Información
            </h2>
            <dl className="info-list">
              {pmTicket.priority && (
                <div className="info-row">
                  <dt className="info-label">Prioridad</dt>
                  <dd className="info-value capitalize">{pmTicket.priority}</dd>
                </div>
              )}
              {pmTicket.type && (
                <div className="info-row">
                  <dt className="info-label">Tipo</dt>
                  <dd className="info-value">{pmTicket.type}</dd>
                </div>
              )}
              {pmTicket.module && (
                <div className="info-row">
                  <dt className="info-label">Módulo</dt>
                  <dd className="info-value">{pmTicket.module}</dd>
                </div>
              )}
            </dl>
          </div>

          {reviewLinks.length > 0 && (
            <div className="card-oceo info-card">
              <h2 className="section-title">
                <LinkIcon className="w-4 h-4" />
                {reviewLinks.length > 1 ? 'Links de revisión' : 'URL de revisión'}
              </h2>
              <div className="flex flex-col gap-2">
                {reviewLinks.map((url, idx) => (
                  <a key={`${url}-${idx}`} href={url} target="_blank" rel="noopener noreferrer" className="test-url-link">
                    {url}
                  </a>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* QA evidence */}
        <MediaGrid urls={ticket.qa_evidence || []} title="Evidencia" icon={PhotoIcon} />

        {/* Client evidence */}
        <MediaGrid urls={clientEvidence} title="Mis observaciones" icon={PhotoIcon} />

        {/* Dev evidence */}
        <MediaGrid urls={devEvidence} title="Resultado del desarrollo" icon={PhotoIcon} />

        {/* Attachments */}
        <MediaGrid urls={attachments} title="Archivos adjuntos" icon={LinkIcon} />

        {canActOnPhase && requestingChanges && (
          <div className="detail-section card-oceo">
            <h2 className="section-title">
              <PhotoIcon className="w-4 h-4" />
              Evidencia del cliente
            </h2>
            <p className="text-sm text-oceo-text-3 mb-3">Adjunta fotos o videos para ilustrar los cambios necesarios.</p>
            <FilesUpload
              ref={clientFilesUploadRef}
              value={clientEvidence}
              onChange={setClientEvidence}
              accept="image/*,video/*"
              maxFiles={10}
              maxSizeMB={50}
              disabled={savingEvidence}
            />
          </div>
        )}

        {canActOnPhase && requestingChanges && (
          <div className="detail-section card-oceo">
            <h2 className="section-title">
              <PencilIcon className="w-4 h-4" />
              Observaciones
            </h2>
            <textarea
              value={requestChangesComment}
              onChange={(e) => setRequestChangesComment(e.target.value)}
              placeholder="Describe los cambios necesarios..."
              className="input-oceo w-full min-h-[120px] resize-y"
              disabled={savingEvidence}
            />
          </div>
        )}

        {canActOnPhase && (
          <div className="detail-section action-bar flex justify-end gap-3">
            {requestingChanges ? (
              <>
                <button onClick={handleCancelRequestChanges} disabled={savingEvidence} className="btn-secondary">
                  <XMarkIcon className="w-5 h-5" />
                  Cancelar
                </button>
                <button onClick={handleSubmitRequestChanges} disabled={savingEvidence} className="btn-primary">
                  <XCircleIcon className="w-5 h-5" />
                  {savingEvidence ? 'Enviando…' : 'Enviar solicitud'}
                </button>
              </>
            ) : (
              <>
                <button onClick={handlePhaseRequestChanges} disabled={savingEvidence} className="btn-danger">
                  <XCircleIcon className="w-5 h-5" />
                  Solicitar cambios
                </button>
                <button onClick={handleApprove} disabled={savingEvidence} className="btn-success">
                  <CheckCircleIcon className="w-5 h-5" />
                  Aprobar
                </button>
              </>
            )}
          </div>
        )}

      </div>
    )
  }

  // ─── ADMIN / QA / EMPLOYEE VIEW ───────────────────────────────
  return (
    <div className="ticket-detail-page admin-view" ref={containerRef}>
      {/* Header */}
      <div className="detail-section detail-header">
        <Link to={backHref} className="btn-icon">
          <ArrowLeftIcon className="w-5 h-5" />
        </Link>
        <div className="detail-header-content">
          <div className="detail-header-row">
            <h1 className="detail-title">{ticket.title}</h1>
            <div className="flex items-center gap-2 flex-wrap">
              {ticket.status === 'waiting' && ticket.phase === 'qa_review' && store.checkPermissions(['qa_review']) && (
                <>
                  {sessionUrls.kimi ? (
                    <a href={sessionUrls.kimi} target="_blank" rel="noopener noreferrer" className="btn-secondary inline-flex items-center gap-1.5">
                      <ArrowTopRightOnSquareIcon className="w-4 h-4" /> Open Kimi
                    </a>
                  ) : (
                    <button onClick={() => handleOpenSession('kimi')} disabled={openingSession} className="btn-secondary">
                      <ArrowTopRightOnSquareIcon className="w-4 h-4" />
                      {openingSession ? 'Creating…' : 'Create Kimi'}
                    </button>
                  )}
                  {sessionUrls.opencode ? (
                    <a href={sessionUrls.opencode} target="_blank" rel="noopener noreferrer" className="btn-secondary inline-flex items-center gap-1.5">
                      <ArrowTopRightOnSquareIcon className="w-4 h-4" /> Open OpenCode
                    </a>
                  ) : (
                    <button onClick={() => handleOpenSession('opencode')} disabled={openingSession} className="btn-secondary">
                      <ArrowTopRightOnSquareIcon className="w-4 h-4" />
                      {openingSession ? 'Creating…' : 'Create OpenCode'}
                    </button>
                  )}
                </>
              )}
            </div>
          </div>
          <div className="detail-badges">
            {ticket.ticket_number != null && (
              <span className="status-badge badge-neutral">#{ticket.ticket_number}</span>
            )}
            {ticket.params?.pm_ticket?.module && (
              <span className="status-badge badge-warning">{ticket.params.pm_ticket.module}</span>
            )}
            <span className={`status-badge ${statusColors[ticket.status] || 'badge-active'}`}>
              {statusLabels[ticket.status] || ticket.status}
            </span>
            {ticket.coding_tier && (
              <span className={`status-badge ${tierColors[ticket.coding_tier] || 'badge-neutral'}`}>
                {tierLabels[ticket.coding_tier] || ticket.coding_tier}
              </span>
            )}
            {ticket.coding_tool && !ticket.coding_tier && (
              <span className="status-badge badge-neutral">{ticket.coding_tool}</span>
            )}
            {ticket.approval_required && (
              <span className={`status-badge ${approvalColors[ticket.approval_state] || 'badge-active'}`}>
                {approvalLabels[ticket.approval_state] || ticket.approval_state}
              </span>
            )}
            <span className="project-badge">
              <TagIcon className="w-3 h-3" />
              {ticket.project || 'Sin proyecto'}
            </span>
            {ticket.involved_user_ids?.length > 0 && (
              <span className="project-badge">
                <UserIcon className="w-3 h-3" />
                {ticket.involved_user_ids.slice(0, 2).map(uid => usersMap[uid] || uid).join(', ')}
                {ticket.involved_user_ids.length > 2 && ` +${ticket.involved_user_ids.length - 2}`}
              </span>
            )}
            {ticket.phase && (
              <span className="status-badge badge-info">{ticket.phase}</span>
            )}
          </div>
        </div>
      </div>

      {/* Description — full width */}
      <div className="detail-section card-oceo">
        <h2 className="section-title">
          <DocumentTextIcon className="w-4 h-4" />
          Descripción
        </h2>
        <p className="description-text">{ticket.description || 'Sin descripción'}</p>
      </div>

      {/* Acceptance Criteria — full width */}
      {acceptanceCriteria.length > 0 && (
        <div className="detail-section card-oceo">
          <h2 className="section-title">
            <ListBulletIcon className="w-4 h-4" />
            Criterios de aceptación
          </h2>
          <ul className="acceptance-list">
            {acceptanceCriteria.map((item, idx) => (
              <li key={idx} className="acceptance-item">
                <CheckCircleIcon className="w-4 h-4 acceptance-check" />
                <span>{item}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* PM Info + Technical Info — 2 columns */}
      <div className="detail-section info-grid">
        {Object.keys(pmTicket).length > 0 && (
          <div className="card-oceo info-card">
            <h2 className="section-title">
              <RocketLaunchIcon className="w-4 h-4" />
              Requerimiento
            </h2>
            <div className="flex flex-wrap items-center gap-2">
              {pmTicket.module && (
                <span className="status-badge badge-warning">{pmTicket.module}</span>
              )}
              {pmTicket.type && (
                <span className="status-badge badge-info">{pmTicket.type}</span>
              )}
              {pmTicket.priority && (
                <span className={`status-badge ${
                  pmTicket.priority === 'critical' ? 'badge-danger' :
                  pmTicket.priority === 'high' ? 'badge-warning' :
                  pmTicket.priority === 'low' ? 'badge-neutral' :
                  'badge-info'
                }`}>{pmTicket.priority}</span>
              )}
              {pmTicket.client_review !== undefined && (
                <span className={`status-badge ${pmTicket.client_review ? 'badge-success' : 'badge-neutral'}`}>
                  {pmTicket.client_review ? 'Revisión cliente' : 'Sin revisión cliente'}
                </span>
              )}
            </div>
          </div>
        )}

        {!(isQA && !isAdmin) && (
          <div className="card-oceo info-card">
            <h2 className="section-title">
              <CpuChipIcon className="w-4 h-4" />
              Configuración técnica
            </h2>
            <dl className="info-list">
              <div className="info-row">
                <dt className="info-label"><TagIcon className="w-4 h-4" /> Prioridad</dt>
                <dd className="info-value capitalize">{ticket.priority || 'media'}</dd>
              </div>
              <div className="info-row">
                <dt className="info-label"><UserIcon className="w-4 h-4" /> Agente</dt>
                <dd className="info-value capitalize">{ticket.agent_hint || ticket.agent || 'ceo'}</dd>
              </div>
              {ticket.coding_tier && (
                <div className="info-row">
                  <dt className="info-label"><CogIcon className="w-4 h-4" /> Tier</dt>
                  <dd className="info-value">
                    <span className={`status-badge ${tierColors[ticket.coding_tier] || 'badge-neutral'}`}>
                      {tierLabels[ticket.coding_tier] || ticket.coding_tier}
                    </span>
                  </dd>
                </div>
              )}
              {ticket.coding_tool && (
                <div className="info-row">
                  <dt className="info-label"><WrenchIcon className="w-4 h-4" /> Herramienta</dt>
                  <dd className="info-value">{ticket.coding_tool}</dd>
                </div>
              )}
              {ticket.coding_session_title && (
                <div className="info-row">
                  <dt className="info-label">Sesión CLI</dt>
                  <dd className="info-value">{ticket.coding_session_title}</dd>
                </div>
              )}
              {ticket.coding_session_id && (
                <div className="info-row">
                  <dt className="info-label">Session ID</dt>
                  <dd className="info-value">{ticket.coding_session_id}</dd>
                </div>
              )}
              <div className="info-row">
                <dt className="info-label"><GlobeAltIcon className="w-4 h-4" /> Fuente</dt>
                <dd className="info-value capitalize">{ticket.source || 'api'}</dd>
              </div>
              <div className="info-row">
                <dt className="info-label"><CalendarIcon className="w-4 h-4" /> Creado</dt>
                <dd className="info-value">{formatDate(ticket.created_at)}</dd>
              </div>
              {ticket.started_at && (
                <div className="info-row">
                  <dt className="info-label">Iniciado</dt>
                  <dd className="info-value">{formatDate(ticket.started_at)}</dd>
                </div>
              )}
              {ticket.completed_at && (
                <div className="info-row">
                  <dt className="info-label">Completado</dt>
                  <dd className="info-value">{formatDate(ticket.completed_at)}</dd>
                </div>
              )}
              {ticket.resume_at && (
                <div className="info-row">
                  <dt className="info-label">Reanudar en</dt>
                  <dd className="info-value">{formatDate(ticket.resume_at)}</dd>
                </div>
              )}
            </dl>
          </div>
        )}
      </div>

      {/* Links de prueba */}
      {store.checkPermissions(['qa_review']) ? (
        <div className="detail-section card-oceo">
          <h2 className="section-title"><LinkIcon className="w-4 h-4" /> Links de prueba</h2>
          {qaReviewLinks.length > 0 && (
            <div className="flex flex-col gap-1.5 mb-3">
              {qaReviewLinks.map((link, idx) => (
                <div key={idx} className="flex items-center gap-2 bg-oceo-surface border border-oceo-stroke rounded-lg px-3 py-1.5">
                  <a href={link} target="_blank" rel="noopener noreferrer" className="text-oceo-blue-400 text-sm truncate flex-1 hover:underline">{link}</a>
                  <button onClick={() => handleRemoveLink(idx)} className="btn-icon btn-icon-sm text-oceo-text-3 hover:text-oceo-danger" title="Quitar link" type="button" disabled={savingEvidence}>
                    <XMarkIcon className="w-4 h-4" />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="flex gap-2">
            <input
              type="url"
              value={newLink}
              onChange={(e) => setNewLink(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), handleAddLink())}
              placeholder="URL de revisión"
              className="input-oceo input-sm flex-1"
              disabled={savingEvidence}
            />
            <button onClick={handleAddLink} disabled={savingEvidence || !newLink.trim()} className="btn-secondary btn-sm" type="button">
              <PlusIcon className="w-4 h-4" />
            </button>
          </div>
        </div>
      ) : (
        reviewLinks.length > 0 && (
          <div className="detail-section card-oceo">
            <h2 className="section-title"><LinkIcon className="w-4 h-4" /> {reviewLinks.length > 1 ? 'Links de prueba' : 'URL de prueba'}</h2>
            <div className="flex flex-col gap-2">
              {reviewLinks.map((url, idx) => (
                <a key={`${url}-${idx}`} href={url} target="_blank" rel="noopener noreferrer" className="test-url-link">{url}</a>
              ))}
            </div>
          </div>
        )
      )}

      {/* Dev evidence */}
      <MediaGrid urls={devEvidence} title="Evidencia del desarrollo" icon={PhotoIcon} />

      {/* QA evidence */}
      <MediaGrid urls={ticket.qa_evidence || []} title="Evidencia" icon={PhotoIcon} />

      {/* Client evidence */}
      <MediaGrid urls={clientEvidence} title="Observaciones del cliente" icon={PhotoIcon} />

      {/* Attachments */}
      <MediaGrid urls={attachments} title="Archivos adjuntos" icon={LinkIcon} />

      {/* QA Evidence Upload */}
      {store.checkPermissions(['qa_review']) && (
        <div className="detail-section card-oceo">
          <h2 className="section-title">
            <PhotoIcon className="w-4 h-4" />
            Evidencia
          </h2>
          <p className="text-sm text-oceo-text-3 mb-3">Sube fotos o videos de la revisión.</p>
          <FilesUpload
            ref={filesUploadRef}
            value={qaEvidence}
            onChange={setQaEvidence}
            accept="image/*,video/*"
            maxFiles={10}
            maxSizeMB={50}
            disabled={savingEvidence}
          />
        </div>
      )}

      {/* Execution Steps — full width */}
      {ticket.steps?.length > 0 && !(isQA && !isAdmin) && (
        <div className="detail-section card-oceo">
          <h2 className="section-title"><CogIcon className="w-4 h-4" /> Pasos de ejecución</h2>
          <div className="steps-list">
            {ticket.steps.map((step, idx) => (
              <div key={idx} className={`step-item step-${step.status}`}>
                <div className="step-header">
                  <span className={`status-badge ${
                    step.status === 'ok' ? 'badge-success' :
                    step.status === 'failed' ? 'badge-danger' :
                    step.status === 'skipped' ? 'badge-neutral' :
                    'badge-warning'
                  }`}>{step.status}</span>
                  <span className="step-name">{step.step}</span>
                </div>
                {step.detail && <p className="step-detail">{step.detail}</p>}
                {step.timestamp && <span className="step-time">{formatDateShort(step.timestamp)}</span>}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Error — full width */}
      {ticket.error && !(isQA && !isAdmin) && (
        <div className="detail-section card-oceo error-card">
          <h2 className="section-title"><ExclamationTriangleIcon className="w-4 h-4" /> Error</h2>
          <pre className="error-pre">{ticket.error}</pre>
        </div>
      )}

      {/* Last Response Summary — full width */}
      {ticket.last_response_summary && !isQA && !ticket.last_response_summary.toLowerCase().includes('manual override') && (
        <div className="detail-section card-oceo">
          <h2 className="section-title"><DocumentTextIcon className="w-4 h-4" /> Último resumen</h2>
          <p className="description-text">{ticket.last_response_summary}</p>
        </div>
      )}

      {/* Phase approvals & changes — full width */}
      {reviews.length > 0 && !isQA && (
        <div className="detail-section card-oceo">
          <h2 className="section-title"><ClockIcon className="w-4 h-4" /> Aprobaciones y cambios</h2>
          <div className="reviews-list">
            {reviews.map((review) => (
              <div key={review.review_id} className="review-item">
                <div className="review-header">
                  <span className={`status-badge ${
                    review.action === 'approve' ? 'badge-success' :
                    review.action === 'reject' ? 'badge-danger' :
                    review.action === 'request_changes' ? 'badge-warning' :
                    'badge-active'
                  }`}>
                    {review.action === 'approve' ? 'Aprobado' :
                     review.action === 'reject' ? 'Rechazado' :
                     review.action === 'request_changes' ? 'Cambios solicitados' :
                     'Comentario'}
                  </span>
                  <span className="review-meta">por {review.actor_id} · {formatDate(review.created_at)}</span>
                </div>
                {review.comment && <p className="review-comment">{review.comment}</p>}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Ticket activity history — full width */}
      {ticket.history?.length > 0 && !(isQA && !isAdmin) && (
        <div className="detail-section card-oceo">
          <h2 className="section-title"><ClockIcon className="w-4 h-4" /> Historial de actividades</h2>
          <div className="reviews-list">
            {[...ticket.history].reverse().map((entry, idx) => (
              <div key={idx} className="review-item">
                <div className="review-header">
                  <span className="status-badge badge-active">{entry.action}</span>
                  <span className="review-meta">por {entry.actor_id} · {formatDate(entry.timestamp)}</span>
                </div>
                {entry.detail && <p className="review-comment">{entry.detail}</p>}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Action bar */}
      {(store.checkPermissions(['qa_review']) || canActOnPhase) && (
        <div className="detail-section flex justify-end gap-3">
          {store.checkPermissions(['qa_review']) && (
            <button onClick={handleSaveQAEvidence} disabled={savingEvidence} className="btn-secondary">
              <CheckCircleIcon className="w-5 h-5" />
              {savingEvidence ? 'Guardando…' : 'Guardar'}
            </button>
          )}
          {canActOnPhase && store.checkPermissions(['qa_review']) && (
            <button onClick={handleSaveAndApprove} disabled={savingEvidence} className="btn-success">
              <CheckCircleIcon className="w-5 h-5" />
              {savingEvidence ? 'Guardando…' : 'Guardar y Aprobar'}
            </button>
          )}
          {canActOnPhase && !store.checkPermissions(['qa_review']) && (
            <button onClick={handleApprove} className="btn-success">
              <CheckCircleIcon className="w-4 h-4" />
              Aprobar
            </button>
          )}
          {store.checkPermissions(['qa_review']) && ticket?.status === 'waiting' && ticket?.phase === 'client_signoff' && (
            <button onClick={handleRevertApproval} className="btn-secondary">
              <CheckCircleIcon className="w-5 h-5" />
              Revertir aprobación
            </button>
          )}
        </div>
      )}
    </div>
  )
}
