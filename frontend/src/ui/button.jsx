import { cva } from "class-variance-authority";
import { cn } from "@/lib/cn";

// Near-square, not pill: this is panel hardware. `ghost` is the workhorse --
// most controls in an instrument are unfilled, and the one filled button per
// view is what tells you where the primary action is.
const button = cva(
  "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-sm font-medium " +
    "transition-colors disabled:pointer-events-none disabled:opacity-40",
  {
    variants: {
      variant: {
        primary: "bg-accent text-accent-ink hover:bg-accent/85",
        ghost: "border border-line text-fg-muted hover:border-line-strong hover:text-fg",
        quiet: "text-fg-muted hover:text-fg",
        danger: "border border-danger/40 text-danger hover:bg-danger/10",
      },
      size: {
        sm: "h-7 px-2.5 text-xs",
        md: "h-8 px-3 text-sm",
        lg: "h-10 px-4 text-base",
        icon: "h-8 w-8",
      },
    },
    defaultVariants: { variant: "ghost", size: "md" },
  },
);

export function Button({ className, variant, size, type = "button", ...props }) {
  return <button type={type} className={cn(button({ variant, size }), className)} {...props} />;
}

export { button as buttonVariants };
