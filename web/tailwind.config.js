/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: '#e2e8f0',
        panel: '#131920',
        line: '#232e3c',
        moss: '#22c55e',
        berry: '#a78bfa',
        amber: '#f59e0b',
        cyan: '#2dd4bf',
      },
      boxShadow: {
        soft: '0 12px 30px rgba(0, 0, 0, 0.3)',
      },
    },
  },
  plugins: [],
};
