import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('/node_modules/react-pdf/') || id.includes('/node_modules/pdfjs-dist/')) {
            return 'pdf'
          }
          if (id.includes('/node_modules/antd/') || id.includes('/node_modules/@ant-design/')) {
            return 'ui'
          }
          if (id.includes('/node_modules/react-markdown/') || id.includes('/node_modules/remark-gfm/')) {
            return 'markdown'
          }
          if (id.includes('/node_modules/react/') || id.includes('/node_modules/react-dom/')) {
            return 'vendor'
          }
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.js',
    clearMocks: true,
  },
})
