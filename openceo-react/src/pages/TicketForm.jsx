import { useContext, useState, useEffect } from 'react'
import { useNavigate, useParams, Link } from 'react-router-dom'
import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import {
  ArrowLeftIcon,
  TicketIcon,
  DocumentTextIcon,
  FolderIcon,
  TagIcon,
  BoltIcon,
  ShieldCheckIcon,
  PlayIcon,
  PauseIcon,
  PencilIcon,
} from '@heroicons/react/24/outline'
import client from '@/api'
import { StoreContext } from '@/context/store'

const SOURCES = [
  { value: 'api', label: 'API' },
  { value: 'jira', label: 'Jira' },
  { value: 'notion', label: 'Notion' },
  { value: 'github', label: 'GitHub' },
  { value: 'telegram', label: 'Telegram' },
  { value: 'voice', label: 'Voz' },
  { value: 'whatsapp', label: 'WhatsApp' },
]

const AGENTS = [
  { value: '', label: '— Ninguno —' },
  { value: 'dev', label: 'Dev' },
  { value: 'pm', label: 'PM' },
  { value: 'finance', label: 'Finance' },
  { value: 'secretary', label: 'Secretary' },
  { value: 'sales', label: 'Sales' },
  { value: 'marketing', label: 'Marketing' },
  { value: 'media', label: 'Media' },
  { value: 'ceo', label: 'CEO' },
]

const PRIORITIES = [
  { value: 'low', label: 'Baja' },
  { value: 'medium', label: 'Media' },
  { value: 'high', label: 'Alta' },
  { value: 'critical', label: 'Crítica' },
]

const CODING_TOOLS = [
  { value: '',         label: 'Auto (el sistema decide)' },
  { value: 'tier_s',   label: 'Tier S · Consensus (Planner + Builder + 3 reviewers)' },
  { value: 'codex',    label: 'Codex' },
  { value: 'claude',   label: 'Claude' },
  { value: 'gemini',   label: 'Gemini' },
  { value: 'opencode', label: 'OpenCode' },
  { value: 'kimi',     label: 'Kimi' },
  { value: 'glm',      label: 'GLM' },
]

export default function TicketForm() {
  const navigate = useNavigate()
  const { id } = useParams()
  const store = useContext(StoreContext)
  const isEdit = Boolean(id)

  const [submitting, setSubmitting] = useState(false)
  const [loadingTicket, setLoadingTicket] = useState(isEdit)
  const [projects, setProjects] = useState([])
  const [loadingProjects, setLoadingProjects] = useState(true)
  const [form, setForm] = useState({
    title: '',
    description: '',
    project: '',
    source: 'api',
    source_id: '',
    agent_hint: '',
    workflow_key: '',
    priority: 'medium',
    coding_tool: '',
    approval_required: false,
    autorun: true,
  })

  const prefersReducedMotion = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  useGSAP(() => {
    if (prefersReducedMotion) return
    const ctx = gsap.context(() => {
      gsap.from('.form-section', { y: 15, opacity: 0, stagger: 0.1, duration: 0.4, ease: 'power2.out' })
    })
    return () => ctx.revert()
  }, [prefersReducedMotion])

  useEffect(() => {
    loadProjects()
    if (isEdit) loadTicket()
  }, [id])

  const loadProjects = async () => {
    try {
      const res = await client.get('/api/projects')
      const list = res.data || []
      setProjects(list)
      if (list.length === 1 && !isEdit) {
        setForm(prev => ({ ...prev, project: list[0].name }))
      }
    } catch {
      setProjects([])
    } finally {
      setLoadingProjects(false)
    }
  }

  const loadTicket = async () => {
    setLoadingTicket(true)
    try {
      const res = await client.get(`/api/tasks/${id}`)
      const t = res.data
      setForm({
        title: t.title || '',
        description: t.description || '',
        project: t.project || '',
        source: t.source || 'api',
        source_id: t.source_id || '',
        agent_hint: t.agent_hint || '',
        workflow_key: t.workflow_key || '',
        priority: t.priority || 'medium',
        coding_tool: t.coding_tool || '',
        approval_required: t.approval_required || false,
        autorun: t.autorun ?? true,
      })
    } catch {
      navigate('/dashboard/tickets/list')
    } finally {
      setLoadingTicket(false)
    }
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!form.title.trim() || !form.description.trim()) return

    setSubmitting(true)
    try {
      const payload = {
        title: form.title.trim(),
        description: form.description.trim(),
        project: form.project.trim() || null,
        agent_hint: form.agent_hint || null,
        workflow_key: form.workflow_key || null,
        priority: form.priority,
        coding_tool: form.coding_tool || null,
        approval_required: form.approval_required,
        autorun: form.autorun,
      }
      if (isEdit) {
        await client.patch(`/api/tasks/${id}`, payload)
        navigate(`/dashboard/tickets/${id}`)
      } else {
        const createPayload = {
          ...payload,
          source: form.source,
          source_id: form.source_id.trim() || null,
        }
        await client.post('/api/tasks', createPayload)
        navigate('/dashboard/tickets/list')
      }
    } catch (err) {
      console.error(isEdit ? 'Error updating ticket:' : 'Error creating ticket:', err)
    } finally {
      setSubmitting(false)
    }
  }

  if (loadingTicket) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="animate-spin rounded-full h-10 w-10 border-2 border-oceo-blue-400 border-t-transparent" />
      </div>
    )
  }

  return (
    <div className="form-page">
      {/* Header */}
      <div className="section-header">
        <div className="flex items-center gap-3">
          <Link to={isEdit ? `/dashboard/tickets/${id}` : '/dashboard/tickets/list'} className="btn-icon">
            <ArrowLeftIcon className="w-5 h-5" />
          </Link>
          <div>
            <h1 className="dashboard-title">{isEdit ? 'Editar Ticket' : 'Nuevo Ticket'}</h1>
            <p className="dashboard-subtitle">{isEdit ? 'Modifica los datos de la tarea' : 'Crea una tarea en el sistema'}</p>
          </div>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="form-grid">
        {/* Basic Data */}
        <div className="form-section card-oceo">
          <h2 className="section-title">
            <TicketIcon className="w-4 h-4" />
            Información general
          </h2>
          <div className="form-fields-grid">
            <div className="form-field">
              <label className="form-label" htmlFor="title">
                <TagIcon className="w-3.5 h-3.5" />
                Título <span className="required-star">*</span>
              </label>
              <input
                id="title"
                type="text"
                value={form.title}
                onChange={e => setForm({ ...form, title: e.target.value })}
                required
                placeholder="Resumen breve de la tarea"
                className="input-oceo"
              />
            </div>

            <div className="form-field">
              <label className="form-label" htmlFor="project">
                <FolderIcon className="w-3.5 h-3.5" />
                Proyecto
              </label>
              {loadingProjects ? (
                <div className="input-oceo flex items-center" style={{ color: 'var(--oceo-text-3)', fontSize: '0.8rem' }}>
                  Cargando proyectos...
                </div>
              ) : projects.length === 0 ? (
                <input
                  id="project"
                  type="text"
                  value={form.project}
                  onChange={e => setForm({ ...form, project: e.target.value })}
                  placeholder="Nombre del proyecto"
                  className="input-oceo"
                />
              ) : projects.length === 1 ? (
                <div className="input-oceo flex items-center" style={{ color: 'var(--oceo-text-2)', fontSize: '0.875rem' }}>
                  {projects[0].title || projects[0].name}
                </div>
              ) : (
                <select
                  id="project"
                  value={form.project}
                  onChange={e => setForm({ ...form, project: e.target.value })}
                  className="input-oceo"
                >
                  <option value="">— Selecciona —</option>
                  {projects.map(p => (
                    <option key={p.name} value={p.name}>{p.title || p.name}</option>
                  ))}
                </select>
              )}
            </div>

            {!isEdit && (
              <>
                <div className="form-field">
                  <label className="form-label" htmlFor="source">
                    <BoltIcon className="w-3.5 h-3.5" />
                    Origen
                  </label>
                  <select
                    id="source"
                    value={form.source}
                    onChange={e => setForm({ ...form, source: e.target.value })}
                    className="input-oceo"
                  >
                    {SOURCES.map(s => (
                      <option key={s.value} value={s.value}>{s.label}</option>
                    ))}
                  </select>
                </div>

                <div className="form-field">
                  <label className="form-label" htmlFor="source_id">
                    ID de origen
                  </label>
                  <input
                    id="source_id"
                    type="text"
                    value={form.source_id}
                    onChange={e => setForm({ ...form, source_id: e.target.value })}
                    placeholder="Opcional — ID externo"
                    className="input-oceo"
                  />
                </div>
              </>
            )}

            <div className="form-field">
              <label className="form-label" htmlFor="agent_hint">
                Agente asignado
              </label>
              <select
                id="agent_hint"
                value={form.agent_hint}
                onChange={e => setForm({ ...form, agent_hint: e.target.value })}
                className="input-oceo"
              >
                {AGENTS.map(a => (
                  <option key={a.value} value={a.value}>{a.label}</option>
                ))}
              </select>
            </div>

            <div className="form-field">
              <label className="form-label" htmlFor="workflow_key">
                Workflow
              </label>
              <select
                id="workflow_key"
                value={form.workflow_key}
                onChange={e => setForm({ ...form, workflow_key: e.target.value })}
                className="input-oceo"
              >
                <option value="">Auto / default del proyecto</option>
                {Object.keys((projects.find(p => p.name === form.project) || projects[0])?.workflow_templates || {}).map(key => (
                  <option key={key} value={key}>{key}</option>
                ))}
              </select>
            </div>

            <div className="form-field">
              <label className="form-label" htmlFor="coding_tool">
                <BoltIcon className="w-3.5 h-3.5" />
                Herramienta de código
              </label>
              <select
                id="coding_tool"
                value={form.coding_tool}
                onChange={e => setForm({ ...form, coding_tool: e.target.value })}
                className="input-oceo"
              >
                {CODING_TOOLS.map(t => (
                  <option key={t.value} value={t.value}>{t.label}</option>
                ))}
              </select>
              {form.coding_tool === 'tier_s' && (
                <p className="form-hint" style={{ color: 'var(--oceo-text-3)', fontSize: '0.75rem', marginTop: '0.25rem' }}>
                  Tier S corre un consenso multi-modelo (más lento y costoso): Planner → Plan Reviewer →
                  Builder → 3 reviewers × 2 rondas. Úsalo para tickets grandes o críticos.
                </p>
              )}
            </div>

            <div className="form-field">
              <label className="form-label" htmlFor="priority">
                Prioridad
              </label>
              <select
                id="priority"
                value={form.priority}
                onChange={e => setForm({ ...form, priority: e.target.value })}
                className="input-oceo"
              >
                {PRIORITIES.map(p => (
                  <option key={p.value} value={p.value}>{p.label}</option>
                ))}
              </select>
            </div>
          </div>
        </div>

        {/* Description */}
        <div className="form-section card-oceo">
          <h2 className="section-title">
            <DocumentTextIcon className="w-4 h-4" />
            Descripción <span className="required-star">*</span>
          </h2>
          <div className="form-field form-field-full" style={{ marginTop: 0 }}>
            <textarea
              id="description"
              value={form.description}
              onChange={e => setForm({ ...form, description: e.target.value })}
              required
              placeholder="Describe la tarea con el mayor detalle posible..."
              rows={8}
              className="input-oceo"
              style={{ paddingTop: '12px', paddingBottom: '12px', resize: 'vertical', minHeight: '120px' }}
            />
          </div>
        </div>

        {/* Options */}
        <div className="form-section card-oceo">
          <h2 className="section-title">
            <ShieldCheckIcon className="w-4 h-4" />
            Opciones
          </h2>
          <div className="form-field form-field-full" style={{ marginTop: 0, display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
            <label className="checkbox-label checkbox-label-lg">
              <input
                type="checkbox"
                checked={form.approval_required}
                onChange={e => setForm({ ...form, approval_required: e.target.checked })}
                className="checkbox-oceo"
              />
              <ShieldCheckIcon className="w-4 h-4" />
              <span>Requiere aprobación antes de ejecutar</span>
            </label>

            <label className={`checkbox-label checkbox-label-lg ${!form.autorun ? 'permission-active' : ''}`}>
              <input
                type="checkbox"
                checked={form.autorun}
                onChange={e => setForm({ ...form, autorun: e.target.checked })}
                className="checkbox-oceo"
              />
              {form.autorun ? <PlayIcon className="w-4 h-4" /> : <PauseIcon className="w-4 h-4" />}
              <span>{form.autorun ? 'Ejecutar automáticamente' : 'Pausado — esperar ejecución manual'}</span>
            </label>

            {!form.autorun && (
              <p className="empty-text" style={{ textAlign: 'left', padding: '0.25rem 0 0', fontSize: '0.75rem' }}>
                El ticket se mantendrá en estado <strong>En cola</strong> y no se ejecutará hasta que pulses "Ejecutar" desde el detalle.
              </p>
            )}
          </div>
        </div>

        {/* Actions */}
        <div className="form-actions">
          <button type="submit" disabled={submitting} className="btn-primary">
            {submitting ? (isEdit ? 'Guardando...' : 'Creando...') : (isEdit ? 'Guardar cambios' : 'Crear ticket')}
          </button>
          <Link to={isEdit ? `/dashboard/tickets/${id}` : '/dashboard/tickets/list'} className="btn-secondary">
            Cancelar
          </Link>
        </div>
      </form>
    </div>
  )
}
