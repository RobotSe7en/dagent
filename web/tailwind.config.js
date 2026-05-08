/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: '#1a1d24',
        panel: '#ffffff',
        line: '#e2e4ea',
        moss: '#16a34a',
        berry: '#7c3aed',
        amber: '#d97706',
        cyan: '#0ea5a5',
      },
      boxShadow: {
        soft: '0 2px 8px rgba(0, 0, 0, 0.06)',
      },
    },
  },
  plugins: [],
};
