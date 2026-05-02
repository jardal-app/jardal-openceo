import { useContext, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import {
  PlusIcon,
  FolderIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
} from '@heroicons/react/24/outline'
import client from '@/api'
import { StoreContext } from '@/context/store'
import OptionsCRUD from '@/components/OptionsCRUD'
import { swalConfirm } from '@/utils/swalConfirm'
import { swalError } from '@/utils/swalError'

export default function ProjectList() {
  const store = useContext(StoreContext)
  const [projects, setProjects] = useState([])
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(1)
  const [itemsPerPage, setItemsPerPage] = useState(10)

  const handleItemsPerPageChange = (value) => {
    setItemsPerPage(Number(value))
    setPage(1)
  }

  const prefersReducedMotion = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  useGSAP(() => {
    if (prefersReducedMotion) return
    const ctx = gsap.context(() => {
      gsap.from('.project-row', { opacity: 0, y: 10, stagger: 0.04, duration: 0.3, ease: 'power2.out' })
    })
    return () => ctx.revert()
  }, [projects, prefersReducedMotion])

  useEffect(() => { loadProjects() }, [])

  const loadProjects = async () => {
    setLoading(true)
    try {
      const response = await client.get('/api/projects')
      setProjects(response.data)
    } catch { /* silent */ } finally { setLoading(false) }
  }

  const handleDelete = async (name) => {
    const confirmed = await swalConfirm({
      title: '¿Eliminar proyecto?',
      text: 'Esta acción no se puede deshacer.',
      icon: 'warning',
      confirmText: 'Eliminar',
    })
    if (!confirmed) return
    try {
      await client.delete(`/api/projects/${name}`)
      loadProjects()
    } catch (err) {
      swalError(err)
    }
  }

  const totalPages = Math.ceil(projects.length / itemsPerPage)
  const paginated = projects.slice((page - 1) * itemsPerPage, page * itemsPerPage)

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <div className="animate-spin rounded-full h-10 w-10 border-2 border-oceo-blue-400 border-t-transparent" />
      </div>
    )
  }

  return (
    <div className="projects-page">
      <div className="section-header">
        <div>
          <h1 className="dashboard-title">Proyectos</h1>
          <p className="dashboard-subtitle">Gestión de proyectos</p>
        </div>
        <div className="dashboard-header-actions">
          <span className="results-count">{projects.length} resultado{projects.length !== 1 ? 's' : ''}</span>
          <select
            value={itemsPerPage}
            onChange={(e) => handleItemsPerPageChange(e.target.value)}
            className="input-oceo input-sm"
            style={{ width: 90 }}
          >
            <option value={10}>10 / pág</option>
            <option value={50}>50 / pág</option>
            <option value={100}>100 / pág</option>
            <option value={200}>200 / pág</option>
            <option value={500}>500 / pág</option>
          </select>
          <Link to="/dashboard/admin/projects/create" className="btn-primary btn-sm">
            <PlusIcon className="w-4 h-4" />
            Nuevo Proyecto
          </Link>
        </div>
      </div>

      <div className="card-oceo table-card">
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Nombre</th>
                <th>Display</th>
                <th>Descripción</th>
                <th>Workflows</th>
                <th>Acciones</th>
              </tr>
            </thead>
            <tbody>
              {projects.length === 0 ? (
                <tr>
                  <td colSpan={5} className="empty-cell">
                    <div className="empty-state">
                      <FolderIcon className="w-10 h-10" style={{ color: 'var(--oceo-text-3)' }} />
                      <p>No hay proyectos</p>
                    </div>
                  </td>
                </tr>
              ) : (
                paginated.map((project) => (
                  <tr key={project.name} className="project-row">
                    <td><code className="ticket-id">{project.name}</code></td>
                    <td>{project.display_name}</td>
                    <td className="project-cell">{project.description || '—'}</td>
                    <td className="project-cell">
                      {Object.keys(project.workflow_templates || {}).slice(0, 3).map(key => (
                        <span key={key} className="status-badge badge-neutral mr-1">{key}</span>
                      ))}
                    </td>
                    <td>
                      <OptionsCRUD
                        editHref={`/dashboard/admin/projects/update/${project.name}`}
                        onDelete={() => handleDelete(project.name)}
                        editPerm="manage_projects"
                        deletePerm="manage_projects"
                        itemName="proyecto"
                      />
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="pagination">
            <span className="pagination-info">
              Página {page} de {totalPages}
            </span>
            <div className="pagination-buttons">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page === 1}
                className="btn-secondary btn-sm"
              >
                <ChevronLeftIcon className="w-4 h-4" />
                Anterior
              </button>
              <button
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page === totalPages}
                className="btn-secondary btn-sm"
              >
                Siguiente
                <ChevronRightIcon className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
