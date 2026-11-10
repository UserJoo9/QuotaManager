// QR codes via the self-hosted /assets/qrcode.min.js (loaded on first use only).
type QRCtor = { new (el: HTMLElement, o: object): unknown; CorrectLevel: { M: unknown } };
let loading: Promise<QRCtor> | null = null;
function load(): Promise<QRCtor> {
  const w = window as unknown as { QRCode?: QRCtor };
  if (w.QRCode) return Promise.resolve(w.QRCode);
  return (loading ||= new Promise<QRCtor>((res, rej) => { const s = document.createElement("script"); s.src = "/assets/qrcode.min.js"; s.onload = () => (w.QRCode ? res(w.QRCode) : rej(new Error("qr"))); s.onerror = rej; document.head.append(s); }));
}
export async function drawQr(el: HTMLElement, text: string, size: number) {
  const Q = await load(); el.replaceChildren(); new Q(el, { text, width: size, height: size, correctLevel: Q.CorrectLevel.M });
}
