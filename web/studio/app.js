// Atlas Studio — front-end for the default /pipeline/* API.
// The server is the source of truth: the browser keeps only the run_id, and every
// screen is rebuilt from GET /pipeline/status (+ blueprint). No build step.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const STEPS = [
  { n: 1, title: "Config & source", sub: "Create a run" },
  { n: 2, title: "Director", sub: "Blueprint" },
  { n: 3, title: "Cast", sub: "Characters & voices" },
  { n: 4, title: "Assets", sub: "Backgrounds & storyboards" },
  { n: 5, title: "Scenes", sub: "Video + voice" },
  { n: 6, title: "Music", sub: "Background bed" },
  { n: 7, title: "Render", sub: "Final video" },
];

const state = {
  runId: null,
  status: null,
  blueprint: null,      // resolved blueprint.json
  placeholder: null,    // pre-select snapshot (placeholder ids), once /select has run
  characters: null,
  voices: null,
  cast: {},             // placeholder_id -> { char_id, audio_id }
  step: 1,
  working: false,       // a request is in flight: all paid buttons disabled
  poll: null,
  bust: Date.now(),     // cache-buster for media after regeneration
};

// ------------------------------------------------------------------ helpers
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "disabled") el.disabled = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function fmtErr(detail) {
  if (typeof detail === "string") return detail;
  if (detail && Array.isArray(detail.errors)) return detail.errors.join("\n");
  if (Array.isArray(detail)) return detail.map(d => d.msg ? `${(d.loc || []).join(".")}: ${d.msg}` : JSON.stringify(d)).join("\n");
  return JSON.stringify(detail);
}

class ApiError extends Error {
  constructor(status, detail) { super(fmtErr(detail)); this.status = status; this.detail = detail; }
}

async function api(path, options = {}) {
  const res = await fetch(`/pipeline${path}`, options);
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON body */ }
  if (!res.ok) throw new ApiError(res.status, data ? (data.detail ?? data) : res.statusText);
  return data;
}

const post = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

function notice(message, error = false) {
  const box = $("#notice");
  box.textContent = message;
  box.classList.toggle("error", error);
  box.classList.remove("hidden");
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (!error) setTimeout(() => box.classList.add("hidden"), 6000);
}

function showBusy(title, text) {
  $("#busyTitle").textContent = title;
  $("#busyText").textContent = text || "Please keep this page open.";
  $("#busy").classList.remove("hidden");
}
const hideBusy = () => $("#busy").classList.add("hidden");

// In-page replacement for window.confirm(). The native popup returns false without
// showing anything once a browser suppresses dialogs (or in an embedded browser that
// has none), which made a paid button look dead: no prompt, no request.
function confirmAction(message, okLabel = "Continue") {
  return new Promise(resolve => {
    const done = answer => { document.removeEventListener("keydown", onKey); overlay.remove(); resolve(answer); };
    const onKey = e => { if (e.key === "Escape") done(false); };
    const overlay = h("div", { class: "busy", role: "alertdialog", "aria-modal": "true" },
      h("div", { class: "busy-card" },
        h("p", { style: "white-space:pre-line;color:inherit" }, message),
        h("div", { class: "actions", style: "justify-content:center;margin-top:18px" },
          h("button", { class: "button secondary", onclick: () => done(false) }, "Cancel"),
          h("button", { class: "button danger", onclick: () => done(true) }, okLabel))));
    document.addEventListener("keydown", onKey);
    document.body.append(overlay);
  });
}

// Run one action with the page locked. Paid actions only ever run from a click.
async function run(title, text, fn, { overlay = true } = {}) {
  if (state.working) return;
  state.working = true;
  renderAll();
  if (overlay) showBusy(title, text);
  try {
    return await fn();
  } catch (e) {
    notice(`${title} failed${e.status ? ` (${e.status})` : ""}:\n${e.message}`, true);
  } finally {
    state.working = false;
    hideBusy();
    await refresh();
  }
}

const media = rel => `/pipeline/files/${state.runId}/${rel}?t=${state.bust}`;

// ------------------------------------------------------------------ state
async function refresh() {
  if (!state.runId) { renderAll(); return; }
  try {
    state.status = await api(`/status?run_id=${state.runId}`);
    if (state.status.blueprint) {
      const b = await api(`/blueprint?run_id=${state.runId}`);
      state.blueprint = b.blueprint;
      state.placeholder = b.placeholder;
    } else {
      state.blueprint = state.placeholder = null;
    }
  } catch (e) {
    if (e.status === 404) { setRun(null); notice("That run no longer exists.", true); return; }
    notice(`Could not load run: ${e.message}`, true);
  }
  await refreshCost();
  renderAll();
}

async function refreshCost() {
  const total = $("#costTotal"), note = $("#costNote"), card = $(".cost-card");
  if (!state.runId) { total.textContent = "—"; note.textContent = "Select a run to see its cost."; card.classList.remove("warn"); return; }
  try {
    const c = await api(`/cost?run_id=${state.runId}`);
    const unpriced = (c.unpriced || []).length > 0;
    total.textContent = unpriced ? `≥ $${c.total_usd.toFixed(2)}` : `$${c.total_usd.toFixed(2)}`;
    note.textContent = unpriced ? "Undercount: some usage has no price on file." : "Across all stages of this run.";
    card.classList.toggle("warn", unpriced);
  } catch { total.textContent = "—"; note.textContent = "Cost unavailable."; }
}

function setRun(id) {
  state.runId = id;
  state.status = state.blueprint = state.placeholder = null;
  state.cast = {};
  state.bust = Date.now();
  try { id ? localStorage.setItem("atlas_studio_run", id) : localStorage.removeItem("atlas_studio_run"); } catch { /* storage blocked */ }
  $("#runPill").textContent = id || "No run selected";
  if (!id) { go(1); renderAll(); return; }
  refresh().then(() => go(furthestStep()));
}

// First step the user still has to act on.
function furthestStep() {
  const s = state.status;
  if (!s) return 1;
  if (!s.source) return 1;
  if (!s.blueprint) return 2;
  if (!s.selected) return 3;
  if (s.backgrounds === 0) return 4;
  if (s.scenes_total === 0 || s.scenes_ready < s.scenes_total) return 5;
  if (!s.final) return 7;
  return 7;
}

// Which steps are reachable, expressed as the same preconditions the API enforces.
function unlocked(n) {
  const s = state.status;
  if (n === 1) return true;
  if (!s) return false;
  switch (n) {
    case 2: return s.source;
    case 3: return s.blueprint;
    case 4: return s.selected;
    case 5: return s.backgrounds > 0;
    case 6: return s.blueprint;
    case 7: return s.scenes_total > 0 && s.scenes_ready === s.scenes_total;
  }
}

function isDone(n) {
  const s = state.status;
  if (!s) return false;
  switch (n) {
    case 1: return s.source;
    case 2: return s.blueprint;
    case 3: return s.selected;
    case 4: return s.backgrounds > 0;
    case 5: return s.scenes_total > 0 && s.scenes_ready === s.scenes_total;
    case 6: return s.music;
    case 7: return s.final;
  }
}

function go(n) {
  state.step = n;
  $$(".stage").forEach(el => el.classList.toggle("active", Number(el.dataset.stage) === n));
  $$(".nav-step").forEach(b => b.classList.toggle("active", Number(b.dataset.step) === n));
  window.scrollTo({ top: 0, behavior: "smooth" });
  // lazy library loads, only when the step is first opened
  if (n === 3) loadLibraries();
  if (n === 1) loadRuns();
}

// ------------------------------------------------------------------ rendering
function renderAll() {
  renderNav();
  renderDirector();
  renderCast();
  renderAssets();
  renderScenes();
  renderMusic();
  renderRender();
  $$(".stage").forEach(el => el.classList.toggle("active", Number(el.dataset.stage) === state.step));
  $("#createRun").disabled = state.working;
}

function renderNav() {
  const nav = $("#stepNav");
  nav.replaceChildren(...STEPS.map(st => h("button", {
    class: `nav-step${state.step === st.n ? " active" : ""}${isDone(st.n) ? " done" : ""}`,
    "data-step": st.n,
    disabled: !unlocked(st.n) || state.working,
    onclick: () => go(st.n),
  }, h("b", {}, isDone(st.n) ? "✓" : String(st.n).padStart(2, "0")), h("span", {}, st.title, h("small", {}, st.sub)))));
}

function head(n, title, text, chip) {
  return h("div", { class: "stage-head" },
    h("div", {}, h("span", { class: "stage-kicker" }, `Step 0${n}`), h("h2", {}, title), h("p", {}, text)),
    chip ? h("span", { class: `status-chip ${chip.cls}` }, chip.text) : null);
}

function locked(stageEl, n, why) {
  stageEl.replaceChildren(head(n, STEPS[n - 1].title, STEPS[n - 1].sub), h("div", { class: "panel empty-state" }, why));
}

const spendHint = text => h("span", { class: "cost-hint" }, `Uses credits: ${text}`);

// ---- Step 2: director
function renderDirector() {
  const el = $("#stage2"), s = state.status;
  if (!unlocked(2)) return locked(el, 2, "Create a run with a source file first.");
  const bp = state.blueprint;
  const kids = [head(2, "Director", "GPT-4o reads the source and writes the blueprint: scenes, characters, backgrounds and music.",
    { cls: bp ? "ok" : "", text: bp ? "Blueprint ready" : "Not generated" })];

  const btn = h("button", {
    class: `button ${bp ? "danger" : "primary"}`, disabled: state.working,
    onclick: () => generateBlueprint(!!bp),
  }, bp ? "Regenerate blueprint" : "Generate blueprint →");
  kids.push(h("div", { class: "approval-bar" },
    spendHint("GPT-4o"), h("div", { class: "actions" }, btn,
      bp ? h("button", { class: "button primary", onclick: () => go(3) }, "Continue to cast →") : null)));

  if (bp) {
    kids.push(h("div", { class: "metric-row" },
      metric("Title", bp.title), metric("Scenes", bp.scenes.length),
      metric("Characters", bp.character_count), metric("Backgrounds", bp.backgrounds.length)));
    kids.push(h("div", { class: "panel" }, h("h3", {}, "Style & music"),
      h("p", {}, h("strong", {}, "Style: "), bp.style), h("p", {}, h("strong", {}, "Music: "), bp.music_style)));
    kids.push(h("div", { class: "panel" }, h("h3", {}, "Scenes"),
      h("table", {}, h("thead", {}, h("tr", {}, ["#", "Type", "Topic", "Narration", "Length"].map(t => h("th", {}, t)))),
        h("tbody", {}, bp.scenes.map((sc, i) => h("tr", {},
          h("td", {}, `${i + 1}. ${sc.id}`),
          h("td", {}, h("span", { class: `tag${sc.type === "B-Roll" ? " broll" : ""}` }, sc.type === "B-Roll" ? `B-Roll${sc.broll_role ? ` · ${sc.broll_role}` : ""}` : "Speaking")),
          h("td", {}, sc.topic || "—"),
          h("td", {}, sc.text || "—"),
          h("td", {}, sc.length ? `${sc.length}s` : "—")))))));
  }
  el.replaceChildren(...kids);
}

const metric = (label, value) => h("div", {}, h("span", {}, label), h("strong", {}, String(value)));

async function generateBlueprint(force) {
  if (force && !await confirmAction("Regenerate the blueprint?\n\nThis spends credits and DELETES your character/voice selection for this run. Generated assets are not removed but may no longer match.", "Regenerate")) return;
  return run("Directing", "GPT-4o is writing the blueprint (can take a minute).", async () => {
    await post("/direct", { run_id: state.runId, force });
    state.cast = {};
    notice("Blueprint generated.");
  });
}

// ---- Step 3: cast
async function loadLibraries() {
  if (!state.runId || !unlocked(3)) return;
  // independent calls: the ElevenLabs voice list is the slow one, so don't queue it
  // behind the characters
  const chars = state.characters ? null : api("/characters").then(
    r => { state.characters = r; renderCast(); },
    e => notice(`Character library: ${e.message}`, true));
  const voices = state.voices ? null : api("/audios").then(
    r => { state.voices = r; renderCast(); },
    e => notice(`Voice list: ${e.message}`, true));
  await Promise.all([chars, voices]);
}

function placeholderIds() {
  const bp = state.placeholder || state.blueprint;
  const ids = new Set();
  (bp?.scenes || []).forEach(sc => (sc.characters || []).forEach(c => ids.add(c)));
  return [...ids];
}

function castProblems() {
  const ids = placeholderIds(), problems = [];
  const chosen = ids.map(p => state.cast[p]);
  ids.forEach((p, i) => {
    if (!chosen[i]?.char_id) problems.push(`${p}: pick a character`);
    else if (!chosen[i].audio_id) problems.push(`${p}: pick a voice`);
  });
  const seen = new Map();
  ids.forEach((p, i) => {
    const c = chosen[i]?.char_id;
    if (!c) return;
    if (seen.has(c)) problems.push(`${p} and ${seen.get(c)} use the same character`);
    seen.set(c, p);
  });
  return problems;
}

function renderCast() {
  const el = $("#stage3");
  if (!unlocked(3)) return locked(el, 3, "Generate the blueprint first.");
  const s = state.status, ids = placeholderIds();
  const kids = [head(3, "Cast", `Choose one library character and one voice for each of the ${ids.length} role(s) in this video.`,
    { cls: s.selected ? "ok" : "", text: s.selected ? "Selection saved" : "Not selected" })];

  if (!state.characters || !state.voices) {
    kids.push(h("div", { class: "panel empty-state" }, "Loading characters and voices…"));
    return el.replaceChildren(...kids);
  }
  if (s.selected) kids.push(h("p", { class: "muted" }, "A selection is already saved. Submitting again replaces it (free)."));

  ids.forEach(p => {
    const cur = state.cast[p] || {};
    const takenByOthers = new Set(ids.filter(o => o !== p).map(o => state.cast[o]?.char_id).filter(Boolean));
    const grid = h("div", { class: "char-grid" }, state.characters.map(c => h("button", {
      class: `char-card${cur.char_id === c.char_id ? " selected" : ""}`,
      disabled: takenByOthers.has(c.char_id) || state.working,
      title: c.description || c.char_id,
      onclick: () => { state.cast[p] = { ...cur, char_id: c.char_id }; renderCast(); },
    }, h("img", { src: `/pipeline/characters/${encodeURIComponent(c.char_id)}/image`, alt: c.char_id, loading: "lazy" }),
      h("div", {}, c.char_id.replace(/^char_/, "")))));

    const voice = state.voices.find(v => (v.voice_id ?? v.audio_id) === cur.audio_id);
    const select = h("select", {
      onchange: e => { state.cast[p] = { ...cur, audio_id: e.target.value }; renderCast(); },
    }, h("option", { value: "" }, "Choose a voice…"),
      state.voices.map(v => h("option", {
        value: v.voice_id, selected: v.voice_id === cur.audio_id,
      }, `${v.name} · ${v.gender}${v.languages?.length ? ` · ${v.languages.join(", ")}` : ""}`)));
    kids.push(h("div", { class: "cast-row" }, h("h4", {}, p), grid,
      h("div", { class: "voice-line" }, select,
        voice?.preview_url ? h("audio", { controls: true, src: voice.preview_url, preload: "none" }) : null)));
  });

  const problems = castProblems();
  if (problems.length) kids.push(h("ul", { class: "errors" }, problems.map(x => h("li", {}, x))));
  kids.push(h("div", { class: "approval-bar" }, h("span", { class: "muted" }, "Free. Saves the selection and resolves the blueprint."),
    h("button", { class: "button primary", disabled: problems.length > 0 || state.working, onclick: submitCast },
      "Save selection →")));
  el.replaceChildren(...kids);
}

function submitCast() {
  const selections = placeholderIds().map(p => ({
    placeholder_id: p, char_id: state.cast[p].char_id, audio_id: state.cast[p].audio_id,
  }));
  return run("Saving selection", "", async () => {
    await post("/select", { run_id: state.runId, voice_source: "elevenlabs", selections });
    notice("Selection saved.");
    state.step = 4;
  }, { overlay: false });
}

// ---- Step 4: assets
function renderAssets() {
  const el = $("#stage4"), s = state.status;
  if (!unlocked(4)) return locked(el, 4, "Save the character and voice selection first.");
  const bp = state.blueprint, have = s.backgrounds > 0;
  const kids = [head(4, "Assets", "GPT Image 2 paints the backgrounds and the 2×2 storyboards that Veo uses as references.",
    { cls: have ? "ok" : "", text: have ? `${s.backgrounds} backgrounds · ${s.storyboards} storyboards` : "Not generated" })];

  kids.push(h("div", { class: "approval-bar" }, spendHint("GPT Image 2"), h("div", { class: "actions" },
    have
      ? [h("button", { class: "button secondary", disabled: state.working, onclick: () => genAssets("resume") }, "Resume (fill missing)"),
         h("button", { class: "button danger", disabled: state.working, onclick: () => genAssets("force") }, "Regenerate all"),
         h("button", { class: "button primary", onclick: () => go(5) }, "Continue to scenes →")]
      : h("button", { class: "button primary", disabled: state.working, onclick: () => genAssets("new") }, "Generate assets →"))));

  if (have) {
    kids.push(h("div", { class: "panel" }, h("h3", {}, "Backgrounds"), gallery("backgrounds", bp.backgrounds.map(b => b.id))));
    kids.push(h("div", { class: "panel" }, h("h3", {}, "Storyboards"), gallery("storyboard", bp.storyboard.map(b => b.id))));
  }
  el.replaceChildren(...kids);
}

function gallery(dir, ids) {
  return h("div", { class: "gallery" }, ids.map(id => h("figure", {},
    h("img", { src: media(`${dir}/${id}.png`), alt: id, loading: "lazy", onerror: e => { e.target.replaceWith(h("div", { class: "empty-state" }, "missing")); } }),
    h("figcaption", {}, id))));
}

async function genAssets(mode) {
  if (mode === "force" && !await confirmAction("Regenerate ALL backgrounds and storyboards? This spends credits again.", "Regenerate")) return;
  return run("Generating assets", "Painting backgrounds and storyboards…", async () => {
    await post("/assets", { run_id: state.runId, force: mode === "force", resume: mode === "resume" });
    state.bust = Date.now();
    notice("Assets generated.");
  });
}

// ---- Step 5: scenes
function renderScenes() {
  const el = $("#stage5"), s = state.status;
  if (!unlocked(5)) return locked(el, 5, "Generate assets first.");
  const done = s.scenes_ready === s.scenes_total && s.scenes_total > 0;
  const any = s.scenes_ready > 0 || s.veo_raw > 0;
  const kids = [head(5, "Scenes", "MiniMax H3 generates each clip, then ElevenLabs voices it and the clip is trimmed to the speech. Scenes run one after another and take several minutes each.",
    { cls: done ? "ok" : any ? "warn" : "", text: `${s.scenes_ready}/${s.scenes_total} ready` })];

  kids.push(h("div", { class: "panel" }, h("div", { class: "field" },
    h("label", { for: "diarizer" }, "Word-timing method"),
    h("select", { id: "diarizer", disabled: state.working },
      h("option", { value: "gemini" }, "Gemini (billed per scene)"),
      h("option", { value: "whisper" }, "Whisper (local, needs optional install)")))));

  kids.push(h("div", { class: "panel" }, h("div", { class: "field" },
    h("label", { for: "turbo" }, h("input", { id: "turbo", type: "checkbox", disabled: state.working, checked: state.turbo, onchange: e => { state.turbo = e.target.checked; } }),
      " Turbo mode — 4 steps + speed LoRA, faster but may be lower quality. Use the same run to A/B against normal."))));

  const act = (label, cls, mode) => h("button", { class: `button ${cls}`, disabled: state.working, onclick: () => genScenes(mode) }, label);
  kids.push(h("div", { class: "approval-bar" }, spendHint("MiniMax H3 + ElevenLabs + diarizer"), h("div", { class: "actions" },
    !any ? act("Generate scenes →", "primary", "new")
      : [done ? null : act("Resume (generate missing)", "primary", "resume"),
         s.veo_raw > 0 ? act("Re-run voice only (no video)", "secondary", "tts_only") : null,
         act("Regenerate all", "danger", "force"),
         done ? h("button", { class: "button primary", onclick: () => go(6) }, "Continue →") : null])));

  const pct = s.scenes_total ? Math.round(100 * s.scenes_ready / s.scenes_total) : 0;
  const readyIds = new Set(s.scene_states.filter(x => x.ready).map(x => x.id));
  // One scene on its own: it may be generated or redone without touching the rest. A
  // scene that continues from an earlier one needs that one first (its last frame is the seed).
  const sceneActions = sc => {
    const blocker = sc.continues_from && !readyIds.has(sc.continues_from) ? sc.continues_from : null;
    const btn = (label, cls, mode, title) => h("button", {
      class: `button ${cls} small`, disabled: state.working || (mode !== "tts_only" && !!blocker),
      title: (mode !== "tts_only" && blocker) ? `Generate ${blocker} first - this scene continues from its last frame` : title,
      onclick: () => genScenes(mode, sc.id),
    }, label);
    return h("div", { class: "actions" },
      sc.ready ? btn("Regenerate", "danger", "force", "Run the video model again for this scene only")
               : btn("Generate", "primary", "new", "Generate this scene only"),
      sc.type !== "B-Roll" && sc.raw ? btn("Re-voice", "secondary", "tts_only", "Redo the voice on the existing clip - no video generation") : null,
      blocker ? h("small", { class: "muted" }, `needs ${blocker}`) : null);
  };
  kids.push(h("div", { class: "panel" }, h("div", { class: "progress" }, h("i", { style: `width:${pct}%` })),
    h("table", {}, h("thead", {}, h("tr", {}, ["Scene", "Type", "Mode", "Video clip", "Voiced / ready", "Preview", "This scene"].map(t => h("th", {}, t)))),
      h("tbody", {}, s.scene_states.map(sc => h("tr", {},
        h("td", {}, sc.id),
        h("td", {}, h("span", { class: `tag${sc.type === "B-Roll" ? " broll" : ""}` }, sc.type === "B-Roll" ? "B-Roll" : "Speaking")),
        h("td", {}, sc.turbo == null ? "—" : sc.turbo ? "Turbo" : "Normal"),
        h("td", {}, sc.raw ? "✓" : "…"), h("td", {}, sc.ready ? "✓" : "…"),
        h("td", {}, sc.ready ? h("video", { src: media(`scenes/${sc.id}.mp4`), controls: true, preload: "none", width: 200 }) : "—"),
        h("td", {}, sceneActions(sc))))))));
  el.replaceChildren(...kids);
}

// `sceneId` limits the run to that one scene; without it the whole run is processed.
async function genScenes(mode, sceneId) {
  if (mode === "force" && !await confirmAction(sceneId
      ? `Regenerate ${sceneId}? This re-runs MiniMax H3 for this scene and spends credits again.`
      : "Regenerate EVERY scene? This re-runs MiniMax H3 for all scenes and spends credits again.", "Regenerate")) return;
  const body = {
    run_id: state.runId, tts_provider: "elevenlabs", video_provider: "minimax_h3",
    diarizer: $("#diarizer")?.value || "gemini", turbo: !!state.turbo,
    // scene_id already means "just this scene", so it replaces force/resume.
    force: !sceneId && mode === "force", resume: mode === "resume", tts_only: mode === "tts_only",
  };
  if (sceneId) body.scene_id = sceneId;
  // Synchronous POST; progress comes from polling /status, which the server
  // answers concurrently because handlers run in the threadpool.
  state.poll = setInterval(async () => {
    try { state.status = await api(`/status?run_id=${state.runId}`); renderScenes(); renderNav(); } catch { /* transient */ }
  }, 3000);
  return run(sceneId ? `Generating ${sceneId}` : "Generating scenes", "", async () => {
    const out = await post("/scenes", body);
    state.bust = Date.now();
    const stale = out.stale_dependents?.length
      ? ` ${out.stale_dependents.join(", ")} continued from this scene's old last frame - regenerate ${out.stale_dependents.length > 1 ? "them" : "it"} to follow on from the new one.` : "";
    notice(`${sceneId ? `${sceneId} done.` : "Scenes done."} Run total so far: $${out.total_cost_usd.toFixed(2)}${out.reused ? ` (${out.reused} reused)` : ""}.${stale}`);
  }, { overlay: false }).finally(() => { clearInterval(state.poll); state.poll = null; });
}

// ---- Step 6: music
function renderMusic() {
  const el = $("#stage6"), s = state.status;
  if (!unlocked(6)) return locked(el, 6, "Generate the blueprint first.");
  const kids = [head(6, "Music", "Optional. Lyria composes a background bed from the blueprint's music style; render mixes it in only if it exists.",
    { cls: s.music ? "ok" : "", text: s.music ? "Track ready" : "No track" })];
  kids.push(h("div", { class: "panel" }, h("p", {}, h("strong", {}, "Music style: "), state.blueprint.music_style)));
  kids.push(h("div", { class: "approval-bar" }, spendHint("Google Lyria 3 Clip"), h("div", { class: "actions" },
    s.music
      ? h("button", { class: "button danger", disabled: state.working, onclick: () => genMusic(true) }, "Regenerate")
      : h("button", { class: "button primary", disabled: state.working, onclick: () => genMusic(false) }, "Generate music →"),
    h("button", { class: "button secondary", disabled: !unlocked(7), onclick: () => go(7) }, s.music ? "Continue →" : "Skip →"))));
  if (s.music) kids.push(h("div", { class: "panel" }, h("audio", { controls: true, src: media("music_bg/track.mp3") })));
  el.replaceChildren(...kids);
}

async function genMusic(force) {
  if (force && !await confirmAction("Replace the existing music track? This spends credits.", "Replace")) return;
  return run("Generating music", "Lyria is composing…", async () => {
    await post("/music", { run_id: state.runId, force });
    state.bust = Date.now();
    notice("Music generated.");
  });
}

// ---- Step 7: render
function renderRender() {
  const el = $("#stage7"), s = state.status;
  if (!unlocked(7)) return locked(el, 7, "All scenes must be generated before rendering.");
  const kids = [head(7, "Render", "ffmpeg merges the scenes, adds the title, transitions and subtitles, and mixes the music. Free: runs locally.",
    { cls: s.final ? "ok" : "", text: s.final ? "Final video ready" : "Not rendered" })];
  kids.push(h("div", { class: "panel" }, h("div", { class: "field" },
    h("label", { for: "subMethod" }, "Subtitle renderer"),
    h("select", { id: "subMethod", disabled: state.working },
      [["boxed", "Boxed (default)"], ["ass", "ASS (needs libass)"], ["libass", "libass"], ["drawtext", "drawtext"], ["auto", "Auto"]]
        .map(([v, t]) => h("option", { value: v }, t))))));
  kids.push(h("div", { class: "approval-bar" }, h("span", { class: "muted" }, s.music ? "Music will be mixed in." : "No music track: the video will have voice only."),
    h("button", { class: "button primary", disabled: state.working, onclick: () => render(s.final) }, s.final ? "Re-render" : "Render video →")));
  if (s.final) {
    kids.push(h("div", { class: "panel" }, h("video", { class: "player", controls: true, src: media("final.mp4") }),
      h("p", {}, h("a", { class: "button secondary", href: media("final.mp4"), download: `${state.runId}.mp4` }, "Download MP4"))));
  }
  el.replaceChildren(...kids);
}

function render(force) {
  return run("Rendering", "Merging scenes, subtitles and music…", async () => {
    const out = await post("/render", {
      run_id: state.runId, force: !!force, subtitle_method: $("#subMethod")?.value || "boxed",
    });
    state.bust = Date.now();
    notice(out.font_warning ? `Rendered, with a warning:\n${out.font_warning}` : "Rendered.", !!out.font_warning);
  });
}

// ---- Step 1: config + run picker
async function loadRuns() {
  const picker = $("#runPicker");
  try {
    const runs = await api("/runs");
    picker.replaceChildren(h("option", { value: "" }, runs.length ? "Choose a run…" : "No previous runs"),
      runs.map(r => h("option", { value: r.run_id, selected: r.run_id === state.runId },
        `${r.run_id} · ${r.title || "untitled"} · ${r.stage}`)));
  } catch (e) { notice(`Could not list runs: ${e.message}`, true); }
}

function createRun() {
  const file = $("#cfgFile").files[0];
  const cfg = {
    language: $("#cfgLanguage").value.trim(), audience: $("#cfgAudience").value.trim(),
    depth: $("#cfgDepth").value, blueprint_type: $("#cfgType").value,
    style: $("#cfgStyle").value, objective: $("#cfgObjective").value.trim(),
  };
  const missing = [["language", "Language"], ["audience", "Audience"], ["objective", "Objective"]].filter(([k]) => !cfg[k]).map(([, l]) => l);
  if (missing.length) return notice(`Please fill in: ${missing.join(", ")}.`, true);
  if (!file) return notice("Please choose a .txt source file.", true);
  if (!file.name.toLowerCase().endsWith(".txt")) return notice("Only .txt sources are accepted.", true);

  return run("Creating run", "Saving config and uploading source…", async () => {
    const created = await post("/config", cfg);
    const fd = new FormData();
    fd.append("run_id", created.run_id);
    fd.append("file", file);
    try {
      await api("/upload", { method: "POST", body: fd });
    } catch (e) {
      // the run exists (config saved) but has no source: keep it so the user can see why
      setRun(created.run_id);
      throw e;
    }
    setRun(created.run_id);
    notice(`Run ${created.run_id} created.`);
  });
}

// ------------------------------------------------------------------ boot
$("#createRun").addEventListener("click", createRun);
$("#openRun").addEventListener("click", () => { const v = $("#runPicker").value; if (v) setRun(v); });

(function boot() {
  renderAll();
  go(1);
  let last = null;
  try { last = localStorage.getItem("atlas_studio_run"); } catch { /* storage blocked */ }
  if (last) setRun(last);
})();
