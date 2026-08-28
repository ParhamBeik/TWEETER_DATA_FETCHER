import * as Primitive from "@radix-ui/react-tabs";
import { cn } from "@/lib/cn";

export const Tabs = Primitive.Root;
export const TabPanel = Primitive.Content;

export function TabList({ className, ...props }) {
  return (
    <Primitive.List
      className={cn("flex items-center gap-1 border-b border-line", className)}
      {...props}
    />
  );
}

/** Underlined rather than filled: a tab marks position, it is not an action. */
export function Tab({ className, ...props }) {
  return (
    <Primitive.Trigger
      className={cn(
        "-mb-px border-b-2 border-transparent px-3 py-2 text-sm font-medium text-fg-muted",
        "transition-colors hover:text-fg data-[state=active]:border-accent data-[state=active]:text-fg",
        className,
      )}
      {...props}
    />
  );
}
