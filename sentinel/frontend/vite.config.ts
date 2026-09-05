import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    // Proxy in dev so the browser sees one origin and CORS never enters
    // the picture. In production nginx does the same job.
    proxy: {
      '/api': { target: process.env.VITE_API_URL || 'http://localhost:8000', changeOrigin: true },
      '/ws': { target: process.env.VITE_API_URL || 'http://localhost:8000', ws: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    rollupOptions: {
      output: {
        // MapLibre is ~800 KB on its own. Splitting it keeps the initial
        // command-centre payload small; the map only loads when an
        // operator opens a map page.
        manualChunks: {
          maplibre: ['maplibre-gl'],
          charts: ['recharts'],
          vendor: ['react', 'react-dom', 'react-router-dom'],
        },
      },
    },
  },
})
