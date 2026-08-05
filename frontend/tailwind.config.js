/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Dark-first palette. The UI ships dark; these are the only surfaces.
        base: '#09090b',
        surface: '#121215',
        elevated: '#18181b',
        line: '#27272a',
        muted: '#a1a1aa',
        subtle: '#71717a',
        accent: {
          DEFAULT: '#8b5cf6',
          soft: '#a78bfa',
          dim: '#6d28d9',
        },
      },
      fontFamily: {
        sans: ['Inter var', 'Inter', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
      },
      keyframes: {
        'fade-in': {
          from: { opacity: '0', transform: 'translateY(4px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        bar: {
          '0%, 100%': { transform: 'scaleY(0.3)' },
          '50%': { transform: 'scaleY(1)' },
        },
      },
      animation: {
        'fade-in': 'fade-in 0.2s ease-out',
        bar: 'bar 1s ease-in-out infinite',
      },
    },
  },
  plugins: [],
}
