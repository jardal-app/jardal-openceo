import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const devPort = Number(env.VITE_DEV_SERVER_PORT || 3000)
  const allowedHosts = ['dev.openceo.jardal.app', 'api.openceo.jardal.app']
  if (env.VITE_FRONTEND_URL) {
    try {
      const hostname = new URL(env.VITE_FRONTEND_URL).hostname
      if (hostname && !allowedHosts.includes(hostname)) allowedHosts.push(hostname)
    } catch {}
  }
  const hmr = env.VITE_HMR_HOST ? {
    host: env.VITE_HMR_HOST,
    protocol: env.VITE_HMR_PROTOCOL || 'wss',
    clientPort: Number(env.VITE_HMR_CLIENT_PORT || 443),
  } : undefined

  return {
    plugins: [
      react(),
      tailwindcss(),
    ],
    build: {
      chunkSizeWarningLimit: 1500,
      sourcemap: false,
      target: 'ES2020',
      rollupOptions: {
        output: {
          manualChunks: {
            vendor: ['react', 'react-dom', 'react-router-dom'],
          },
        },
      },
    },
    server: {
      host: true,
      port: devPort,
      strictPort: true,
      allowedHosts,
      origin: env.VITE_DEV_SERVER_ORIGIN || undefined,
      hmr,
    },
    resolve: {
      alias: [
        { find: '@', replacement: path.resolve(__dirname, 'src') },
      ],
    },
  }
})
