/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#0b1020",
        surface: "#121a2d",
        line: "#26334d",
        accent: "#6ee7d2",
      },
    },
  },
  plugins: [],
};
