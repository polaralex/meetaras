// The preview: the videos and slide placed as in the export (geometry from the server), the labels
// (the same images as the export), and the intro/outro.
import { $ } from "./common.js";
import { S, SPEAKER_SHOTS, label, lowerThirdStarts, segmentAt, startTime, styleOf } from "./model.js";

export const pres = $("presVid");
export const bumperVid = $("bumperVid");
export const screenVid = $("screenVid");
export const slideImg = $("slideImg");

function place(el, box) {
  el.style.left = box.x * 100 + "%";
  el.style.top = box.y * 100 + "%";
  el.style.width = box.w * 100 + "%";
  el.style.height = box.h * 100 + "%";
  el.style.visibility = "visible";
}
const FULL = { x: 0, y: 0, w: 1, h: 1 };
// Show only the `crop` part (fractions of the canvas) of a canvas-sized video, filling `box`:
// the screen recording's 16:9 core without its bars, as in the export.
function placeCropped(el, box, crop, radius) {
  const w = box.w / crop.w,
    h = box.h / crop.h;
  place(el, { x: box.x - crop.x * w, y: box.y - crop.y * h, w, h });
  const pct = (v) => `${v * 100}%`;
  el.style.clipPath = `inset(${pct(crop.y)} ${pct(1 - crop.x - crop.w)} ${pct(1 - crop.y - crop.h)} ${pct(crop.x)}${
    radius ? ` round ${radius}px` : ""
  })`;
}
function hide(el) {
  el.style.visibility = "hidden";
}
let shownSlide = null;
// Mirrors render.screen_or_presenter(): outside the screen recording, a screen layout shows the presenter.
function effectiveSegment(t) {
  const seg = segmentAt(t);
  if (!seg.layout.startsWith("screen")) return seg;
  const target = t - S.screenOffset;
  if (S.hasScreen && target >= 0 && target < S.screenDuration) return seg;
  return { ...seg, layout: "presenter", outsideScreen: true };
}
export function renderStage() {
  const t = pres.currentTime;
  const seg = effectiveSegment(t);
  const L = S.layout;
  $("screenGap").classList.add("hidden");
  $("cutShade").classList.toggle("hidden", seg.layout !== "cut");
  if (seg.layout === "presenter" || seg.layout === "cut") {
    place(pres, FULL);
    hide(slideImg);
    hide(screenVid);
  } else {
    const style = styleOf(seg.layout);
    const box = style === "full" ? null : L.styles[style];
    const content = seg.layout.startsWith("slide") ? slideImg : screenVid;
    hide(content === slideImg ? screenVid : slideImg);
    for (const el of [pres, slideImg, screenVid]) {
      el.style.borderRadius = el.style.boxShadow = el.style.clipPath = el.style.zIndex = el.style.border = "";
    }
    if (box) place(pres, box.pip);
    else hide(pres);
    // The smaller picture on top gets rounded corners and a shadow, like the rendered one.
    const insetEl = box && box.inset === "pip" ? pres : box && box.inset === "content" ? content : null;
    const W = $("stage").clientWidth;
    const radius = L.inset.radius * W;
    if (insetEl) {
      const [off, blur, opacity] = L.inset.shadow;
      insetEl.style.borderRadius = `${radius}px`;
      insetEl.style.boxShadow = `0 ${off * W}px ${blur * W}px rgba(0,0,0,${opacity})`;
      insetEl.style.zIndex = 3;
    }
    if (content === slideImg) {
      if (shownSlide !== seg.slide) {
        slideImg.src = `media/slide/${seg.slide}.png`;
        shownSlide = seg.slide;
      }
      place(slideImg, box ? box.content : FULL);
    } else {
      const target = t - S.screenOffset;
      if (!S.hasScreen || target < 0 || target >= S.screenDuration) {
        hide(screenVid);
        $("screenGap").classList.remove("hidden");
      } else {
        const crop = box ? L.screen_crop : null;
        if (crop) placeCropped(screenVid, box.content, crop, insetEl === screenVid ? radius : 0);
        else place(screenVid, box ? box.content : FULL);
        syncScreen(target);
      }
    }
  }
  if (seg.layout === "presenter" || seg.layout === "cut") pres.style.border = "none";
  renderLabels(seg, t);
  if (!seg.layout.startsWith("screen") && !screenVid.paused) screenVid.pause();
  $("modeNow").textContent = seg.outsideScreen ? "speaker (no screen recording here)" : label(seg);
  $("modeNow").style.background = seg.layout === "cut" ? "#555" : `var(--${seg.layout.replace("_", "-")})`;
  $("modeNow").style.color = seg.layout === "cut" ? "#fff" : "#0d0d10";
}
function syncScreen(target) {
  if (Math.abs(screenVid.currentTime - target) > 0.25) screenVid.currentTime = target;
  screenVid.playbackRate = pres.playbackRate;
  if (pres.paused && !screenVid.paused) screenVid.pause();
  if (!pres.paused && screenVid.paused) screenVid.play().catch(() => {});
}

// ---------- labels (same images as the export) ----------
const LABELS_FOR = {
  presenter: ["badge"],
  slide: ["strip_corner"],
  screen: ["strip_corner"],
  slide_side: ["strip_side", "panel"],
  screen_side: ["strip_side", "panel"],
  slide_speaker: ["badge"],
  screen_speaker: ["badge"],
};
export function setOverlays(overlays) {
  S.overlays = overlays || {};
  for (const name of ["badge", "strip_corner", "strip_side", "panel", "lower_third"]) {
    const el = $("ovl_" + name);
    const ov = S.overlays[name];
    if (ov) {
      if (el.getAttribute("src") !== ov.url) el.src = ov.url;
      place(el, ov.box);
    }
    el.style.visibility = "hidden";
  }
}
function renderLabels(seg, t) {
  const want = new Set(S.bumper ? [] : LABELS_FOR[seg.layout] || []);
  for (const name of ["badge", "strip_corner", "strip_side", "panel"]) {
    $("ovl_" + name).style.visibility = want.has(name) && S.overlays[name] ? "visible" : "hidden";
  }
  const lt = $("ovl_lower_third");
  const T = S.layout.lower_third;
  const start = SPEAKER_SHOTS.includes(seg.layout) && S.overlays.lower_third && !S.bumper
    ? lowerThirdStarts().find((s) => t >= s && t < s + T.in + 2 * T.fade + T.hold)
    : undefined;
  if (start === undefined) {
    lt.style.visibility = "hidden";
    return;
  }
  const local = t - start;
  const fadeIn = Math.min(1, Math.max(0, (local - T.in) / T.fade));
  const fadeOut = Math.min(1, Math.max(0, (T.in + T.fade + T.hold + T.fade - local) / T.fade));
  lt.style.transition = "none";
  lt.style.opacity = Math.min(fadeIn, fadeOut);
  lt.style.visibility = "visible";
}

// ---------- intro / outro ----------
export function playBumper(name, then = null) {
  pres.pause();
  S.bumper = name;
  bumperVid.src = `media/${name}.mp4`;
  bumperVid.classList.remove("hidden");
  bumperVid.onended = () => {
    stopBumper();
    if (then) then();
  };
  bumperVid.play().catch(() => {});
}
export function stopBumper() {
  if (!S.bumper) return;
  bumperVid.pause();
  bumperVid.classList.add("hidden");
  bumperVid.onended = null;
  S.bumper = null;
}
export function playAll() {
  stopBumper();
  S.playAll = true;
  const talk = () => {
    pres.currentTime = startTime();
    pres.play();
  };
  if (S.bumpers.intro) playBumper("intro", talk);
  else talk();
}
