import { useState } from "react";
import { Button } from "@/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/ui/dialog";
import { Field, Input } from "@/ui/field";
import { ErrorNote } from "@/ui/controls";
import { compact } from "@/format";

/**
 * Typed confirmation for deleting a search.
 *
 * The list is not boilerplate: deleting a query really does take its schedule,
 * its queued run, its stored results, its run history and its pagination cursors
 * with it, and none of that is recoverable. Naming each one is the difference
 * between a warning and an informed decision.
 */
export default function DeleteDialog({ open, onOpenChange, search, onConfirm }) {
  const [typed, setTyped] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const name = search?.name || search?.slug || "";

  async function confirm() {
    setBusy(true);
    setError("");
    try {
      await onConfirm();
      onOpenChange(false);
      setTyped("");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next);
        if (!next) setTyped("");
      }}
    >
      <DialogContent
        title={`Delete "${name}"`}
        description="This removes the whole job behind the query, not just the saved row."
      >
        <ul className="flex flex-col gap-1.5 text-sm text-fg-muted">
          {[
            "The schedule — it stops running",
            "Any run already waiting in the queue",
            `${compact(search?.hit_count || 0)} stored result${search?.hit_count === 1 ? "" : "s"}`,
            "Its whole run history",
            "Its pagination cursors and saved raw pages",
          ].map((line) => (
            <li key={line} className="annunciator border-l-danger/50">
              {line}
            </li>
          ))}
        </ul>

        <p className="mt-4 text-xs text-fg-dim">
          Results that another saved search also matched are kept for that search.
        </p>

        <Field
          className="mt-4"
          label={`Type the name to confirm`}
          hint={`Enter "${name}" exactly.`}
        >
          {(props) => (
            <Input
              {...props}
              value={typed}
              autoComplete="off"
              onChange={(e) => setTyped(e.target.value)}
            />
          )}
        </Field>

        {error && <ErrorNote className="mt-3">{error}</ErrorNote>}

        <DialogFooter>
          <Button variant="quiet" onClick={() => onOpenChange(false)}>
            Keep it
          </Button>
          <Button variant="danger" disabled={typed !== name || busy} onClick={confirm}>
            Delete search
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
