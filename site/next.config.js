/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'export',
  images: { unoptimized: true },
  trailingSlash: true,
  // Vercel will pick this up automatically; for non-Vercel hosts it produces
  // a static `out/` directory after `next build`.
};

module.exports = nextConfig;
