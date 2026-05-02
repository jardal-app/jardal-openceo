import Swal from 'sweetalert2'

function cleanupSwalContainer() {
  document.querySelectorAll('.swal2-container').forEach(el => {
    if (!el.querySelector('.swal2-popup')) {
      el.remove()
    }
  })
}

/**
 * Reusable SweetAlert2 confirm dialog that respects the global dark theme.
 * @param {Object} opts
 * @param {string} opts.title — dialog title
 * @param {string} opts.text — dialog body text
 * @param {string} [opts.icon='warning'] — Swal icon
 * @param {string} [opts.confirmText='Confirmar'] — confirm button text
 * @param {string} [opts.cancelText='Cancelar'] — cancel button text
 * @returns {Promise<boolean>} — true if confirmed, false otherwise
 */
export async function swalConfirm({
  title = '¿Estás seguro?',
  text = '',
  icon = 'warning',
  confirmText = 'Confirmar',
  cancelText = 'Cancelar',
} = {}) {
  const result = await Swal.fire({
    title,
    text,
    icon,
    showCancelButton: true,
    confirmButtonText: confirmText,
    cancelButtonText: cancelText,
    reverseButtons: true,
  })

  cleanupSwalContainer()

  return result.isConfirmed
}
