import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        // r!z3 pastel theme
        'r1z3-beige': '#FAF8F5',
        'r1z3-peach': '#FFD8BE',
        'r1z3-matcha': '#D8E2DC',
        'r1z3-lavender': '#E8E8E4',
        'r1z3-dark': '#2D2D2D',
        'r1z3-text': '#4A4A4A',
      },
    },
  },
  plugins: [],
};
export default config;
