import { useState } from "react";
import { Button } from "@/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/ui/dialog";
import { Field, Input, Select, Textarea } from "@/ui/field";
import { Segmented } from "@/ui/controls";
import { ErrorNote } from "@/ui/controls";
import {
  DEPTHS,
  HINTS,
  INTERVALS,
  PRODUCTS,
  PRODUCT_HINT,
  ROLLING_HOURS,
  buildQuery,
} from "./query";

const BLANK = {
  name: "",
  product: "Top",
  pagination_depth: 1,
  rolling_hours: 24,
  interval_seconds: 1800,
  raw_query: "",
};

const BLANK_BUILDER = { terms: "", from: "", language: "", minFaves: "", since: "", until: "" };

/**
 * Create or edit a saved search.
 *
 * Two ways in, one output: the guided fields write the operator syntax, and the
 * query box below shows exactly what will be sent and stays editable. Neither is
 * a wrapper around the other -- typing in the box is how you use an operator the
 * builder does not cover.
 */
export default function QueryDialog({ open, onOpenChange, search, onSubmit, saving }) {
  const editing = Boolean(search);
  const [form, setForm] = useState(() => (search ? { ...BLANK, ...search } : BLANK));
  const [builder, setBuilder] = useState(BLANK_BUILDER);
  const [error, setError] = useState("");

  const set = (patch) => setForm((current) => ({ ...current, ...patch }));

  function applyBuilder(patch) {
    const next = { ...builder, ...patch };
    setBuilder(next);
    set({ raw_query: buildQuery(next) });
  }

  async function submit(event) {
    event.preventDefault();
    setError("");
    try {
      await onSubmit({
        ...form,
        pagination_depth: Number(form.pagination_depth),
        rolling_hours: Number(form.rolling_hours),
        interval_seconds: Number(form.interval_seconds),
      });
      onOpenChange(false);
      setForm(BLANK);
      setBuilder(BLANK_BUILDER);
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title={editing ? "Edit search" : "New search"}
        description={
          editing
            ? "Changes take effect on the next run. Results already collected are kept."
            : "This runs once immediately, then keeps running on the schedule you set."
        }
      >
        <form onSubmit={submit} className="flex flex-col gap-5">
          {!editing && (
            <fieldset className="flex flex-col gap-3 rounded-sm border border-line p-3">
              <legend className="eyebrow px-1">Build the query</legend>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Words or phrases">
                  {(props) => (
                    <Input
                      {...props}
                      placeholder="gold OR bullion"
                      value={builder.terms}
                      onChange={(e) => applyBuilder({ terms: e.target.value })}
                    />
                  )}
                </Field>
                <Field label="From accounts" hint="Handles, comma separated.">
                  {(props) => (
                    <Input
                      {...props}
                      placeholder="@reuters, @business"
                      value={builder.from}
                      onChange={(e) => applyBuilder({ from: e.target.value })}
                    />
                  )}
                </Field>
                <Field label="Language" hint="Two-letter code, e.g. en or fa.">
                  {(props) => (
                    <Input
                      {...props}
                      placeholder="en"
                      value={builder.language}
                      onChange={(e) => applyBuilder({ language: e.target.value })}
                    />
                  )}
                </Field>
                <Field label="Minimum likes" hint="Filters out the long tail.">
                  {(props) => (
                    <Input
                      {...props}
                      type="number"
                      min="0"
                      placeholder="0"
                      value={builder.minFaves}
                      onChange={(e) => applyBuilder({ minFaves: e.target.value })}
                    />
                  )}
                </Field>
                <Field label="Posted after">
                  {(props) => (
                    <Input
                      {...props}
                      type="date"
                      value={builder.since}
                      onChange={(e) => applyBuilder({ since: e.target.value })}
                    />
                  )}
                </Field>
                <Field label="Posted before">
                  {(props) => (
                    <Input
                      {...props}
                      type="date"
                      value={builder.until}
                      onChange={(e) => applyBuilder({ until: e.target.value })}
                    />
                  )}
                </Field>
              </div>
            </fieldset>
          )}

          <Field
            label="Query sent to X"
            hint="Edit this directly to use any operator the fields above do not cover."
          >
            {(props) => (
              <Textarea
                {...props}
                required
                rows={2}
                placeholder="(Iran OR Gold) lang:en min_faves:1000"
                value={form.raw_query}
                onChange={(e) => set({ raw_query: e.target.value })}
              />
            )}
          </Field>

          <Field label="Name" hint="Shown in the sidebar. Defaults to the query.">
            {(props) => (
              <Input
                {...props}
                placeholder="Gold watch"
                value={form.name || ""}
                onChange={(e) => set({ name: e.target.value })}
              />
            )}
          </Field>

          <div className="flex flex-col gap-1">
            <span className="eyebrow">Result set</span>
            <Segmented
              label="Result set"
              options={PRODUCTS}
              value={form.product}
              onChange={(product) => set({ product })}
              className="self-start"
            />
            <p className="text-xs text-fg-dim">{PRODUCT_HINT[form.product]}</p>
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Pages per run" hint={HINTS.depth}>
              {(props) => (
                <Select
                  {...props}
                  value={form.pagination_depth}
                  onChange={(e) => set({ pagination_depth: e.target.value })}
                >
                  {DEPTHS.map((value) => (
                    <option key={value} value={value}>
                      {value} {value === 1 ? "page" : "pages"}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label="Look back" hint={HINTS.rolling}>
              {(props) => (
                <Select
                  {...props}
                  value={form.rolling_hours}
                  onChange={(e) => set({ rolling_hours: e.target.value })}
                >
                  {ROLLING_HOURS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label="Runs every" hint={HINTS.interval}>
              {(props) => (
                <Select
                  {...props}
                  value={form.interval_seconds}
                  onChange={(e) => set({ interval_seconds: e.target.value })}
                >
                  {INTERVALS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </div>

          {error && <ErrorNote>{error}</ErrorNote>}

          <DialogFooter>
            <Button variant="quiet" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={saving || !form.raw_query.trim()}>
              {editing ? "Save changes" : "Create and run"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
