import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        mono: ['"JetBrains Mono"', "ui-monospace", "Menlo", "monospace"],
        editorial: ['"Instrument Serif"', "Georgia", "serif"],
      },
      colors: {
        // CRT/research-instrument palette. Committed dark.
        ink: {
          950: "#08080a",
          900: "#0a0a0b",
          800: "#0f0f12",
          700: "#131316",
          600: "#1a1a1f",
          500: "#26262c",
          400: "#3a3a42",
        },
        bone: {
          50: "#f5f5f0",
          100: "#ecece4",
          300: "#a9a9af",
          400: "#7a7a82",
          500: "#5a5a62",
        },
        crt: {
          amber: "#ffb800",
          long: "#00d68f",
          short: "#ff4d6d",
          info: "#5ad8ff",
          violet: "#b08cff",
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
