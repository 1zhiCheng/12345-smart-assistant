/** @type {import('next').NextConfig} */
const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";

const nextConfig = {
  output: "standalone",
  experimental: {
    // 本机 CPU 转写长录音后还会进行角色分离和工单整理；
    // 保持代理连接 10 分钟，避免浏览器把尚在运行的转写误报为 Failed to fetch。
    proxyTimeout: 600000,
  },
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${BACKEND_URL}/api/:path*` },
      { source: "/healthz", destination: `${BACKEND_URL}/healthz` },
      { source: "/metrics", destination: `${BACKEND_URL}/metrics` },
    ];
  },
};

export default nextConfig;
