// State stores — reactive client data layer.
// Owns the current client-side source of truth, selectors, and invariant-preserving mutations.
// Does NOT call APIs, services, or transport.

import { createStore } from "../../core/store.js";
import { bundleUsagePercent } from "../../model/domain/index.js";

// ---- Dashboard store ----

/**
 * Initial dashboard state.
 * @typedef {Object} DashboardState
 * @property {boolean} authed - whether the session is authenticated
 * @property {Object|null} dashboard - latest dashboard snapshot
 * @property {boolean} live - live updates enabled
 * @property {boolean} loading - request in flight
 * @property {Object|null} error - last error
 */
const initialDashboardState = {
  authed: false,
  dashboard: null,
  live: false,
  loading: false,
  error: null,
};

/**
 * Creates the dashboard store.
 *
 * @returns {Object} store API and selectors:
 *   {setDashboard, setAuthed, setLive, setLoading, setError, reset,
 *    getState, getDashboard, getAuthed, getLive, getLoading, getError,
 *    getUsers, getBundle, getVpnStatus, getUserById, getDeviceById,
 *    getUsagePercent, subscribe}
 */
export function createDashboardStore() {
  const store = createStore({ ...initialDashboardState });

  // One source of truth for reading the current dashboard snapshot.
  const dashboardSnapshot = () => store.get().dashboard;

  const api = {
    /** @param {Object} snapshot */
    setDashboard: (snapshot) => {
      store.set({ dashboard: snapshot, loading: false, error: null });
    },
    /** @param {boolean} authed */
    setAuthed: (authed) => {
      store.set({ authed });
    },
    /** @param {boolean} live */
    setLive: (live) => {
      store.set({ live });
    },
    /** @param {boolean} loading */
    setLoading: (loading) => {
      store.set({ loading });
    },
    /** @param {Object|null} error */
    setError: (error) => {
      store.set({ error, loading: false });
    },
    reset: () => {
      store.set({ ...initialDashboardState });
    },
  };

  const selectors = {
    getState: () => store.get(),
    getDashboard: () => dashboardSnapshot(),
    getAuthed: () => store.get().authed,
    getLive: () => store.get().live,
    getLoading: () => store.get().loading,
    getError: () => store.get().error,
    /** @returns {Array} */
    getUsers: () => {
      const d = dashboardSnapshot();
      return d ? d.users : [];
    },
    /** @returns {Object|null} */
    getBundle: () => {
      const d = dashboardSnapshot();
      return d ? d.bundle : null;
    },
    /** @returns {Object|null} */
    getVpnStatus: () => {
      const d = dashboardSnapshot();
      return d ? d.vpn : null;
    },
    /**
     * @param {string} id
     * @returns {Object|null}
     */
    getUserById: (id) => {
      const d = dashboardSnapshot();
      if (!d) return null;
      return d.users.find((u) => u.id === id) || null;
    },
    /**
     * @param {string} deviceId
     * @returns {Object|null} device with `owner` name attached
     */
    getDeviceById: (deviceId) => {
      const d = dashboardSnapshot();
      if (!d) return null;
      for (const u of d.users) {
        const device = u.devices.find((d) => d.id === deviceId);
        if (device) return { ...device, owner: u.name };
      }
      return null;
    },
    /** @returns {number} usage percent (0 when no bundle) */
    getUsagePercent: () => {
      const d = dashboardSnapshot();
      return d && d.bundle ? bundleUsagePercent(d.bundle) : 0;
    },
    subscribe: (fn) => store.subscribe(fn),
  };

  return { ...api, ...selectors };
}

// ---- Preference store ----

/**
 * Initial preference state.
 * @typedef {Object} PrefsState
 * @property {string} theme @property {string} lang @property {string} accent
 * @property {string} tone @property {string} particles @property {string} fx
 * @property {string} density @property {boolean} rail
 */
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

/**
 * Creates the preferences store.
 * `applyPrefs` is an intentional no-op here; the composition root applies prefs.
 *
 * @returns {Object} store API and selectors:
 *   {setPref, applyPrefs, reset, getState, getPrefs, getTheme, getLang,
 *    getAccent, getTone, getParticles, getFx, getDensity, getRail, subscribe}
 */
export function createPrefsStore() {
  const store = createStore({ ...initialPrefsState });

  const api = {
    /** @param {string} key @param {*} value */
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
    /** @returns {PrefsState} shallow copy of current prefs */
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

/**
 * Creates the live connection store.
 *
 * @returns {Object} store API and selectors:
 *   {setConnected, incrementReconnect, resetReconnect,
 *    getState, isConnected, getReconnectAttempts, subscribe}
 */
export function createLiveStore() {
  const store = createStore({
    connected: false,
    lastUpdate: null,
    reconnectAttempts: 0,
  });

  const api = {
    /** @param {boolean} connected */
    setConnected: (connected) => {
      store.set({
        connected,
        lastUpdate: connected ? Date.now() : store.get().lastUpdate,
      });
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
