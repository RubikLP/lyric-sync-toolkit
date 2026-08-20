const state = {
  steps: [],
  selectedPath: "", // relative to /music root; "" means the root itself
  currentBrowsePath: "",
  activeJobId: null,
};

async function api(path, opts) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = body.detail || msg;
    } catch (e) { /* body wasn't JSON, keep statusText */ }
    throw new Error(msg);
  }
  return res.json();
}

// ---------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------

document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("view-" + btn.dataset.view).classList.add("active");
  });
});

// ---------------------------------------------------------------------
// Settings + Whisper test
// ---------------------------------------------------------------------

async function loadSettings() {
  const s = await api("/api/settings");
  document.getElementById("whisper-url").value = s.whisper_url || "";
}

document.getElementById("test-whisper-btn").addEventListener("click", async () => {
  const url = document.getElementById("whisper-url").value.trim();
  const statusEl = document.getElementById("whisper-status");
  statusEl.className = "status-badge testing";
  statusEl.textContent = "Testing…";
  try {
    const result = await api("/api/test-whisper", { method: "POST", body: JSON.stringify({ url }) });
    if (result.ok) {
      statusEl.className = "status-badge ok";
      statusEl.textContent = "✓ Connected — " + result.detail;
    } else {
      statusEl.className = "status-badge fail";
      statusEl.textContent = "✕ " + result.detail;
    }
  } catch (e) {
    statusEl.className = "status-badge fail";
    statusEl.textContent = "✕ Error: " + e.message;
  }
});

document.getElementById("save-settings-btn").addEventListener("click", async () => {
  const whisper_url = document.getElementById("whisper-url").value.trim();
  await api("/api/settings", { method: "POST", body: JSON.stringify({ whisper_url }) });
  const btn = document.getElementById("save-settings-btn");
  const original = btn.textContent;
  btn.textContent = "Saved ✓";
  setTimeout(() => (btn.textContent = original), 1500);
});

// ---------------------------------------------------------------------
// Folder browser
// ---------------------------------------------------------------------

async function openBrowser() {
  state.currentBrowsePath = state.selectedPath;
  document.getElementById("browse-modal").classList.remove("hidden");
  await renderBrowser();
}

async function renderBrowser() {
  const data = await api("/api/browse?path=" + encodeURIComponent(state.currentBrowsePath));
  state.currentBrowsePath = data.path;
  document.getElementById("browse-breadcrumb").textContent = "/music" + (data.path ? "/" + data.path : "");
  const list = document.getElementById("browse-list");
  list.innerHTML = "";

  if (data.error) {
    list.innerHTML = `<div class="empty">${data.error}</div>`;
    return;
  }
  if (data.entries.length === 0) {
    list.innerHTML = "<div class='empty'>No subfolders here.</div>";
  }
  data.entries.forEach((name) => {
    const item = document.createElement("div");
    item.className = "browse-item";
    item.textContent = "📁 " + name;
    item.addEventListener("click", () => {
      state.currentBrowsePath = data.path ? data.path + "/" + name : name;
      renderBrowser();
    });
    list.appendChild(item);
  });

  const upBtn = document.getElementById("browse-up");
  upBtn.disabled = data.parent === null;
  upBtn.onclick = () => {
    state.currentBrowsePath = data.parent;
    renderBrowser();
  };
}

document.getElementById("browse-btn").addEventListener("click", openBrowser);
document.getElementById("browse-close").addEventListener("click", () => {
  document.getElementById("browse-modal").classList.add("hidden");
});
document.getElementById("browse-select").addEventListener("click", () => {
  state.selectedPath = state.currentBrowsePath;
  document.getElementById("selected-path").value = "/music" + (state.selectedPath ? "/" + state.selectedPath : "");
  document.getElementById("browse-modal").classList.add("hidden");
});

// ---------------------------------------------------------------------
// Pipeline step cards
// ---------------------------------------------------------------------

async function loadSteps() {
  state.steps = await api("/api/steps");
  const container = document.getElementById("steps-container");
  container.innerHTML = "";
  state.steps.forEach((step) => container.appendChild(renderStepCard(step)));
}

function renderStepCard(step) {
  const card = document.createElement("div");
  card.className = "card step-card";
  card.id = "step-" + step.id;

  const title = document.createElement("h3");
  title.textContent = step.label;
  card.appendChild(title);

  const desc = document.createElement("p");
  desc.className = "step-desc";
  desc.textContent = step.description;
  card.appendChild(desc);

  const fieldsWrap = document.createElement("div");
  fieldsWrap.className = "fields";
  step.fields.forEach((field) => {
    const row = document.createElement("label");
    row.className = "field-row";
    if (field.type === "bool_flag") {
      row.innerHTML = `<span>${field.label}</span><input type="checkbox" data-field="${field.name}" ${field.default ? "checked" : ""}>`;
    } else if (field.type === "select") {
      const opts = field.options.map((o) => `<option value="${o}" ${o === field.default ? "selected" : ""}>${o}</option>`).join("");
      row.innerHTML = `<span>${field.label}</span><select data-field="${field.name}">${opts}</select>`;
    } else {
      row.innerHTML = `<span>${field.label}</span><input type="text" data-field="${field.name}" placeholder="${field.default ?? ""}">`;
    }
    fieldsWrap.appendChild(row);
  });
  card.appendChild(fieldsWrap);

  const runBtn = document.createElement("button");
  runBtn.className = "primary run-btn";
  runBtn.textContent = "Run";
  runBtn.addEventListener("click", () => runStep(step, card));
  card.appendChild(runBtn);

  const logBox = document.createElement("pre");
  logBox.className = "log-box hidden";
  card.appendChild(logBox);

  const reportLink = document.createElement("a");
  reportLink.className = "report-link hidden";
  reportLink.textContent = "View full report →";
  reportLink.target = "_blank";
  card.appendChild(reportLink);

  card._runBtn = runBtn;
  card._logBox = logBox;
  card._reportLink = reportLink;
  return card;
}

function collectFieldValues(step, card) {
  const values = {};
  step.fields.forEach((field) => {
    const el = card.querySelector(`[data-field="${field.name}"]`);
    if (field.type === "bool_flag") {
      values[field.name] = el.checked;
    } else if (field.type === "int") {
      values[field.name] = el.value.trim() === "" ? null : parseInt(el.value.trim(), 10);
    } else if (field.type === "float") {
      values[field.name] = el.value.trim() === "" ? null : parseFloat(el.value.trim());
    } else {
      values[field.name] = el.value;
    }
  });
  return values;
}

function setAllRunButtonsDisabled(disabled) {
  document.querySelectorAll(".run-btn").forEach((b) => (b.disabled = disabled));
}

async function runStep(step, card) {
  if (state.activeJobId) {
    alert("A step is already running. Wait for it to finish.");
    return;
  }
  const values = collectFieldValues(step, card);
  card._logBox.classList.remove("hidden");
  card._logBox.textContent = "Starting…";
  card._reportLink.classList.add("hidden");
  setAllRunButtonsDisabled(true);

  let jobId;
  try {
    const resp = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ subcommand: step.id, root: state.selectedPath, values }),
    });
    jobId = resp.job_id;
  } catch (e) {
    card._logBox.textContent = "Failed to start: " + e.message;
    setAllRunButtonsDisabled(false);
    return;
  }

  state.activeJobId = jobId;
  pollJob(jobId, card);
}

async function pollJob(jobId, card) {
  try {
    const data = await api("/api/jobs/" + jobId);
    card._logBox.textContent = data.output || "(no output yet)";
    card._logBox.scrollTop = card._logBox.scrollHeight;
    if (data.status === "running" || data.status === "starting") {
      setTimeout(() => pollJob(jobId, card), 1500);
    } else {
      state.activeJobId = null;
      setAllRunButtonsDisabled(false);
      card._reportLink.href = "/api/reports/" + data.report_name;
      card._reportLink.classList.remove("hidden");
      const label = data.status === "done" ? "Done." : `Failed (exit code ${data.returncode}).`;
      card._logBox.textContent += `\n\n--- ${label} ---`;
    }
  } catch (e) {
    card._logBox.textContent += "\n\nError while polling: " + e.message;
    state.activeJobId = null;
    setAllRunButtonsDisabled(false);
  }
}

async function resumeActiveJobIfAny() {
  try {
    const cur = await api("/api/current-job");
    if (!cur.job_id) return;
    const data = await api("/api/jobs/" + cur.job_id);
    const card = document.getElementById("step-" + data.subcommand);
    if (card) {
      state.activeJobId = cur.job_id;
      setAllRunButtonsDisabled(true);
      card._logBox.classList.remove("hidden");
      pollJob(cur.job_id, card);
    }
  } catch (e) { /* nothing to resume */ }
}

// ---------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------

loadSettings();
loadSteps().then(resumeActiveJobIfAny);
