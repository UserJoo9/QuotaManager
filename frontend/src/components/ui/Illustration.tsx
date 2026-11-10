/*
 * Spot illustrations for empty, error and "all clear" states. One family: 1.75 px line art in
 * the text-tertiary token, one soft accent shape behind each subject, no gradients, no
 * external files (CSP-clean, themes automatically, ~0 KB on the wire when unused).
 * Decorative only: the words next to them carry the meaning (aria-hidden).
 */
const S = { fill: "none", stroke: "currentColor", strokeWidth: 1.75, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
const Soft = (p: { cx: number; cy: number; r: number }) => <circle {...p} className="fill-accent-soft" />;
const Ground = () => <path d="M14 84h92" {...S} strokeDasharray="2 5" opacity={0.6} />;

const ART: Record<string, JSX.Element> = {
  people: <><Soft cx={60} cy={46} r={30} /><circle cx={48} cy={40} r={9} {...S} /><path d="M32 68c2-10 9-15 16-15s14 5 16 15" {...S} /><circle cx={74} cy={44} r={7} {...S} /><path d="M66 56c3-1 5-2 8-2 6 0 11 4 13 12" {...S} /><Ground /></>,
  network: <><Soft cx={60} cy={48} r={30} /><path d="M38 46a32 32 0 0 1 44 0" {...S} /><path d="M45 54a21 21 0 0 1 30 0" {...S} /><path d="M52 62a10 10 0 0 1 16 0" {...S} /><circle cx={60} cy={70} r={2.5} className="fill-current" /><Ground /></>,
  shield: <><Soft cx={60} cy={46} r={30} /><path d="M60 20l22 8v16c0 14-9 23-22 27-13-4-22-13-22-27V28z" {...S} /><path d="M51 45l6 6 12-12" {...S} /><Ground /></>,
  share: <><Soft cx={60} cy={46} r={30} /><rect x={34} y={30} width={30} height={38} rx={4} {...S} /><rect x={56} y={38} width={30} height={38} rx={4} {...S} className="fill-surface" /><path d="M71 64V50m-6 6l6-6 6 6" {...S} /><Ground /></>,
  vpn: <><Soft cx={60} cy={46} r={30} /><rect x={42} y={44} width={36} height={26} rx={4} {...S} /><path d="M49 44v-7a11 11 0 0 1 22 0v7" {...S} /><path d="M60 54v6" {...S} /><Ground /></>,
  filter: <><Soft cx={58} cy={44} r={28} /><circle cx={54} cy={42} r={16} {...S} /><path d="M66 54l14 14" {...S} /><path d="M47 42h14" {...S} /><Ground /></>,
  search: <><Soft cx={58} cy={44} r={28} /><circle cx={54} cy={42} r={16} {...S} /><path d="M66 54l14 14" {...S} /><Ground /></>,
  check: <><Soft cx={60} cy={44} r={28} /><circle cx={60} cy={44} r={20} {...S} /><path d="M50 45l7 7 14-15" {...S} /><Ground /></>,
  insights: <><Soft cx={60} cy={46} r={30} /><path d="M34 70V28M34 70h52" {...S} /><path d="M42 60l10-12 9 7 15-19" {...S} /><circle cx={76} cy={36} r={2.5} className="fill-current" /><Ground /></>,
  bolt: <><Soft cx={60} cy={46} r={30} /><path d="M64 20L44 50h14l-4 22 22-32H62z" {...S} /><Ground /></>,
  info: <><Soft cx={60} cy={44} r={28} /><circle cx={60} cy={44} r={20} {...S} /><path d="M60 41v12M60 34h.01" {...S} /><Ground /></>,
  alert: <><Soft cx={60} cy={46} r={30} /><path d="M60 22l26 46H34z" {...S} /><path d="M60 40v13M60 60h.01" {...S} /><Ground /></>,
  wifioff: <><Soft cx={60} cy={46} r={30} /><path d="M38 46a32 32 0 0 1 10-7M72 39a32 32 0 0 1 10 7" {...S} /><path d="M45 54a21 21 0 0 1 9-5M66 49a21 21 0 0 1 9 5" {...S} /><circle cx={60} cy={68} r={2.5} className="fill-current" /><path d="M40 26l40 46" {...S} /><Ground /></>,
  compass: <><Soft cx={60} cy={44} r={28} /><circle cx={60} cy={44} r={22} {...S} /><path d="M68 36l-5 12-11 4 5-12z" {...S} /><Ground /></>,
  crash: <><Soft cx={60} cy={46} r={30} /><rect x={32} y={26} width={56} height={40} rx={5} {...S} /><path d="M32 36h56" {...S} /><path d="M52 46l16 12M68 46L52 58" {...S} /><Ground /></>,
  update: <><Soft cx={60} cy={46} r={30} /><path d="M80 44a20 20 0 1 1-6-14" {...S} /><path d="M76 22v9h-9" {...S} /><Ground /></>,
};
export const hasArt = (k: string) => k in ART;
export function Illustration({ name, size = 120, className = "" }: { name: string; size?: number; className?: string }) {
  const art = ART[name] ?? ART.info;
  return <svg viewBox="0 0 120 96" width={size} height={size * 0.8} className={"text-fg-3 " + className} aria-hidden focusable="false">{art}</svg>;
}
