// State stores — reactive client data layer.
// Owns the current client-side source of truth, selectors, and invariant-preserving mutations.
// Does NOT call APIs, services, or transport.

import { createStore } from "../../core/store.js";
import { createUser, createDevice, createBundle, createVpnStatus, isUserBlocked, isUserOnline, bundleUsagePercent } from "../../model/domain/index.js";

// ---- Dashboard store ----
const initialDashboardState = {
  authed: false,
  dashboard: null,
  live: false,
  loading: false,
  error: null,
};

export function createDashboardStore() {
  const store = createStore({ ...initialDashboardState });

  const api = {
    setDashboard: (snapshot) => {
      store.set({ dashboard: snapshot, loading: false, error: null });
    },
    setAuthed: (authed) => {
      store.set({ authed });
    },
    setLive: (live) => {
      store.set({ live });
    },
    setLoading: (loading) => {
      store.set({ loading });
    },
    setError: (error) => {
      store.set({ error, loading: false });
    },
    reset: () => {
      store.set({ ...initialDashboardState });
    },
  };

  const selectors = {
    getState: () => store.get(),
    getDashboard: () => store.get().dashboard,
    getAuthed: () => store.get().authed,
    getLive: () => store.get().live,
    getLoading: () => store.get().loading,
    getError: () => store.get().error,
    getUsers: () => {
      const d = store.get().dashboard;
      return d ? d.users : [];
    },
    getBundle: () => {
      const d = store.get().dashboard;
      return d ? d.bundle : null;
    },
    getVpnStatus: () => {
      const d = store.get().dashboard;
      return d ? d.vpn : null;
    },
    getUserById: (id) => {
      const d = store.get().dashboard;
      if (!d) return null;
      return d.users.find((u) => u.id === id) || null;
    },
    getDeviceById: (deviceId) => {
      const d = store.get().dashboard;
      if (!d) return null;
      for (const u of d.users) {
        const d_ = u.devices.find((d) => d.id === deviceId);
        if (d_) return { ...d_, owner: u.name };
      }
      return null;
    },
    getUsagePercent: () => {
      const d = store.get().dashboard;
      return d && d.bundle ? bundleUsagePercent(d.bundle) : 0;
    },
    subscribe: (fn) => store.subscribe(fn),
  };

  return { ...api, ...selectors };
}

// ---- Preference store ----
const initialPrefsState = {
  theme: "dark",
  lang: "en",
  accent: "azure",
  tone: "",
  particles: "mesh",
  fx: "rich",
  density: "comfortable",
  rail: false,
};

export function createPrefsStore() {
  const store = createStore({ ...initialPrefsState });

  const api = {
    setPref: (key, value) => {
      store.set({ [key]: value });
    },
    applyPrefs: () => {
      // No-op here; applyPrefs is called by the composition root
    },
    reset: () => {
      store.set({ ...initialPrefsState });
    },
  };

  const selectors = {
    getState: () => store.get(),
    getPrefs: () => ({ ...store.get() }),
    getTheme: () => store.get().theme,
    getLang: () => store.get().lang,
    getAccent: () => store.get().accent,
    getTone: () => store.get().tone,
    getParticles: () => store.get().particles,
    getFx: () => store.get().fx,
    getDensity: () => store.get().density,
    getRail: () => store.get().rail,
    subscribe: (fn) => store.subscribe(fn),
  };

  return { ...api, ...selectors };
}

// ---- Live data store ----
export function createLiveStore() {
  const store = createStore({
    connected: false,
    lastUpdate: null,
    reconnectAttempts: 0,
  });

  const api = {
    setConnected: (connected) => {
      store.set({ connected, lastUpdate: connected ? Date.now() : store.get().lastUpdate });
    },
    incrementReconnect: () => {
      store.set({ reconnectAttempts: store.get().reconnectAttempts + 1 });
    },
    resetReconnect: () => {
      store.set({ reconnectAttempts: 0 });
    },
  };

  const selectors = {
    getState: () => store.get(),
    isConnected: () => store.get().connected,
    getReconnectAttempts: () => store.get().reconnectAttempts,
    subscribe: (fn) => store.subscribe(fn),
  };

  return { ...api, ...selectors };
}
