// Meetaras home: the talks in the library, and New talk with a file picker served by Meetaras
// (a browser can't hand a web page the path of a file, and videos are used where they are).
import { $, ApiError, api, el, showError } from "./common.js";

const H = { library: "", lastDir: "", nameEdited: false };
try {
  H.lastDir = localStorage.getItem("meetaras.lastDir") || "";
} catch {
  /* storage unavailable: start at home */
}

// ---------- the talks ----------
const STEP_NAMES = { convert: "converting", slides: "slides", draft: "drafting", captions: "captions", metadata: "YouTube text", export: "exporting" };
function stateOf(t) {
  if (t.error) return ["broken", "timeline.txt has an error"];
  const st = t.status;
  if (!st) return t.output ? ["done", "Exported"] : ["", "Not made yet"];
  if (st.state === "running") {
    const [name, step] = Object.entries(st.steps).find(([, s]) => s.state === "running") || ["", null];
    const pct = step && step.progress != null ? ` ${Math.round(step.progress * 100)}%` : "";
    return ["running", `Making: ${STEP_NAMES[name] || "starting"}${pct}`];
  }
  return {
    review: ["review", "Draft ready to review"],
    done: ["done", "Done"],
    failed: ["failed", "Failed"],
    interrupted: ["failed", "Interrupted"],
  }[st.state];
}
function ago(seconds) {
  const d = Date.now() / 1000 - seconds;
  if (d < 90) return "just now";
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  return new Date(seconds * 1000).toLocaleDateString();
}
// Polls on, also after an error: the app may come back, or the failure pass.
async function loadTalks() {
  let data = null;
  try {
    data = await api("/api/talks");
    showError("");
  } catch (err) {
    showError(err instanceof ApiError ? err.message : "meetaras isn't running any more: start it again with `meetaras`.");
  }
  if (data) showTalks(data);
  const running = data && data.talks.some((t) => t.status && t.status.state === "running");
  setTimeout(loadTalks, running ? 2000 : 10000);
}
function showTalks(data) {
  H.library = data.library;
  $("libPath").textContent = data.library;
  const body = $("talks").tBodies[0];
  body.replaceChildren();
  for (const t of data.talks) {
    const row = el("tr");
    row.dataset.id = t.id;
    const name = el("td");
    name.append(el("div", "title", t.talk || t.id), el("div", "sub", [t.speaker, t.event, t.date].filter(Boolean).join(" · ") || t.folder));
    const [cls, text] = stateOf(t);
    const state = el("td");
    state.append(el("span", `state ${cls}`, text));
    if (t.status && t.status.error) state.append(el("div", "sub", t.status.error));
    row.append(name, state, el("td", "sub", ago(t.updated)));
    row.addEventListener("click", () => (location.href = t.url));
    body.append(row);
  }
  $("empty").classList.toggle("hidden", data.talks.length > 0);
  $("talks").classList.toggle("hidden", data.talks.length === 0);
}

// ---------- new talk ----------
const form = $("newForm");
function slug(text) {
  return text
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9Ͱ-Ͽ]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
}
function folderHint() {
  const name = form.elements.name.value.trim();
  $("folderHint").textContent = name ? `The talk goes in ${H.library}/${name}` : "";
}
form.elements.talk.addEventListener("input", () => {
  if (!H.nameEdited) form.elements.name.value = slug(form.elements.talk.value);
  folderHint();
});
form.elements.name.addEventListener("input", () => {
  H.nameEdited = true;
  folderHint();
});
$("newBtn").onclick = () => {
  $("newError").classList.add("hidden");
  $("newDialog").showModal();
};
$("newCancel").onclick = () => $("newDialog").close();
form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = {};
  for (const input of form.elements) {
    if (!input.name) continue;
    body[input.name] = input.type === "checkbox" ? input.checked : input.value.trim();
  }
  if (!body.name) body.name = slug(body.talk || body.video.split("/").pop().replace(/\.[^.]*$/, ""));
  body.make = true;
  $("newSubmit").disabled = true;
  try {
    location.href = (await api("/api/talks", { method: "POST", body })).url;
  } catch (err) {
    $("newError").textContent = err.message;
    $("newError").classList.remove("hidden");
  } finally {
    $("newSubmit").disabled = false;
  }
});

// ---------- file picker ----------
const P = { kind: "video", target: null, path: "", request: 0 };
function size(n) {
  return n > 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n > 1e6 ? `${(n / 1e6).toFixed(0)} MB` : `${Math.ceil(n / 1e3)} kB`;
}
async function browse(path) {
  const request = ++P.request;
  let data;
  try {
    data = await api(`/api/files?kind=${P.kind}&path=${encodeURIComponent(path)}`);
  } catch (err) {
    if (request !== P.request) return;
    $("pickerPath").setCustomValidity(err.message);
    $("pickerPath").reportValidity();
    return;
  }
  if (request !== P.request) return; // a later click or typed path won
  $("pickerPath").setCustomValidity("");
  P.path = data.path;
  $("pickerPath").value = data.path;
  $("pickerUp").disabled = !data.parent;
  $("pickerUp").onclick = () => browse(data.parent);
  const places = $("places");
  places.replaceChildren();
  for (const p of data.places) {
    const b = el("button", "", p.label);
    b.type = "button";
    b.title = p.path;
    b.onclick = () => browse(p.path);
    places.append(b);
  }
  const entries = $("entries");
  entries.replaceChildren();
  for (const d of data.dirs) {
    const b = el("button", "dir");
    b.type = "button";
    b.append(el("span", "", d));
    b.onclick = () => browse(`${data.path}/${d}`);
    entries.append(b);
  }
  for (const f of data.files) {
    const b = el("button", "file");
    b.type = "button";
    b.append(el("span", "", f.name), el("span", "size", size(f.size)));
    b.onclick = () => choose(`${data.path}/${f.name}`);
    entries.append(b);
  }
  if (!data.dirs.length && !data.files.length) entries.append(el("p", "hint", "Nothing to choose here."));
}
function choose(path) {
  P.target.value = path;
  P.target.dispatchEvent(new Event("input"));
  H.lastDir = path.slice(0, path.lastIndexOf("/"));
  try {
    localStorage.setItem("meetaras.lastDir", H.lastDir);
  } catch {
    /* fine: it's only a convenience */
  }
  $("picker").close();
}
document.querySelectorAll("[data-pick]").forEach((b) =>
  b.addEventListener("click", () => {
    P.kind = b.dataset.pick;
    P.target = b.previousElementSibling;
    $("pickerTitle").textContent = `Choose: ${b.parentElement.firstElementChild.textContent.replace(" *", "")}`;
    const current = P.target.value.trim();
    const slash = current.lastIndexOf("/"); // the folder of the file in the field; a bare name has none
    browse(slash > 0 ? current.slice(0, slash) : slash === 0 ? "/" : H.lastDir);
    $("picker").showModal();
  })
);
$("pickerClose").onclick = () => $("picker").close();
$("pickerPath").addEventListener("input", () => P.request++); // typing wins over a listing still loading
$("pickerPath").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") {
    ev.preventDefault();
    browse($("pickerPath").value);
  }
});

// First start: say what's missing (the checks run ffmpeg and claude once, so not on every refresh).
async function checkSetup() {
  const data = await api("/api/setup");
  const missing = data.checks.filter((c) => c.required && !c.ok).map((c) => c.name);
  const captions = data.checks.some((c) => c.action === "captions");
  const b = $("setupBanner");
  if (!missing.length && !captions) return;
  b.classList.toggle("error", missing.length > 0);
  b.textContent = missing.length
    ? `meetaras needs ${missing.join(" and ")} before it can make videos. `
    : "Captions aren't set up yet (videos are made without them). ";
  const link = el("a", "", "Open Setup");
  link.href = "/setup";
  b.append(link);
  b.classList.remove("hidden");
}

loadTalks();
checkSetup();
