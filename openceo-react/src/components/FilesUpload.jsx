import { useState, useRef, useEffect, forwardRef, useImperativeHandle } from 'react'
import {
  DocumentIcon,
  TrashIcon,
  CloudArrowUpIcon,
  CloudArrowDownIcon,
  PlusIcon,
  ArrowPathIcon,
  ExclamationTriangleIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline'
import client from '@/api'

/**
 * FilesUpload – Visualmente idéntico al jardal-vpn original,
 * con colores OpenCEO + proxy R2 + paste de portapapeles.
 */
const FilesUpload = forwardRef(function FilesUpload(
  {
    value = [],
    onChange,
    accept = 'image/*,video/*',
    maxFiles = 10,
    maxSizeMB = 50,
    disabled = false,
    label = 'Archivos',
    helperText = 'Máximo {maxFiles} archivos, hasta {maxSizeMB} MB cada uno',
  },
  ref
) {
  const [isDragging, setIsDragging] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState(null)
  const [pendingFiles, setPendingFiles] = useState([])
  const [removedKeys, setRemovedKeys] = useState([])
  const [lightboxUrl, setLightboxUrl] = useState(null)
  const inputRef = useRef(null)
  const pendingFilesRef = useRef([])

  const extractKey = (url) => {
    try {
      const clean = url.split('?')[0]
      return clean.split('/').pop() || null
    } catch { return null }
  }

  useEffect(() => {
    pendingFilesRef.current = pendingFiles
  }, [pendingFiles])

  useEffect(() => {
    return () => {
      for (const item of pendingFilesRef.current) {
        if (item.previewUrl) {
          URL.revokeObjectURL(item.previewUrl)
        }
      }
    }
  }, [])

  const visibleCommittedUrls = value.filter((url) => {
    const key = extractKey(url)
    return !key || !removedKeys.includes(key)
  })

  /* ── paste from clipboard ── */
  useEffect(() => {
    const onPaste = (e) => {
      if (disabled || uploading) return
      const items = e.clipboardData?.items
      if (!items) return
      const files = []
      for (const item of items) {
        if (item.kind === 'file') {
          const file = item.getAsFile()
          if (file) files.push(file)
        }
      }
      if (files.length) {
        e.preventDefault()
        handleFiles(files)
      }
    }
    window.addEventListener('paste', onPaste)
    return () => window.removeEventListener('paste', onPaste)
  }, [disabled, uploading, pendingFiles, removedKeys, value])

  /* ── drag helpers ── */
  const handleDragOver = (e) => {
    e.preventDefault()
    if (!isDragging) setIsDragging(true)
  }

  const handleDragEnter = (e) => {
    e.preventDefault()
    setIsDragging(true)
  }

  const handleDragLeave = (e) => {
    e.preventDefault()
    if (!e.currentTarget.contains(e.relatedTarget)) {
      setIsDragging(false)
    }
  }

  const handleDrop = (e) => {
    e.preventDefault()
    setIsDragging(false)
    if (disabled || uploading) return
    handleFiles(Array.from(e.dataTransfer.files))
  }

  useEffect(() => {
    const onDragEnd = () => setIsDragging(false)
    window.addEventListener('dragend', onDragEnd)
    return () => window.removeEventListener('dragend', onDragEnd)
  }, [])

  /* ── type detection using key (proxy URLs end in /download) ── */
  const isImage = (url) => {
    const key = extractKey(url) || ''
    return /\.(jpg|jpeg|png|gif|webp|svg)$/i.test(key)
  }

  const isVideo = (url) => {
    const key = extractKey(url) || ''
    return /\.(mp4|webm|mov|mkv)$/i.test(key)
  }

  const isPendingImage = (file) => file?.type?.startsWith('image/')

  const isPendingVideo = (file) => file?.type?.startsWith('video/')

  const validateFiles = (files) => {
    if (files.length + visibleCommittedUrls.length + pendingFiles.length > maxFiles) {
      return `Máximo ${maxFiles} archivos en total.`
    }
    for (const f of files) {
      if (f.size > maxSizeMB * 1024 * 1024) {
        return `El archivo "${f.name}" excede ${maxSizeMB} MB.`
      }
    }
    if (accept && accept !== '*') {
      const types = accept.split(',').map((t) => t.trim())
      for (const f of files) {
        const ok = types.some((t) => {
          if (t.endsWith('/*')) return f.type.startsWith(t.slice(0, -1))
          if (t.startsWith('.')) return f.name.toLowerCase().endsWith(t)
          return f.type === t
        })
        if (!ok) return `Tipo no permitido: "${f.name}".`
      }
    }
    return null
  }

  const handleFiles = (files) => {
    if (!files || files.length === 0) return
    const validationError = validateFiles(files)
    if (validationError) {
      setError(validationError)
      return
    }
    const existing = new Set(
      pendingFiles.map((item) => `${item.file.name}:${item.file.size}:${item.file.lastModified}`)
    )
    const seen = new Set(existing)
    const newPending = files
      .filter((file) => {
        const fingerprint = `${file.name}:${file.size}:${file.lastModified}`
        if (seen.has(fingerprint)) return false
        seen.add(fingerprint)
        return true
      })
      .map((file) => ({
        id: `${file.name}-${file.size}-${file.lastModified}-${Math.random().toString(36).slice(2, 8)}`,
        file,
        previewUrl: URL.createObjectURL(file),
      }))

    if (newPending.length === 0) return
    setError(null)
    setPendingFiles((prev) => [...prev, ...newPending])
  }

  const handleInputChange = (e) => {
    handleFiles(Array.from(e.target.files))
    e.target.value = ''
  }

  /* ── remove file ── */
  const removeExistingFile = (index) => {
    const url = visibleCommittedUrls[index]
    const key = extractKey(url)
    if (key) {
      setRemovedKeys((prev) => (prev.includes(key) ? prev : [...prev, key]))
    }
  }

  const removePendingFile = (index) => {
    setPendingFiles((prev) => {
      const item = prev[index]
      if (item?.previewUrl) {
        URL.revokeObjectURL(item.previewUrl)
      }
      return prev.filter((_, fileIndex) => fileIndex !== index)
    })
  }

  /* ── imperative ref ── */
  useImperativeHandle(ref, () => ({
    prepareSave: async () => {
      const keptUrls = value.filter((url) => {
        const key = extractKey(url)
        return !key || !removedKeys.includes(key)
      })
      if (pendingFiles.length === 0) {
        return {
          urls: keptUrls,
          uploadedKeys: [],
          removedKeys: [...removedKeys],
        }
      }

      setUploading(true)
      setError(null)
      const formData = new FormData()
      pendingFiles.forEach((item) => formData.append('files', item.file))

      try {
        const res = await client.post('/api/upload', formData)
        const uploaded = res.data || []
        return {
          urls: [...keptUrls, ...uploaded.map((item) => item.url)],
          uploadedKeys: uploaded.map((item) => item.key).filter(Boolean),
          removedKeys: [...removedKeys],
        }
      } catch (err) {
        const detail = err?.response?.data?.detail
        const msg = Array.isArray(detail)
          ? detail.map((entry) => entry.msg).join('; ')
          : detail || err.message || 'Error al subir archivos'
        setError(msg)
        throw err
      } finally {
        setUploading(false)
      }
    },
    rollbackPreparedSave: async (uploadedKeys) => {
      if (!uploadedKeys?.length) return
      for (const key of uploadedKeys) {
        try { await client.delete(`/api/upload/${key}`) } catch { /* best effort */ }
      }
    },
    finalizePreparedSave: async ({ urls, removedKeys: keysToDelete = [] } = {}) => {
      let failedDeletes = 0
      for (const key of keysToDelete) {
        try {
          await client.delete(`/api/upload/${key}`)
        } catch {
          failedDeletes += 1
        }
      }
      for (const item of pendingFiles) {
        if (item.previewUrl) {
          URL.revokeObjectURL(item.previewUrl)
        }
      }
      setPendingFiles([])
      setRemovedKeys([])
      setError(
        failedDeletes > 0
          ? `Se guardó el ticket, pero no se pudieron borrar ${failedDeletes} archivo(s) del storage.`
          : null
      )
      return { failedDeletes }
    },
    resetCommitted: async () => {
      for (const item of pendingFiles) {
        if (item.previewUrl) {
          URL.revokeObjectURL(item.previewUrl)
        }
      }
      setPendingFiles([])
      setRemovedKeys([])
      setError(null)
    },
  }))

  const addMoreFiles = () => {
    inputRef.current?.click()
  }

  const truncateFileName = (name) =>
    name.length > 8 ? `${name.substring(0, 8)}…` : name

  const extractName = (url) => {
    try {
      const noQuery = url.split('?')[0]
      const decoded = decodeURIComponent(noQuery)
      const name = decoded.split('/').pop()
      return name || 'archivo'
    } catch {
      return 'archivo'
    }
  }

  /* ── render thumbnail ── */
  const renderCommittedThumbnail = (url, index) => {
    const name = extractName(url)
    const img = isImage(url)
    const vid = isVideo(url)

    return (
      <div key={`${url}-${index}`} className="relative w-28 h-28 rounded-md shadow-xl overflow-hidden">
        {img ? (
          <img src={url} alt={name} className="w-28 h-28 object-cover rounded-md" loading="lazy" />
        ) : vid ? (
          <video src={url} preload="metadata" className="w-28 h-28 object-cover rounded-md" />
        ) : (
          <DocumentIcon className="w-28 h-28 text-[var(--oceo-text-3)] rounded-md" />
        )}

        {/* file name overlay (bottom) */}
        <div className="absolute bottom-0 w-full bg-black/50 text-white text-[11px] px-1 py-0.5 rounded-b-md truncate">
          {truncateFileName(name)}
        </div>

        {/* green button – open in new tab (center) */}
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          className="absolute top-1/2 left-1/2 transform -translate-x-1/2 -translate-y-1/2 flex justify-center items-center w-10 h-10 rounded-full bg-[var(--oceo-success)]/80 hover:bg-white transition-colors"
          title="Ver"
          onClick={(e) => e.stopPropagation()}
        >
          <CloudArrowDownIcon className="h-6 w-6 text-white hover:text-[var(--oceo-success)]" />
        </a>

        {/* red button – delete (top-right) */}
        <button
          type="button"
          className="absolute top-1 right-1 z-10 flex justify-center items-center w-7 h-7 rounded-full bg-[var(--oceo-danger)]/80 hover:bg-white transition-colors"
          onClick={() => removeExistingFile(index)}
          disabled={uploading || disabled}
          title="Eliminar"
        >
          <TrashIcon className="h-4 w-4 text-white hover:text-[var(--oceo-danger)]" />
        </button>
      </div>
    )
  }

  const renderPendingThumbnail = (item, index) => {
    const img = isPendingImage(item.file)
    const vid = isPendingVideo(item.file)

    return (
      <div key={item.id} className="relative w-28 h-28 rounded-md shadow-xl overflow-hidden ring-2 ring-[var(--oceo-blue-500)]/35">
        {img ? (
          <img src={item.previewUrl} alt={item.file.name} className="w-28 h-28 object-cover rounded-md" loading="lazy" />
        ) : vid ? (
          <video src={item.previewUrl} preload="metadata" className="w-28 h-28 object-cover rounded-md" />
        ) : (
          <DocumentIcon className="w-28 h-28 text-[var(--oceo-text-3)] rounded-md" />
        )}

        <div className="absolute top-1 left-1 z-10 rounded-full bg-[var(--oceo-blue-500)] px-2 py-0.5 text-[10px] font-medium text-white">
          Pendiente
        </div>

        <div className="absolute bottom-0 w-full bg-black/50 text-white text-[11px] px-1 py-0.5 rounded-b-md truncate">
          {truncateFileName(item.file.name)}
        </div>

        <button
          type="button"
          className="absolute top-1 right-1 z-10 flex justify-center items-center w-7 h-7 rounded-full bg-[var(--oceo-danger)]/80 hover:bg-white transition-colors"
          onClick={() => removePendingFile(index)}
          disabled={uploading || disabled}
          title="Quitar"
        >
          <TrashIcon className="h-4 w-4 text-white hover:text-[var(--oceo-danger)]" />
        </button>
      </div>
    )
  }

  const hasFiles = visibleCommittedUrls.length > 0 || pendingFiles.length > 0
  return (
    <div className="files-upload-v2 flex flex-col gap-2">
      {label && (
        <label className="block text-sm font-medium text-[var(--oceo-text-2)]">
          {label}
        </label>
      )}

      <div
        className={`fu-container relative border border-[var(--oceo-stroke)] rounded-lg max-h-80 overflow-y-auto bg-[var(--oceo-surface)] shadow-md transition-colors ${isDragging ? 'border-[var(--oceo-blue-500)]' : ''}`}
        onDragOver={handleDragOver}
        onDragEnter={handleDragEnter}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        {/* drag overlay */}
        {isDragging && (
          <div className="fu-overlay">
            <div className="flex items-center gap-3">
              <CloudArrowUpIcon className="h-10 w-10 text-white" />
              <p className="text-white text-xl font-medium">Soltar Aquí</p>
            </div>
          </div>
        )}

        {/* loading overlay */}
        {uploading && (
          <div className="fu-overlay">
            <div className="flex flex-col items-center gap-2">
              <ArrowPathIcon className="h-10 w-10 text-white animate-spin" />
              <p className="text-white text-sm">Subiendo archivos…</p>
            </div>
          </div>
        )}

        <input
          ref={inputRef}
          type="file"
          multiple
          accept={accept}
          className="hidden"
          onChange={handleInputChange}
          disabled={disabled || uploading}
        />

        {hasFiles ? (
          <div className="flex flex-wrap gap-2 p-3">
            {/* add-more button */}
            <button
              type="button"
              onClick={addMoreFiles}
              disabled={disabled || uploading || visibleCommittedUrls.length + pendingFiles.length >= maxFiles}
              className="w-28 h-28 flex-none flex items-center justify-center rounded-md border-2 border-dashed border-[var(--oceo-blue-500)] text-[var(--oceo-blue-500)] hover:bg-[var(--oceo-blue-500)]/10 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              title="Agregar más archivos"
            >
              <PlusIcon className="h-8 w-8" />
            </button>

            {visibleCommittedUrls.map((url, idx) => renderCommittedThumbnail(url, idx))}
            {pendingFiles.map((item, idx) => renderPendingThumbnail(item, idx))}
          </div>
        ) : (
          <div
            className="flex flex-col items-center justify-center h-48 gap-2 cursor-pointer text-[var(--oceo-text-3)] hover:text-[var(--oceo-text-2)] transition-colors"
            onClick={() => !disabled && !uploading && inputRef.current?.click()}
          >
            <CloudArrowUpIcon className="h-10 w-10" />
            <p className="text-sm text-center px-4">
              Arrastra o <strong className="text-[var(--oceo-blue-500)] cursor-pointer">selecciona</strong> archivos
            </p>
            <p className="text-xs text-[var(--oceo-text-3)]">
              {helperText.replace('{maxFiles}', maxFiles).replace('{maxSizeMB}', maxSizeMB)}
            </p>
            <p className="text-[11px] text-[var(--oceo-text-3)] opacity-70">
              También puedes pegar (Ctrl+V) imágenes
            </p>
          </div>
        )}
      </div>

      {/* error banner */}
      {error && (
        <div className="files-upload-error flex items-center gap-2 px-3 py-2 rounded-md">
          <ExclamationTriangleIcon className="w-4 h-4 flex-shrink-0" />
          <span className="text-sm flex-1">{error}</span>
          <button
            type="button"
            className="btn-icon btn-icon-sm"
            onClick={() => setError(null)}
            title="Cerrar"
          >
            <XMarkIcon className="w-3 h-3" />
          </button>
        </div>
      )}

      {/* Lightbox */}
      {lightboxUrl && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm"
          onClick={() => setLightboxUrl(null)}
        >
          <button
            type="button"
            className="absolute top-4 right-4 text-white hover:text-gray-300"
            onClick={() => setLightboxUrl(null)}
          >
            <XMarkIcon className="w-8 h-8" />
          </button>
          <img
            src={lightboxUrl}
            alt="Vista completa"
            className="max-w-[90vw] max-h-[90vh] object-contain rounded-lg shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </div>
  )
})

export default FilesUpload
