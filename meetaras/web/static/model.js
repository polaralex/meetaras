// The editor's model: the talk being edited (S), times in the timeline file's format, and the parts
// its changes make (mirroring timeline.py and labels.py). No DOM here.

export const FPS = 30;
export const LEAD = 0.3; // seconds subtracted from marks made while playing (reaction time)

export const S = {
  duration: 0,
  slides: 0,
  hasScreen: false,
  screenDuration: 0,
  layout: null,
  events: [], // {time, kind, slide, style, note}, sorted by time
  screenOffset: 0,
  mtime: 0,
  selected: null, // index into events
  undo: [],
  redo: [],
  pxPerSec: 1,
  saveTimer: null,
  saving: false,
  dirty: false,
  bumpers: {}, // {intro: {file, duration}, outro: {...}} from the timeline header
  bumper: null, // "intro" / "outro" while one plays in the preview
  playAll: false, // playing as exported: intro → talk → outro
  info: {}, // event, date, speaker, role, talk, lower_third_every, compression
  overlays: {}, // label images from the server: {name: {url, box}}
};

// ---------- time (same formats as the timeline file) ----------
export function fmt(t, decimals = 1) {
  const f = 10 ** decimals;
  const r = Math.round(Math.abs(t) * f) / f; // rounded first: 59.96 is 1:00.0, not 0:60.0
  const h = Math.floor(r / 3600);
  const m = Math.floor((r % 3600) / 60);
  const ss = (r % 60).toFixed(decimals).padStart(decimals ? 3 + decimals : 2, "0");
  return (t < 0 && r > 0 ? "-" : "") + (h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`);
}
export function parseTime(text) {
  const m = String(text).trim().match(/^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$/);
  if (!m) return null;
  const [, a, b, sec] = m;
  if (a !== undefined && b !== undefined) return +a * 3600 + +b * 60 + +sec;
  if (a !== undefined) return +a * 60 + +sec;
  return +sec;
}

// ---------- changes and the parts they make ----------
export const isChange = (e) => e.kind !== "start" && e.kind !== "end";
export function layoutOf(e) {
  if (e.kind === "slide") return { layout: e.style ? `slide_${e.style}` : "slide", slide: e.slide };
  if (e.kind === "screen") return { layout: e.style ? `screen_${e.style}` : "screen", slide: null };
  if (e.kind === "cut") return { layout: "cut", slide: null };
  return { layout: "presenter", slide: null };
}
export const styleOf = (layout) => (layout.includes("_") ? layout.split("_")[1] : "");
export function label(l) {
  return {
    presenter: "speaker",
    slide: `slide ${l.slide}`,
    slide_side: `slide ${l.slide} side`,
    slide_full: `slide ${l.slide} full`,
    slide_speaker: `slide ${l.slide}, speaker large`,
    screen: "screen",
    screen_side: "screen side",
    screen_full: "screen full",
    screen_speaker: "screen, speaker large",
    cut: "left out",
  }[l.layout];
}
export function command(e) {
  if (e.kind === "slide") return `slide ${e.slide}${e.style ? " " + e.style : ""}`;
  if (e.kind === "screen") return `screen${e.style ? " " + e.style : ""}`;
  return e.kind;
}
export const startTime = () => (S.events.find((e) => e.kind === "start") || { time: 0 }).time;
export const endTime = () => (S.events.find((e) => e.kind === "end") || { time: S.duration }).time;

// Segments over the whole video (the trimmed-away parts are shaded, not removed), each with the
// index of the event that starts it. Mirrors Timeline.segments() in timeline.py, which joins changes in a
// row that show the same. With `joined` false every change is its own piece, as the timeline bar shows
// them: a split is visible and can be selected and dragged before it gets its own style.
export function segments(joined = true) {
  const out = [];
  let cur = { layout: "presenter", slide: null, index: null, start: 0 };
  S.events.forEach((e, i) => {
    if (!isChange(e)) return;
    const l = layoutOf(e);
    if (joined && l.layout === cur.layout && l.slide === cur.slide) return;
    if (e.time > cur.start) out.push({ ...cur, end: e.time });
    cur = { ...l, index: i, start: e.time };
  });
  out.push({ ...cur, end: S.duration });
  return out.filter((s) => s.end > s.start);
}
export function segmentAt(t) {
  const segs = segments(false);
  return segs.find((s) => t >= s.start && t < s.end) || segs[segs.length - 1];
}
export function lastSlideBefore(t) {
  let n = null;
  for (const e of S.events) {
    if (e.time > t) break;
    if (e.kind === "slide") n = e.slide;
  }
  return n || 1;
}

// ---------- labels ----------
export const SPEAKER_SHOTS = ["presenter", "slide_speaker", "screen_speaker"]; // labels.speaker_shot()
// Mirrors labels.lower_third_segments(): first speaker shot, then again after `every` minutes.
export function lowerThirdStarts() {
  const every = Number(S.info.lower_third_every) || 0;
  const starts = [];
  let last = null;
  for (const seg of segments()) {
    if (!SPEAKER_SHOTS.includes(seg.layout) || seg.end <= startTime() || seg.start >= endTime()) continue;
    const s = Math.max(seg.start, startTime());
    if (last === null || (every > 0 && s - last >= every * 60)) {
      starts.push(s);
      last = s;
    }
  }
  return starts;
}
