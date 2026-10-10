import { defineConfig } from "vite";
export default defineConfig({
  base: "./",
  build: {
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
        entryFileNames: "app.js",
        assetFileNames: (asset) =>
          asset.names?.some((n) => n.endsWith(".css"))
            ? "style.css"
            : "[name][extname]",
      },
    },
  },
});
