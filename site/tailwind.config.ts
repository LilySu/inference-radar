import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './lib/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: '#0a0c10',
        panel: '#11141c',
        border: '#1f2937',
        accent: {
          green: '#34d399',
          red: '#f87171',
          purple: '#a78bfa',
          amber: '#fbbf24',
          sky: '#38bdf8',
          slate: '#64748b',
        },
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'ui-monospace', 'monospace'],
        display: ['Syne', 'sans-serif'],
      },
    },
  },
  plugins: [],
};
export default config;
