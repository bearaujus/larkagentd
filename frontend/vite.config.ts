import { defineConfig } from "vite";

export default defineConfig({
  plugins: [
    {
      name: "local-preview-request-target",
      configurePreviewServer(server) {
        // Firefox interception can use HTTP absolute-form request targets.
        // Production Gateway uses urlsplit already; normalize only the local preview.
        server.middlewares.use((req, _res, next) => {
          if (req.url?.startsWith("http://127.0.0.1:4173/")) {
            const url = new URL(req.url);
            req.url = url.pathname + url.search;
          }
          next();
        });
      },
    },
  ],
  base: "/dashboard/",
  // Never load the application's private root .env into a frontend build.
  envDir: false,
  build: {
    outDir: "../src/larkagentd/gateway/static",
    emptyOutDir: true,
    sourcemap: false,
    rolldownOptions: {
      onwarn(warning, warn) {
        // This is a browser-only application; dependency RSC directives are inert.
        if (warning.code === "MODULE_LEVEL_DIRECTIVE") return;
        warn(warning);
      },
      output: {
        codeSplitting: {
          groups: [
            {
              name: "charts",
              test: /node_modules\/(recharts|d3-|victory|@reduxjs|react-redux)/,
            },
          ],
        },
      },
    },
  },
});
