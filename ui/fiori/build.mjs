// Bundle the UI5 Web Components into one offline file for the fallback UI.
import { build } from "esbuild";

await build({
  entryPoints: ["components.js"],
  bundle: true,
  format: "esm",
  minify: true,
  target: "es2021",
  outfile: "../fallback/assets/ui5.js",
  legalComments: "none",
  logLevel: "info",
});
