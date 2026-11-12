// Shared by meetaras's pages: DOM helpers, the error banner, and calls to the app's API.

export const $ = (id) => document.getElementById(id);

export function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

export function escapeHtml(s) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

// Logs are updated in place and stay at the newest line, unless the reader has scrolled up.
export function setLog(pre, lines) {
  const text = lines.join("\n");
  if (pre.textContent === text) return;
  const atEnd = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 8;
  pre.textContent = text;
  if (atEnd) pre.scrollTop = pre.scrollHeight;
}

// The page's error banner (#error); an empty message hides it.
export function showError(msg) {
  $("error").textContent = msg || "";
  $("error").classList.toggle("hidden", !msg);
}
export const hideError = () => showError("");

// FastAPI's `detail`: a message, or for invalid input a list of {loc, msg}.
export function detailText(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => `${(d.loc || []).slice(1).join(".") || "input"}: ${d.msg}`).join("; ");
  return "";
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

// JSON to and from the app. Throws ApiError with the server's message when it says no (and the
// fetch's own error when the app isn't running).
export async function api(path, { method = "GET", body } = {}) {
  const init = body === undefined ? { method } : { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, detailText(data.detail) || `${res.status} ${res.statusText}`);
  return data;
}

// api(), showing a failure in the error banner: null then.
export async function call(path, options) {
  try {
    return await api(path, options);
  } catch (err) {
    showError(err.message);
    return null;
  }
}
