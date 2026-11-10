// One icon family for the whole UI: 24px stroke paths, one weight.
import { icons } from "./icons";

const extra: Record<string, string> = {
  grip: "M9 6h.01|M15 6h.01|M9 12h.01|M15 12h.01|M9 18h.01|M15 18h.01",
  up: "M12 19V5|M5 12l7-7 7 7", down: "M12 5v14|M19 12l-7 7-7-7",
  more: "M5 12h.01|M12 12h.01|M19 12h.01", help: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z|M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3|M12 17h.01",
  copy: "M9 9h11v11H9z|M5 15H4V4h11v1", filter: "M3 5h18|M6 12h12|M10 19h4", reset: "M3 12a9 9 0 1 0 3-6.7L3 8|M3 3v5h5",
  sort: "M8 4v16|M4 8l4-4 4 4|M16 20V4|M12 16l4 4 4-4", minus: "M5 12h14", wifioff: "M2 2l20 20|M8.5 16a5.5 5.5 0 0 1 7 0|M12 19.5h.01|M5 12.5a10 10 0 0 1 5-2.7|M16.5 10.3a10 10 0 0 1 2.5 2.2",
  edit: "M4 20h4L19 9l-4-4L4 16z|M13 7l4 4", calendar: "M4 6h16v14H4z|M4 10h16|M8 3v4|M16 3v4", pin: "M12 17v5|M8 3h8l-1 6 3 4H6l3-4z",
};
export function Icon({ name, size = 18, className = "", label }: { name: string; size?: number; className?: string; label?: string }) {
  const d = extra[name] ?? icons[name] ?? icons.dot;
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={"shrink-0 " + className} fill="none" stroke="currentColor" strokeWidth={1.75}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden={label ? undefined : true} role={label ? "img" : undefined} aria-label={label}>
      {d.split("|").map((p, i) => <path key={i} d={p} />)}
    </svg>
  );
}
