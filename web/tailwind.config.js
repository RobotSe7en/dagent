/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: '#172029',
        panel: '#f7f8f5',
        line: '#d9ded7',
        moss: '#5e7f67',
        berry: '#8b4f6f',
        amber: '#c18b3b',
        cyan: '#327a8d',
      },
      boxShadow: {
        soft: '0 12px 30px rgba(23, 32, 41, 0.08)',
      },
    },
  },
  plugins: [],
};

