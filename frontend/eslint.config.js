// Flat config. Deliberately a correctness gate, not a style one.
//
// The suite and the Vite build both pass on code with a stale useEffect
// dependency list, a hook behind a condition, or a component declared inside
// another component -- bugs that surface as a panel that quietly stops
// updating, or a form that loses what you typed. That class is what this file
// is for.
//
// No formatting rules and no stylistic plugin: reformatting 8k lines of working
// JSX would bury every real change in whitespace, and Prettier is not installed.
import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";

// eslint-plugin-react-hooks 7 ships two kinds of rule in one preset. The
// rules-of-hooks family describes React itself and is enabled below. The rest
// -- purity, refs, immutability, set-state-in-effect, and the manual-
// memoization rules -- describe what the React Compiler needs in order to
// memoize safely, and this project does not run the compiler.
//
// They are off rather than silenced case by case, because each one fires on a
// deliberate, documented pattern here: the latest-ref idiom in usePoll and
// useLiveRefresh, `Date.now()` in a render that formats "N days deep", and the
// effects that reset a list when its filters change. Turning them on would mean
// rewriting working code to satisfy an optimizer nothing here uses. Revisit the
// whole block if the compiler is ever adopted -- that is the point at which the
// advice becomes load-bearing.
const COMPILER_RULES_OFF = {
  "react-hooks/purity": "off",
  "react-hooks/refs": "off",
  "react-hooks/immutability": "off",
  "react-hooks/set-state-in-effect": "off",
  "react-hooks/use-memo": "off",
  "react-hooks/void-use-memo": "off",
  "react-hooks/preserve-manual-memoization": "off",
  "react-hooks/incompatible-library": "off",
  "react-hooks/unsupported-syntax": "off",
  "react-hooks/config": "off",
  "react-hooks/gating": "off",
};

export default [
  {
    ignores: ["dist/**", "coverage/**", "node_modules/**"],
  },
  js.configs.recommended,
  reactHooks.configs.flat["recommended-latest"],
  {
    files: ["**/*.{js,jsx}"],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: "module",
      globals: { ...globals.browser, ...globals.es2024 },
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    rules: {
      ...COMPILER_RULES_OFF,
      // A stale dependency list is a real bug -- the Feed search box used to
      // revert a filter chip clicked while a debounce was pending, and this is
      // the rule that names it. A warning rather than an error: some of the
      // remaining ones are load-bearing suppressions with their reason written
      // next to them, and a red CI on those would only teach people to disable
      // the rule wholesale.
      "react-hooks/exhaustive-deps": "warn",
      // An empty catch is how this codebase spells "best effort" (see api.js
      // and auth.jsx); every one carries a comment saying so.
      "no-empty": ["error", { allowEmptyCatch: true }],
      "no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrors: "none" },
      ],
    },
  },
  {
    // Vitest injects describe/it/expect/vi through `globals: true`.
    files: ["**/*.test.{js,jsx}", "src/test/**"],
    languageOptions: {
      globals: { ...globals.node, ...globals.vitest },
    },
  },
  {
    // Build-time config runs in Node, where `process` exists.
    files: ["*.config.js"],
    languageOptions: { globals: { ...globals.node } },
  },
];
