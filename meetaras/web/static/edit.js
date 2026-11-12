// Editing the changes (every edit goes through commit, for undo) and saving them to timeline.txt.
import { $, ApiError, api, hideError, showError } from "./common.js";
import { LEAD, S, isChange, lastSlideBefore, layoutOf, segmentAt, styleOf } from "./model.js";
import { renderAll } from "./bar.js";
import { pres, setOverlays } from "./stage.js";

export function snapshot() {
  return JSON.stringify({ events: S.events, screenOffset: S.screenOffset, info: S.info });
}
export function commit(mutator) {
  S.undo.push(snapshot());
  if (S.undo.length > 200) S.undo.shift();
  S.redo = [];
  mutator();
  S.events.sort((a, b) => a.time - b.time);
  scheduleSave();
  renderAll();
}
function restore(snap) {
  const d = JSON.parse(snap);
  S.events = d.events;
  S.screenOffset = d.screenOffset;
  S.info = d.info;
  S.selected = null;
  scheduleSave();
  renderAll();
}
export function undo() {
  if (!S.undo.length) return;
  S.redo.push(snapshot());
  restore(S.undo.pop());
}
export function redo() {
  if (!S.redo.length) return;
  S.undo.push(snapshot());
  restore(S.redo.pop());
}

function markTime() {
  const t = pres.currentTime - (pres.paused ? 0 : LEAD);
  return Math.max(0, Math.round(t * 10) / 10);
}

// Apply a layout: edits the selected change, or adds one at the playhead (replacing a change
// that is already within 0.2 s of it).
export function apply(change) {
  commit(() => {
    const sel = S.selected != null ? S.events[S.selected] : null;
    if (sel && isChange(sel)) {
      Object.assign(sel, change, { note: "" });
      return;
    }
    const t = markTime();
    const near = S.events.findIndex((e) => isChange(e) && Math.abs(e.time - t) < 0.2);
    if (near >= 0) Object.assign(S.events[near], change, { note: "" });
    else S.events.push({ time: t, slide: null, style: "", note: "", ...change });
  });
}
export function slideChange(n, style) {
  return { kind: "slide", slide: Math.min(Math.max(1, n), S.slides), style };
}
export function currentSlideMode() {
  const sel = S.selected != null ? S.events[S.selected] : null;
  const l = sel && isChange(sel) ? layoutOf(sel) : segmentAt(pres.currentTime);
  return l;
}
export function stepSlide(delta) {
  const l = currentSlideMode();
  const base = l.slide || lastSlideBefore(pres.currentTime);
  apply(slideChange(base + delta, l.layout.startsWith("slide") ? styleOf(l.layout) : ""));
}
export function setTrim(kind) {
  commit(() => {
    S.events = S.events.filter((e) => e.kind !== kind);
    S.events.push({ time: Math.round(pres.currentTime * 10) / 10, kind, slide: null, style: "", note: "" });
  });
}
export function deleteSelected() {
  let i = S.selected;
  if (i == null) {
    const seg = segmentAt(pres.currentTime);
    i = seg ? seg.index : null;
  }
  if (i == null) return;
  commit(() => {
    S.events.splice(i, 1);
    S.selected = null;
  });
}
export function select(i) {
  S.selected = i;
  renderAll();
}

// Split at the playhead: a new change showing the same as now, selected so that the next key or button
// sets the style of the part after it. On an existing change, just selects it.
export function split() {
  const t = markTime();
  const near = S.events.findIndex((e) => isChange(e) && Math.abs(e.time - t) < 0.2);
  if (near >= 0) return select(near);
  const seg = segmentAt(t);
  const [kind, style = ""] = seg.layout.split("_");
  const ev = { time: t, kind, slide: kind === "slide" ? seg.slide : null, style, note: "" };
  S.selected = null;
  commit(() => S.events.push(ev));
  select(S.events.indexOf(ev));
}

// ---------- saving ----------
export function setStatus(text, cls = "") {
  const el = $("saveStatus");
  el.textContent = text;
  el.className = "status " + cls;
}
export function scheduleSave() {
  S.dirty = true;
  setStatus("Unsaved changes", "dirty");
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(save, 400);
}
export async function save() {
  if (S.saving) return scheduleSave();
  S.saving = true;
  try {
    const body = await api("api/timeline", {
      method: "PUT",
      body: { events: S.events, screen_offset: S.screenOffset, info: S.info, base_mtime: S.mtime },
    });
    S.mtime = body.mtime;
    setOverlays(body.overlays);
    S.dirty = false;
    hideError();
    setStatus("Saved to timeline.txt");
  } catch (err) {
    if (!(err instanceof ApiError)) {
      setStatus("Not saved (server unreachable)", "bad");
    } else if (err.status === 412) {
      $("conflict").classList.remove("hidden"); // changed on disk since this page loaded it
      setStatus("Not saved", "bad");
    } else {
      showError(err.message || "Save failed");
      setStatus("Not saved", "bad");
    }
  } finally {
    S.saving = false;
  }
}
