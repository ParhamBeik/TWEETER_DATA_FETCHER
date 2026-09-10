import { Component } from "react";
import { useLocation } from "react-router-dom";
import { Button } from "@/ui/button";
import { Panel, PanelBody, PanelHead } from "@/ui/panel";

/**
 * The last thing standing between a thrown render and a white page.
 *
 * There was no boundary anywhere in this app, so any exception during render --
 * a chart handed a shape it did not expect, a payload missing a field a
 * `.map()` walks -- unmounted the whole tree and left a blank document. Nothing
 * on screen said what happened, and nothing on the server logged it either,
 * because it never reached the server.
 *
 * It also covers the two lazily-imported routes. `import()` rejects when the
 * chunk cannot be fetched, which is exactly what a stale index.html asking for
 * a bundle the current image no longer has produces (see the no-cache rule in
 * frontend/nginx.conf). Suspense has no answer for that on its own -- a
 * rejected lazy import is thrown, and without a boundary it is fatal.
 *
 * A class, because `componentDidCatch`/`getDerivedStateFromError` have no hook
 * equivalent; this is the one place React still requires one.
 */
class Boundary extends Component {
  state = { error: null };

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // The browser console is where an operator will look, and it is the only
    // sink here -- there is no error-reporting service in this project and
    // adding one is a decision, not a detail.
    console.error("Unhandled render error", error, info?.componentStack);
  }

  componentDidUpdate(previous) {
    // Navigating away is the cheapest recovery there is, and it costs nothing
    // to offer: the broken screen is one route, not the session.
    if (this.state.error && previous.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <Panel role="alert" className="mx-auto max-w-lg">
        <PanelHead
          label="Error"
          title="This screen stopped rendering"
          lede="The rest of the console still works — pick another section, or reload to start over. The details are in the browser console."
        />
        <PanelBody className="flex flex-wrap items-center gap-2">
          <Button variant="primary" onClick={() => window.location.reload()}>
            Reload the console
          </Button>
          <code className="min-w-0 truncate font-mono text-xs text-fg-dim">
            {String(this.state.error?.message || this.state.error)}
          </code>
        </PanelBody>
      </Panel>
    );
  }
}

/**
 * Wraps `Boundary` with the current path, so moving to another section clears a
 * caught error instead of leaving the failure pinned over every route.
 */
export default function ErrorBoundary({ children }) {
  const { pathname } = useLocation();
  return <Boundary resetKey={pathname}>{children}</Boundary>;
}
