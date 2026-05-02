import { useContext, useEffect, useState } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import { ArrowLeftIcon, UsersIcon, UserIcon, KeyIcon, AtSymbolIcon, ShieldCheckIcon, TagIcon, FolderIcon } from '@heroicons/react/24/outline'
import client from '@/api'
import { StoreContext } from '@/context/store'
import { swalError } from '@/utils/swalError'

export default function UserForm() {
  const { id } = useParams()
  const navigate = useNavigate()
  const store = useContext(StoreContext)
  const isEdit = Boolean(id)
  const [loading, setLoading] = useState(isEdit)
  const [submitting, setSubmitting] = useState(false)
  const [groups, setGroups] = useState([])
  const [projects, setProjects] = useState([])
  const [form, setForm] = useState({
    username: '',
    password: '',
    display_name: '',
    email: '',
    is_admin: false,
    is_client: false,
    group_ids: [],
    allowed_projects: [],
  })

  const prefersReducedMotion = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  useGSAP(() => {
    if (prefersReducedMotion) return
    const ctx = gsap.context(() => {
      gsap.from('.form-section', { y: 15, opacity: 0, stagger: 0.1, duration: 0.4, ease: 'power2.out' })
    })
    return () => ctx.revert()
  }, [prefersReducedMotion])

  useEffect(() => { loadGroups(); loadProjects(); if (isEdit) loadUser() }, [id])

  const loadGroups = async () => {
    try {
      const res = await client.get('/api/groups')
      setGroups(res.data)
    } catch { /* silent */ }
  }

  const loadProjects = async () => {
    try {
      const res = await client.get('/api/projects')
      setProjects(res.data || [])
    } catch { /* silent */ }
  }

  const loadUser = async () => {
    try {
      const res = await client.get(`/api/users/${id}`)
      const u = res.data
      setForm({
        username: u.username || '',
        password: '',
        display_name: u.display_name || '',
        email: u.email || '',
        is_admin: u.is_admin || false,
        is_client: u.is_client || false,
        group_ids: (u.group_ids || []).map(g => g.group_id || g._id || g),
        allowed_projects: u.allowed_projects || [],
      })
    } catch { /* silent */ } finally { setLoading(false) }
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    setSubmitting(true)
    try {
      if (isEdit) {
        await client.patch(`/api/users/${id}`, form)
      } else {
        await client.post('/api/users', form)
      }
      navigate('/dashboard/admin/users/list')
    } catch (err) {
      swalError(err)
    } finally {
      setSubmitting(false)
    }
  }

  const toggleGroup = (groupId) => {
    setForm(prev => ({
      ...prev,
      group_ids: prev.group_ids.includes(groupId)
        ? prev.group_ids.filter(g => g !== groupId)
        : [...prev.group_ids, groupId],
    }))
  }

  const toggleProject = (projectName) => {
    setForm(prev => ({
      ...prev,
      allowed_projects: prev.allowed_projects.includes(projectName)
        ? prev.allowed_projects.filter(p => p !== projectName)
        : [...prev.allowed_projects, projectName],
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
          <Link to="/dashboard/admin/users/list" className="btn-icon">
            <ArrowLeftIcon className="w-5 h-5" />
          </Link>
          <div>
            <h1 className="dashboard-title">{isEdit ? 'Editar Usuario' : 'Nuevo Usuario'}</h1>
            <p className="dashboard-subtitle">{isEdit ? 'Modifica los datos del usuario' : 'Crea un nuevo usuario en el sistema'}</p>
          </div>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="form-grid">
        {/* Basic Data */}
        <div className="form-section card-oceo">
          <h2 className="section-title">
            <UserIcon className="w-4 h-4" />
            Datos básicos
          </h2>
          <div className="form-fields-grid">
            <div className="form-field">
              <label className="form-label" htmlFor="username">
                <AtSymbolIcon className="w-3.5 h-3.5" />
                Usuario
              </label>
              <input id="username" type="text" value={form.username} onChange={e => setForm({ ...form, username: e.target.value })} required className="input-oceo" />
            </div>
            <div className="form-field">
              <label className="form-label" htmlFor="password">
                <KeyIcon className="w-3.5 h-3.5" />
                Contraseña {!isEdit && <span className="required-star">(requerida)</span>}
              </label>
              <input id="password" type="password" value={form.password} onChange={e => setForm({ ...form, password: e.target.value })} required={!isEdit} className="input-oceo" />
            </div>
            <div className="form-field">
              <label className="form-label" htmlFor="display_name">
                <UsersIcon className="w-3.5 h-3.5" />
                Nombre
              </label>
              <input id="display_name" type="text" value={form.display_name} onChange={e => setForm({ ...form, display_name: e.target.value })} className="input-oceo" />
            </div>
            <div className="form-field">
              <label className="form-label" htmlFor="email">
                <AtSymbolIcon className="w-3.5 h-3.5" />
                Email
              </label>
              <input id="email" type="email" value={form.email} onChange={e => setForm({ ...form, email: e.target.value })} className="input-oceo" />
            </div>
          </div>
          <div className="form-field form-field-full">
            <label className="checkbox-label checkbox-label-lg">
              <input type="checkbox" checked={form.is_admin} onChange={e => setForm({ ...form, is_admin: e.target.checked })} className="checkbox-oceo" />
              <ShieldCheckIcon className="w-4 h-4" />
              <span>Administrador</span>
            </label>
            <label className="checkbox-label checkbox-label-lg ml-6">
              <input type="checkbox" checked={form.is_client} onChange={e => setForm({ ...form, is_client: e.target.checked })} className="checkbox-oceo" />
              <UsersIcon className="w-4 h-4" />
              <span>Cliente</span>
            </label>
          </div>
        </div>

        {/* Projects */}
        {projects.length > 0 && (
          <div className="form-section card-oceo">
            <h2 className="section-title">
              <FolderIcon className="w-4 h-4" />
              Proyectos permitidos
            </h2>
            {form.is_admin ? (
              <p className="empty-text" style={{ textAlign: 'left', padding: '0.5rem 0' }}>
                Los administradores tienen acceso a todos los proyectos automáticamente.
              </p>
            ) : projects.length === 0 ? (
              <p className="empty-text" style={{ textAlign: 'left', padding: '0.5rem 0' }}>
                No hay proyectos configurados.
              </p>
            ) : (
              <div className="groups-grid">
                {projects.map(p => (
                  <label key={p.name} className={`group-checkbox ${form.allowed_projects.includes(p.name) ? 'permission-active' : ''}`}>
                    <input
                      type="checkbox"
                      checked={form.allowed_projects.includes(p.name)}
                      onChange={() => toggleProject(p.name)}
                      className="checkbox-oceo"
                    />
                    <span>{p.title || p.name}</span>
                  </label>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Groups */}
        <div className="form-section card-oceo">
          <h2 className="section-title">
            <TagIcon className="w-4 h-4" />
            Grupos
          </h2>
          {groups.length === 0 ? (
            <p className="empty-text">No hay grupos disponibles</p>
          ) : (
            <div className="groups-grid">
              {groups.map(g => (
                <label key={g.group_id || g._id || g.id} className="group-checkbox">
                  <input type="checkbox" checked={form.group_ids.includes(g.group_id || g._id || g.id)} onChange={() => toggleGroup(g.group_id || g._id || g.id)} className="checkbox-oceo" />
                  <span>{g.name}</span>
                </label>
              ))}
            </div>
          )}
        </div>

        {/* Actions */}
        <div className="form-actions">
          <button type="submit" disabled={submitting} className="btn-primary">
            {submitting ? 'Guardando...' : (isEdit ? 'Guardar cambios' : 'Crear usuario')}
          </button>
          <Link to="/dashboard/admin/users/list" className="btn-secondary">
            Cancelar
          </Link>
        </div>
      </form>
    </div>
  )
}
