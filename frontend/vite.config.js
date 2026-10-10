import { defineConfig } from "vite";
export default defineConfig({
  base: "./",
  build: {
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        chunkFileNames: "assets/[name]-[hash].js",
        entryFileNames: "app.js",
        assetFileNames: (asset) =>
          asset.names?.some((n) => n.endsWith(".css"))
            ? "style.css"
            : "[name][extname]",
      },
    },
  },
});
