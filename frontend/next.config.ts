import type { NextConfig } from "next";

/** Django API origin as seen from the Next.js server (Docker service name in compose, localhost in dev). */
const API_INTERNAL_URL = (process.env.API_INTERNAL_URL || "http://localhost:8000").replace(/\/+$/, "");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // Django routes have no trailing slash; never let Next add or strip one on proxied paths.
  skipTrailingSlashRedirect: true,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API_INTERNAL_URL}/api/:path*` },
      { source: "/healthz", destination: `${API_INTERNAL_URL}/healthz` },
    ];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
};

export default nextConfig;
