/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Light Mode Base
        gray: {
          50: '#f9fafb',
          100: '#f3f4f6',
          200: '#e5e7eb',
          300: '#d1d5db',
          400: '#9ca3af',
          500: '#6b7280',
          600: '#4b5563',
          700: '#374151',
          800: '#1f2937',
          900: '#111827',
          950: '#030712',
        },
        // Primary Accents
        primary: {
          50: '#eff6ff',
          100: '#dbeafe',
          200: '#bfdbfe',
          300: '#93c5fd',
          400: '#60a5fa',
          500: '#3b82f6',
          600: '#2563eb',
          700: '#1d4ed8',
          800: '#1e40af',
          900: '#1e3a8a',
          950: '#172554',
          neon: '#00f0ff',
        },
        // Secondary Accents
        secondary: {
          50: '#f5f3ff',
          100: '#ede9fe',
          200: '#ddd6fe',
          300: '#c4b5fd',
          400: '#a78bfa',
          500: '#8b5cf6',
          600: '#7c3aed',
          700: '#6d28d9',
          800: '#5b21b6',
          900: '#4c1d95',
          950: '#2e1065',
          neon: '#b026ff',
        },
        glass: {
          100: 'rgba(255, 255, 255, 0.1)',
          200: 'rgba(255, 255, 255, 0.2)',
          300: 'rgba(255, 255, 255, 0.3)',
          dark: 'rgba(0, 0, 0, 0.3)',
          'dark-hover': 'rgba(0, 0, 0, 0.4)',
        }
      },
      fontFamily: {
        sans: ['Inter', 'SF Pro Display', 'system-ui', 'sans-serif'],
      },
      boxShadow: {
        'glass': '0 8px 32px 0 rgba(31, 38, 135, 0.07)',
        'glass-hover': '0 8px 32px 0 rgba(31, 38, 135, 0.15)',
        'neon-blue': '0 0 10px rgba(0, 240, 255, 0.5), 0 0 20px rgba(0, 240, 255, 0.3)',
        'neon-purple': '0 0 10px rgba(176, 38, 255, 0.5), 0 0 20px rgba(176, 38, 255, 0.3)',
        'neumorphic': '20px 20px 60px #d1d1d1, -20px -20px 60px #ffffff',
        'neumorphic-dark': '20px 20px 60px #0b0b0b, -20px -20px 60px #1e1e1e',
      },
      backdropBlur: {
        'xs': '2px',
      },
      animation: {
        'blob': 'blob 20s infinite',
        'squash': 'squash 0.2s ease-in-out',
      },
      keyframes: {
        blob: {
          "0%": {
            transform: "translate(0vw, 0vh) scale(1)",
          },
          "25%": {
            transform: "translate(80vw, -20vh) scale(1.2)",
          },
          "50%": {
            transform: "translate(-20vw, 50vh) scale(0.9)",
          },
          "75%": {
            transform: "translate(50vw, 20vh) scale(1.1)",
          },
          "100%": {
            transform: "translate(0vw, 0vh) scale(1)",
          },
        },
        squash: {
          '0%, 100%': { transform: 'scale(1)' },
          '50%': { transform: 'scale(0.95)' },
        }
      }
    },
  },
  plugins: [],
}
