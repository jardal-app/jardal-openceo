import { useContext, useEffect, useState } from 'react'
import { Link, useParams, useNavigate } from 'react-router-dom'
import { ArrowLeftIcon } from '@heroicons/react/24/outline'
import client from '@/api'
import { StoreContext } from '@/context/store'
import { swalError } from '@/utils/swalError'

export default function ProjectForm() {
  const store = useContext(StoreContext)
  const { name } = useParams()
  const navigate = useNavigate()
  const isEdit = Boolean(name)

  const [form, setForm] = useState({
    name: '',
    display_name: '',
    description: '',
    workflow_templates_json: '',
  })
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (isEdit) {
      loadProject()
    }
  }, [name])

  const loadProject = async () => {
    try {
      const response = await client.get('/api/projects')
      const found = response.data.find((p) => p.name === name)
      if (found) {
        setForm({
          name: found.name,
          display_name: found.display_name || found.name,
          description: found.description || '',
          workflow_templates_json: JSON.stringify(found.workflow_templates || {}, null, 2),
        })
      }
    } catch { /* silent */ }
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!form.name.trim() || !form.display_name.trim()) return
    setSubmitting(true)
    try {
      let workflowTemplates = {}
      if (form.workflow_templates_json.trim()) {
        workflowTemplates = JSON.parse(form.workflow_templates_json)
      }
      if (isEdit) {
        await client.patch(`/api/projects/${name}`, {
          display_name: form.display_name.trim(),
          description: form.description.trim(),
          workflow_templates: workflowTemplates,
        })
      } else {
        await client.post('/api/projects', {
          name: form.name.trim().toLowerCase().replace(/\s+/g, '-'),
          display_name: form.display_name.trim(),
          description: form.description.trim(),
          workflow_templates: workflowTemplates,
        })
      }
      navigate('/dashboard/admin/projects/list')
    } catch (err) {
      swalError(err)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="ticket-form-page">
      <div className="section-header">
        <Link to="/dashboard/admin/projects/list" className="btn-icon">
          <ArrowLeftIcon className="w-5 h-5" />
        </Link>
        <div>
          <h1 className="dashboard-title">{isEdit ? 'Editar Proyecto' : 'Nuevo Proyecto'}</h1>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="card-oceo form-card">
        <div className="form-grid">
          <div className="form-field">
            <label className="form-label">Nombre (ID)</label>
            <input
              type="text"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              disabled={isEdit}
              placeholder="mi-proyecto"
              className="input-oceo"
              required
            />
          </div>
          <div className="form-field">
            <label className="form-label">Nombre visible</label>
            <input
              type="text"
              value={form.display_name}
              onChange={(e) => setForm({ ...form, display_name: e.target.value })}
              placeholder="Mi Proyecto"
              className="input-oceo"
              required
            />
          </div>
          <div className="form-field form-field-full">
            <label className="form-label">Descripción</label>
            <textarea
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
              placeholder="Descripción del proyecto..."
              className="input-oceo"
              rows={4}
            />
          </div>
          <div className="form-field form-field-full">
            <label className="form-label">Workflow templates JSON</label>
            <textarea
              value={form.workflow_templates_json}
              onChange={(e) => setForm({ ...form, workflow_templates_json: e.target.value })}
              placeholder='{"dev.default":{"phases":[...]}}'
              className="input-oceo"
              rows={18}
              style={{ height: 'auto', fontFamily: 'monospace', fontSize: '0.78rem', lineHeight: 1.45 }}
            />
            <p className="empty-text" style={{ textAlign: 'left', padding: '0.4rem 0 0' }}>
              Puedes sobreescribir `dev.default` o `dev.client_visible` por proyecto. Las fases humanas usan `permission` y se atienden desde dashboards.
            </p>
          </div>
        </div>
        <div className="form-actions">
          <button type="submit" disabled={submitting} className="btn-primary">
            {submitting ? 'Guardando...' : 'Guardar'}
          </button>
          <Link to="/dashboard/admin/projects/list" className="btn-secondary">Cancelar</Link>
        </div>
      </form>
    </div>
  )
}
