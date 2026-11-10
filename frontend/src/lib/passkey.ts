// Passkeys in the browser: base64url <-> ArrayBuffer around navigator.credentials.
import { api } from "./api";

const toBuf = (s: string) => { const b = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4)); const u = new Uint8Array(b.length); for (let i = 0; i < b.length; i++) u[i] = b.charCodeAt(i); return u.buffer; };
const toB64 = (buf: ArrayBuffer | null) => { if (!buf) return null; let s = ""; for (const x of new Uint8Array(buf)) s += String.fromCharCode(x); return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); };
const isIp = (h: string) => /^\d{1,3}(\.\d{1,3}){3}$/.test(h) || h.includes(":");

/** Why passkeys can't work on this address (null = they can). */
export async function passkeyBlocker(): Promise<null | "browser" | "ip-address" | "not-https" | "no-platform"> {
  if (typeof window === "undefined" || !window.PublicKeyCredential || !navigator.credentials) return "browser";
  if (isIp(location.hostname)) return "ip-address";
  if (!window.isSecureContext) return "not-https";
  try { if (!(await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable())) return "no-platform"; } catch { return "no-platform"; }
  return null;
}
/** A friendly default name for the passkey this device is about to create. */
export function deviceLabel() {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return "iPhone (Face ID)";
  if (/iPad/.test(ua)) return "iPad";
  if (/Android/.test(ua)) return "Android phone";
  if (/Macintosh/.test(ua)) return "Mac (Touch ID)";
  if (/Windows/.test(ua)) return "Windows Hello";
  return "This device";
}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type J = any;
function creationOptions(o: J): PublicKeyCredentialCreationOptions {
  return { ...o, challenge: toBuf(o.challenge), user: { ...o.user, id: toBuf(o.user.id) },
    excludeCredentials: (o.excludeCredentials || []).map((c: J) => ({ ...c, id: toBuf(c.id) })) };
}
function requestOptions(o: J): PublicKeyCredentialRequestOptions {
  return { ...o, challenge: toBuf(o.challenge), allowCredentials: (o.allowCredentials || []).map((c: J) => ({ ...c, id: toBuf(c.id) })) };
}
function serialize(c: PublicKeyCredential): J {
  const r = c.response as AuthenticatorAttestationResponse & AuthenticatorAssertionResponse;
  const response: J = { clientDataJSON: toB64(r.clientDataJSON) };
  if ("attestationObject" in r && r.attestationObject) { response.attestationObject = toB64(r.attestationObject); response.transports = r.getTransports?.() || []; }
  if ("authenticatorData" in r && r.authenticatorData) { response.authenticatorData = toB64(r.authenticatorData); response.signature = toB64(r.signature); response.userHandle = toB64(r.userHandle); }
  return { id: c.id, rawId: toB64(c.rawId), type: c.type, response, clientExtensionResults: c.getClientExtensionResults?.() || {}, authenticatorAttachment: c.authenticatorAttachment };
}
/** Create a passkey on THIS device (platform authenticator only). */
export async function createPasskey(name: string) {
  const { token, options } = await api.post<{ token: string; options: J }>("/api/passkeys/register/options");
  const cred = (await navigator.credentials.create({ publicKey: creationOptions(options) })) as PublicKeyCredential | null;
  if (!cred) throw new DOMException("cancelled", "NotAllowedError");
  return api.post<{ ok: boolean; passkey: J }>("/api/passkeys/register/verify", { token, credential: serialize(cred), name });
}
/** Sign in with a passkey stored on this device. */
export async function signInWithPasskey() {
  const { token, options } = await api.post<{ token: string; options: J }>("/api/passkeys/login/options");
  const cred = (await navigator.credentials.get({ publicKey: requestOptions(options) })) as PublicKeyCredential | null;
  if (!cred) throw new DOMException("cancelled", "NotAllowedError");
  return api.post<{ ok: boolean }>("/api/passkeys/login/verify", { token, credential: serialize(cred) });
}
export const isCancel = (e: unknown) => (e as DOMException)?.name === "NotAllowedError" || (e as DOMException)?.name === "AbortError";
