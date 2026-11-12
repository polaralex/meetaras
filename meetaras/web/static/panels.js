// The talk's panels around the editor: Make video (the strip at the top), Export preview, captions and
// the YouTube dialog. They poll the server while something runs.
import { $, api, call, hideError, setLog, showError } from "./common.js";
import { S, fmt } from "./model.js";
import { save } from "./edit.js";

// ---------- export preview ----------
export async function startExport() {
  if (S.dirty) await save();
  if (await call("api/export", { method: "POST" })) pollExport();
}
export async function pollExport() {
  const st = await api("api/export");
  const running = st.state === "running";
  $("exportPreview").disabled = running;
  if (st.state === "idle") return;
  const lines = st.lines || [];
  let pct = 0;
  let step = 1,
    steps = 5;
  for (const l of lines) {
    const m = l.match(/^\[(\d)\/(\d)\]/);
    if (m) {
      [step, steps] = [+m[1], +m[2]];
      pct = ((step - 1) / steps) * 100;
    }
    const c = l.match(/clip (\d+)\/(\d+)/);
    if (c) pct = ((step - 1 + +c[1] / +c[2]) / steps) * 100;
    const f = l.match(/^\s+(loudness|export|clip|normalize): (\d+)%$/); // percentage within the current step
    if (f && !c) pct = Math.max(pct, ((step - 1 + +f[2] / 100) / steps) * 100);
  }
  if (st.state === "done") pct = 100;
  $("exportHead").textContent = `Preview export: ${st.state}`;
  $("exportProgress").classList.toggle("hidden", !running);
  $("exportProgress").value = pct;
  $("exportLink").classList.toggle("hidden", st.state !== "done");
  $("exportLink").firstChild.href = `media/output/${st.output}`;
  $("exportLink").firstChild.textContent = `Open ${st.output}`;
  $("exportLog").classList.remove("hidden");
  setLog($("exportLog"), lines.filter((l) => !/^\s+\w+: \d+%$/.test(l)));
  if (running) setTimeout(pollExport, 1000);
}

// ---------- captions ----------
export async function pollCaptions() {
  const c = await api("api/captions");
  const msg = $("captionsMsg");
  let again = 0;
  $("macHelp").textContent = "";
  if (c.transcript) {
    msg.textContent = c.srt
      ? "Ready: every export writes the captions (.srt) next to the video in the output folder."
      : "Transcript ready: the next export writes the captions (.srt) next to the video in the output folder.";
  } else if (c.writing) {
    msg.textContent = "Writing the folder for the Mac… (the first time it also copies the 574 MB model)";
    again = 1000;
  } else if (c.mac && c.mac.done === c.mac.pieces) {
    msg.textContent = "The Mac is done: importing…";
    const out = await call("api/captions/import", { method: "POST" });
    if (out) msg.textContent = `Imported from the Mac: ${out.captions} captions, written next to the video.`;
    return;
  } else if (c.mac) {
    msg.textContent = `Waiting for the Mac: ${c.mac.done} of ${c.mac.pieces} pieces done.`;
    $("macHelp").textContent = c.instructions.replace("{folder}", c.mac.folder);
    again = 5000;
  } else {
    msg.textContent = c.whisper
      ? "Make video transcribes the talk on this computer (about 1–2× the talk's length without a GPU). A Mac is faster:"
      : "whisper.cpp isn't set up here (see Setup on the home page). Transcribe on a Mac instead:";
  }
  if (c.error) showError(`Transcribe on a Mac: ${c.error}`);
  $("macBox").classList.toggle("hidden", c.transcript);
  $("macWrite").disabled = c.writing;
  if (!$("macFolder").value) $("macFolder").value = c.mac ? c.mac.folder : c.default_folder;
  if (again) setTimeout(pollCaptions, again);
}
export async function writeMacBundle() {
  if (!(await call("api/captions/mac", { method: "POST", body: { folder: $("macFolder").value } }))) return;
  hideError();
  pollCaptions();
}

// ---------- YouTube ----------
const Y = { data: null, withChapters: "", markdown: "" };
export async function loadYouTube() {
  const y = await api("api/youtube");
  const status = $("ytStatus");
  $("ytFields").classList.toggle("hidden", !y.data);
  $("ytRegenerate").disabled = $("ytSave").disabled = y.running;
  $("ytMarkdown").disabled = !y.data;
  $("ytRegenerate").textContent = y.data ? "Write again" : "Write it";
  if (y.running) {
    status.textContent = y.claude ? "Claude is writing… (about a minute)" : "Writing the chapters…";
    setTimeout(loadYouTube, 2000);
    return;
  }
  if (!y.data) {
    status.textContent = "Not written yet: Make video writes it after the captions, or press Write it.";
  } else {
    const d = y.data;
    const by = d.ai ? "Written by Claude from the transcript and the slides." : d.error ? `Claude failed (${d.error}): chapters only.`
      : "Chapters only: Claude isn't set up (see Setup on the home page).";
    status.textContent = [by, d.edited ? "You have edited it; Make video leaves it alone." : "",
      y.stale ? "The edit has changed since: chapter times may be off. Write again to update them." : ""].filter(Boolean).join(" ");
    Y.data = d;
    Y.withChapters = y.description_with_chapters;
    Y.markdown = y.markdown;
    $("ytTitle").value = d.title;
    $("ytTitles").replaceChildren(...d.titles.filter((t) => t !== d.title).map((t) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "link";
      b.textContent = t;
      b.title = "Use this title";
      b.onclick = () => ($("ytTitle").value = t);
      return b;
    }));
    $("ytDescription").value = d.description;
    $("ytChapters").value = d.chapters.map((c) => `${fmt(Math.floor(c.time), 0)} ${c.title}`).join("\n");
    $("ytTags").value = d.tags.join(", ");
  }
  if (y.error) status.textContent += ` Error: ${y.error}`;
}
export async function saveYouTube() {
  let out;
  try {
    out = await api("api/youtube", {
      method: "PUT",
      body: {
        title: $("ytTitle").value,
        description: $("ytDescription").value,
        chapters: $("ytChapters").value,
        tags: $("ytTags").value.split(","),
      },
    });
  } catch (err) {
    $("ytSaved").textContent = err.message;
    return false;
  }
  Y.withChapters = out.description_with_chapters;
  Y.markdown = out.markdown;
  $("ytSaved").textContent = "Saved (also in the output folder)";
  return true;
}
export async function regenerateYouTube() {
  if (Y.data && Y.data.edited && !confirm("Write it again? Your edits are replaced.")) return;
  await call("api/youtube/regenerate", { method: "POST", body: {} });
  loadYouTube();
}
// Everything in one Markdown text (made by the server, as in the output folder), after saving what is shown.
$("ytMarkdown").onclick = async () => {
  if (!(await saveYouTube())) return;
  await navigator.clipboard.writeText(Y.markdown);
  $("ytSaved").textContent = "Saved, and copied all as Markdown";
};
document.querySelectorAll("#youtubeDialog [data-copy]").forEach((b) =>
  b.addEventListener("click", async () => {
    if (b.dataset.copy === "description" && !(await saveYouTube())) return; // copy what is shown, with its chapters
    const text = { title: $("ytTitle").value, description: Y.withChapters, tags: $("ytTags").value }[b.dataset.copy];
    await navigator.clipboard.writeText(text);
    const label = b.textContent;
    b.textContent = "Copied";
    setTimeout(() => (b.textContent = label), 1500);
  })
);
document.querySelectorAll("#youtubeDialog input, #youtubeDialog textarea").forEach((el) => {
  el.addEventListener("keydown", (ev) => ev.stopPropagation());
  el.addEventListener("input", () => ($("ytSaved").textContent = "Not saved"));
});

// ---------- Make video ----------
const STEP_NAMES = { convert: "Convert", slides: "Slides", draft: "Draft", captions: "Captions", metadata: "Metadata", export: "Export" };
// editorStarted: the editor below is open; openEditor: opens it (set by the page) once the talk is ready.
export const M = { editorStarted: false, polling: false, openEditor: null };
function renderMake(m) {
  const st = m.status;
  const steps = $("steps");
  // A progress line, not buttons: mark, name, and for the running step its percentage.
  if (!steps.children.length) {
    for (const [key, name] of Object.entries(STEP_NAMES)) {
      steps.insertAdjacentHTML("beforeend", `<span class="step" data-step="${key}"><i></i>${name}<span class="pct"></span></span>`);
    }
  }
  const STATE_TEXT = { waiting: "not done yet", running: "running", done: "done", skipped: "skipped", failed: "failed", interrupted: "interrupted" };
  for (const el of steps.children) {
    const step = st ? st.steps[el.dataset.step] : null;
    const state = step ? step.state : "waiting";
    el.className = `step ${state}`;
    el.title = `${STEP_NAMES[el.dataset.step]}: ${STATE_TEXT[state]}${step && step.detail ? ` (${step.detail})` : ""}`;
    el.firstChild.textContent = { done: "✓", skipped: "–", failed: "✕", interrupted: "‖", running: "●" }[state] || "○";
    el.lastChild.textContent = state === "running" && step.progress != null ? ` ${Math.round(step.progress * 100)}%` : "";
  }
  const running = st && st.state === "running";
  const msg = !st
    ? "Not made yet."
    : {
        running: "Making the video… you can close this page; it keeps going.",
        review: "Draft ready: review it below, then Make video for the final export.",
        done: "Done.",
        failed: `Failed: ${st.error || "see the log"}`,
        interrupted: "Interrupted. Make video continues where it stopped.",
      }[st.state];
  $("makeMsg").textContent = msg + (m.whisper ? "" : " Captions are skipped: whisper.cpp isn't installed.");
  $("makeMsg").classList.toggle("bad", !!st && st.state === "failed");
  $("makeBtn").disabled = running;
  $("makeBtn").textContent = running ? "Making…" : "Make video";
  $("outputLink").classList.toggle("hidden", !m.output || running);
  $("openFolder").classList.toggle("hidden", !m.output || running);
  setLog($("makeLog"), m.log);
  if (st && st.state === "failed") $("makeLog").classList.remove("hidden");
}
export async function pollMake() {
  if (M.polling) return;
  M.polling = true;
  let watched = false; // a run went on while this page looked
  try {
    for (;;) {
      const m = await call("api/make");
      if (!m) return;
      renderMake(m);
      if (!M.editorStarted) {
        $("name").textContent = m.name;
        document.title = `meetaras · ${m.name}`;
        $("notReady").classList.toggle("hidden", m.ready);
        $("notReadyMsg").textContent = m.reason || "";
        if (m.ready) {
          M.editorStarted = true;
          await M.openEditor();
        }
      }
      if (!(m.status && m.status.state === "running")) {
        if (M.editorStarted && watched) pollCaptions(); // the run may have transcribed or imported captions
        return;
      }
      watched = true;
      await new Promise((r) => setTimeout(r, 1000));
    }
  } finally {
    M.polling = false;
  }
}
export async function startMake() {
  if (S.dirty) await save();
  if (!(await call("api/make", { method: "POST", body: { review: $("reviewToggle").checked } }))) return;
  hideError();
  pollMake();
}
