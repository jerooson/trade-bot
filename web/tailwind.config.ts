import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Inter"', '"Segoe UI"', "system-ui", "sans-serif"],
        mono: ['"SFMono-Regular"', "Consolas", "ui-monospace", "monospace"],
        editorial: ['"Inter"', '"Segoe UI"', "system-ui", "sans-serif"],
      },
      colors: {
        // CRT/research-instrument palette. Committed dark.
        ink: {
          950: "#070b14",
          900: "#0b1120",
          800: "#111a2d",
          700: "#172238",
          600: "#1e2c45",
          500: "#2b3b58",
          400: "#425574",
        },
        bone: {
          50: "#f7f9fc",
          100: "#e9eef7",
          300: "#a9b7cb",
          400: "#7f90aa",
          500: "#60718c",
          600: "#475872",
        },
        crt: {
          amber: "#f4b860",
          long: "#45d6a2",
          short: "#ff7185",
          info: "#65b8ff",
          violet: "#a78bfa",
        },
      },
      boxShadow: {
        glow: "0 0 0 1px rgba(255,184,0,0.35), 0 0 24px -8px rgba(255,184,0,0.45)",
        glowGreen: "0 0 0 1px rgba(0,214,143,0.30), 0 0 18px -8px rgba(0,214,143,0.40)",
      },
      keyframes: {
        scan: {
          "0%": { transform: "translateY(-100%)" },
          "100%": { transform: "translateY(100%)" },
        },
        pulseDot: {
          "0%, 100%": { opacity: "1", transform: "scale(1)" },
          "50%": { opacity: "0.55", transform: "scale(0.92)" },
        },
        slideIn: {
          from: { opacity: "0", transform: "translateY(-6px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        scan: "scan 8s linear infinite",
        pulseDot: "pulseDot 1.5s ease-in-out infinite",
        slideIn: "slideIn 240ms ease-out",
      },
    },
  },
  plugins: [],
} satisfies Config;
