import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import * as Tooltip from "@radix-ui/react-tooltip";
import "./styles/app.css";
import App from "./shell/App";
import { SessionProvider, queryClient } from "./lib/session";
import { ToastProvider } from "./components/ui/overlays";
import { applyPrefs, getPrefs } from "./lib/prefs";
import { setLang, loadLang } from "./lib/i18n";

applyPrefs();
setLang(getPrefs().lang);
// first paint waits only for the one dictionary in use
loadLang(getPrefs().lang).catch(() => loadLang("en")).finally(() => createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <Tooltip.Provider delayDuration={350}>
        <ToastProvider>
          <SessionProvider>
            <App />
          </SessionProvider>
        </ToastProvider>
      </Tooltip.Provider>
    </QueryClientProvider>
  </StrictMode>,
));
