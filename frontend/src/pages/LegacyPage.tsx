// Development migration link to the complete Python-served dashboard.
export default function LegacyPage() {
  const base = import.meta.env.VITE_QM_LEGACY_URL || "http://127.0.0.1:8080/";
  return <a href={new URL(location.hash, base).href}>Open existing dashboard</a>;
}
