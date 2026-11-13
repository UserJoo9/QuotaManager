// Transport layer — generic HTTP mechanics. Knows how bytes cross the wire,
// not what the business decides. No domain logic, no screen awareness.

const CSRF_HEADER = "X-QM-CSRF";
const CSRF_VALUE = "1";

/**
 * Send one JSON request and parse the JSON response.
 * @param {"GET"|"POST"|"PATCH"|"DELETE"} method
 * @param {string} path
 * @param {any} [body]
 * @returns {Promise<unknown>} Parsed JSON body (null on empty/unparseable body).
 * @throws {Error} When `res.ok` is false; message from `detail` or `HTTP <status>`.
 */
async function request(method, path, body) {
  const opts = {
    method,
    headers: { "Content-Type": "application/json", [CSRF_HEADER]: CSRF_VALUE },
    credentials: "same-origin",
  };
  if (body !== undefined) opts.body = JSON.stringify(body);

  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }

  if (!res.ok) {
    const msg = (data && data.detail) || `HTTP ${res.status}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

/** @type {{get: function(string): Promise<unknown>, post: function(string, *): Promise<unknown>, patch: function(string, *): Promise<unknown>, del: function(string): Promise<unknown>}} */
export const http = {
  get:  (path)  => request("GET", path),
  post: (path, body) => request("POST", path, body),
  patch:(path, body) => request("PATCH", path, body),
  del:  (path)  => request("DELETE", path),
};
