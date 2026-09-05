import type { NextConfig } from "next";

// Where the FastAPI backend listens. Hardcoding this meant the UI could only
// ever talk to a local process, so any deployment needed a source edit.
const BACKEND_URL = process.env.BACKEND_URL || "http://127.0.0.1:8000";
const isDevelopment = process.env.NODE_ENV === "development";

// The S3 build. Opt-in via `npm run build:static` rather than always-on,
// because a static export cannot serve rewrites: turning it on unconditionally
// would silently remove the /backend proxy that `npm run dev` depends on.
// In the deployed stack CloudFront does that routing instead, which is why
// `API = "/backend"` in lib/api.ts is correct in both worlds.
const isStaticExport = process.env.NEXT_OUTPUT === "export";

// The dev/`next start` proxy. Omitted entirely from the static export rather
// than returning an empty list: Next detects the *presence* of the rewrites
// key, so leaving it in place warns "rewrites ... are not applied when
// exporting" on every export, which reads like a misconfiguration when it is
// the intended build.
const proxyRewrites = {
  async rewrites() {
    return [
      {
        source: "/backend/:path*",
        destination: `${BACKEND_URL}/:path*`,
      },
    ];
  },
};

const nextConfig: NextConfig = {
  ...(isStaticExport ? { output: "export" as const } : proxyRewrites),
  devIndicators: false,
  // Keep dev artifacts separate from production builds. Running `next build`
  // while `next dev` is open otherwise replaces shared chunks underneath the
  // dev server and produces misleading "Cannot find module './NNN.js'"
  // runtime errors in the browser.
  distDir: isDevelopment ? ".next-dev" : ".next",
  // The app is commonly opened as localhost, while the embedded browser and
  // backend health checks may address the same dev server through 127.0.0.1.
  // Treat both loopback hostnames as trusted development origins so Next does
  // not report its own asset requests as cross-origin issues.
  allowedDevOrigins: ["localhost", "127.0.0.1"],
};

export default nextConfig;
