// App layer — composition root.
// Constructs transports, APIs, services, and stores; injects narrow dependencies;
// assembles routes and context providers; connects them to screens.
// Construction happens here, not in hooks, services, stores, or components.

import { http } from "../transport/http.js";
import {
  dashboardApi, bundleApi, userApi, deviceApi,
  networkApi, securityApi, dnsApi, vpnApi, systemApi,
} from "../api/index.js";
import { createDashboardStore, createPrefsStore, createLiveStore } from "../state/stores/index.js";
import { createHooks } from "../hooks/index.js";
import {
  DashboardApiPort, BundleApiPort, UserApiPort, DeviceApiPort,
  NetworkApiPort, SecurityApiPort, DnsApiPort, VpnApiPort, SystemApiPort,
  DashboardStorePort, PrefsStorePort, ToastPort,
} from "../port/index.js";

// ---- Toast implementation (UI-bound but injected as port) ----
import { toast } from "../components/toast.js";
const toastPort = {
  show: (message, kind = "") => toast(message, kind),
};

// ---- Dependency injection container ----
export function createAppContainer() {
  // Transport is already a singleton module (http)
  // API layer is already a singleton module (dashboardApi, etc.)

  // Stores
  const dashboardStore = createDashboardStore();
  const prefsStore = createPrefsStore();
  const liveStore = createLiveStore();

  // Hooks (presentation adapters) with injected dependencies
  const hooks = createHooks({
    api: {
      dashboard: dashboardApi,
      bundle: bundleApi,
      user: userApi,
      device: deviceApi,
      network: networkApi,
      security: securityApi,
      dns: dnsApi,
      vpn: vpnApi,
      system: systemApi,
    },
    store: dashboardStore,
    prefs: prefsStore,
    live: liveStore,
    toast: toastPort,
  });

  return {
    // Ports (contracts)
    ports: {
      [DashboardApiPort]: dashboardApi,
      [BundleApiPort]: bundleApi,
      [UserApiPort]: userApi,
      [DeviceApiPort]: deviceApi,
      [NetworkApiPort]: networkApi,
      [SecurityApiPort]: securityApi,
      [DnsApiPort]: dnsApi,
      [VpnApiPort]: vpnApi,
      [SystemApiPort]: systemApi,
      [DashboardStorePort]: dashboardStore,
      [PrefsStorePort]: prefsStore,
      [ToastPort]: toastPort,
    },
    // Stores
    stores: {
      dashboard: dashboardStore,
      prefs: prefsStore,
      live: liveStore,
    },
    // Hooks
    hooks,
    // Services are accessed through hooks
    // API is accessed through hooks
    // Transport is internal to API layer
  };
}

// Singleton container instance
let container = null;

export function getContainer() {
  if (!container) {
    container = createAppContainer();
  }
  return container;
}

export function resetContainer() {
  container = null;
}
