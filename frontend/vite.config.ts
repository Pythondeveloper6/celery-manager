import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // Relative asset URLs (./assets/...): the same build works at the root and
  // when the dashboard is mounted under a prefix inside another app.
  base: './',
})
