// Shared layer — dependency-light pure utilities.
// Genuinely reusable helpers with no feature state, API calls, or components.

/**
 * Format bytes to human-readable string.
 * @param {number} bytes
 * @param {number} [decimals=2]
 * @returns {string}
 */
export function formatBytes(bytes, decimals = 2) {
  if (bytes === null || bytes === undefined) return "0 B";
  if (bytes < 0) return "0 B";
  if (bytes === 0) return "0 B";
  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + " " + sizes[i];
}

/**
 * Format bits per second to human-readable string.
 * @param {number} bps
 * @returns {string}
 */
export function formatBps(bps) {
  const n = +bps || 0;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} Mbps`;
  if (n >= 1e3) return `${Math.round(n / 1e3)} Kbps`;
  return `${Math.round(n)} bps`;
}

/**
 * Clamp a value between min and max.
 * @param {number} value
 * @param {number} min
 * @param {number} max
 * @returns {number}
 */
export function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

/**
 * Debounce a function.
 * @param {Function} fn
 * @param {number} delay
 * @returns {Function}
 */
export function debounce(fn, delay) {
  let timer = null;
  return function (...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), delay);
  };
}

/**
 * Throttle a function.
 * @param {Function} fn
 * @param {number} limit
 * @returns {Function}
 */
export function throttle(fn, limit) {
  let inThrottle;
  return function (...args) {
    if (!inThrottle) {
      fn.apply(this, args);
      inThrottle = true;
      setTimeout(() => (inThrottle = false), limit);
    }
  };
}

/**
 * Deep clone a plain object/array (JSON-serializable values only).
 * @param {*} obj
 * @returns {*}
 */
export function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj));
}

/**
 * Generate a random ID.
 * @param {string} [prefix='id']
 * @returns {string}
 */
export function generateId(prefix = "id") {
  return `${prefix}_${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * Check if an object is empty.
 * @param {*} obj
 * @returns {boolean}
 */
export function isEmpty(obj) {
  if (obj == null) return true;
  if (Array.isArray(obj) || typeof obj === "string") return obj.length === 0;
  return Object.keys(obj).length === 0;
}

/**
 * Group array by key.
 * @param {Array} array
 * @param {Function} keyFn
 * @returns {Object}
 */
export function groupBy(array, keyFn) {
  return array.reduce((groups, item) => {
    const key = keyFn(item);
    if (!groups[key]) groups[key] = [];
    groups[key].push(item);
    return groups;
  }, {});
}

/**
 * Sort array by key.
 * @param {Array} array
 * @param {Function} keyFn
 * @param {"asc"|"desc"} [direction='asc']
 * @returns {Array}
 */
export function sortBy(array, keyFn, direction = "asc") {
  return [...array].sort((a, b) => {
    const aVal = keyFn(a);
    const bVal = keyFn(b);
    if (aVal < bVal) return direction === "asc" ? -1 : 1;
    if (aVal > bVal) return direction === "asc" ? 1 : -1;
    return 0;
  });
}

/**
 * Escape HTML special characters.
 * @param {string} text
 * @returns {string}
 */
export function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

/**
 * Sleep for a duration.
 * @param {number} ms
 * @returns {Promise}
 */
export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Retry a function with exponential backoff.
 * @param {Function} fn
 * @param {number} [maxAttempts=3]
 * @param {number} [delay=1000]
 * @returns {Promise}
 */
export async function retry(fn, maxAttempts = 3, delay = 1000) {
  let lastError;
  for (let i = 0; i < maxAttempts; i++) {
    try {
      return await fn();
    } catch (e) {
      lastError = e;
      if (i < maxAttempts - 1) {
        await sleep(delay * Math.pow(2, i));
      }
    }
  }
  throw lastError;
}

/**
 * Parse query string from URL.
 * @param {string} url
 * @returns {Object<string, string>}
 */
export function parseQueryString(url) {
  const params = {};
  const query = url.split("?")[1] || "";
  for (const pair of query.split("&")) {
    const [key, value] = pair.split("=").map(decodeURIComponent);
    if (key) params[key] = value;
  }
  return params;
}

/**
 * Build query string from object.
 * @param {Object} params
 * @returns {string}
 */
export function buildQueryString(params) {
  return Object.entries(params)
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");
}

/**
 * Capitalize first letter.
 * @param {string} text
 * @returns {string}
 */
export function capitalize(text) {
  if (!text) return text;
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * Truncate text with ellipsis.
 * @param {string} text
 * @param {number} maxLength
 * @returns {string}
 */
export function truncate(text, maxLength) {
  if (!text || text.length <= maxLength) return text;
  return text.slice(0, maxLength) + "…";
}
