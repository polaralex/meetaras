// The timeline bar under the preview, the side panel (changes, video info) and the slide strip.
import { $, escapeHtml } from "./common.js";
import { S, command, endTime, fmt, isChange, label, layoutOf, parseTime, segmentAt, segments, startTime, styleOf } from "./model.js";
import { apply, commit, currentSlideMode, scheduleSave, select, slideChange, snapshot } from "./edit.js";
import { pres, renderStage, stopBumper } from "./stage.js";

export function renderAll() {
  renderStage();
  renderTimeline();
  renderEvents();
  renderStrip();
  $("screenOffset").value = S.screenOffset.toFixed(1);
  renderInfo();
  $("undoBtn").disabled = !S.undo.length;
  $("redoBtn").disabled = !S.redo.length;
  $("toolDelete").disabled = S.selected == null;
  document.querySelectorAll("#toolbar .needsScreen").forEach((b) => b.classList.toggle("hidden", !S.hasScreen));
  $("toolTarget").textContent = S.selected != null ? `Change the selected change (${fmt(S.events[S.selected].time)}) to:` : "From here, show:";
}

// ---------- timeline bar ----------
export function renderTimeline() {
  const W = Math.max($("timelineWrap").clientWidth, S.duration * S.pxPerSec);
  const px = W / S.duration;
  $("timeline").style.width = W + "px";

  const track = $("track");
  track.innerHTML = "";
  for (const seg of segments(false)) {
    const d = document.createElement("div");
    d.className = `seg ${seg.layout}` + (seg.index != null && seg.index === S.selected ? " sel" : "");
    d.style.left = seg.start * px + "px";
    d.style.width = (seg.end - seg.start) * px + "px";
    const tag = { side: " ▯▯", full: " ▣", speaker: " ◱" }[styleOf(seg.layout)] || "";
    d.textContent =
      seg.layout === "cut" ? "left out" : (seg.layout === "presenter" ? "speaker" : seg.layout.startsWith("screen") ? "screen" : seg.slide) + tag;
    d.title = `${label(seg)} · ${fmt(seg.start)}–${fmt(seg.end)}`;
    d.dataset.index = seg.index ?? "";
    if (seg.index != null) {
      const h = document.createElement("div");
      h.className = "handle";
      h.title = "Drag to move this change";
      h.addEventListener("pointerdown", (ev) => startDrag(ev, seg.index, px));
      d.appendChild(h);
    }
    track.appendChild(d);
  }

  const trim = $("trim");
  trim.innerHTML = "";
  const a = startTime(),
    b = endTime();
  if (a > 0) trim.insertAdjacentHTML("beforeend", `<div style="left:0;width:${a * px}px"></div>`);
  if (b < S.duration) trim.insertAdjacentHTML("beforeend", `<div style="left:${b * px}px;right:0"></div>`);

  const ruler = $("ruler");
  ruler.innerHTML = "";
  const steps = [1, 5, 10, 30, 60, 120, 300, 600];
  const step = steps.find((s) => s * px >= 70) || 900;
  for (let t = 0; t < S.duration; t += step) {
    ruler.insertAdjacentHTML("beforeend", `<span style="left:${t * px}px">${fmt(t, 0)}</span>`);
  }
  renderPlayhead();
}
export function renderPlayhead() {
  const W = $("timeline").clientWidth;
  const x = (pres.currentTime / S.duration) * W;
  $("playhead").style.left = x + "px";
  const wrap = $("timelineWrap");
  if (!pres.paused && (x < wrap.scrollLeft || x > wrap.scrollLeft + wrap.clientWidth - 40)) {
    wrap.scrollLeft = x - 80;
  }
}
function startDrag(ev, index, px) {
  ev.stopPropagation();
  ev.preventDefault();
  const e = S.events[index];
  const before = snapshot();
  const prev = S.events.slice(0, index).reverse().find(isChange);
  const next = S.events.slice(index + 1).find(isChange);
  const lo = prev ? prev.time + 0.1 : 0;
  const hi = next ? next.time - 0.1 : S.duration;
  const x0 = ev.clientX,
    t0 = e.time;
  S.selected = index;
  const move = (m) => {
    e.time = Math.round(Math.min(hi, Math.max(lo, t0 + (m.clientX - x0) / px)) * 10) / 10;
    pres.currentTime = e.time;
    renderAll();
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    if (e.time !== t0) {
      S.undo.push(before);
      S.redo = [];
      e.note = "";
      scheduleSave();
      renderAll();
    }
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}
$("timeline").addEventListener("click", (ev) => {
  stopBumper();
  S.playAll = false;
  const rect = $("timeline").getBoundingClientRect();
  const t = ((ev.clientX - rect.left) / rect.width) * S.duration;
  pres.currentTime = Math.max(0, Math.min(S.duration - 0.05, t));
  const seg = ev.target.closest(".seg");
  S.selected = seg && seg.dataset.index !== "" ? Number(seg.dataset.index) : null;
  renderAll();
});

// ---------- side panel ----------
export function renderEvents() {
  const box = $("events");
  const nowSeg = segmentAt(pres.currentTime);
  box.innerHTML = "";
  S.events.forEach((e, i) => {
    const row = document.createElement("div");
    const l = layoutOf(e);
    row.className = "ev" + (i === S.selected ? " sel" : "") + (nowSeg && nowSeg.index === i ? " now" : "");
    row.style.borderLeftColor = e.kind === "cut" ? "#555" : isChange(e) ? `var(--${l.layout.replace("_", "-")})` : "#666";
    const check = /CHECK/.test(e.note);
    row.innerHTML = `<input value="${fmt(e.time)}" title="Edit the time (m:ss.s)">
      <div><div class="cmd">${e.kind === "cut" ? "left out" : command(e)}</div>${e.note ? `<div class="note ${check ? "check" : ""}">${escapeHtml(e.note)}</div>` : ""}</div>
      <button title="Unsplit: join this part with the one before it">✕</button>`;
    row.addEventListener("click", (ev) => {
      if (ev.target.tagName === "INPUT" || ev.target.tagName === "BUTTON") return;
      pres.currentTime = e.time;
      select(i);
    });
    const input = row.querySelector("input");
    input.addEventListener("keydown", (ev) => {
      ev.stopPropagation();
      if (ev.key === "Enter") input.blur();
      if (ev.key === "Escape") {
        input.value = fmt(e.time);
        input.blur();
      }
    });
    input.addEventListener("change", () => {
      const t = parseTime(input.value);
      if (t == null || t < 0 || t > S.duration) {
        input.value = fmt(e.time);
        return;
      }
      commit(() => {
        e.time = t;
        e.note = "";
      });
    });
    row.querySelector("button").addEventListener("click", () =>
      commit(() => {
        S.events.splice(i, 1);
        S.selected = null;
      })
    );
    box.appendChild(row);
  });
  const sel = S.selected != null ? S.events[S.selected] : null;
  const ed = $("editing");
  ed.classList.toggle("hidden", !sel);
  if (sel) ed.textContent = `Editing the change at ${fmt(sel.time)} (${sel.kind === "cut" ? "left out" : command(sel)}). Keys and slide clicks change it; Esc to stop.`;
}
export function renderStrip() {
  const n = segmentAt(pres.currentTime).slide;
  document.querySelectorAll(".thumb").forEach((el) => el.classList.toggle("now", Number(el.dataset.n) === n));
}
export function buildStrip() {
  const strip = $("strip");
  for (let n = 1; n <= S.slides; n++) {
    const d = document.createElement("div");
    d.className = "thumb";
    d.dataset.n = n;
    d.title = `Slide ${n}: show from here (keeps the current style)`;
    d.innerHTML = `<img loading="lazy" src="media/slide/${n}.png" alt=""><span>${n}</span>`;
    d.addEventListener("click", () => {
      const l = currentSlideMode();
      apply(slideChange(n, l.layout.startsWith("slide") ? styleOf(l.layout) : ""));
    });
    strip.appendChild(d);
  }
}

// ---------- video info and sound ----------
function renderInfo() {
  document.querySelectorAll("[data-info]").forEach((input) => {
    const v = S.info[input.dataset.info] ?? "";
    if (input.type === "checkbox") input.checked = v !== false;
    else if (document.activeElement !== input) input.value = v;
  });
}
document.querySelectorAll("[data-info]").forEach((input) => {
  input.addEventListener("keydown", (ev) => ev.stopPropagation());
  input.addEventListener("change", () => {
    const key = input.dataset.info;
    const value =
      input.type === "checkbox" ? input.checked : key === "lower_third_every" ? Math.max(0, Number(input.value) || 0) : input.value.trim();
    commit(() => (S.info = { ...S.info, [key]: value }));
  });
});
