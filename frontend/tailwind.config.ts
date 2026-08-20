import type { Config } from "tailwindcss";

export default {
  // Matches how the app actually switches themes: src/theme.ts writes
  // data-theme="dark"/"light" on <html> so an explicit choice can override the
  // OS in both directions. Leaving this as "media" would mean any future
  // `dark:` utility silently followed the OS and ignored the user's choice —
  // a trap, since there are no `dark:` utilities today to make it visible.
  darkMode: ['selector', '[data-theme="dark"]'],
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
