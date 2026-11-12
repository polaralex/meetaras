// Meetaras talk page: the Make video strip and the editor. The editor reads and writes timeline.txt
// through the API and previews layouts with CSS. URLs are relative to the talk page (/talk/<id>/).
// Modules: model.js (the edit, times, parts), stage.js (preview), edit.js (edits, saving), bar.js
// (timeline bar, side panel), panels.js (Make video, Export preview, captions, YouTube); this one
// wires keys and buttons and opens the editor.
import { $, call, showError } from "./common.js";
import { FPS, S, endTime, fmt, lastSlideBefore, segmentAt, startTime } from "./model.js";
import { bumperVid, playAll, playBumper, pres, renderStage, screenVid, setOverlays, stopBumper } from "./stage.js";
import { apply, commit, currentSlideMode, deleteSelected, redo, select, setStatus, setTrim, slideChange, split, stepSlide, undo } from "./edit.js";
import { buildStrip, renderAll, renderEvents, renderPlayhead, renderStrip, renderTimeline } from "./bar.js";
import { M, loadYouTube, pollCaptions, pollExport, pollMake, regenerateYouTube, saveYouTube, startExport, startMake, writeMacBundle } from "./panels.js";

// ---------- playback loop ----------
let lastSeg = null;
function tick() {
  if (S.bumper) {
    $("clock").textContent = `${S.bumper} ${fmt(bumperVid.currentTime)} / ${fmt(S.bumpers[S.bumper].duration, 0)}`;
    $("playBtn").textContent = bumperVid.paused ? "▶" : "❚❚";
    return requestAnimationFrame(tick);
  }
  $("clock").textContent = `${fmt(pres.currentTime)} / ${fmt(S.duration, 0)}`;
  $("playBtn").textContent = pres.paused ? "▶" : "❚❚";
  const seg = segmentAt(pres.currentTime);
  const key = seg && `${seg.index}:${seg.layout}:${seg.slide}`;
  if (key !== lastSeg) {
    lastSeg = key;
    renderEvents();
    renderStrip();
  }
  if (!pres.paused && seg && seg.layout === "cut") pres.currentTime = Math.min(seg.end, S.duration - 0.05); // play as exported
  renderStage();
  renderPlayhead();
  if (!pres.paused && pres.currentTime >= endTime()) {
    pres.pause();
    if (S.playAll && S.bumpers.outro) playBumper("outro");
    else S.playAll = false;
  }
  requestAnimationFrame(tick);
}

// ---------- keys ----------
function togglePlay() {
  if (S.bumper) {
    bumperVid.paused ? bumperVid.play() : bumperVid.pause();
    return;
  }
  if (pres.paused) {
    if (pres.currentTime >= endTime() - 0.05) pres.currentTime = startTime();
    pres.play();
  } else pres.pause();
}
function seek(dt) {
  stopBumper();
  pres.currentTime = Math.max(0, Math.min(S.duration - 0.05, pres.currentTime + dt));
}
// Every editing action, used by both the keys and the button bar above the timeline.
const slideHere = () => currentSlideMode().slide || lastSlideBefore(pres.currentTime);
const onScreen = () => currentSlideMode().layout.startsWith("screen");
function screenChange(style) {
  if (!S.hasScreen) return showError("No screen recording: add `screen <file>` to the timeline header first.");
  apply({ kind: "screen", slide: null, style });
}
const ACTIONS = {
  play: togglePlay,
  back5: () => seek(-5),
  back1: () => seek(-1),
  fwd1: () => seek(1),
  fwd5: () => seek(5),
  frameBack: () => (pres.pause(), seek(-1 / FPS)),
  frameFwd: () => (pres.pause(), seek(1 / FPS)),
  nextSlide: () => stepSlide(1),
  prevSlide: () => stepSlide(-1),
  slide: () => apply(slideChange(slideHere(), "")),
  side: () => apply(slideChange(slideHere(), "side")),
  // Full screen / speaker large for whatever is shown: the screen recording if that's on, otherwise the slide.
  full: () => (onScreen() ? screenChange("full") : apply(slideChange(slideHere(), "full"))),
  speakerLarge: () => (onScreen() ? screenChange("speaker") : apply(slideChange(slideHere(), "speaker"))),
  speaker: () => apply({ kind: "presenter", slide: null, style: "" }),
  screen: () => screenChange(""),
  screenSide: () => screenChange("side"),
  cut: () => apply({ kind: "cut", slide: null, style: "" }),
  split,
  trimStart: () => setTrim("start"),
  trimEnd: () => setTrim("end"),
  delete: deleteSelected,
  escape: () => (S.bumper ? (stopBumper(), (S.playAll = false)) : select(null)),
  playAll,
  help: openHelp,
  undo,
  redo,
};
function keyAction(ev) {
  const k = ev.key.length === 1 ? ev.key.toLowerCase() : ev.key;
  const shift = ev.shiftKey;
  if (ev.ctrlKey || ev.metaKey) return k === "z" ? (shift ? "redo" : "undo") : null;
  return {
    " ": "play", ArrowLeft: shift ? "back1" : "back5", ArrowRight: shift ? "fwd1" : "fwd5",
    ",": "frameBack", ".": "frameFwd", n: "nextSlide", b: "prevSlide",
    s: shift ? "side" : "slide", f: "full", v: "speakerLarge", p: "speaker", c: shift ? "screenSide" : "screen",
    x: "cut", k: "split", "[": "trimStart", "]": "trimEnd", Delete: "delete", Backspace: "delete",
    Escape: "escape", Home: "playAll", "?": "help",
  }[k];  // prettier-ignore
}
document.addEventListener("keydown", (ev) => {
  if (!M.editorStarted || ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT") return;
  if ($("helpDialog").open || $("youtubeDialog").open) return; // dialogs handle their own keys
  const action = keyAction(ev);
  if (!action) return;
  ACTIONS[action]();
  ev.preventDefault();
});
document.querySelectorAll("#toolbar [data-action]").forEach((b) => {
  b.addEventListener("mousedown", (ev) => ev.preventDefault()); // keep focus off buttons, so Space stays play/pause
  b.addEventListener("click", () => ACTIONS[b.dataset.action]());
});

// ---------- help ----------
function openHelp() {
  pres.pause();
  $("helpDialog").showModal();
}
function maybeShowHelpOnce() {
  try {
    if (!localStorage.getItem("meetaras.helpSeen")) {
      localStorage.setItem("meetaras.helpSeen", "1");
      openHelp();
    }
  } catch {
    /* storage unavailable: just don't auto-open */
  }
}

// ---------- wiring ----------
$("helpBtn").onclick = openHelp;
$("playBtn").onclick = togglePlay;
$("introBtn").onclick = () => playBumper("intro");
$("outroBtn").onclick = () => playBumper("outro");
$("playAllBtn").onclick = playAll;
$("undoBtn").onclick = undo;
$("redoBtn").onclick = redo;
$("exportPreview").onclick = startExport;
$("makeBtn").onclick = startMake;
$("macWrite").onclick = writeMacBundle;
$("openFolder").onclick = () => call("api/open-folder", { method: "POST" });
$("youtubeBtn").onclick = () => {
  pres.pause();
  $("ytSaved").textContent = "";
  $("youtubeDialog").showModal();
  loadYouTube();
};
$("ytSave").onclick = saveYouTube;
$("ytRegenerate").onclick = regenerateYouTube;
$("logBtn").onclick = () => {
  $("makeLog").classList.toggle("hidden");
  $("makeLog").scrollTop = $("makeLog").scrollHeight;
};
$("speed").onchange = (e) => (pres.playbackRate = Number(e.target.value));
$("zoom").oninput = (e) => {
  const fit = $("timelineWrap").clientWidth / S.duration;
  S.pxPerSec = fit * Math.pow(60, Number(e.target.value) / 100); // up to 60× (about 1 s ≈ 40 px for a 55 min talk)
  renderTimeline();
  const x = (pres.currentTime / S.duration) * $("timeline").clientWidth;
  $("timelineWrap").scrollLeft = x - $("timelineWrap").clientWidth / 2;
};
document.querySelectorAll("[data-nudge]").forEach((b) =>
  b.addEventListener("click", () => commit(() => (S.screenOffset = Math.round((S.screenOffset + Number(b.dataset.nudge)) * 10) / 10)))
);
$("screenOffset").addEventListener("keydown", (ev) => ev.stopPropagation());
$("screenOffset").addEventListener("change", (e) => {
  const v = Number(e.target.value);
  if (Number.isFinite(v)) commit(() => (S.screenOffset = v));
});
$("reloadBtn").onclick = async () => {
  const body = await call("api/reload", { method: "POST" });
  if (!body) return;
  S.events = body.events;
  S.screenOffset = body.screen_offset;
  S.mtime = body.mtime;
  S.info = body.info;
  setOverlays(body.overlays);
  S.undo = [];
  S.redo = [];
  S.selected = null;
  $("conflict").classList.add("hidden");
  setStatus("Reloaded from disk");
  renderAll();
};
function fitStage() {
  const box = $("stageBox");
  const w = Math.min(box.clientWidth, (box.clientHeight * 16) / 9);
  $("stage").style.width = Math.floor(w) + "px";
  $("stage").style.height = Math.floor((w * 9) / 16) + "px";
}
new ResizeObserver(fitStage).observe($("stageBox"));
window.addEventListener("resize", renderTimeline);
window.addEventListener("beforeunload", (ev) => {
  if (S.dirty) ev.preventDefault();
});

async function initEditor() {
  const st = await call("api/state");
  if (!st) return;
  $("editorArea").classList.remove("hidden");
  document.querySelectorAll(".editorOnly").forEach((el) => el.classList.add("shown"));
  Object.assign(S, {
    duration: st.duration,
    slides: st.slides,
    hasScreen: st.has_screen,
    screenDuration: st.screen_duration,
    layout: st.layout,
    events: st.timeline.events,
    screenOffset: st.timeline.screen_offset,
    mtime: st.timeline.mtime,
    info: st.timeline.info,
  });
  setOverlays(st.timeline.overlays);
  const { compression, resolution, ...labels } = st.timeline.info;
  if (!Object.values(labels).some((v) => v)) $("infoBox").open = true; // invite filling it in
  if (st.camera) {
    const screen = st.screen_size ? `, the screen recording ${st.screen_size[0]}×${st.screen_size[1]}` : "";
    $("cameraSize").textContent = `The camera video is ${st.camera[0]}×${st.camera[1]}${screen}. `;
  }
  $("screenRow").classList.toggle("hidden", !st.has_screen);
  S.bumpers = st.bumpers || {};
  const { intro, outro } = S.bumpers;
  if (intro || outro) {
    const parts = [intro && `${intro.file} before`, outro && `${outro.file} after`].filter(Boolean);
    $("bumpers").textContent = `Export adds ${parts.join(" and ")} the talk. ▶ All plays everything as exported.`;
    $("bumpers").classList.remove("hidden");
    $("playAllBtn").classList.remove("hidden");
  }
  $("introBtn").classList.toggle("hidden", !intro);
  $("outroBtn").classList.toggle("hidden", !outro);
  pres.src = "media/presenter.mp4";
  if (st.has_screen) screenVid.src = "media/screen.mp4";
  pres.currentTime = startTime();
  S.pxPerSec = $("timelineWrap").clientWidth / S.duration;
  buildStrip();
  renderAll();
  setStatus(st.external_audio ? "Saved · preview plays the camera audio" : "Saved");
  $("helpFile").textContent = st.timeline_file.split("/").pop();
  pollExport();
  pollCaptions();
  maybeShowHelpOnce();
  requestAnimationFrame(tick);
}
M.openEditor = initEditor;
setStatus("");
pollMake();
