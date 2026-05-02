import { useContext, useState, useRef, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useGSAP } from '@gsap/react'
import gsap from 'gsap'
import './Login.css'
import Loading from '@/components/Loading'
import { StoreContext } from '@/context/store'
import client from '@/api'

function Login() {
  const store = useContext(StoreContext)
  const navigate = useNavigate()
  const shellRef = useRef(null)
  const heroRef = useRef(null)
  const cardRef = useRef(null)
  const logoRef = useRef(null)

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  // Redirect when already authenticated
  useEffect(() => {
    if (store.user && localStorage.getItem('heaven')) {
      navigate('/dashboard/inicio', { replace: true })
    }
  }, [store.user, navigate])

  const prefersReducedMotion = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  useGSAP(() => {
    if (prefersReducedMotion) return
    const ctx = gsap.context(() => {
      gsap.from(heroRef.current, { autoAlpha: 0, x: -24, duration: 0.9, ease: 'power2.out' })
      gsap.from(cardRef.current, { autoAlpha: 0, x: 24, scale: 0.98, duration: 0.9, ease: 'power2.out', delay: 0.15 })
      gsap.from(logoRef.current, { autoAlpha: 0, y: -12, duration: 0.7, ease: 'power2.out', delay: 0.3 })
    }, shellRef)
    return () => ctx.revert()
  }, [prefersReducedMotion])

  function handleSubmit(e) {
    e.preventDefault()
    setError(null)

    if (!username.trim()) {
      setError({ title: 'Usuario requerido', body: 'Ingresa tu nombre de usuario.' })
      return
    }
    if (!password.trim()) {
      setError({ title: 'Contraseña requerida', body: 'Ingresa tu contraseña.' })
      return
    }

    setLoading(true)
    store.setLoading(true)

    client.post("/api/auth/login", { username: username.trim(), password })
      .then(r => {
        const payload = r?.data
        if (payload?.user) {
          store.login({ token: payload.token || 'heaven', user: payload.user })
          setLoading(false)
          store.setLoading(false)
          navigate('/dashboard/inicio', { replace: true })
          return
        }
        setError({ title: 'Error', body: 'No se pudo iniciar sesión.' })
        setLoading(false)
        store.setLoading(false)
      })
      .catch(e => {
        setLoading(false)
        store.setLoading(false)
        const status = e.response?.status
        if (status === 401) {
          setError({ title: 'Credenciales inválidas', body: 'Verifica tu usuario y contraseña.' })
        } else {
          setError({ title: 'Error de acceso', body: 'Intenta de nuevo más tarde.' })
        }
      })
  }

  return (
    <>
      {store.loading && <Loading />}
      <div className="login-shell" ref={shellRef}>
        {/* Hero Panel */}
        <div className="login-hero" ref={heroRef}>
          <div className="login-hero-logo" ref={logoRef}>
            <span className="login-hero-logo-text">Open<span>CEO</span></span>
          </div>
          <h1 className="login-hero-title">Página de administración empresarial</h1>
        </div>

        {/* Login Card */}
        <div className="login-card-wrap" ref={cardRef}>
          <div className="login-card">
            <div className="login-card-header">
              <div className="login-card-badge">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="12" cy="12" r="10"/>
                  <path d="M12 8v4l3 3"/>
                </svg>
              </div>
              <div>
                <h2 className="login-card-title">Iniciar sesión</h2>
              </div>
            </div>

            {error && (
              <div className="login-notice login-notice-error">
                <div className="login-notice-content">
                  <p className="login-notice-title">{error.title}</p>
                  <p className="login-notice-body">{error.body}</p>
                </div>
                <button onClick={() => setError(null)} className="login-notice-close" type="button">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
                </button>
              </div>
            )}

            <form onSubmit={handleSubmit} noValidate className="login-form">
              <div className="login-field">
                <label className="login-label" htmlFor="username">Nombre de usuario</label>
                <div className="login-input-wrap">
                  <svg className="login-field-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
                    <circle cx="12" cy="7" r="4"/>
                  </svg>
                  <input
                    id="username"
                    type="text"
                    autoComplete="username"
                    value={username}
                    onChange={e => setUsername(e.target.value)}
                    placeholder="nombre de usuario"
                    className="login-input"
                    disabled={loading}
                  />
                </div>
              </div>

              <div className="login-field">
                <label className="login-label" htmlFor="password">Contraseña</label>
                <div className="login-input-wrap">
                  <svg className="login-field-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
                    <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
                  </svg>
                  <input
                    id="password"
                    type="password"
                    autoComplete="current-password"
                    value={password}
                    onChange={e => setPassword(e.target.value)}
                    placeholder="contraseña"
                    className="login-input"
                    disabled={loading}
                  />
                </div>
              </div>

              <button type="submit" className="login-submit" disabled={loading}>
                {loading ? (
                  <>
                    <div className="login-spinner" />
                    Autenticando...
                  </>
                ) : (
                  'Iniciar Sesión'
                )}
              </button>
            </form>

            <div className="login-card-footer">
              <p>OpenCEO © Jardal</p>
            </div>
          </div>
        </div>
      </div>
    </>
  )
}

export default Login
