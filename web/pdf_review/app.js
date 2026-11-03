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
    renderSource();
    notice(`${data.page_count} slides extracted. Review the recommended scope before continuing.`);
  } catch (error) {
    notice(error.message, true);
  } finally { hideBusy(); }
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
$("#toScript").addEventListener("click", () => { unlock(2); go(2); });

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

$("#draftLocal").addEventListener("click", () => createDraft(false));
$("#draftAi").addEventListener("click", () => createDraft(true));

function renderScript() {
  $("#scriptEmpty").classList.add("hidden");
  $("#scriptEditor").classList.remove("hidden");
  $("#scriptTitle").value = state.script.title;
  $("#scriptStatus").textContent = state.script.mode === "openai" ? "OpenAI draft · verify facts" : "Local extraction · edit required";
  $("#scriptSections").innerHTML = state.script.sections.map((section, index) => `
    <article class="script-section" data-index="${index}">
      <div class="source-tag">SLIDE ${section.page}</div>
      <textarea aria-label="Narration for slide ${section.page}">${escapeHtml(section.narration)}</textarea>
      <div class="duration-box">SECONDS<input type="number" min="5" max="15" value="${section.duration_s}"></div>
    </article>`).join("");
  $$(".script-section").forEach(card => {
    const index = Number(card.dataset.index);
    $("textarea", card).addEventListener("input", event => { state.script.sections[index].narration = event.target.value; syncRuntime(); });
    $("input", card).addEventListener("input", event => { state.script.sections[index].duration_s = Number(event.target.value); syncRuntime(); });
  });
  syncRuntime();
}

function syncRuntime() {
  const total = state.script.sections.reduce((sum, section) => sum + Number(section.duration_s || 0), 0);
  $("#runtimeReadout").textContent = `${total} sec`;
}

$("#approveScript").addEventListener("change", event => $("#toScenes").disabled = !event.target.checked);
$("#toScenes").addEventListener("click", () => {
  state.script.title = $("#scriptTitle").value.trim();
  state.scenes = state.script.sections.map((section, index) => ({
    id: `scene_${String(index + 1).padStart(2, "0")}`,
    page: section.page,
    duration_s: Number(section.duration_s),
    narration: section.narration,
    prompt: section.visual_direction || "A professional presenter stands to the left of a large display, speaking to camera and naturally gesturing toward the approved source visual on the right. Preserve the source visual faithfully and keep all labels legible.",
    crop_path: "",
  }));
  renderScenes();
  unlock(3);
  go(3);
});

function renderScenes() {
  $("#sceneCount").textContent = state.scenes.length;
  $("#sceneRuntime").textContent = `${state.scenes.reduce((sum, scene) => sum + scene.duration_s, 0)}s`;
  $("#sceneList").innerHTML = state.scenes.map((scene, index) => `
    <article class="scene-card" data-index="${index}">
      <div class="scene-number"><b>${String(index + 1).padStart(2, "0")}</b><span>SLIDE ${scene.page}</span><span>${scene.duration_s} SEC</span></div>
      <div><label>Narration</label><textarea class="scene-narration">${escapeHtml(scene.narration)}</textarea></div>
      <div><label>Avatar & display direction</label><textarea class="scene-prompt">${escapeHtml(scene.prompt)}</textarea></div>
    </article>`).join("");
  $$(".scene-card").forEach(card => {
    const scene = state.scenes[Number(card.dataset.index)];
    $(".scene-narration", card).addEventListener("input", event => scene.narration = event.target.value);
    $(".scene-prompt", card).addEventListener("input", event => scene.prompt = event.target.value);
  });
}

$("#approveScenes").addEventListener("change", event => $("#toCrops").disabled = !event.target.checked);
$("#toCrops").addEventListener("click", () => {
  state.visualChoices.clear();
  state.visualSelected.clear();
  state.crops.clear();
  state.cropResults.clear();
  $("#visualChoiceGrid").classList.add("hidden");
  $("#visualSelectionEmpty").classList.remove("hidden");
  $("#cropWorkspace").classList.add("hidden");
  $("#cropStatus").textContent = "Visual selection required";
  syncVisualSelection();
  unlock(4);
  go(4);
});

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

$("#useSceneSlides").addEventListener("click", () => {
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
  notice("Scene-linked slides loaded as a manual starting point. Review them before confirming.");
});

$("#selectVisualsAi").addEventListener("click", async () => {
  showBusy("Selecting the best source slides", `Comparing ${state.selected.size} approved slides with ${state.scenes.length} scenes in one OpenAI call. No images or video are generated.`);
  try {
    const data = await jsonPost("/select-visuals", {
      project_id: state.project.project_id,
      candidate_pages: [...state.selected].sort((a, b) => a - b),
      scenes: state.scenes.map(({ id, page, narration, prompt }) => ({ id, page, narration, prompt })),
      max_visuals: Math.min(8, Math.max(1, state.scenes.length)),
    });
    state.visualChoices = new Map(data.choices.map(choice => [choice.page, choice]));
    state.visualSelected = new Set(data.choices.map(choice => choice.page));
    invalidateVisualConfirmation();
    renderVisualChoices();
    $("#cropStatus").textContent = `${data.choices.length} AI-selected slides · review required`;
    notice(`OpenAI selected ${data.choices.length} visual slides from ${data.candidate_count} candidates. Review the shortlist before cropping.`);
  } catch (error) { notice(error.message, true); } finally { hideBusy(); }
});

$("#confirmVisuals").addEventListener("click", () => {
  const pages = [...state.visualSelected];
  state.crops.clear();
  state.cropResults.clear();
  pages.forEach(page => state.crops.set(page, { page, x: .08, y: .10, width: .84, height: .80, label: state.visualChoices.get(page)?.label || "Source visual" }));
  state.scenes.forEach(scene => {
    const mapped = [...state.visualChoices.values()].find(choice => state.visualSelected.has(choice.page) && choice.scene_ids?.includes(scene.id));
    scene.visual_page = mapped?.page || (state.visualSelected.has(scene.page) ? scene.page : pages[0]);
  });
  $("#cropCallText").textContent = `${state.crops.size} image-analysis call${state.crops.size === 1 ? "" : "s"} if you request AI crop boxes. No video generation.`;
  $("#cropWorkspace").classList.remove("hidden");
  $("#cropStatus").textContent = `${state.crops.size} visual slides confirmed`;
  renderCrops();
  $("#cropWorkspace").scrollIntoView({ behavior: "smooth", block: "start" });
});

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

$("#suggestCrops").addEventListener("click", async () => {
  const pages = [...state.crops.keys()];
  showBusy("Finding source visuals", `${pages.length} explicit OpenAI analysis call${pages.length === 1 ? "" : "s"}; no video calls.`);
  try {
    const data = await jsonPost("/suggest-crops", { project_id: state.project.project_id, pages });
    data.suggestions.forEach(item => state.crops.set(item.page, item));
    state.cropResults.clear();
    renderCrops();
    $("#cropStatus").textContent = `${data.calls} AI suggestions · review required`;
    notice("Crop suggestions received. Adjust every box and render previews before approval.");
  } catch (error) { notice(error.message, true); } finally { hideBusy(); }
});

$("#renderCrops").addEventListener("click", async () => {
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
});

function syncCropApproval() {
  const ready = $("#approveCrops").checked && state.cropResults.size === state.crops.size && state.crops.size > 0;
  $("#toPreflight").disabled = !ready;
  if ($("#approveCrops").checked && !ready) notice("Render all crop previews before opening preflight.", true);
}
$("#approveCrops").addEventListener("change", syncCropApproval);

$("#toPreflight").addEventListener("click", async () => {
  showBusy("Preparing the locked preflight", "Writing the review manifest only. No Kie task will be submitted.");
  try {
    state.scenes.forEach(scene => scene.crop_path = state.cropResults.get(scene.visual_page || scene.page)?.crop_path || "");
    const manifest = await jsonPost("/preflight", {
      project_id: state.project.project_id,
      avatar_url: $("#avatarUrl").value.trim(),
      scenes: state.scenes,
    });
    $("#manifestJson").textContent = JSON.stringify(manifest, null, 2);
    const first = [...state.cropResults.values()][0];
    if (first) {
      $("#finalCropPreview").src = first.url;
      $("#displayPlaceholder").classList.add("hidden");
    }
    unlock(5);
    go(5);
  } catch (error) { notice(error.message, true); } finally { hideBusy(); }
});

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
}
