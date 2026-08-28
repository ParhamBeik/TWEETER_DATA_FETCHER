import * as Primitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/cn";
import { Button } from "@/ui/button";

// Radix rather than a hand-rolled modal: focus trapping, restore-on-close,
// Escape, scroll lock and the aria wiring are all things a dialog has to get
// right and none of them are the interesting part of this app.
export const Dialog = Primitive.Root;
export const DialogTrigger = Primitive.Trigger;
export const DialogClose = Primitive.Close;

export function DialogContent({ title, description, className, children }) {
  return (
    <Primitive.Portal>
      <Primitive.Overlay className="fixed inset-0 z-40 bg-ink-900/80 backdrop-blur-sm" />
      <Primitive.Content
        className={cn(
          "fixed left-1/2 top-1/2 z-50 w-[min(38rem,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2",
          "max-h-[calc(100vh-4rem)] overflow-y-auto rounded-md border border-line-strong bg-ink-800",
          className,
        )}
      >
        <div className="flex items-start justify-between gap-4 border-b border-line px-4 py-3">
          <div>
            <Primitive.Title className="text-md font-semibold tracking-tight">
              {title}
            </Primitive.Title>
            {description && (
              <Primitive.Description className="mt-1 text-xs text-fg-muted">
                {description}
              </Primitive.Description>
            )}
          </div>
          <Primitive.Close asChild>
            <Button variant="quiet" size="icon" aria-label="Close">
              <X className="size-4" />
            </Button>
          </Primitive.Close>
        </div>
        <div className="p-4">{children}</div>
      </Primitive.Content>
    </Primitive.Portal>
  );
}

export function DialogFooter({ className, ...props }) {
  return (
    <div
      className={cn("mt-5 flex items-center justify-end gap-2 border-t border-line pt-4", className)}
      {...props}
    />
  );
}
