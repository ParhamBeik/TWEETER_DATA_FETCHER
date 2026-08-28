import { clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** Merge class lists, letting a caller's utility win over a component default. */
export function cn(...inputs) {
  return twMerge(clsx(inputs));
}
