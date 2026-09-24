import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev: API on :8000. Prod: the FastAPI app serves frontend/dist itself.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
      '/files': 'http://localhost:8000',
    },
  },
})
