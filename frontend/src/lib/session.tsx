import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, setUnauthorizedHandler } from "./api";
import { startLive, type LiveState } from "./live";
import type { Dashboard } from "./types";

// Memory-only cache: nothing authenticated is ever persisted to disk. Cleared on logout/401.
export const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false, retry: 1, staleTime: 30_000, gcTime: 5 * 60_000 } },
});
export const DASH = ["dashboard"] as const;
const fetchDashboard = () => api.get<Dashboard>("/api/dashboard");

interface Session {
  /** null = still checking; "unreachable" = the gateway did not answer at all */
  authed: boolean | null | "unreachable";
  retryBoot: () => void; live: LiveState; lastUpdated: number | null;
  /** True while data is fresh enough to act on (live socket, or a recent poll). */
  connected: boolean;
  refresh: () => Promise<Dashboard | undefined>; signedIn: () => void; logout: () => Promise<void>;
}
const Ctx = createContext<Session>(null as unknown as Session);
export const useSession = () => useContext(Ctx);

export function SessionProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [authed, setAuthed] = useState<boolean | null | "unreachable">(null);
  const [bootTry, setBootTry] = useState(0);
  const [live, setLive] = useState<LiveState>("connecting");
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const stop = useRef<null | (() => void)>(null);

  const end = useCallback(() => { stop.current?.(); stop.current = null; qc.clear(); setLastUpdated(null); setAuthed(false); }, [qc]);
  const refresh = useCallback(async () => {
    const d = await qc.fetchQuery({ queryKey: DASH, queryFn: fetchDashboard, staleTime: 0 });
    setLastUpdated(Date.now()); return d;
  }, [qc]);
  const begin = useCallback(() => {
    setAuthed(true);
    if (stop.current) return;
    // One first-data path: the socket's initial snapshot. Only if it is slow do we fetch once.
    const fallback = setTimeout(() => { if (!qc.getQueryData(DASH)) refresh().catch(() => {}); }, 1200);
    const s = startLive((d) => { qc.setQueryData(DASH, d); setLastUpdated(Date.now()); }, setLive);
    stop.current = () => { clearTimeout(fallback); s(); };
  }, [qc, refresh]);

  useEffect(() => {
    setUnauthorizedHandler(() => end());
    let gone = false;
    api.get<{ authenticated: boolean }>("/api/me").then((me) => { if (gone) return; if (me.authenticated) begin(); else setAuthed(false); })
      .catch((e) => { if (!gone) setAuthed((e as { status?: number }).status === 0 ? "unreachable" : false); });
    return () => { gone = true; stop.current?.(); stop.current = null; };
  }, [begin, end, bootTry]);

  const logout = useCallback(async () => { try { await api.post("/api/logout"); } catch { /* ignore */ } end(); }, [end]);
  const connected = live === "live" || live === "polling";
  return <Ctx.Provider value={{ authed, retryBoot: () => { setAuthed(null); setBootTry((x) => x + 1); }, live, lastUpdated, connected, refresh, signedIn: begin, logout }}>{children}</Ctx.Provider>;
}
/** The shared dashboard snapshot. Never polled by Query: the socket keeps it fresh. */
export function useDashboard() {
  return useQuery({ queryKey: DASH, queryFn: fetchDashboard, enabled: false, staleTime: Infinity }).data;
}
