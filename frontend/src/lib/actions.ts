import { useCallback, useState } from "react";
import { api } from "./api";
import { useSession } from "./session";
import { useToast } from "../components/ui/overlays";
import { t } from "./i18n";

// Every mutation goes through the same endpoints as before.
export const actions = {
  patchUser: (id: number, body: object) => api.patch(`/api/users/${id}`, body),
  patchDevice: (id: number, body: object) => api.patch(`/api/devices/${id}`, body),
  dnsUser: (id: number, dns_server: string) => api.patch(`/api/users/${id}/dns`, { dns_server }),
  dnsDevice: (id: number, dns_server: string) => api.patch(`/api/devices/${id}/dns`, { dns_server }),
  blockUser: (id: number, block: boolean) => api.patch(`/api/users/${id}`, { block }),
  blockDevice: (id: number, block: boolean) => api.patch(`/api/devices/${id}`, { block }),
  kickUser: (id: number) => api.del(`/api/users/${id}?blacklist=false`),
  blacklistUser: (id: number) => api.del(`/api/users/${id}?blacklist=true`),
  kickDevice: (id: number) => api.del(`/api/devices/${id}?blacklist=false`),
  blacklistDevice: (id: number) => api.del(`/api/devices/${id}?blacklist=true`),
  approveGuest: (uid: number) => api.post(`/api/guest/user/${uid}/approve`),
  rejectGuest: (uid: number) => api.post(`/api/guest/user/${uid}/reject`),
  makeRegular: (uid: number) => api.post(`/api/users/${uid}/make-regular`),
  createUser: (body: object) => api.post("/api/users", body),
  createDevice: (body: object) => api.post("/api/devices", body),
  topupUser: (id: number, extra_gb: number) => api.post(`/api/users/${id}/topup`, { extra_gb }),
  topupDevice: (id: number, extra_gb: number) => api.post(`/api/devices/${id}/topup`, { extra_gb }),
};

/**
 * Run a gateway change truthfully: pending while the server works, then RE-READ the
 * dashboard, and only then report success. Errors keep the form as it is.
 * (No optimistic UI here — only appearance/sidebar preferences are optimistic.)
 */
export function useAction() {
  const { refresh } = useSession();
  const toast = useToast();
  const [pending, setPending] = useState<string | null>(null);
  const run = useCallback(async (key: string, fn: () => Promise<unknown>, ok?: string, okSub?: string): Promise<boolean> => {
    setPending(key);
    try {
      await fn();
      try { await refresh(); } catch { /* the push will catch up */ }
      toast({ kind: "success", title: ok || t("act.done"), sub: okSub });
      return true;
    } catch (e) {
      toast({ kind: "error", title: t("act.failed"), sub: (e as Error).message });
      return false;
    } finally { setPending(null); }
  }, [refresh, toast]);
  return { run, pending };
}
