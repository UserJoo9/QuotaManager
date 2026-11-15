// Passkeys (WebAuthn) in the browser: base64url <-> ArrayBuffer around navigator.credentials.
// Server side: quota/passkeys.py (/api/passkeys/*). A passkey lives on the device that
// created it, so "this device has one" is remembered locally to show the sign-in button.
import { api } from "./api.js";

const FLAG = "quota_passkey_here";
const toBuf = (s) => { const b = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4)); const u = new Uint8Array(b.length); for (let i = 0; i < b.length; i++) u[i] = b.charCodeAt(i); return u.buffer; };
const toB64 = (buf) => { if (!buf) return null; let s = ""; for (const x of new Uint8Array(buf)) s += String.fromCharCode(x); return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); };
const isIp = (host) => /^\d{1,3}(\.\d{1,3}){3}$/.test(host) || host.includes(":");

/** Why passkeys can't work on this address/browser (null = they can). */
export async function passkeyBlocker() {
  if (!window.PublicKeyCredential || !navigator.credentials) return "browser";
  if (isIp(location.hostname)) return "ip-address";
  if (!window.isSecureContext) return "not-https";
  try { if (!(await window.PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable())) return "no-platform"; } catch { return "no-platform"; }
  return null;
}
/** A friendly default name for the passkey this device is about to create. */
export function deviceLabel() {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return "iPhone";
  if (/iPad/.test(ua)) return "iPad";
  if (/Android/.test(ua)) return "Android";
  if (/Macintosh/.test(ua)) return "Mac";
  if (/Windows/.test(ua)) return "Windows";
  return "Passkey";
}
export const hasLocalPasskey = () => { try { return localStorage.getItem(FLAG) === "1"; } catch { return false; } };
export const setLocalPasskey = (on) => { try { if (on) localStorage.setItem(FLAG, "1"); else localStorage.removeItem(FLAG); } catch { /* private mode */ } };
export const isCancel = (e) => e && (e.name === "NotAllowedError" || e.name === "AbortError");

const creationOptions = (o) => ({ ...o, challenge: toBuf(o.challenge), user: { ...o.user, id: toBuf(o.user.id) },
  excludeCredentials: (o.excludeCredentials || []).map((c) => ({ ...c, id: toBuf(c.id) })) });
const requestOptions = (o) => ({ ...o, challenge: toBuf(o.challenge),
  allowCredentials: (o.allowCredentials || []).map((c) => ({ ...c, id: toBuf(c.id) })) });
function serialize(c) {
  const r = c.response, response = { clientDataJSON: toB64(r.clientDataJSON) };
  if (r.attestationObject) { response.attestationObject = toB64(r.attestationObject); response.transports = r.getTransports ? r.getTransports() : []; }
  if (r.authenticatorData) { response.authenticatorData = toB64(r.authenticatorData); response.signature = toB64(r.signature); response.userHandle = toB64(r.userHandle); }
  return { id: c.id, rawId: toB64(c.rawId), type: c.type, response,
    clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {}, authenticatorAttachment: c.authenticatorAttachment };
}
const cancelled = () => { const e = new Error("cancelled"); e.name = "NotAllowedError"; return e; };

/** Create a passkey on THIS device (built-in authenticator only). */
export async function createPasskey(name) {
  const { token, options } = await api.post("/api/passkeys/register/options");
  const cred = await navigator.credentials.create({ publicKey: creationOptions(options) });
  if (!cred) throw cancelled();
  const res = await api.post("/api/passkeys/register/verify", { token, credential: serialize(cred), name });
  setLocalPasskey(true);
  return res;
}
/** Sign in with a passkey stored on this device. */
export async function signInWithPasskey() {
  const { token, options } = await api.post("/api/passkeys/login/options");
  const cred = await navigator.credentials.get({ publicKey: requestOptions(options) });
  if (!cred) throw cancelled();
  const res = await api.post("/api/passkeys/login/verify", { token, credential: serialize(cred) });
  setLocalPasskey(true);
  return res;
}
