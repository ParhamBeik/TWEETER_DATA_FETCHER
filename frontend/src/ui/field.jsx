import { forwardRef, useId } from "react";
import { cn } from "@/lib/cn";

const control =
  "w-full rounded-sm border border-line bg-ink-850 px-2.5 py-1.5 text-sm text-fg " +
  "placeholder:text-fg-dim hover:border-line-strong focus:border-accent focus:outline-none";

// forwardRef because callers focus it directly -- the account picker moves focus
// into its filter box when the popover opens. React 18 does not pass `ref`
// through as an ordinary prop.
export const Input = forwardRef(function Input({ className, ...props }, ref) {
  return <input ref={ref} className={cn(control, className)} {...props} />;
});

export function Textarea({ className, ...props }) {
  return <textarea className={cn(control, "font-mono text-xs leading-5", className)} {...props} />;
}

export function Select({ className, children, ...props }) {
  // Native select on purpose: two call sites do not justify shipping a listbox
  // implementation, and the platform one is already keyboard- and screen-reader
  // correct on every browser this runs in.
  return (
    <select className={cn(control, "appearance-none pr-6", className)} {...props}>
      {children}
    </select>
  );
}

/**
 * Label + control + hint, wired together by id.
 *
 * `hint` is where a control says what it will actually do -- the search form is
 * full of knobs (depth, rolling window, interval) whose effect on a run is not
 * guessable from their name, and an unexplained knob is one nobody touches.
 */
export function Field({ label, hint, error, children, className }) {
  const id = useId();
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <label htmlFor={id} className="eyebrow">
        {label}
      </label>
      {children({ id, "aria-describedby": hint ? `${id}-hint` : undefined })}
      {hint && (
        <p id={`${id}-hint`} className="text-xs text-fg-dim">
          {hint}
        </p>
      )}
      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  );
}
