import { useEffect, useState } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import { ArrowLeftIcon, TagIcon, SparklesIcon } from '@heroicons/react/24/outline'
import client from '@/api'
import { swalError } from '@/utils/swalError'

const ALL_PERMISSIONS = [
  'read_tickets', 'write_tickets', 'approve_tickets', 'reject_tickets',
  'review_tickets', 'qa_review', 'manage_users', 'manage_groups', 'manage_projects',
  'admin_dashboard', 'client_review',
]

const permissionLabels = {
  read_tickets: 'Ver tickets',
  write_tickets: 'Crear/editar tickets',
  approve_tickets: 'Aprobar tickets',
  reject_tickets: 'Rechazar tickets',
  review_tickets: 'Revisar tickets',
  qa_review: 'Revisión QA',
  manage_users: 'Gestionar usuarios',
  manage_groups: 'Gestionar grupos',
  manage_projects: 'Gestionar proyectos',
  admin_dashboard: 'Ver dashboard',
  client_review: 'Revisión de cliente',
}

export default function GroupForm() {
  const { id } = useParams()
  const navigate = useNavigate()
  const isEdit = Boolean(id)
  const [loading, setLoading] = useState(isEdit)
  const [submitting, setSubmitting] = useState(false)
  const [form, setForm] = useState({ name: '', description: '', permissions: [] })

  const prefersReducedMotion = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  useGSAP(() => {
    if (prefersReducedMotion) return
    const ctx = gsap.context(() => {
      gsap.from('.form-section', { y: 15, opacity: 0, stagger: 0.1, duration: 0.4, ease: 'power2.out' })
    })
    return () => ctx.revert()
  }, [prefersReducedMotion])

  useEffect(() => { if (isEdit) loadGroup() }, [id])

  const loadGroup = async () => {
    try {
      const res = await client.get(`/api/groups/${id}`)
      const g = res.data
      setForm({ name: g.name || '', description: g.description || '', permissions: g.permissions || [] })
    } catch { /* silent */ } finally { setLoading(false) }
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    setSubmitting(true)
    try {
      if (isEdit) {
        await client.patch(`/api/groups/${id}`, form)
      } else {
        await client.post('/api/groups', form)
      }
      navigate('/dashboard/admin/groups/list')
    } catch (err) {
      swalError(err)
    } finally {
      setSubmitting(false)
    }
  }

  const togglePermission = (perm) => {
    setForm(prev => ({
      ...prev,
      permissions: prev.permissions.includes(perm)
        ? prev.permissions.filter(p => p !== perm)
        : [...prev.permissions, perm],
    }))
  }

  if (loading) {
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
        <div className="flex items-center gap-4">
          <Link to="/dashboard/admin/groups/list" className="btn-icon">
            <ArrowLeftIcon className="w-5 h-5" />
          </Link>
          <div>
            <h1 className="dashboard-title">{isEdit ? 'Editar Grupo' : 'Nuevo Grupo'}</h1>
            <p className="dashboard-subtitle">{isEdit ? 'Modifica el grupo y sus permisos' : 'Crea un nuevo grupo con permisos específicos'}</p>
          </div>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="form-grid">
        {/* Group Data */}
        <div className="form-section card-oceo">
          <h2 className="section-title">
            <TagIcon className="w-4 h-4" />
            Datos del grupo
          </h2>
          <div className="form-fields-grid">
            <div className="form-field form-field-full">
              <label className="form-label" htmlFor="name">Nombre</label>
              <input id="name" type="text" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} required className="input-oceo" />
            </div>
          </div>
        </div>

        {/* Permissions */}
        <div className="form-section card-oceo">
          <h2 className="section-title">
            <SparklesIcon className="w-4 h-4" />
            Permisos
          </h2>
          <div className="permissions-grid">
            {ALL_PERMISSIONS.map(perm => (
              <label key={perm} className={`permission-card ${form.permissions.includes(perm) ? 'permission-active' : ''}`}>
                <input type="checkbox" checked={form.permissions.includes(perm)} onChange={() => togglePermission(perm)} className="checkbox-oceo" />
                <span className="permission-name">{permissionLabels[perm] || perm}</span>
              </label>
            ))}
          </div>
        </div>

        {/* Actions */}
        <div className="form-actions">
          <button type="submit" disabled={submitting} className="btn-primary">
            {submitting ? 'Guardando...' : (isEdit ? 'Guardar cambios' : 'Crear grupo')}
          </button>
          <Link to="/dashboard/admin/groups/list" className="btn-secondary">
            Cancelar
          </Link>
        </div>
      </form>
    </div>
  )
}
