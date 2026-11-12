// Meetaras Setup: the checks of `meetaras doctor`, commands to copy, and Set up captions
// (the same `meetaras doctor --fix`, run in the background).
import { $, call, el, setLog } from "./common.js";

function command(text) {
  // Lines that look like commands get a Copy button; sentences are shown as they are.
  if (!/^(sudo |brew |xcode-select|curl |npm )/.test(text)) return el("div", "hint", text);
  const row = el("div", "cmd");
  const copy = el("button", "", "Copy");
  copy.onclick = async () => {
    await navigator.clipboard.writeText(text);
    copy.textContent = "Copied";
    setTimeout(() => (copy.textContent = "Copy"), 1500);
  };
  row.append(el("code", "", text), copy);
  return row;
}

let running = false;
async function load(withChecks = true) {
  const data = await call("/api/setup");
  if (!data) return;
  if (withChecks) {
    const body = $("checks").tBodies[0];
    body.replaceChildren();
    let offered = false;
    for (const c of data.checks) {
      const row = el("tr", c.ok ? "ok" : c.required ? "missing" : "optional");
      const status = el("td");
      status.append(el("div", "", c.detail));
      for (const line of c.fix) status.append(command(line));
      if (c.action === "captions" && !offered) {
        offered = true; // one button sets up both whisper.cpp and the model
        const b = el("button", "primary", "Set up captions");
        b.id = "setupCaptions";
        b.title = `Downloads the model (checked) and builds whisper.cpp (${data.gpu}) into ${data.data}`;
        b.onclick = start;
        status.append(b);
      }
      row.append(el("td", "mark", c.ok ? "✓" : c.required ? "✕" : "–"), el("td", "title", c.name), el("td", "sub", c.purpose), status);
      body.append(row);
    }
  }
  const job = data.job;
  running = !!job && job.state === "running";
  $("job").classList.toggle("hidden", !job);
  if (job) {
    const what = { download: "Downloading the model", build: "Building whisper.cpp" }[job.step] || "Setting up captions";
    $("jobHead").textContent = {
      running: `${what}…`,
      done: "Captions are set up.",
      failed: `Setting up captions failed: ${job.error}`,
      interrupted: "Setting up captions was interrupted; start it again to continue.",
    }[job.state];
    $("jobProgress").classList.toggle("hidden", !running || job.progress == null);
    $("jobProgress").value = (job.progress || 0) * 100;
    setLog($("jobLog"), data.log);
  }
  const b = $("setupCaptions");
  if (b) b.disabled = running;
  if (running) setTimeout(() => load(false).then(() => !running && load()), 1000);
}
async function start() {
  if (await call("/api/setup/captions", { method: "POST" })) load(false);
}
$("recheck").onclick = () => load();
load();
