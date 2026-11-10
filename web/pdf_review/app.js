const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const state = {
  project: null,
  selected: new Set(),
  script: null,
  scenes: [],
  visualChoices: new Map(),
  visualSelected: new Set(),
  crops: new Map(),
  cropResults: new Map(),
  videos: new Map(),
};

function showBusy(title, text = "Please keep this page open.") {
  $("#busyTitle").textContent = title;
  $("#busyText").textContent = text;
  $("#busy").classList.remove("hidden");
}

function hideBusy() { $("#busy").classList.add("hidden"); }

function notice(message, error = false) {
  const box = $("#notice");
  box.textContent = message;
  box.classList.toggle("error", error);
  box.classList.remove("hidden");
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (!error) setTimeout(() => box.classList.add("hidden"), 6000);
}

async function api(path, options = {}) {
  const response = await fetch(`/pdf-review${path}`, options);
  let data;
  try { data = await response.json(); } catch { data = { detail: response.statusText }; }
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data);
    throw new Error(detail);
  }
  return data;
}

function jsonPost(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

function settings() {
  return {
    objective: $("#objective").value.trim(),
    language: $("#language").value.trim() || "English",
    target_seconds: Number($("#duration").value),
    avatar_url: $("#avatarUrl").value.trim(),
  };
}

function go(step) {
  $$(".stage").forEach(stage => stage.classList.toggle("active", Number(stage.dataset.stage) === step));
  $$(".nav-step").forEach(button => button.classList.toggle("active", Number(button.dataset.step) === step));
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function unlock(step) {
  const button = $(`.nav-step[data-step="${step}"]`);
  button.disabled = false;
}

$$(".nav-step").forEach(button => button.addEventListener("click", () => !button.disabled && go(Number(button.dataset.step))));
$("#duration").addEventListener("change", event => $("#heroDuration").textContent = `${event.target.value} sec`);

async function importSource(mode) {
  const cfg = settings();
  showBusy("Reading the PDF", "Rendering slides and extracting text locally. No AI or video call is made.");
  try {
    let data;
    if (mode === "url") {
      const pdf_url = $("#pdfUrl").value.trim();
      if (!pdf_url) throw new Error("Enter an S3 or CloudFront PDF URL.");
      data = await jsonPost("/import-url", { pdf_url, ...cfg });
    } else {
      const file = $("#pdfFile").files[0];
      if (!file) throw new Error("Choose a local PDF first.");
      const form = new FormData();
      form.append("file", file);
      const params = new URLSearchParams({
        objective: cfg.objective,
        language: cfg.language,
        target_seconds: String(cfg.target_seconds),
        avatar_url: cfg.avatar_url,
      });
      data = await api(`/import-file?${params}`, { method: "POST", body: form });
    }
    state.project = data;
    state.selected = new Set(data.recommended_pages);
    state.script = null;
    state.scenes = [];
    state.visualChoices.clear();
    state.visualSelected.clear();
    state.crops.clear();
    state.cropResults.clear();
    state.videos.clear();
    renderSource();
    await maybeUploadCharacter(data.project_id);
    notice(`${data.page_count} slides extracted. Review the recommended scope before continuing.`);
  } catch (error) {
    notice(error.message, true);
  } finally { hideBusy(); }
}

// Optional: if a character image was chosen, attach it to the freshly created project.
// It's an upload alternative to the avatar reference URL and never blocks import.
async function maybeUploadCharacter(projectId) {
  const file = $("#characterFile").files[0];
  if (!file) return;
  showBusy("Attaching character image", "Storing the presenter reference with the project. No AI or video call.");
  try {
    const form = new FormData();
    form.append("file", file);
    const result = await api(`/import-character?project_id=${encodeURIComponent(projectId)}`, { method: "POST", body: form });
    state.project.character_path = result.character_path;
    $("#characterStatus").textContent = `Character image attached: ${file.name}`;
  } catch (error) {
    $("#characterStatus").textContent = `Character upload failed: ${error.message}`;
  }
}

$("#importUrl").addEventListener("click", () => importSource("url"));
$("#importFile").addEventListener("click", () => importSource("file"));

function renderSource() {
  const data = state.project;
  $("#sourceResults").classList.remove("hidden");
  $("#projectId").textContent = data.project_id;
  $("#pageCount").textContent = data.page_count;
  $("#sourceStatus").textContent = "Extracted · review required";
  const grid = $("#slideGrid");
  grid.innerHTML = data.slides.map(slide => `
    <button class="slide-card ${state.selected.has(slide.page) ? "selected" : ""}" data-page="${slide.page}" title="${escapeHtml(slide.title)}">
      <span class="slide-check">✓</span>
      <img src="${slide.thumbnail}" alt="Slide ${slide.page}" loading="lazy">
      <span class="slide-info"><span>Slide ${slide.page}</span><strong>${escapeHtml(slide.title)}</strong></span>
    </button>`).join("");
  $$(".slide-card", grid).forEach(card => card.addEventListener("click", () => {
    const page = Number(card.dataset.page);
    if (state.selected.has(page)) state.selected.delete(page); else state.selected.add(page);
    card.classList.toggle("selected", state.selected.has(page));
    syncSelected();
  }));
  syncSelected();
}

function syncSelected() {
  $("#selectedCount").textContent = state.selected.size;
  $("#toScript").disabled = !$("#approveSource").checked || state.selected.size < 1;
}

$("#clearSlides").addEventListener("click", () => { state.selected.clear(); renderSource(); });
$("#selectAllSlides").addEventListener("click", () => {
  state.selected = new Set(state.project.slides.map(slide => slide.page));
  renderSource();
});
$("#recommendedSlides").addEventListener("click", () => { state.selected = new Set(state.project.recommended_pages); renderSource(); });
$("#approveSource").addEventListener("change", syncSelected);
$("#toScript").addEventListener("click", () => { unlock(2); go(2); createDraft(true); });

function draftPayload() {
  return {
    project_id: state.project.project_id,
    selected_pages: [...state.selected].sort((a, b) => a - b),
    objective: $("#objective").value.trim(),
    language: $("#language").value.trim() || "English",
    target_seconds: Number($("#duration").value),
  };
}

async function createDraft(ai) {
  showBusy(ai ? "OpenAI is drafting the script" : "Building a local draft", ai ? "One text-generation call; no video call." : "Extracting source text locally; no API cost.");
  try {
    state.script = await jsonPost(ai ? "/draft-ai" : "/draft-local", draftPayload());
    renderScript();
    notice(ai ? "OpenAI draft received. Check every fact against its source slide." : "Local extractive draft created. Edit it into natural narration or use OpenAI synthesis.");
  } catch (error) { notice(error.message, true); } finally { hideBusy(); }
}

// Drafting is automatic: /draft-ai fires when the user continues from Step 01.
// The draft-local / draft-ai choice buttons were removed with the manual Step 02 entry.

function renderScript() {
  $("#scriptEmpty").classList.add("hidden");
  $("#scriptEditor").classList.remove("hidden");
  $("#scriptTitle").value = state.script.title;
  $("#scriptStatus").textContent = state.script.mode === "openai" ? "OpenAI draft · verify facts" : "Local extraction · review required";
  $("#scriptSections").innerHTML = state.script.sections.map((section, index) => `
    <article class="script-section" data-index="${index}">
      <div class="source-tag">SLIDE ${section.page}</div>
      <div class="narration-text">${escapeHtml(section.narration)}</div>
      <div class="duration-box">SECONDS<input type="number" min="5" max="15" value="${section.duration_s}"></div>
      <button class="regen-btn" data-index="${index}">Regenerate</button>
    </article>`).join("");
  $$(".script-section").forEach(card => {
    const index = Number(card.dataset.index);
    $("input", card).addEventListener("input", event => { state.script.sections[index].duration_s = Number(event.target.value); syncRuntime(); });
    $(".regen-btn", card).addEventListener("click", () => toggleRegenForm(card, index));
  });
  syncRuntime();
}

function toggleRegenForm(card, index) {
  const existing = $(".regen-form", card);
  if (existing) { existing.remove(); return; }
  const form = document.createElement("div");
  form.className = "regen-form";
  form.innerHTML = `<input type="text" placeholder="e.g. Focus more on part A instead of B" aria-label="Adjustment instruction"><button class="button primary">Regenerate section</button>`;
  card.appendChild(form);
  const input = $("input[type=text]", form);
  input.focus();
  $("button", form).addEventListener("click", () => regenerateSection(index, input.value.trim(), card));
  input.addEventListener("keydown", event => { if (event.key === "Enter") regenerateSection(index, input.value.trim(), card); });
}

async function regenerateSection(index, instruction, card) {
  if (!instruction) { notice("Enter an instruction describing how to adjust this section.", true); return; }
  showBusy("Regenerating section", "One OpenAI call to rewrite this section based on your instruction.");
  try {
    const data = await jsonPost("/regenerate-section", {
      project_id: state.project.project_id,
      section_index: index,
      instruction,
      language: $("#language").value.trim() || "English",
    });
    state.script.sections[index] = data.section;
    renderScript();
    notice(`Section for slide ${data.section.page} regenerated.`);
  } catch (error) { notice(error.message, true); } finally { hideBusy(); }
}

const MAX_RUNTIME_SECONDS = 90;

function syncRuntime() {
  const total = state.script.sections.reduce((sum, section) => sum + Number(section.duration_s || 0), 0);
  $("#runtimeReadout").textContent = `${total} sec`;
  syncScriptGate(total);
}

// Continue is allowed only when the script is approved AND total runtime fits the 90s budget.
function syncScriptGate(total) {
  if (total == null) {
    total = state.script ? state.script.sections.reduce((sum, section) => sum + Number(section.duration_s || 0), 0) : 0;
  }
  const tooLong = total > MAX_RUNTIME_SECONDS;
  $("#runtimeReadout").classList.toggle("over-limit", tooLong);
  const warning = $("#lengthWarning");
  warning.classList.toggle("hidden", !tooLong);
  if (tooLong) {
    warning.textContent = `Total runtime is ${total} sec — it must be ${MAX_RUNTIME_SECONDS} seconds or less. Shorten section durations before continuing.`;
  }
  $("#toScenes").disabled = !$("#approveScript").checked || tooLong;
}

$("#approveScript").addEventListener("change", () => syncScriptGate());
$("#toScenes").addEventListener("click", () => {
  state.script.title = $("#scriptTitle").value.trim();
  // Step 03 is automated and never shown: scenes are built verbatim from the approved script,
  // then the flow jumps straight into Step 04 (source visuals).
  state.scenes = state.script.sections.map((section, index) => ({
    id: `scene_${String(index + 1).padStart(2, "0")}`,
    page: section.page,
    duration_s: Number(section.duration_s),
    narration: section.narration,
    prompt: section.visual_direction || "A professional presenter stands to the left of a large display, speaking to camera and naturally gesturing toward the approved source visual on the right. Preserve the source visual faithfully and keep all labels legible.",
    crop_path: "",
  }));
  enterCrops();
});

// enterCrops() is the Step 04 entry. It used to sit behind the (now hidden) Step 03
// "Review source visuals" button; the automated script step calls it directly.
function enterCrops() {
  unlock(4);
  go(4);
  // Step 04A is automated: use the deterministic scene-linked slides (no LLM call),
  // then immediately confirm so the user lands straight in the crop workspace.
  // The "Use scene-linked slides" / "Select with OpenAI" buttons remain for manual overrides.
  useSceneSlides({ announce: false });
  confirmVisuals();
}

function slideByPage(page) { return state.project.slides.find(slide => slide.page === page); }

function syncVisualSelection() {
  $("#visualSelectedCount").textContent = state.visualSelected.size;
  $("#confirmVisuals").disabled = state.visualSelected.size < 1;
}

function invalidateVisualConfirmation() {
  $("#cropWorkspace").classList.add("hidden");
  state.crops.clear();
  state.cropResults.clear();
  $("#approveCrops").checked = false;
  $("#toPreflight").disabled = true;
  $("#cropStatus").textContent = "Review visual selection";
}

function renderVisualChoices() {
  const choices = [...state.visualChoices.values()];
  $("#visualSelectionEmpty").classList.toggle("hidden", choices.length > 0);
  const grid = $("#visualChoiceGrid");
  grid.classList.toggle("hidden", choices.length === 0);
  grid.innerHTML = choices.map(choice => {
    const slide = slideByPage(choice.page);
    const sceneText = choice.scene_ids?.length ? `Supports ${choice.scene_ids.join(", ")}` : "Manual selection";
    return `<button class="visual-choice ${state.visualSelected.has(choice.page) ? "selected" : ""}" data-page="${choice.page}">
      <img src="${slide.thumbnail}" alt="Slide ${choice.page}">
      <span class="visual-choice-copy"><strong>Slide ${choice.page} · ${escapeHtml(choice.label || slide.title)}</strong><small class="visual-choice-scenes">${escapeHtml(sceneText)}</small><small>${escapeHtml(choice.rationale || slide.title)}</small></span>
    </button>`;
  }).join("");
  $$(".visual-choice", grid).forEach(card => card.addEventListener("click", () => {
    const page = Number(card.dataset.page);
    if (state.visualSelected.has(page)) state.visualSelected.delete(page); else state.visualSelected.add(page);
    card.classList.toggle("selected", state.visualSelected.has(page));
    invalidateVisualConfirmation();
    syncVisualSelection();
  }));
  syncVisualSelection();
}

function useSceneSlides({ announce = true } = {}) {
  const grouped = new Map();
  state.scenes.forEach(scene => {
    const existing = grouped.get(scene.page) || {
      page: scene.page,
      scene_ids: [],
      label: slideByPage(scene.page)?.title || "Scene-linked slide",
      rationale: "This slide is cited by the approved script scene.",
    };
    existing.scene_ids.push(scene.id);
    grouped.set(scene.page, existing);
  });
  state.visualChoices = grouped;
  state.visualSelected = new Set(grouped.keys());
  invalidateVisualConfirmation();
  renderVisualChoices();
  if (announce) notice("Scene-linked slides loaded as a manual starting point. Review them before confirming.");
}

async function confirmVisuals() {
  const pages = [...state.visualSelected];
  if (pages.length < 1) return;
  state.crops.clear();
  state.cropResults.clear();
  pages.forEach(page => state.crops.set(page, { page, x: .08, y: .10, width: .84, height: .80, label: state.visualChoices.get(page)?.label || "Source visual" }));
  state.scenes.forEach(scene => {
    const mapped = [...state.visualChoices.values()].find(choice => state.visualSelected.has(choice.page) && choice.scene_ids?.includes(scene.id));
    scene.visual_page = mapped?.page || (state.visualSelected.has(scene.page) ? scene.page : pages[0]);
  });
  $("#cropCallText").textContent = `Crops are detected and rendered automatically on confirm. Re-run or adjust any box below, then re-render.`;
  $("#cropWorkspace").classList.remove("hidden");
  $("#cropStatus").textContent = `${state.crops.size} visual slides confirmed`;
  renderCrops();
  $("#cropWorkspace").scrollIntoView({ behavior: "smooth", block: "start" });
  // Automate the first pass: detect figure crops, then render previews, so the user
  // lands on finished results. The buttons remain for manual re-runs after adjustments.
  await runAutoCrops();
  await runRenderCrops();
}
$("#confirmVisuals").addEventListener("click", confirmVisuals);

function renderCrops() {
  const pages = [...state.crops.keys()];
  $("#cropList").innerHTML = pages.map(page => {
    const crop = state.crops.get(page), slide = slideByPage(page), result = state.cropResults.get(page);
    return `<article class="crop-card" data-page="${page}">
      <div class="crop-canvas"><img src="${slide.thumbnail}" alt="Slide ${page}"><span class="crop-box"></span></div>
      <div class="crop-controls"><h4>Slide ${page}</h4><p>${escapeHtml(crop.label || slide.title)}</p><div class="coord-grid">
        ${coordInput("X", "x", crop.x)}${coordInput("Y", "y", crop.y)}${coordInput("Width", "width", crop.width)}${coordInput("Height", "height", crop.height)}
      </div></div>
      <div class="crop-preview">${result ? `<img src="${result.url}?v=${Date.now()}" alt="Rendered crop from slide ${page}">` : "Render to inspect source pixels"}</div>
    </article>`;
  }).join("");
  $$(".crop-card").forEach(card => {
    const page = Number(card.dataset.page), crop = state.crops.get(page);
    $$('input[type="number"]', card).forEach(input => input.addEventListener("input", () => {
      crop[input.dataset.key] = Math.max(0, Math.min(1, Number(input.value)));
      state.cropResults.delete(page);
      updateCropBox(card, crop);
    }));
    updateCropBox(card, crop);
  });
}

function coordInput(label, key, value) {
  return `<label>${label}<input type="number" data-key="${key}" min="0" max="1" step="0.01" value="${Number(value).toFixed(2)}"></label>`;
}

function updateCropBox(card, crop) {
  const box = $(".crop-box", card);
  box.style.left = `${crop.x * 100}%`;
  box.style.top = `${crop.y * 100}%`;
  box.style.width = `${Math.min(crop.width, 1 - crop.x) * 100}%`;
  box.style.height = `${Math.min(crop.height, 1 - crop.y) * 100}%`;
}

async function runAutoCrops() {
  const pages = [...state.crops.keys()];
  // narration gives the model context for "what matters" on multi-figure slides
  const scenes = state.scenes
    .filter(scene => pages.includes(scene.visual_page || scene.page))
    .map(scene => ({ page: scene.visual_page || scene.page, narration: scene.narration }));
  showBusy("Auto-detecting crops", "Reading exact figure coordinates from the PDF; OpenAI only picks a region on slides with more than one figure.");
  try {
    const data = await jsonPost("/auto-crops", { project_id: state.project.project_id, pages, scenes });
    let boxed = 0;
    const empty = [];
    data.suggestions.forEach(item => {
      if (item.width != null) {
        state.crops.set(item.page, {
          page: item.page, x: item.x, y: item.y, width: item.width, height: item.height,
          label: item.label || slideByPage(item.page)?.title || "Source visual",
        });
        boxed += 1;
      } else {
        state.crops.delete(item.page);
        empty.push(item.page);
      }
    });
    state.cropResults.clear();
    renderCrops();
    $("#cropStatus").textContent = `${boxed} auto crop${boxed === 1 ? "" : "s"} · ${data.calls} AI call${data.calls === 1 ? "" : "s"} · review required`;
    const tail = empty.length ? ` No standalone figure on slide${empty.length === 1 ? "" : "s"} ${empty.join(", ")} — adjust those manually.` : "";
    notice(`Auto-detected ${boxed} crop box${boxed === 1 ? "" : "es"} (${data.calls} paid AI call${data.calls === 1 ? "" : "s"}, only for multi-figure slides).${tail} Adjust and render previews before approval.`);
  } catch (error) { notice(error.message, true); } finally { hideBusy(); }
}
$("#autoCrops").addEventListener("click", runAutoCrops);

async function runRenderCrops() {
  showBusy("Rendering crop previews", "Cropping the original PDF locally; no AI call.");
  try {
    const crops = [...state.crops.values()].map(crop => ({
      ...crop,
      width: Math.min(crop.width, 1 - crop.x),
      height: Math.min(crop.height, 1 - crop.y),
    }));
    const data = await jsonPost("/apply-crops", { project_id: state.project.project_id, crops });
    data.crops.forEach(crop => state.cropResults.set(crop.page, crop));
    renderCrops();
    $("#cropStatus").textContent = `${data.crops.length} previews rendered`;
    notice("Source crops rendered from the original PDF. Inspect legibility before approval.");
  } catch (error) { notice(error.message, true); } finally { hideBusy(); }
}
$("#renderCrops").addEventListener("click", runRenderCrops);

function syncCropApproval() {
  const ready = $("#approveCrops").checked && state.cropResults.size === state.crops.size && state.crops.size > 0;
  $("#toPreflight").disabled = !ready;
  if ($("#approveCrops").checked && !ready) notice("Render all crop previews before opening preflight.", true);
}
$("#approveCrops").addEventListener("change", syncCropApproval);

$("#toPreflight").addEventListener("click", async () => {
  state.scenes.forEach(scene => scene.crop_path = state.cropResults.get(scene.visual_page || scene.page)?.crop_path || "");
  unlock(5);
  go(5);
});

// --- Step 05: Character generation (kie.ai Nano Banana) ---

async function generateCharacterFromScript() {
  showBusy("Generating character", "OpenAI analyses the script, then kie.ai generates a presenter image. This may take a minute.");
  try {
    const data = await jsonPost("/generate-character", { project_id: state.project.project_id });
    state.project.character_path = data.character_path;
    showCharacterPreview(data.character_url, data.character_prompt, data.credits_consumed);
    notice(`Character generated (${data.credits_consumed ?? "?"} credits). Review it and adjust if needed.`);
  } catch (error) { notice(error.message, true); } finally { hideBusy(); }
}

async function regenerateCharacterWithPrompt() {
  const userPrompt = $("#characterRegenPrompt").value.trim();
  if (!userPrompt) { notice("Enter a prompt describing how to adjust the character.", true); return; }
  showBusy("Regenerating character", "Editing the previous image with your prompt via kie.ai. This may take a minute.");
  try {
    const data = await jsonPost("/regenerate-character", { project_id: state.project.project_id, prompt: userPrompt });
    state.project.character_path = data.character_path;
    showCharacterPreview(data.character_url, null, data.credits_consumed);
    notice(`Character regenerated (${data.credits_consumed ?? "?"} credits). Review the result.`);
  } catch (error) { notice(error.message, true); } finally { hideBusy(); }
}

function showCharacterPreview(url, prompt, credits) {
  const img = $("#characterPreviewImg");
  img.src = `${url}?v=${Date.now()}`;
  img.classList.remove("hidden");
  $("#characterPlaceholder").classList.add("hidden");
  if (prompt) {
    $("#characterPromptText").textContent = `Prompt: ${prompt}`;
    $("#characterPromptText").classList.remove("hidden");
  }
  if (credits != null) {
    $("#characterCredits").textContent = `Credits consumed: ${credits}`;
    $("#characterCredits").classList.remove("hidden");
  }
  $("#characterRegenPanel").classList.remove("hidden");
  $("#characterGenStatus").textContent = "Generated · review required";
  $("#approveCharacter").checked = false;
  syncCharacterApproval();
}

function syncCharacterApproval() {
  const hasImage = !$("#characterPreviewImg").classList.contains("hidden");
  $("#toPreflightFromChar").disabled = !$("#approveCharacter").checked || !hasImage;
}

$("#generateCharacter").addEventListener("click", generateCharacterFromScript);
$("#regenerateCharacter").addEventListener("click", regenerateCharacterWithPrompt);
$("#characterRegenPrompt").addEventListener("keydown", event => { if (event.key === "Enter") regenerateCharacterWithPrompt(); });
$("#approveCharacter").addEventListener("change", syncCharacterApproval);

$("#toPreflightFromChar").addEventListener("click", async () => {
  showBusy("Preparing preflight", "Writing the review manifest. No kie.ai task is submitted until you click Generate.");
  try {
    const manifest = await jsonPost("/preflight", {
      project_id: state.project.project_id,
      avatar_url: $("#avatarUrl").value.trim(),
      scenes: state.scenes,
    });
    manifest._contract = videoContract();
    $("#manifestJson").textContent = JSON.stringify(manifest, null, 2);
    const first = [...state.cropResults.values()][0];
    if (first) {
      $("#finalCropPreview").src = first.url;
      $("#displayPlaceholder").classList.add("hidden");
    }
    renderGeneration();
    unlock(6);
    go(6);
  } catch (error) { notice(error.message, true); } finally { hideBusy(); }
});

// --- Step 06 video generation (live, paid kie.ai Seedance jobs) ---
function renderGeneration() {
  $("#genList").innerHTML = state.scenes.map((scene, index) => {
    const crop = state.cropResults.get(scene.visual_page || scene.page);
    const video = state.videos.get(scene.id);
    const preview = video
      ? `<video src="${video.url}?v=${Date.now()}" controls preload="metadata"></video>`
      : (crop ? `<img src="${crop.url}" alt="Reference for scene ${index + 1}">` : "No reference crop");
    const meta = video
      ? `Generated · ${video.credits ?? "?"} credits${video.composited === false ? " · no composite" : ""}`
      : `Slide ${scene.page} · ${scene.duration_s}s`;
    return `<article class="gen-card" data-scene="${scene.id}">
      <div class="gen-media">${preview}</div>
      <div class="gen-info"><h4>Scene ${String(index + 1).padStart(2, "0")}</h4><p>${escapeHtml(scene.narration.slice(0, 140))}</p><small>${escapeHtml(meta)}</small></div>
      <button class="button secondary gen-one" data-scene="${scene.id}">${video ? "Regenerate" : "Generate"}</button>
    </article>`;
  }).join("");
  $$(".gen-one").forEach(button => button.addEventListener("click", () => {
    const scene = state.scenes.find(item => item.id === button.dataset.scene);
    if (scene) generateOne(scene);
  }));
}

function videoContract() {
  return {
    model: $("#specModel").value,
    resolution: $("#specResolution").value,
    aspect_ratio: $("#specAspect").value,
    duration_s: Math.max(4, Math.min(15, Number($("#specDuration").value) || 8)),
    generate_audio: $("#specAudio").value === "true",
  };
}

async function generateOne(scene) {
  const contract = videoContract();
  showBusy(`Generating scene ${scene.id}`, "Live kie.ai Seedance job — uploading references, generating with green-screen, then compositing the slide. This can take a few minutes.");
  try {
    const data = await jsonPost("/generate-scene", {
      project_id: state.project.project_id,
      scene_id: scene.id,
      prompt: scene.prompt,
      narration: scene.narration,
      crop_path: scene.crop_path || "",
      duration_s: contract.duration_s,
      aspect_ratio: contract.aspect_ratio,
      resolution: contract.resolution,
      generate_audio: contract.generate_audio,
      model: contract.model,
    });
    state.videos.set(scene.id, { url: data.url, credits: data.credits_consumed, taskId: data.task_id, composited: data.composited });
    renderGeneration();
    notice(`Scene ${scene.id} generated (${data.credits_consumed ?? "?"} credits).`);
    return true;
  } catch (error) {
    notice(`Scene ${scene.id} failed: ${error.message}`, true);
    return false;
  } finally { hideBusy(); }
}

$("#generateAll").addEventListener("click", async () => {
  const total = state.scenes.length;
  let done = 0, failed = 0;
  for (const scene of state.scenes) {
    $("#genStatus").textContent = `Generating scene ${done + failed + 1} of ${total}…`;
    const ok = await generateOne(scene);
    if (ok) done += 1; else failed += 1;
  }
  $("#genStatus").textContent = `Done: ${done} generated${failed ? `, ${failed} failed` : ""} of ${total}.`;
});

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
}
