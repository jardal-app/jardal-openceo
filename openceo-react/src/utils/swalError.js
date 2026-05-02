import Swal from 'sweetalert2'

function cleanupSwalContainer() {
  document.querySelectorAll('.swal2-container').forEach(el => {
    if (!el.querySelector('.swal2-popup')) {
      el.remove()
    }
  })
}

/**
 * Show a SweetAlert2 error toast/dialog with the backend message.
 * @param {Error|AxiosError|any} error — the thrown error
 * @param {string} [fallback='Ocurrió un error inesperado.'] — fallback message
 * @returns {Promise} — resolves after the dialog is dismissed
 */
export async function swalError(error, fallback = 'Ocurrió un error inesperado.') {
  const message =
    error?.response?.data?.detail ||
    error?.response?.data?.message ||
    error?.message ||
    fallback

  await Swal.fire({
    title: 'Error',
    text: message,
    icon: 'error',
    confirmButtonText: 'Entendido',
  })

  cleanupSwalContainer()
}
