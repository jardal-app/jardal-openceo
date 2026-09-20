import { useContext, useEffect, useState, useCallback } from 'react'
import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import Swal from 'sweetalert2'
import {
  SparklesIcon,
  LightBulbIcon,
  CalendarDaysIcon,
  FlagIcon,
  BoltIcon,
  ArrowPathIcon,
  ArrowTrendingUpIcon,
  CheckBadgeIcon,
  ClipboardDocumentListIcon,
  ExclamationTriangleIcon,
  LinkIcon,
  PaperAirplaneIcon,
  LockClosedIcon,
} from '@heroicons/react/24/outline'
import client from '@/api'
import { StoreContext } from '@/context/store'

const numberFormat = new Intl.NumberFormat('es-MX')

// Horizon distingue "desconocido" de "cero" (seccion 9.3): un dato no medido se
// muestra como desconocido, nunca como 0, porque cero es una medida real.
function minutes(value) {
  if (value === null || value === undefined || value === '') return 'desconocido'
  return `${numberFormat.format(value)} min`
}

function plain(value) {
  if (value === null || value === undefined || value === '') return 'desconocido'
  return String(value)
}

function formatDate(dateStr) {
  if (!dateStr) return '—'
  return new Date(dateStr).toLocaleDateString('es-MX', {
    day: '2-digit', month: 'long', year: 'numeric',
  })
}

function formatTime(dateStr) {
  if (!dateStr) return ''
  return new Date(dateStr).toLocaleTimeString('es-MX', { hour: '2-digit', minute: '2-digit' })
}

const planStatus = {
  ready: { label: 'Con plan', badge: 'badge-success' },
  empty: { label: 'Día saturado', badge: 'badge-warning' },
  needs_input: { label: 'Falta un dato', badge: 'badge-info' },
}

const blockKind = {
  scheduled: 'Con horario',
  on_demand: 'A demanda',
  reserved_exercise: 'Ejercicio',
  reserved_rest: 'Descanso',
}

const uncertaintyLabel = {
  verified: 'verificado',
  declared: 'declarado',
  estimated: 'estimado',
  unknown: 'desconocido',
}

const actionStatus = {
  ready: 'Lista',
  in_progress: 'En curso',
  waiting: 'En espera',
  done: 'Hecha',
  blocked: 'Bloqueada',
  parked: 'Aparcada',
}

export default function PersonalSpace() {
  const store = useContext(StoreContext)
  const [loading, setLoading] = useState(true)
  const [summary, setSummary] = useState(null)
  const [plan, setPlan] = useState(null)
  const [next, setNext] = useState(null)
  const [delegation, setDelegation] = useState(null)
  const [delivery, setDelivery] = useState(null)
  const [failed, setFailed] = useState(false)
  const [deliveryLoading, setDeliveryLoading] = useState(false)
  const [freeMinutes, setFreeMinutes] = useState('')
  const [exerciseIncluded, setExerciseIncluded] = useState(false)
  const [captureText, setCaptureText] = useState('')
  const [progressText, setProgressText] = useState('')
  const [busy, setBusy] = useState('')

  const prefersReducedMotion = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  useGSAP(() => {
    if (prefersReducedMotion || loading) return
    const ctx = gsap.context(() => {
      gsap.from('.horizon-card', { y: 18, opacity: 0, stagger: 0.08, duration: 0.45, ease: 'power2.out' })
      gsap.from('.section-header', { y: -10, opacity: 0, duration: 0.35, ease: 'power2.out' })
    })
    return () => ctx.revert()
  }, [loading, prefersReducedMotion])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [summaryRes, planRes, nextRes, delegationRes] = await Promise.all([
        client.get('/api/horizon/plan'),
        client.get('/api/horizon/plan/today'),
        client.get('/api/horizon/next-action'),
        client.get('/api/horizon/delegation/summary'),
      ])
      setSummary(summaryRes.data)
      setPlan(planRes.data)
      setNext(nextRes.data)
      setDelegation(delegationRes.data)
      setFailed(false)
    } catch (error) {
      setFailed(true)
      store.showErrors([error?.response?.data?.detail || 'No se pudo cargar el espacio personal.'])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const replanDay = async (event) => {
    event.preventDefault()
    const parsed = freeMinutes === '' ? null : Number(freeMinutes)
    if (parsed !== null && (!Number.isFinite(parsed) || parsed < 0)) {
      store.showErrors(['Los minutos libres deben ser un número mayor o igual a cero.'])
      return
    }
    setBusy('plan')
    try {
      const response = await client.post('/api/horizon/plan/day', {
        free_minutes: parsed,
        exercise_included: exerciseIncluded,
      })
      setPlan(response.data)
      store.addToast({ title: 'Plan actualizado', message: 'Plan del día recalculado con la capacidad declarada.' })
    } catch (error) {
      store.showErrors([error?.response?.data?.detail || 'No se pudo replanificar el día.'])
    } finally {
      setBusy('')
    }
  }

  const captureIdea = async (event) => {
    event.preventDefault()
    const text = captureText.trim()
    if (!text) return
    setBusy('capture')
    try {
      const response = await client.post('/api/horizon/capture', { text })
      setCaptureText('')
      const data = response.data || {}
      store.addToast({
        title: data.captured ? 'Captura guardada' : 'Ya lo teníamos',
        message: data.message || 'Captura registrada.',
      })
      await load()
    } catch (error) {
      store.showErrors([error?.response?.data?.detail || 'No se pudo registrar la captura.'])
    } finally {
      setBusy('')
    }
  }

  const recordProgress = async (event) => {
    event.preventDefault()
    const text = progressText.trim()
    if (!text) return
    setBusy('progress')
    try {
      const response = await client.post('/api/horizon/progress', { text })
      setProgressText('')
      const data = response.data || {}
      store.addToast({
        title: data.recorded ? 'Avance registrado' : 'Sin cambios',
        message: data.message || 'Avance registrado.',
      })
      await load()
    } catch (error) {
      store.showErrors([error?.response?.data?.detail || 'No se pudo registrar el avance.'])
    } finally {
      setBusy('')
    }
  }

  const delegateAction = async (actionId) => {
    const confirmed = await Swal.fire({
      title: '¿Delegar esta acción?',
      text: 'El agente la ejecutará en segundo plano en el proyecto autorizado y consumirá cuota del proveedor.',
      icon: 'question',
      showCancelButton: true,
      confirmButtonText: 'Delegar',
      cancelButtonText: 'Cancelar',
      confirmButtonColor: '#0F6FD3',
      background: '#1a1a1a',
      color: '#f1f5f9',
    })
    if (!confirmed.isConfirmed) return
    setBusy('delegate')
    try {
      const response = actionId
        ? await client.post(`/api/horizon/actions/${actionId}/delegate`, {})
        : await client.post('/api/horizon/delegate', {})
      store.addToast({
        title: response.data?.delegated ? 'Encargo enviado' : 'No se pudo delegar',
        message: response.data?.message || 'Encargo enviado.',
      })
      await load()
    } catch (error) {
      store.showErrors([error?.response?.data?.detail || 'No se pudo delegar la acción.'])
    } finally {
      setBusy('')
    }
  }

  const fetchDelivery = async () => {
    setDeliveryLoading(true)
    try {
      const response = await client.get('/api/horizon/delivery', { params: { channel: 'api' } })
      setDelivery(response.data)
    } catch (error) {
      store.showErrors([error?.response?.data?.detail || 'No se pudo obtener el resultado.'])
    } finally {
      setDeliveryLoading(false)
    }
  }

  const recordMetrics = async (actionId, supervision, rework) => {
    if (!actionId) {
      store.showErrors(['Primero entrega un resultado para saber a qué acción registrarle las métricas.'])
      return
    }
    setBusy('metrics')
    try {
      const response = await client.post(`/api/horizon/actions/${actionId}/metrics`, {
        human_supervision_minutes: supervision,
        human_rework_minutes: rework,
        rework: rework > 0,
      })
      const data = response.data || {}
      store.addToast({
        title: data.recorded ? 'Métricas registradas' : 'No registrado',
        message: data.message || 'Métricas registradas.',
      })
      await load()
    } catch (error) {
      store.showErrors([error?.response?.data?.detail || 'No se pudieron registrar las métricas.'])
    } finally {
      setBusy('')
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="animate-spin rounded-full h-10 w-10 border-2 border-oceo-blue-400 border-t-transparent" />
      </div>
    )
  }

  if (failed && !summary && !plan) {
    return (
      <div className="personal-space-page">
        <div className="section-header">
          <div>
            <h1 className="dashboard-title">Espacio Personal</h1>
            <p className="dashboard-subtitle">Misión, objetivos y plan del CEO — solo para el propietario</p>
          </div>
        </div>
        <div className="card-oceo empty-state-page">
          <ExclamationTriangleIcon className="w-14 h-14" style={{ color: 'var(--oceo-warning)' }} />
          <h3>No pudimos leer el espacio personal</h3>
          <p className="empty-text">El servidor no respondió con el plan ni con los objetivos.</p>
          <button type="button" className="btn-primary btn-sm" onClick={load}>
            <ArrowPathIcon className="w-4 h-4" />
            Reintentar
          </button>
        </div>
      </div>
    )
  }

  const voice = summary?.voice || {}
  const growth = summary?.growth_goal || null
  const capacity = plan?.capacity || {}
  const freeValue = capacity?.free_minutes?.value
  const status = planStatus[plan?.status] || { label: plain(plan?.status), badge: 'badge-neutral' }
  const blocks = plan?.blocks || []
  const excluded = plan?.excluded || []
  const decisions = plan?.pending_decisions || []
  const activeObjectives = summary?.active_objectives || []
  const parkedObjectives = summary?.parked_objectives || []
  const recentCaptures = summary?.recent_captures || []

  return (
    <div className="personal-space-page">
      <div className="section-header">
        <div>
          <h1 className="dashboard-title">Espacio Personal</h1>
          <p className="dashboard-subtitle">Misión, objetivos y plan del CEO — solo para el propietario</p>
        </div>
        <div className="dashboard-header-actions">
          <span className="project-badge">
            <LockClosedIcon className="w-3 h-3" />
            No se comparte con los empleados
          </span>
          <button type="button" className="btn-secondary btn-sm" onClick={load} disabled={loading}>
            <ArrowPathIcon className="w-4 h-4" />
            Actualizar
          </button>
        </div>
      </div>

      <div className="horizon-card card-oceo horizon-mission">
        <div className="horizon-card-head">
          <h2 className="horizon-card-title">
            <SparklesIcon className="w-5 h-5" />
            Misión y rumbo
          </h2>
          {summary?.plan_version && (
            <span className="project-badge">Plan v{summary.plan_version}</span>
          )}
        </div>
        <p className="horizon-mission-text">{plain(summary?.mission)}</p>
        {summary?.message && <p className="horizon-note">{summary.message}</p>}

        <div className="horizon-metrics-row">
          <div className="horizon-metric">
            <span className="horizon-metric-value">{activeObjectives.length}</span>
            <span className="horizon-metric-label">Objetivos activos</span>
          </div>
          <div className="horizon-metric">
            <span className="horizon-metric-value">{parkedObjectives.length}</span>
            <span className="horizon-metric-label">Aparcados</span>
          </div>
          <div className="horizon-metric">
            <span className="horizon-metric-value">
              {voice.confirmed && voice.usable_minutes !== null && voice.usable_minutes !== undefined
                ? numberFormat.format(voice.usable_minutes)
                : '—'}
            </span>
            <span className="horizon-metric-label">Minutos de voz útiles</span>
          </div>
        </div>

        {growth ? (
          <div className="horizon-growth">
            <span className="status-badge badge-info">
              <FlagIcon className="w-3 h-3" />
              Objetivo de crecimiento
            </span>
            <strong>{growth.title}</strong>
            {growth.measure && <span className="horizon-note">{growth.measure}</span>}
          </div>
        ) : (
          <p className="horizon-note">Todavía no hay un objetivo de crecimiento activo.</p>
        )}

        <div className="horizon-voice">
          {voice.confirmed ? (
            <p className="horizon-note">
              Voz: {minutes(voice.usable_minutes)} de {minutes(voice.target_minutes)} útiles; quedan {minutes(voice.remaining_minutes)}.
              {voice.blocker ? ` Bloqueo: ${voice.blocker}.` : ''}
            </p>
          ) : (
            <p className="horizon-note">
              De la voz todavía no tenemos minutos útiles medidos, así que no proyectamos fecha.
            </p>
          )}
        </div>
      </div>

      <div className="horizon-grid">
        <div className="horizon-card card-oceo">
          <div className="horizon-card-head">
            <h2 className="horizon-card-title">
              <CalendarDaysIcon className="w-5 h-5" />
              Plan de hoy
            </h2>
            <span className={`status-badge ${status.badge}`}>{status.label}</span>
          </div>

          <div className="horizon-metrics-row">
            <div className="horizon-metric">
              <span className="horizon-metric-value">{minutes(freeValue)}</span>
              <span className="horizon-metric-label">Tiempo libre declarado</span>
            </div>
            <div className="horizon-metric">
              <span className="horizon-metric-value">{minutes(capacity.billable_minutes)}</span>
              <span className="horizon-metric-label">Facturable</span>
            </div>
            <div className="horizon-metric">
              <span className="horizon-metric-value">{minutes(capacity.growth_budget_minutes)}</span>
              <span className="horizon-metric-label">Presupuesto de crecimiento</span>
            </div>
            <div className="horizon-metric">
              <span className="horizon-metric-value">{minutes(capacity.scheduled_minutes)}</span>
              <span className="horizon-metric-label">Programado</span>
            </div>
          </div>

          <p className="horizon-note">
            Certeza del dato: {uncertaintyLabel[capacity.uncertainty] || 'desconocido'} ·
            fracción aplicada {capacity.fraction != null ? `${(capacity.fraction * 100).toFixed(0)}%` : 'desconocida'} ·
            ejercicio reservado {minutes(capacity.exercise_reserved_minutes)}
            {capacity.exercise_included === false ? ' (no incluido hoy)' : ''}
          </p>

          {plan?.message && <p className="horizon-plan-message">{plan.message}</p>}
          {plan?.reason && <p className="horizon-note">Motivo: {plan.reason}</p>}

          <form className="horizon-inline-form" onSubmit={replanDay}>
            <label className="filter-label" htmlFor="horizon-free">
              Disponibilidad de hoy
            </label>
            <input
              id="horizon-free"
              className="input-oceo input-sm"
              type="number"
              min="0"
              step="5"
              placeholder="minutos libres"
              value={freeMinutes}
              onChange={(event) => setFreeMinutes(event.target.value)}
            />
            <label className="checkbox-label">
              <input
                type="checkbox"
                className="checkbox-oceo"
                checked={exerciseIncluded}
                onChange={(event) => setExerciseIncluded(event.target.checked)}
              />
              Incluir ejercicio
            </label>
            <button type="submit" className="btn-primary btn-sm" disabled={busy === 'plan'}>
              <ArrowPathIcon className="w-4 h-4" />
              {busy === 'plan' ? 'Recalculando…' : 'Replanificar'}
            </button>
          </form>

          {blocks.length === 0 ? (
            <p className="horizon-note">No hay bloques programados para hoy.</p>
          ) : (
            <ul className="horizon-block-list">
              {blocks.map((block) => (
                <li key={block.block_id} className="horizon-block">
                  <div>
                    <span className="horizon-block-label">{block.label || blockKind[block.kind] || 'Bloque'}</span>
                    <span className="horizon-block-meta">
                      {blockKind[block.kind] || block.kind}
                      {block.start_at ? ` · ${formatTime(block.start_at)}` : ''}
                      {block.end_at ? `–${formatTime(block.end_at)}` : ''}
                      {block.is_growth ? '' : ' · no crecimiento'}
                    </span>
                  </div>
                  <span className="status-badge badge-neutral">{minutes(block.minutes)}</span>
                </li>
              ))}
            </ul>
          )}

          {excluded.length > 0 && (
            <div className="horizon-subsection">
              <h3 className="horizon-subtitle">Qué quedó fuera y por qué</h3>
              <ul className="horizon-plain-list">
                {excluded.map((item) => (
                  <li key={item.action_id}>
                    <strong>{item.title || 'Acción'}</strong> — {item.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {decisions.length > 0 && (
            <div className="horizon-subsection">
              <h3 className="horizon-subtitle">Decisiones pendientes</h3>
              <ul className="horizon-plain-list">
                {decisions.map((decision) => (
                  <li key={decision.decision_id}>
                    <strong>{decision.recommendation}</strong>
                    {decision.consequence ? ` — ${decision.consequence}` : ''}
                    {decision.requires_authorization && (
                      <span className="status-badge badge-warning" style={{ marginLeft: 8 }}>
                        <ExclamationTriangleIcon className="w-3 h-3" />
                        Requiere tu autorización
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        <div className="horizon-card card-oceo">
          <div className="horizon-card-head">
            <h2 className="horizon-card-title">
              <BoltIcon className="w-5 h-5" />
              Siguiente acción
            </h2>
            {next?.limits && (
              <span className="project-badge">
                {next.ready_count}/{next.limits.ready_max} listas · {next.in_progress_count}/{next.limits.in_progress_max} en curso
              </span>
            )}
          </div>

          {next?.has_action ? (
            <>
              <h3 className="horizon-action-title">{next.action?.title}</h3>
              <div className="horizon-meta-row">
                <span className="status-badge badge-active">{actionStatus[next.action?.status] || plain(next.action?.status)}</span>
                <span className="status-badge badge-neutral">{minutes(next.action?.effort_estimated_minutes)}</span>
                {next.objective?.title && <span className="project-badge">{next.objective.title}</span>}
              </div>
              {next.action?.next_step && <p className="horizon-note">Siguiente paso: {next.action.next_step}</p>}
              {next.milestone?.title && <p className="horizon-note">Hito: {next.milestone.title}</p>}
              {next.action?.task_id && (
                <p className="horizon-note">
                  Delegada a <strong>{next.action.delegated_project || 'proyecto autorizado'}</strong> · tarea {next.action.task_id}
                  {next.action.delivered_at ? ` · entregada ${formatDate(next.action.delivered_at)}` : ' · pendiente de entregar'}
                </p>
              )}
              <p className="horizon-plan-message">{next.message}</p>
              <div className="horizon-actions-row">
                <button
                  type="button"
                  className="btn-primary btn-sm"
                  disabled={busy === 'delegate'}
                  onClick={() => delegateAction(next.action?.action_id)}
                >
                  <PaperAirplaneIcon className="w-4 h-4" />
                  {busy === 'delegate' ? 'Delegando…' : 'Delegar a dev'}
                </button>
              </div>
            </>
          ) : (
            <>
              <p className="horizon-plan-message">{next?.message}</p>
              <div className="horizon-actions-row">
                <button
                  type="button"
                  className="btn-secondary btn-sm"
                  disabled={busy === 'delegate'}
                  onClick={() => delegateAction('')}
                >
                  <PaperAirplaneIcon className="w-4 h-4" />
                  Delegar la siguiente
                </button>
              </div>
            </>
          )}

          {next?.secondary?.length > 0 && (
            <div className="horizon-subsection">
              <h3 className="horizon-subtitle">Alternativas preparadas</h3>
              <ul className="horizon-plain-list">
                {next.secondary.map((item) => (
                  <li key={item.action_id}>
                    <strong>{item.title}</strong> — {minutes(item.effort_estimated_minutes)}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <form className="horizon-inline-form" onSubmit={recordProgress}>
            <label className="filter-label" htmlFor="horizon-progress">Anotar avance o hecho</label>
            <input
              id="horizon-progress"
              className="input-oceo input-sm"
              type="text"
              placeholder="ej. terminé el borrador"
              value={progressText}
              onChange={(event) => setProgressText(event.target.value)}
            />
            <button type="submit" className="btn-secondary btn-sm" disabled={busy === 'progress' || !progressText.trim()}>
              <CheckBadgeIcon className="w-4 h-4" />
              {busy === 'progress' ? 'Guardando…' : 'Registrar'}
            </button>
          </form>
        </div>
      </div>

      <div className="horizon-card card-oceo">
        <div className="horizon-card-head">
          <h2 className="horizon-card-title">
            <ClipboardDocumentListIcon className="w-5 h-5" />
            Delegación medida
          </h2>
          <button
            type="button"
            className="btn-secondary btn-sm"
            onClick={fetchDelivery}
            disabled={deliveryLoading}
          >
            <LinkIcon className="w-4 h-4" />
            {deliveryLoading ? 'Buscando…' : 'Ver resultado pendiente'}
          </button>
        </div>

        {delegation ? (
          <>
            <div className="horizon-metrics-row">
              <div className="horizon-metric">
                <span className="horizon-metric-value">{delegation.total_delegated}</span>
                <span className="horizon-metric-label">Delegadas</span>
              </div>
              <div className="horizon-metric">
                <span className="horizon-metric-value">{delegation.completed}</span>
                <span className="horizon-metric-label">Completadas</span>
              </div>
              <div className="horizon-metric">
                <span className="horizon-metric-value">{delegation.in_progress}</span>
                <span className="horizon-metric-label">En curso</span>
              </div>
              <div className="horizon-metric">
                <span className="horizon-metric-value">{delegation.pending_delivery}</span>
                <span className="horizon-metric-label">Sin entregar</span>
              </div>
              <div className="horizon-metric">
                <span className="horizon-metric-value">{minutes(delegation.human_supervision_minutes)}</span>
                <span className="horizon-metric-label">Supervisión humana</span>
              </div>
              <div className="horizon-metric">
                <span className="horizon-metric-value">{minutes(delegation.human_rework_minutes)}</span>
                <span className="horizon-metric-label">Retrabajo humano</span>
              </div>
            </div>
            <p className="horizon-note">{delegation.message}</p>
          </>
        ) : (
          <p className="horizon-note">Sin datos de delegación todavía.</p>
        )}

        {delivery && (
          <div className="horizon-delivery">
            {delivery.delivered ? (
              <>
                <div className="horizon-meta-row">
                  <span className={`status-badge ${delivery.already_delivered ? 'badge-neutral' : 'badge-success'}`}>
                    {delivery.already_delivered ? 'Ya entregada antes' : 'Entregada ahora'}
                  </span>
                  <span className="status-badge badge-info">{plain(delivery.status)}</span>
                  {delivery.delivered_at && (
                    <span className="project-badge">Entregada {formatDate(delivery.delivered_at)}</span>
                  )}
                </div>
                <p className="horizon-plan-message">{delivery.result_summary}</p>
                {(delivery.evidence_links || []).length > 0 && (
                  <ul className="horizon-plain-list">
                    {delivery.evidence_links.map((link) => (
                      <li key={link}>
                        <a className="action-link" href={link} target="_blank" rel="noreferrer">{link}</a>
                      </li>
                    ))}
                  </ul>
                )}
                <div className="horizon-actions-row">
                  <button
                    type="button"
                    className="btn-secondary btn-sm"
                    disabled={busy === 'metrics'}
                    onClick={() => recordMetrics(delivery.action_id, 15, 0)}
                  >
                    +15 min supervisión
                  </button>
                  <button
                    type="button"
                    className="btn-secondary btn-sm"
                    disabled={busy === 'metrics'}
                    onClick={() => recordMetrics(delivery.action_id, 0, 15)}
                  >
                    +15 min retrabajo
                  </button>
                </div>
              </>
            ) : (
              <p className="horizon-note">{delivery.message || 'No hay resultados disponibles.'}</p>
            )}
          </div>
        )}
      </div>

      <div className="horizon-grid">
        <div className="horizon-card card-oceo">
          <div className="horizon-card-head">
            <h2 className="horizon-card-title">
              <FlagIcon className="w-5 h-5" />
              Objetivos e hitos
            </h2>
          </div>
          {activeObjectives.length === 0 ? (
            <p className="horizon-note">Todavía no tenemos objetivos importados.</p>
          ) : (
            <ul className="horizon-objective-list">
              {activeObjectives.map((objective) => (
                <li key={objective.objective_id} className="horizon-objective">
                  <div className="horizon-meta-row">
                    <strong>{objective.title}</strong>
                    <span className="status-badge badge-neutral">{objective.kind}</span>
                    <span className="project-badge">{objective.horizon}</span>
                  </div>
                  {objective.measure && <p className="horizon-note">{objective.measure}</p>}
                  {(objective.milestones || []).length > 0 && (
                    <ul className="horizon-milestone-list">
                      {objective.milestones.map((milestone) => (
                        <li key={milestone.milestone_id}>
                          <span>{milestone.title}</span>
                          <span className="horizon-block-meta">
                            base {plain(milestone.base?.value)} → meta {plain(milestone.target?.value)}
                            {milestone.measure ? ` (${milestone.measure})` : ''}
                            {milestone.blocker ? ` · bloqueo: ${milestone.blocker}` : ''}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
          )}

          {parkedObjectives.length > 0 && (
            <div className="horizon-subsection">
              <h3 className="horizon-subtitle">Aparcados (no son obligación de hoy)</h3>
              <ul className="horizon-plain-list">
                {parkedObjectives.map((objective) => (
                  <li key={objective.objective_id}>
                    <strong>{objective.title}</strong>
                    {objective.parked_review_condition ? ` — revisar: ${objective.parked_review_condition}` : ''}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        <div className="horizon-card card-oceo">
          <div className="horizon-card-head">
            <h2 className="horizon-card-title">
              <LightBulbIcon className="w-5 h-5" />
              Capturar
            </h2>
          </div>
          <form className="horizon-stack-form" onSubmit={captureIdea}>
            <input
              className="input-oceo"
              type="text"
              placeholder="Una idea, un pendiente o un compromiso"
              value={captureText}
              onChange={(event) => setCaptureText(event.target.value)}
            />
            <button type="submit" className="btn-primary btn-sm" disabled={busy === 'capture' || !captureText.trim()}>
              <ArrowTrendingUpIcon className="w-4 h-4" />
              {busy === 'capture' ? 'Guardando…' : 'Capturar'}
            </button>
          </form>
          <p className="horizon-note">
            Se deduplica antes de crear trabajo: repetir una idea no duplica nada ni cambia el rumbo.
          </p>

          {recentCaptures.length > 0 && (
            <div className="horizon-subsection">
              <h3 className="horizon-subtitle">Últimas capturas</h3>
              <ul className="horizon-plain-list">
                {recentCaptures.map((capture) => (
                  <li key={capture.capture_id}>
                    <strong>{capture.text}</strong>
                    <span className="horizon-block-meta">
                      {' '}· {capture.kind} · {formatDate(capture.captured_at)}
                      {capture.status === 'parked' && capture.review_condition ? ` · revisar: ${capture.review_condition}` : ''}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}