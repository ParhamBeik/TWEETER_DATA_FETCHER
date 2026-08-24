// Who is signed in, as shared state.
//
// The old version read localStorage during render to decide whether to show the
// app or redirect. That is not reactive: logging in mutated storage without
// telling React, so the guard could still be looking at the previous answer.
// Identity lives in state here, and every consumer re-renders when it changes.
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api, clearTokens, hasSession, refreshSession, setTokens } from "./api";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  // Undecided until the stored refresh token has been tried. Without this a
  // reload renders the guard before the session is restored and bounces a
  // signed-in user to /login.
  const [status, setStatus] = useState(hasSession() ? "restoring" : "anonymous");

  useEffect(() => {
    if (status !== "restoring") return;
    let cancelled = false;
    (async () => {
      const restored = await refreshSession();
      if (cancelled) return;
      if (!restored) {
        clearTokens();
        setStatus("anonymous");
        return;
      }
      try {
        const me = await api("/auth/me/", { retry: false });
        if (cancelled) return;
        setUser(me);
        setStatus("authed");
      } catch {
        if (cancelled) return;
        clearTokens();
        setStatus("anonymous");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [status]);

  const signIn = useCallback((tokens) => {
    setTokens(tokens);
    setUser(tokens.user || null);
    setStatus("authed");
  }, []);

  const signOut = useCallback(async () => {
    const refresh = localStorage.getItem("tsaas_refresh");
    // Best effort: the server blacklists the refresh token so it cannot be
    // reused, but the local session ends either way.
    if (refresh) {
      try {
        await api("/auth/logout/", { method: "POST", body: { refresh }, retry: false });
      } catch {
        /* already expired, or the API is unreachable */
      }
    }
    clearTokens();
    setUser(null);
    setStatus("anonymous");
  }, []);

  const value = useMemo(
    () => ({
      user,
      status,
      authed: status === "authed",
      // The API enforces this independently; this only decides which controls
      // are worth showing.
      isStaff: Boolean(user?.is_staff),
      signIn,
      signOut,
    }),
    [user, status, signIn, signOut]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === null) throw new Error("useAuth must be used inside an AuthProvider");
  return context;
}
