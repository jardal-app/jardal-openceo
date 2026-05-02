import { useState, useRef, useEffect, useContext } from 'react'
import { Link } from 'react-router-dom'
import { createPortal } from 'react-dom'
import {
  EllipsisVerticalIcon,
  PencilIcon,
  TrashIcon,
  EyeIcon,
  PlayIcon,
} from '@heroicons/react/24/outline'
import { StoreContext } from '@/context/store'

export default function OptionsCRUD({
  editHref = null,
  viewHref = null,
  viewState = null,
  onEdit = null,
  onDelete = null,
  onView = null,
  onRun = null,
  editPerm = null,
  deletePerm = null,
  viewPerm = null,
  runPerm = null,
  itemName = 'item',
}) {
  const store = useContext(StoreContext)
  const [open, setOpen] = useState(false)
  const [menuStyle, setMenuStyle] = useState({})
  const ref = useRef(null)
  const menuRef = useRef(null)

  useEffect(() => {
    function handleClickOutside(e) {
      const insideTrigger = ref.current && ref.current.contains(e.target)
      const insideMenu = menuRef.current && menuRef.current.contains(e.target)
      if (!insideTrigger && !insideMenu) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  useEffect(() => {
    if (open && ref.current) {
      const rect = ref.current.getBoundingClientRect()
      const menuWidth = 160
      setMenuStyle({
        position: 'fixed',
        top: rect.bottom + 6,
        left: rect.right - menuWidth,
        zIndex: 9999,
        minWidth: 160,
        background: 'var(--oceo-elevated)',
        border: '1px solid var(--oceo-stroke)',
        borderRadius: 12,
        padding: '0.4rem',
        boxShadow: '0 20px 40px -20px rgba(0, 0, 0, 0.9)',
        display: 'flex',
        flexDirection: 'column',
        gap: '0.15rem',
      })
    }
  }, [open])

  const canEdit = editPerm ? store.checkPermissions([editPerm]) : true
  const canDelete = deletePerm ? store.checkPermissions([deletePerm]) : true
  const canView = viewPerm ? store.checkPermissions([viewPerm]) : true
  const canRun = runPerm ? store.checkPermissions([runPerm]) : true

  const hasAnyAction = (editHref || onEdit) && canEdit || onDelete && canDelete || (viewHref || onView) && canView || onRun && canRun

  if (!hasAnyAction) return null

  return (
    <div className="options-crud" ref={ref}>
      <button
        type="button"
        className="btn-icon btn-icon-sm"
        onClick={() => setOpen(!open)}
        title="Acciones"
      >
        <EllipsisVerticalIcon className="w-4 h-4" />
      </button>

      {open && createPortal(
        <div ref={menuRef} style={menuStyle}>
          {(viewHref || onView) && canView && (
            viewHref ? (
              <Link className="options-crud-item" to={viewHref} state={viewState} onClick={() => setOpen(false)}>
                <EyeIcon className="w-4 h-4" />
                <span>Ver</span>
              </Link>
            ) : (
              <button className="options-crud-item" onClick={() => { onView(); setOpen(false) }}>
                <EyeIcon className="w-4 h-4" />
                <span>Ver</span>
              </button>
            )
          )}
          {editHref && canEdit && (
            <Link className="options-crud-item" to={editHref} onClick={() => setOpen(false)}>
              <PencilIcon className="w-4 h-4" />
              <span>Editar</span>
            </Link>
          )}
          {onEdit && canEdit && !editHref && (
            <button className="options-crud-item" onClick={() => { onEdit(); setOpen(false) }}>
              <PencilIcon className="w-4 h-4" />
              <span>Editar</span>
            </button>
          )}
          {onRun && canRun && (
            <button className="options-crud-item" onClick={() => { onRun(); setOpen(false) }}>
              <PlayIcon className="w-4 h-4" />
              <span>Ejecutar</span>
            </button>
          )}
          {onDelete && canDelete && (
            <button className="options-crud-item options-crud-danger" onClick={() => { onDelete(); setOpen(false) }}>
              <TrashIcon className="w-4 h-4" />
              <span>Eliminar</span>
            </button>
          )}
        </div>,
        document.body
      )}
    </div>
  )
}
