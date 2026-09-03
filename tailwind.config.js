/** Tailwind configuration.
 *
 * The palette is not a style choice. Spec §12.2 fixes the tokens and §12.1
 * principle 5 makes colour semantic: red always means act now, amber means
 * watch, green means safe, grey means no data or not actionable. The same hue
 * must never mean two things, because a blood bank officer reads these screens
 * for four minutes a day and pattern-matches by colour.
 */

/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./web/templates/**/*.html",
    "./web/**/*.py",
  ],
  theme: {
    extend: {
      colors: {
        // Brand — deep clinical red, used sparingly (§12.2)
        brand: {
          50: "#fdf2f4",
          100: "#fbe4e8",
          200: "#f4d3d9",
          300: "#e8a3b0",
          400: "#d75f76",
          500: "#b3122b",
          600: "#a01026",
          700: "#8e0e22",
          800: "#6f0b1a",
          900: "#560814",
        },
        // Semantic status
        critical: {
          50: "#fdecea",
          100: "#fbdbd7",
          200: "#f7ccc7",
          500: "#d32f2f",
          600: "#c62828",
          700: "#b71c1c",
        },
        warn: {
          50: "#fff5e9",
          100: "#feebd2",
          200: "#fbe0bd",
          500: "#f57c00",
          600: "#e06f00",
          700: "#b35c00",
        },
        safe: {
          50: "#eef7ef",
          100: "#dcefdd",
          200: "#c9e3cb",
          500: "#2e7d32",
          600: "#276b2b",
          700: "#1f5623",
        },
        info: {
          50: "#eef4fc",
          100: "#dbe8f8",
          200: "#c6dbf4",
          500: "#1565c0",
          600: "#125aad",
          700: "#0d4386",
        },
        // Neutral ramp tuned for a dense operational screen on a light surface
        ink: {
          DEFAULT: "#16202b",
          secondary: "#4a5a68",
          muted: "#7c8b98",
          faint: "#9fadb9",
        },
        surface: {
          DEFAULT: "#ffffff",
          sunken: "#f2f4f7",
          page: "#f7f8fa",
        },
        line: {
          DEFAULT: "#e3e6ea",
          strong: "#cfd5dc",
        },
        // Fixed colour per blood group, app-wide, so users match by colour
        group: {
          "o-pos": "#c62828",
          "o-neg": "#8e0e22",
          "a-pos": "#1565c0",
          "a-neg": "#0d3f78",
          "b-pos": "#2e7d32",
          "b-neg": "#17501a",
          "ab-pos": "#6a1b9a",
          "ab-neg": "#421061",
        },
      },
      fontFamily: {
        sans: [
          "ui-sans-serif",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        mono: ["IBM Plex Mono", "SF Mono", "ui-monospace", "monospace"],
        urdu: [
          "Noto Nastaliq Urdu",
          "Noto Naskh Arabic",
          "Geeza Pro",
          "Urdu Typesetting",
          "serif",
        ],
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
        xs: ["0.78125rem", { lineHeight: "1.125rem" }],
        sm: ["0.875rem", { lineHeight: "1.375rem" }],
        base: ["0.9375rem", { lineHeight: "1.5rem" }],
      },
      borderRadius: {
        // §12.2: 8px radius
        DEFAULT: "0.5rem",
        lg: "0.625rem",
        xl: "0.875rem",
      },
      boxShadow: {
        // §12.2: a single soft elevation, no heavy drop shadows
        card: "0 1px 2px rgba(16, 24, 40, 0.06), 0 1px 3px rgba(16, 24, 40, 0.04)",
        raised: "0 4px 12px rgba(16, 24, 40, 0.08)",
        pop: "0 12px 32px rgba(16, 24, 40, 0.14)",
        none: "none",
      },
      transitionTimingFunction: {
        smooth: "cubic-bezier(0.4, 0, 0.2, 1)",
      },
      keyframes: {
        "fade-in": {
          from: { opacity: "0", transform: "translateY(2px)" },
          to: { opacity: "1", transform: "none" },
        },
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
      },
      animation: {
        "fade-in": "fade-in 160ms cubic-bezier(0.4, 0, 0.2, 1)",
        shimmer: "shimmer 1.4s infinite",
      },
    },
  },
  plugins: [require("@tailwindcss/forms"), require("@tailwindcss/typography")],
};
