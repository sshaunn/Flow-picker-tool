import { defineConfig } from 'vite'
import { resolve } from 'node:path'
import { copyFileSync, mkdirSync, readdirSync, existsSync } from 'node:fs'

// Spike: vanilla Vite multi-entry build (no @crxjs/vite-plugin yet —
// fewer moving parts while we validate end-to-end). manifest.json and
// static popup HTML are copied verbatim into dist/.
export default defineConfig({
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'esnext',
    minify: false,
    sourcemap: true,
    rollupOptions: {
      input: {
        background: resolve(__dirname, 'src/background.ts'),
        content: resolve(__dirname, 'src/content/flow_dom.ts'),
        popup: resolve(__dirname, 'src/popup/popup.ts'),
      },
      output: {
        entryFileNames: '[name].js',
        chunkFileNames: 'chunks/[name]-[hash].js',
        assetFileNames: '[name][extname]',
        format: 'es',
        inlineDynamicImports: false,
      },
      // Each MV3 entry must be self-contained — chrome can't share
      // chunks across the service worker / content / popup contexts.
      preserveEntrySignatures: 'strict',
    },
  },
  plugins: [
    {
      name: 'copy-static-assets',
      closeBundle() {
        const distDir = resolve(__dirname, 'dist')
        if (!existsSync(distDir)) mkdirSync(distDir, { recursive: true })

        // manifest.json → dist root
        copyFileSync(
          resolve(__dirname, 'manifest.json'),
          resolve(distDir, 'manifest.json'),
        )

        // popup.html → dist root (popup.js loaded from same level)
        copyFileSync(
          resolve(__dirname, 'src/popup/popup.html'),
          resolve(distDir, 'popup.html'),
        )

        // icons/* → dist/icons/
        const iconSrc = resolve(__dirname, 'icons')
        const iconDst = resolve(distDir, 'icons')
        if (existsSync(iconSrc)) {
          mkdirSync(iconDst, { recursive: true })
          for (const f of readdirSync(iconSrc)) {
            copyFileSync(resolve(iconSrc, f), resolve(iconDst, f))
          }
        }
      },
    },
  ],
})
