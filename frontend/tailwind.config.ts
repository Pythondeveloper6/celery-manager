import type { Config } from "tailwindcss";

export default {
  darkMode: "media",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  // Preflight is disabled: the design system in src/styles/tokens.css owns
  // the base reset and typography so Tailwind's generic defaults never leak
  // into the visual identity. Utility classes stay available for layout
  // work in later dashboard screens (Tasks 13-15).
  corePlugins: {
    preflight: false,
  },
  theme: {
    extend: {},
  },
  plugins: [],
} satisfies Config;
