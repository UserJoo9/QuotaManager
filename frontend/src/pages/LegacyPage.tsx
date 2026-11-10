// Migration-only route: operators can use the complete deployed dashboard.
import { t } from "../lib/i18n";
export default function LegacyPage() {
  return <a href={"/" + location.hash}>{t("common.open") || "Open dashboard"}</a>;
}
