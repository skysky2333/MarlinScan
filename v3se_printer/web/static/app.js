"use strict";

const byId = (id) => document.getElementById(id);
const numberValue = (id) => Number(byId(id).value);
const textValue = (id) => byId(id).value.trim();
const DEFAULT_ROI = Object.freeze({ x: 0.2, y: 0.2, width: 0.6, height: 0.6 });
const BUSY_STATES = new Set(["connecting", "disconnecting", "moving", "capturing", "jogging", "calibrating", "scanning", "editing", "stopping"]);
const STOPPABLE_STATES = new Set(["moving", "jogging", "calibrating", "scanning", "editing"]);
const SCAN_DPI_PROFILES = new Set(["analysis", "raw"]);
const ROI_COLORS = { exposure: "#e5a33c", focus: "#55a6da", gray: "#f0f2f3" };
const SCAN_RESULT_LABELS = Object.freeze({
  full_tiff: "Full-resolution TIFF",
  pyramidal_tiff: "Pyramidal TIFF",
  scene_linear_exr: "Scene-linear EXR",
  preview_jpeg: "JPEG preview",
  project_metadata: "Scan parameters",
  recipe_metadata: "RAW development recipe",
  stitch_metadata: "Stitch metadata",
});
const EDITOR_RESULT_LABELS = Object.freeze({
  full_tiff: "Full-resolution TIFF",
  pyramidal_tiff: "Pyramidal OME-TIFF",
  working_linear_exr: "Linear working EXR",
  preview_jpeg: "JPEG preview",
  edit_recipe: "Edit recipe",
  revision_metadata: "Revision metadata",
});
const BED_MAX_X = 220;
const BED_MAX_Y = 220;

let snapshot = null;
let latestPath = null;
let latestImage = null;
let scanCaptureSize = null;
let resultPreviewDir = null;
let resultPreviewUrl = null;
let activeRoi = "exposure";
let roiDraft = null;
let roiPointer = null;
let roiStart = null;
let captureBounds = null;
let exposureAnalysis = null;
let bedTargetInitialized = false;
let lastReportedServerError = null;
let statusPending = false;
let statusPollTimer = null;
let measurementSignature = null;
let targetMeshSignature = null;
let editorProjectsLoaded = false;
let editorLoading = false;
let editorProject = null;
let editorMaterial = "positive";
let editorSource = "mosaic";
let editorPreviewObjectUrl = null;
const pendingButtons = new Set();
const rois = {
  exposure: { ...DEFAULT_ROI },
  focus: { ...DEFAULT_ROI },
  gray: { ...DEFAULT_ROI },
};

const captureCanvas = byId("capture-canvas");
const captureContext = captureCanvas.getContext("2d");
const loupeCanvas = byId("loupe-canvas");
const loupeContext = loupeCanvas.getContext("2d");
const waveformCanvas = byId("waveform-canvas");
const waveformContext = waveformCanvas.getContext("2d");
const exposureSampleCanvas = document.createElement("canvas");
const exposureSampleContext = exposureSampleCanvas.getContext("2d", { willReadFrequently: true });
const bedCanvas = byId("bed-canvas");
const bedContext = bedCanvas.getContext("2d");

function errorText(payload) {
  if (typeof payload.detail === "string") return payload.detail;
  return JSON.stringify(payload.detail);
}

async function requestJson(path, method = "GET", body = null) {
  const options = { method };
  if (body !== null) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(errorText(payload));
  return payload;
}

async function requestBlob(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(errorText(await response.json()));
  return response.blob();
}

function showError(error) {
  byId("notice-text").textContent = error.message;
  byId("notice").hidden = false;
}

function clearNotice() {
  byId("notice").hidden = true;
  byId("notice-text").textContent = "";
}

function runAction(button, path, payload = {}) {
  clearNotice();
  pendingButtons.add(button);
  renderControls();
  return requestJson(path, "POST", payload)
    .then((status) => {
      renderStatus(status);
      return status;
    })
    .catch((error) => {
      showError(error);
      throw error;
    })
    .finally(() => {
      pendingButtons.delete(button);
      renderControls();
    });
}

function action(button, path, payload = {}) {
  runAction(button, path, payload).catch(() => undefined);
}

function setDeviceState(element, text, kind = "") {
  element.textContent = text;
  element.className = `device-state${kind ? ` ${kind}` : ""}`;
}

function formatPosition(position) {
  if (position === null) return "X -   Y -   Z -";
  return `X ${position.x.toFixed(2)}   Y ${position.y.toFixed(2)}   Z ${position.z.toFixed(2)}`;
}

function setFieldText(id, value) {
  byId(id).textContent = value === null || value === "" ? "-" : value;
}

function syncChoiceSelect(id, choices, preferred, blankLabel = null, followPreferred = false, formatChoice = String) {
  const select = byId(id);
  const values = choices.map(String);
  const labels = values.map(formatChoice);
  const signature = JSON.stringify([blankLabel, values, labels]);
  if (select.dataset.choices !== signature) {
    const previous = select.value;
    select.replaceChildren();
    if (blankLabel !== null || values.length === 0) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = blankLabel === null ? "-" : blankLabel;
      select.append(option);
    }
    values.forEach((value, index) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = labels[index];
      select.append(option);
    });
    const preferredValue = preferred === null ? "" : String(preferred);
    if (values.includes(previous)) select.value = previous;
    else if (values.includes(preferredValue)) select.value = preferredValue;
    select.dataset.choices = signature;
  }
  const preferredValue = preferred === null ? "" : String(preferred);
  if (followPreferred && (values.includes(preferredValue) || (preferredValue === "" && blankLabel !== null))) select.value = preferredValue;
}

function shutterSeconds(label) {
  const value = String(label).trim();
  const unknown = /^Unknown value ([0-9a-f]+)$/i.exec(value);
  if (unknown !== null) {
    const raw = Number.parseInt(unknown[1], 16) & 0xff;
    const code = raw >= 0x80 ? raw - 0x100 : raw;
    return 2 ** (-code / 6);
  }
  if (/^bulb$/i.test(value)) return null;
  const fraction = /^(\d+(?:\.\d+)?)(?:\/(\d+(?:\.\d+)?))?$/.exec(value);
  if (fraction === null) return null;
  const numerator = Number(fraction[1]);
  const denominator = fraction[2] === undefined ? 1 : Number(fraction[2]);
  return denominator === 0 ? null : numerator / denominator;
}

function formatShutter(label) {
  const value = String(label);
  if (!/^Unknown value /i.test(value)) return value;
  const seconds = shutterSeconds(value);
  if (seconds === null) return value;
  if (seconds >= 1) return `~${Number(seconds.toPrecision(3))} s`;
  return `~1/${Number((1 / seconds).toPrecision(3))} s`;
}

function formatShuttersInText(value) {
  return String(value).replace(/Unknown value [0-9a-f]+/gi, (label) => formatShutter(label));
}

function syncCameraChoices(camera) {
  const knownChoices = (choices) => choices.map(String).filter((choice) => !choice.toLowerCase().includes("unknown value"));
  const isoChoices = knownChoices(camera.iso_choices).filter((choice) => !choice.toLowerCase().includes("auto"));
  const shutterChoices = camera.shutter_choices.map(String).filter((choice) => shutterSeconds(choice) !== null);
  syncChoiceSelect("camera-iso", isoChoices, camera.iso, null, true);
  syncChoiceSelect("test-shutter", shutterChoices, camera.shutter, "Select shutter", true, formatShutter);
}

function renderMeasurements(measurements) {
  const signature = JSON.stringify(measurements);
  if (signature === measurementSignature) return;
  measurementSignature = signature;
  const log = byId("measurement-log");
  log.replaceChildren();
  if (measurements.length === 0) {
    const empty = document.createElement("li");
    empty.className = "measurement-empty";
    empty.textContent = "No measurements yet.";
    log.append(empty);
    return;
  }
  measurements.slice().sort((left, right) => right.sequence - left.sequence).forEach((measurement) => {
    const item = document.createElement("li");
    item.className = "measurement-entry";

    const heading = document.createElement("div");
    heading.className = "measurement-entry-heading";
    const title = document.createElement("strong");
    title.textContent = `#${measurement.sequence} ${measurement.operation}`;
    const time = document.createElement("time");
    time.dateTime = measurement.timestamp;
    time.textContent = new Date(measurement.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    heading.append(title, time);

    const context = document.createElement("div");
    context.className = "measurement-context";
    [measurement.phase, measurement.profile].forEach((value) => {
      const label = document.createElement("span");
      label.textContent = value;
      context.append(label);
    });
    const accepted = document.createElement("span");
    const acceptance = measurement.accepted === null ? "info" : measurement.accepted ? "accepted" : "rejected";
    accepted.className = `measurement-acceptance ${acceptance}`;
    accepted.textContent = acceptance === "info" ? "Info" : acceptance === "accepted" ? "Accepted" : "Rejected";
    context.append(accepted);

    const values = document.createElement("div");
    values.className = "measurement-values";
    const parameter = document.createElement("span");
    parameter.textContent = formatShuttersInText(measurement.parameter);
    const arrow = document.createElement("span");
    arrow.textContent = "\u2192";
    const result = document.createElement("strong");
    result.textContent = measurement.result;
    values.append(parameter, arrow, result);
    item.append(heading, context, values);
    log.append(item);
  });
}

function renderStatus(status) {
  const previousState = snapshot === null ? null : snapshot.state;
  snapshot = status;
  const printer = status.printer;
  const camera = status.camera;
  const state = status.state;
  const active = BUSY_STATES.has(state);
  const stateDot = byId("state-dot");

  byId("system-state").textContent = state;
  byId("system-message").textContent = status.message;
  renderOperationProgress(status.step_progress);
  stateDot.className = "state-dot";
  stateDot.classList.add(state === "faulted" ? "error" : active ? "busy" : "ready");

  if (printer.faulted) setDeviceState(byId("printer-state"), "Faulted", "error");
  else if (printer.initialized) setDeviceState(byId("printer-state"), "Ready", "ready");
  else if (printer.connected) setDeviceState(byId("printer-state"), "Needs origin", "warning");
  else setDeviceState(byId("printer-state"), "Disconnected");

  if (camera.connected && camera.shutter !== null && camera.configured_profile !== null) setDeviceState(byId("camera-state"), "Ready", "ready");
  else if (camera.connected) setDeviceState(byId("camera-state"), "Needs setup", "warning");
  else if (camera.control_taken) setDeviceState(byId("camera-state"), "Owned", "warning");
  else setDeviceState(byId("camera-state"), "Disconnected");

  const initialization = byId("initialization-state");
  initialization.classList.toggle("ready", printer.initialized);
  initialization.querySelector(".initialization-mark").textContent = printer.initialized ? "OK" : "!";
  initialization.querySelector("strong").textContent = printer.initialized ? "Coordinates initialized" : "Coordinates required";
  initialization.querySelector("span:last-child").textContent = printer.initialized ? "Motion is enabled." : "Initialize after every connection.";

  setFieldText("printer-firmware", printer.firmware);
  setFieldText("printer-machine", printer.machine);
  setFieldText("camera-quality", camera.image_quality);
  setFieldText("camera-configured-profile", camera.configured_profile);
  setFieldText("camera-latest-profile", camera.latest_capture_profile);
  setFieldText("camera-shutter", camera.shutter === null ? null : formatShutter(camera.shutter));
  syncCameraChoices(camera);
  renderMeasurements(status.measurements);
  byId("position-line").textContent = formatPosition(printer.position);

  if (printer.position !== null && !bedTargetInitialized) {
    byId("move-x").value = printer.position.x.toFixed(2);
    byId("move-y").value = printer.position.y.toFixed(2);
    byId("move-z").value = printer.position.z.toFixed(2);
    bedTargetInitialized = true;
  }
  if (!printer.connected) bedTargetInitialized = false;
  syncTargetMesh(activeFocusMesh());

  if (status.latest_jpeg_path !== null && status.latest_jpeg_path !== latestPath) {
    loadLatestImage(status.latest_jpeg_path, camera.latest_capture_profile);
  }
  renderResults(status);
  renderEditorResult(status.editor_result, status.editor_results);
  renderControls();
  drawBed();

  if (status.error !== null && status.error !== lastReportedServerError) {
    lastReportedServerError = status.error;
    showError(new Error(status.error));
  }
  if (status.error === null) lastReportedServerError = null;
  if ((previousState === "calibrating" || previousState === "scanning") && state === "idle" && status.error === null) {
    showWorkflowPanel("results-panel");
  }
  if (previousState === "editing" && state === "idle" && status.error === null) {
    const preferred = editorProject === null ? null : editorProject.directory;
    const revision = status.editor_result;
    const preview = status.editor_results.preview_jpeg;
    loadEditorProjects(preferred, false)
      .then(() => {
        if (revision === null || preview === null) throw new Error("Completed editor revision is missing its preview");
        setEditorSource("mosaic", false);
        showEditorPreview(preview.download_url, `Applied ${revision.revision}`);
      })
      .catch(showError);
  }
  if (active && state !== "jogging") stopJog();
  scheduleStatusPoll();
}

function renderControls() {
  const printer = snapshot === null ? null : snapshot.printer;
  const camera = snapshot === null ? null : snapshot.camera;
  const state = snapshot === null ? "connecting" : snapshot.state;
  const mutationPending = Array.from(pendingButtons).some((button) => button.id !== "refresh-ports");
  const idle = state === "idle" && !mutationPending;
  const printerReady = printer !== null && printer.connected && printer.initialized && !printer.faulted;
  const cameraConnected = camera !== null && camera.connected;
  const cameraReady = cameraConnected && camera.shutter !== null && camera.configured_profile !== null;
  const calibrated = snapshot !== null && snapshot.calibration !== null;
  const disabled = (id, value) => {
    const element = byId(id);
    element.disabled = value || pendingButtons.has(element);
  };

  disabled("emergency-stop", printer === null || !printer.connected);
  disabled("stop-operation", !STOPPABLE_STATES.has(state));
  disabled("printer-connect", (!idle && state !== "faulted") || (printer !== null && printer.connected));
  disabled("printer-disconnect", (!idle && state !== "faulted") || printer === null || !printer.connected);
  disabled("printer-home", !idle || printer === null || !printer.connected || printer.faulted);
  disabled("printer-origin", !idle || printer === null || !printer.connected || printer.faulted);
  disabled("printer-restore", !idle || printer === null || !printer.connected || printer.initialized || printer.faulted || printer.remembered_position == null);
  disabled("camera-control", !idle || camera === null || camera.control_taken);
  disabled("camera-connect", !idle || camera === null || !camera.control_taken || camera.connected);
  disabled("camera-disconnect", !idle || !cameraConnected);
  disabled("camera-iso", !idle || !cameraConnected);
  disabled("test-shutter", !idle || !cameraConnected);
  disabled("camera-test", !idle || !cameraReady);
  disabled("move-submit", !idle || !printerReady);
  disabled("auto-exposure", !idle || !cameraReady);
  disabled("auto-focus", !idle || !printerReady || !cameraReady);
  disabled("focus-grid", !idle || !printerReady || !cameraReady);
  disabled("gray-white-balance", !idle || !cameraReady);
  disabled("start-calibration", !idle || !printerReady || !cameraReady);
  disabled("start-scan", !idle || !printerReady || !cameraReady || !calibrated);
  disabled("editor-project", !idle || editorLoading);
  disabled("editor-refresh-projects", !idle || editorLoading);
  disabled("editor-tile", !idle || editorLoading || editorProject === null || editorSource !== "tile");
  disabled("editor-reset", !idle || editorProject === null);
  disabled("editor-preview-button", !idle || editorLoading || editorProject === null);
  disabled("editor-apply", !idle || editorLoading || editorProject === null);
  disabled("refresh-ports", false);
  document.querySelectorAll("#calibration-form input, #scan-form input").forEach((input) => {
    input.disabled = !idle;
  });
  document.querySelectorAll("[data-current-bound]").forEach((button) => {
    button.disabled = !idle || printer === null || printer.position === null;
  });
  document.querySelectorAll(".jog-button").forEach((button) => {
    button.disabled = !printerReady || (!idle && state !== "jogging");
  });
  document.querySelectorAll("[data-editor-source], [data-editor-material], .editor-control-tab").forEach((button) => {
    button.disabled = !idle || editorLoading || editorProject === null;
  });
  document.querySelectorAll("#editor-form input").forEach((input) => {
    input.disabled = !idle || editorProject === null;
  });
}

function renderResults(status) {
  const calibration = status.calibration;
  const focusSurface = status.focus_grid;
  const resultState = byId("result-state");
  setFieldText("result-iso", status.camera.iso);
  setFieldText("result-shutter", status.camera.shutter === null ? null : formatShutter(status.camera.shutter));
  resultState.className = "result-state";
  if (status.error !== null) {
    resultState.classList.add("error");
    resultState.querySelector(".result-mark").textContent = "!";
    resultState.querySelector("strong").textContent = "Operation failed";
    resultState.querySelector("span:last-child").textContent = status.error;
  } else if (focusSurface !== null || status.calibration !== null || status.last_scan_dir !== null) {
    resultState.classList.add("complete");
    resultState.querySelector(".result-mark").textContent = "OK";
    resultState.querySelector("strong").textContent = "Outputs available";
    resultState.querySelector("span:last-child").textContent = "Latest completed focus surface, calibration, and scan outputs.";
  } else {
    resultState.querySelector(".result-mark").textContent = "-";
    resultState.querySelector("strong").textContent = "No completed work";
    resultState.querySelector("span:last-child").textContent = "Calibration and scan outputs will appear here.";
  }

  if (calibration !== null) {
    const wb = calibration.white_balance;
    setFieldText("result-exposure", formatExposureReading(calibration.exposure));
    setFieldText("result-wb", `R ${wb.red.toFixed(3)}  G ${wb.green.toFixed(3)}  B ${wb.blue.toFixed(3)}`);
    setFieldText("result-calibration-dir", calibration.directory);
  } else {
    setFieldText("result-exposure", null);
    setFieldText("result-wb", null);
    setFieldText("result-calibration-dir", null);
  }
  if (focusSurface !== null) {
    const zValues = focusSurface.measurements.map((measurement) => measurement.z);
    const method = focusMethodLabel(focusSurface.method);
    const range = `${Math.min(...zValues).toFixed(3)}-${Math.max(...zValues).toFixed(3)} mm`;
    setFieldText("result-focus", `${method} · ${zValues.length} measured · Z ${range}`);
    setFieldText("result-focus-dir", focusSurface.directory);
  } else {
    setFieldText("result-focus", null);
    setFieldText("result-focus-dir", null);
  }
  const quick = status.quick_calibration;
  if (quick == null) {
    setFieldText("result-quick-exposure", null);
    setFieldText("result-quick-focus", null);
    setFieldText("result-quick-wb", null);
  } else {
    const exposure = quick.exposure;
    const wb = quick.white_balance;
    setFieldText("result-quick-exposure", exposure === null ? null : `${formatShutter(exposure.shutter)}  ${formatExposureReading(exposure.reading)}`);
    setFieldText("result-quick-focus", quick.focus_z === null ? null : `${quick.focus_z.toFixed(3)} mm`);
    setFieldText("result-quick-wb", wb === null ? null : `R ${wb.red.toFixed(3)}  G ${wb.green.toFixed(3)}  B ${wb.blue.toFixed(3)}`);
  }
  setFieldText("result-scan-dir", status.last_scan_dir);
  renderScanResultFiles(status.scan_results);
  const preview = status.scan_results.preview_jpeg;
  const previewUrl = preview === null ? null : preview.download_url;
  if (status.last_scan_dir !== resultPreviewDir || previewUrl !== resultPreviewUrl) {
    loadResultPreview(status.last_scan_dir, preview);
  }
}

function formatExposureReading(reading) {
  const values = [
    `Meter ${reading.metered_luminance.toFixed(1)}`,
    `P99 ${reading.percentile_99.toFixed(1)}`,
    `JPEG clip ${(reading.clipped_fraction * 100).toFixed(3)}%`,
  ];
  if (reading.raw_saturated_fraction !== null) {
    values.push(`RAW saturation ${(reading.raw_saturated_fraction * 100).toFixed(3)}%`);
  }
  if (reading.raw_highlight_level !== null) {
    values.push(`RAW P99 ${(reading.raw_highlight_level * 100).toFixed(1)}%`);
  }
  if (reading.warning !== null) values.push(reading.warning);
  return values.join("  ");
}

function renderScanResultFiles(scanResults) {
  const list = byId("result-files");
  list.replaceChildren();
  Object.entries(SCAN_RESULT_LABELS).forEach(([artifact, label]) => {
    const result = scanResults[artifact];
    if (result === null) return;
    const item = document.createElement("li");
    item.className = "result-file";
    const description = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = label;
    const name = document.createElement("span");
    name.textContent = result.name;
    description.append(title, name);
    const download = document.createElement("a");
    download.className = "button secondary result-download";
    download.href = result.download_url;
    download.download = result.name;
    download.textContent = "Download";
    item.append(description, download);
    list.append(item);
  });
  list.hidden = list.childElementCount === 0;
}

function loadResultPreview(scanDir, preview) {
  resultPreviewDir = scanDir;
  resultPreviewUrl = preview === null ? null : preview.download_url;
  const image = byId("result-preview");
  const empty = byId("result-preview-empty");
  image.hidden = true;
  empty.hidden = false;
  if (preview === null) {
    empty.querySelector("strong").textContent = "No scan preview";
    empty.querySelector("span").textContent = "A completed mosaic will appear here.";
    return;
  }
  empty.querySelector("strong").textContent = "Loading preview";
  empty.querySelector("span").textContent = "Reading the latest mosaic.";
  image.onload = () => {
    if (resultPreviewDir !== scanDir || resultPreviewUrl !== preview.download_url) return;
    image.hidden = false;
    empty.hidden = true;
  };
  image.onerror = () => {
    if (resultPreviewDir !== scanDir || resultPreviewUrl !== preview.download_url) return;
    empty.querySelector("strong").textContent = "Preview unavailable";
    empty.querySelector("span").textContent = "The scan folder does not contain a mosaic preview.";
  };
  image.src = `${preview.download_url}?t=${Date.now()}`;
}

function showApplicationWorkspace(name) {
  document.querySelectorAll(".workspace-tab").forEach((tab) => {
    const active = tab.dataset.workspace === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  byId("capture-workspace").hidden = name !== "capture";
  byId("editor-workspace").hidden = name !== "editor";
  byId("capture-workspace").setAttribute("tabindex", name === "capture" ? "0" : "-1");
  byId("editor-workspace").setAttribute("tabindex", name === "editor" ? "0" : "-1");
  byId("capture-workspace-tab").tabIndex = name === "capture" ? 0 : -1;
  byId("editor-workspace-tab").tabIndex = name === "editor" ? 0 : -1;
  document.querySelector(".skip-link").href = name === "capture" ? "#camera-stage" : "#editor-preview";
  if (name === "editor" && !editorProjectsLoaded && !editorLoading) {
    loadEditorProjects().catch(showError);
  }
}

function renderEditorResult(result, results) {
  const container = byId("editor-result");
  container.hidden = result == null;
  const list = byId("editor-result-files");
  list.replaceChildren();
  if (result == null) return;
  byId("editor-result-revision").textContent = String(result.revision);
  byId("editor-result-directory").textContent = result.directory;
  Object.entries(EDITOR_RESULT_LABELS).forEach(([artifact, label]) => {
    const file = results[artifact];
    if (file === null) return;
    const item = document.createElement("li");
    const download = document.createElement("a");
    download.href = file.download_url;
    download.download = file.name;
    download.textContent = label;
    item.append(download);
    list.append(item);
  });
}

function clearEditorPreview() {
  if (editorPreviewObjectUrl !== null) URL.revokeObjectURL(editorPreviewObjectUrl);
  editorPreviewObjectUrl = null;
  const image = byId("editor-preview-image");
  image.removeAttribute("src");
  image.hidden = true;
  byId("editor-preview-empty").hidden = false;
}

function showEditorPreview(url, state, objectUrl = false) {
  clearEditorPreview();
  if (objectUrl) editorPreviewObjectUrl = url;
  const image = byId("editor-preview-image");
  const empty = byId("editor-preview-empty");
  empty.querySelector("strong").textContent = "Loading preview";
  empty.querySelector("span").textContent = "Reading scene-linear image data.";
  image.onload = () => {
    image.hidden = false;
    empty.hidden = true;
  };
  image.onerror = () => {
    image.hidden = true;
    empty.hidden = false;
    empty.querySelector("strong").textContent = "Preview unavailable";
    empty.querySelector("span").textContent = "The selected source could not be displayed.";
  };
  image.src = objectUrl ? url : `${url}${url.includes("?") ? "&" : "?"}t=${Date.now()}`;
  byId("editor-preview-state").textContent = state;
}

function clearEditorProject() {
  editorProject = null;
  byId("editor-preview-title").textContent = "No project selected";
  setFieldText("editor-project-tiles", null);
  setFieldText("editor-project-revisions", null);
  setFieldText("editor-project-directory", null);
  byId("editor-tile").replaceChildren();
  clearEditorPreview();
  byId("editor-preview-empty").querySelector("strong").textContent = "No scan loaded";
  byId("editor-preview-empty").querySelector("span").textContent = "Select a completed scan project.";
  byId("editor-preview-source-label").textContent = "Full image";
  byId("editor-preview-state").textContent = "Original preview";
}

function renderEditorProject(project, resetRecipe = true) {
  if (!Array.isArray(project.tiles) || !Array.isArray(project.revisions)) {
    throw new TypeError("Editor project response must include tile and revision arrays");
  }
  editorProject = project;
  byId("editor-preview-title").textContent = project.name;
  setFieldText("editor-project-tiles", String(project.tile_count));
  setFieldText("editor-project-revisions", String(project.revisions.length));
  setFieldText("editor-project-directory", project.directory);
  const tileSelect = byId("editor-tile");
  tileSelect.replaceChildren();
  project.tiles.forEach((tile) => {
    const option = document.createElement("option");
    option.value = String(tile.index);
    option.textContent = tile.label;
    tileSelect.append(option);
  });
  if (resetRecipe) resetEditorRecipe();
}

function loadEditorProject(directory) {
  editorLoading = true;
  clearEditorProject();
  renderControls();
  return requestJson("/api/editor/project", "POST", { project_dir: directory })
    .then((project) => renderEditorProject(project))
    .finally(() => {
      editorLoading = false;
      renderControls();
    });
}

function loadEditorProjects(preferredDirectory = null, resetRecipe = true) {
  const selectedBeforeLoad = preferredDirectory === null && editorProject !== null
    ? editorProject.directory
    : preferredDirectory;
  editorLoading = true;
  clearEditorProject();
  renderControls();
  return requestJson("/api/editor/projects")
    .then((payload) => {
      if (!Array.isArray(payload.projects)) throw new TypeError("Editor projects response must include a projects array");
      const select = byId("editor-project");
      select.replaceChildren();
      payload.projects.forEach((project) => {
        const option = document.createElement("option");
        option.value = project.directory;
        const revisionLabel = project.revision_count === 1 ? "revision" : "revisions";
        option.textContent = `${project.name} · ${project.tile_count} tiles · ${project.revision_count} ${revisionLabel}`;
        select.append(option);
      });
      editorProjectsLoaded = true;
      if (payload.projects.length === 0) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No completed scans";
        select.append(option);
        clearEditorProject();
        return null;
      }
      const selected = payload.projects.some((project) => project.directory === selectedBeforeLoad)
        ? selectedBeforeLoad
        : payload.projects[0].directory;
      select.value = selected;
      return requestJson("/api/editor/project", "POST", { project_dir: selected })
        .then((project) => renderEditorProject(project, resetRecipe));
    })
    .finally(() => {
      editorLoading = false;
      renderControls();
    });
}

function editorRecipe() {
  return {
    version: 1,
    material: editorMaterial,
    exposure_ev: numberValue("editor-exposure-ev"),
    temperature: numberValue("editor-temperature"),
    tint: numberValue("editor-tint"),
    contrast: numberValue("editor-contrast"),
    highlights: numberValue("editor-highlights"),
    shadows: numberValue("editor-shadows"),
    black_point: numberValue("editor-black-point"),
    white_point: numberValue("editor-white-point"),
    saturation: numberValue("editor-saturation"),
    red_balance: numberValue("editor-red-balance"),
    green_balance: numberValue("editor-green-balance"),
    blue_balance: numberValue("editor-blue-balance"),
    film_base_red: numberValue("editor-film-base-red"),
    film_base_green: numberValue("editor-film-base-green"),
    film_base_blue: numberValue("editor-film-base-blue"),
    film_density: numberValue("editor-film-density"),
  };
}

function validEditorRecipe(recipe) {
  if (recipe.white_point > recipe.black_point) return true;
  showError(new Error("White point must be greater than black point"));
  return false;
}

function editorPreviewPayload() {
  return {
    project_dir: editorProject.directory,
    source: editorSource,
    tile_index: editorSource === "tile" ? numberValue("editor-tile") : null,
    recipe: editorRecipe(),
  };
}

function previewEditor() {
  const button = byId("editor-preview-button");
  const payload = editorPreviewPayload();
  if (!validEditorRecipe(payload.recipe)) return;
  clearNotice();
  pendingButtons.add(button);
  byId("editor-preview-state").textContent = "Rendering preview";
  renderControls();
  requestBlob("/api/editor/preview", payload)
    .then((blob) => showEditorPreview(URL.createObjectURL(blob), "Recipe preview", true))
    .catch(showError)
    .finally(() => {
      pendingButtons.delete(button);
      renderControls();
    });
}

function setEditorMaterial(material) {
  editorMaterial = material;
  document.querySelectorAll("[data-editor-material]").forEach((button) => {
    const active = button.dataset.editorMaterial === material;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  byId("editor-film-controls").hidden = material !== "negative";
  byId("editor-preview-state").textContent = "Changes not previewed";
}

function setEditorSource(source, renderPreview = true) {
  editorSource = source;
  document.querySelectorAll("[data-editor-source]").forEach((button) => {
    const active = button.dataset.editorSource === source;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  byId("editor-tile-field").hidden = source !== "tile";
  byId("editor-preview-source-label").textContent = source === "mosaic" ? "Full image" : "Local RAW";
  renderControls();
  if (renderPreview && editorProject !== null) previewEditor();
}

function showEditorControlPanel(panelId) {
  document.querySelectorAll(".editor-control-tab").forEach((tab) => {
    const active = tab.dataset.editorControls === panelId;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll(".editor-control-panel").forEach((panel) => { panel.hidden = panel.id !== panelId; });
}

function syncEditorControl(input) {
  const output = input.parentElement.querySelector("output");
  const value = input.valueAsNumber;
  output.textContent = input.dataset.editorField === "exposure_ev" ? `${value.toFixed(1)} EV` : value.toFixed(2);
}

function resetEditorRecipe() {
  const defaults = {
    exposure_ev: 0,
    temperature: 0,
    tint: 0,
    contrast: 0,
    highlights: 0,
    shadows: 0,
    black_point: 0,
    white_point: 1,
    saturation: 1,
    red_balance: 1,
    green_balance: 1,
    blue_balance: 1,
    film_base_red: 1,
    film_base_green: 1,
    film_base_blue: 1,
    film_density: 1,
  };
  document.querySelectorAll("[data-editor-field]").forEach((input) => {
    input.value = String(defaults[input.dataset.editorField]);
    syncEditorControl(input);
  });
  setEditorMaterial("positive");
  showEditorControlPanel("editor-basic-controls");
  setEditorSource("mosaic", false);
  if (editorProject !== null) showEditorPreview(editorProject.preview_url, "Original preview");
}

function scheduleStatusPoll() {
  window.clearTimeout(statusPollTimer);
  const delay = snapshot !== null && BUSY_STATES.has(snapshot.state) ? 250 : 1000;
  statusPollTimer = window.setTimeout(pollStatus, delay);
}

function pollStatus() {
  if (statusPending) {
    scheduleStatusPoll();
    return;
  }
  statusPending = true;
  requestJson("/api/status")
    .then(renderStatus)
    .catch(showError)
    .finally(() => {
      statusPending = false;
      scheduleStatusPoll();
    });
}

function loadPorts() {
  const refreshButton = byId("refresh-ports");
  pendingButtons.add(refreshButton);
  renderControls();
  requestJson("/api/printer/ports")
    .then((ports) => {
      if (!Array.isArray(ports)) throw new TypeError("Serial port response must be an array");
      const select = byId("printer-port");
      const selected = select.value;
      select.replaceChildren();
      ports.forEach((port) => {
        const option = document.createElement("option");
        option.value = port.device;
        option.textContent = port.label;
        select.append(option);
      });
      if (ports.length === 0) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No ports found";
        select.append(option);
      } else if (ports.some((port) => port.device === selected)) {
        select.value = selected;
      }
    })
    .catch(showError)
    .finally(() => {
      pendingButtons.delete(refreshButton);
      renderControls();
    });
}

function fitCanvas(canvas, context, draw) {
  const rect = canvas.getBoundingClientRect();
  const scale = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(rect.width * scale));
  const height = Math.max(1, Math.round(rect.height * scale));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  context.setTransform(scale, 0, 0, scale, 0, 0);
  draw(rect.width, rect.height);
}

function analyzeExposure() {
  if (latestImage === null) {
    exposureAnalysis = null;
    drawExposureAnalysis();
    return;
  }
  const roi = rois.exposure;
  const sourceX = Math.floor(roi.x * latestImage.naturalWidth);
  const sourceY = Math.floor(roi.y * latestImage.naturalHeight);
  const sourceRight = Math.ceil((roi.x + roi.width) * latestImage.naturalWidth);
  const sourceBottom = Math.ceil((roi.y + roi.height) * latestImage.naturalHeight);
  const sourceWidth = Math.max(1, sourceRight - sourceX);
  const sourceHeight = Math.max(1, sourceBottom - sourceY);
  const waveformWidth = Math.min(320, sourceWidth);
  exposureSampleCanvas.width = sourceWidth;
  exposureSampleCanvas.height = sourceHeight;
  exposureSampleContext.drawImage(latestImage, sourceX, sourceY, sourceWidth, sourceHeight, 0, 0, sourceWidth, sourceHeight);
  const pixels = exposureSampleContext.getImageData(0, 0, sourceWidth, sourceHeight).data;
  const histogram = new Uint32Array(256);
  const waveform = new Uint32Array(waveformWidth * 256);
  let clipped = 0;
  for (let index = 0, pixel = 0; index < pixels.length; index += 4, pixel += 1) {
    const luminance = Math.round(.299 * pixels[index] + .587 * pixels[index + 1] + .114 * pixels[index + 2]);
    histogram[luminance] += 1;
    const waveformX = Math.min(waveformWidth - 1, Math.floor((pixel % sourceWidth) * waveformWidth / sourceWidth));
    waveform[waveformX * 256 + luminance] += 1;
    if (luminance >= 250) clipped += 1;
  }
  const count = sourceWidth * sourceHeight;
  const winsorLow = histogramPercentile(histogram, count, .02);
  const winsorHigh = histogramPercentile(histogram, count, .98);
  let winsorizedTotal = 0;
  histogram.forEach((frequency, luminance) => {
    winsorizedTotal += frequency * Math.max(winsorLow, Math.min(winsorHigh, luminance));
  });
  exposureAnalysis = {
    waveform,
    width: waveformWidth,
    meteredLuminance: winsorizedTotal / count,
    percentile99: histogramPercentile(histogram, count, .99),
    clippedFraction: clipped / count,
  };
  drawExposureAnalysis();
}

function histogramPercentile(histogram, count, fraction) {
  const position = (count - 1) * fraction;
  const lowerRank = Math.floor(position);
  const upperRank = Math.ceil(position);
  const lower = histogramRankValue(histogram, lowerRank);
  const upper = histogramRankValue(histogram, upperRank);
  return lower + (upper - lower) * (position - lowerRank);
}

function histogramRankValue(histogram, rank) {
  let cumulative = 0;
  for (let luminance = 0; luminance < histogram.length; luminance += 1) {
    cumulative += histogram[luminance];
    if (rank < cumulative) return luminance;
  }
  throw new RangeError("Histogram rank exceeds its sample count");
}

function drawExposureAnalysis() {
  const width = waveformCanvas.width;
  const height = waveformCanvas.height;
  waveformContext.clearRect(0, 0, width, height);
  waveformContext.fillStyle = "#15191c";
  waveformContext.fillRect(0, 0, width, height);
  waveformContext.strokeStyle = "#3c4448";
  waveformContext.lineWidth = 1;
  for (let step = 1; step < 4; step += 1) {
    waveformContext.beginPath();
    waveformContext.moveTo(0, Math.round(height * step / 4) + .5);
    waveformContext.lineTo(width, Math.round(height * step / 4) + .5);
    waveformContext.stroke();
  }
  if (exposureAnalysis === null) {
    byId("exposure-state").textContent = "No image";
    byId("exposure-ev").textContent = "-";
    byId("exposure-metered").textContent = "-";
    byId("exposure-p99").textContent = "-";
    byId("exposure-clipped").textContent = "-";
    byId("exposure-marker").style.left = "50%";
    waveformCanvas.setAttribute("aria-label", "No exposure waveform available");
    return;
  }
  let maximum = 0;
  exposureAnalysis.waveform.forEach((count) => { maximum = Math.max(maximum, count); });
  const columnWidth = width / exposureAnalysis.width;
  for (let x = 0; x < exposureAnalysis.width; x += 1) {
    for (let value = 0; value < 256; value += 1) {
      const count = exposureAnalysis.waveform[x * 256 + value];
      if (count === 0) continue;
      const opacity = .18 + .82 * Math.log1p(count) / Math.log1p(maximum);
      waveformContext.fillStyle = `rgba(220, 235, 229, ${opacity})`;
      waveformContext.fillRect(x * columnWidth, height - 1 - value / 255 * (height - 1), Math.ceil(columnWidth), 1);
    }
  }
  const metered = exposureAnalysis.meteredLuminance;
  const p99 = exposureAnalysis.percentile99;
  const clippedPercent = exposureAnalysis.clippedFraction * 100;
  const ev = metered === 0 ? -Infinity : 2.2 * Math.log2(metered / 128);
  const meterPosition = (Math.max(-2, Math.min(2, ev)) + 2) / 4 * 100;
  const state = ev < -1 / 3 ? "Low" : ev > 1 / 3 ? "High" : "On target";
  byId("exposure-state").textContent = state;
  byId("exposure-ev").textContent = metered === 0 ? "< -8 EV" : `${ev >= 0 ? "+" : ""}${ev.toFixed(2)} EV`;
  byId("exposure-metered").textContent = metered.toFixed(1);
  byId("exposure-p99").textContent = p99.toFixed(1);
  byId("exposure-clipped").textContent = `${clippedPercent.toFixed(3)}%`;
  byId("exposure-marker").style.left = `${meterPosition}%`;
  waveformCanvas.setAttribute("aria-label", `Exposure ROI luminance waveform. Meter ${metered.toFixed(1)}, P99 ${p99.toFixed(1)}, JPEG clipped ${clippedPercent.toFixed(3)} percent`);
}

function drawCapture() {
  fitCanvas(captureCanvas, captureContext, (width, height) => {
    captureContext.clearRect(0, 0, width, height);
    captureContext.fillStyle = "#15191c";
    captureContext.fillRect(0, 0, width, height);
    captureBounds = null;
    if (latestImage === null) return;

    const scale = Math.min(width / latestImage.naturalWidth, height / latestImage.naturalHeight);
    const imageWidth = latestImage.naturalWidth * scale;
    const imageHeight = latestImage.naturalHeight * scale;
    captureBounds = { x: (width - imageWidth) / 2, y: (height - imageHeight) / 2, width: imageWidth, height: imageHeight };
    captureContext.drawImage(latestImage, captureBounds.x, captureBounds.y, captureBounds.width, captureBounds.height);

    Object.keys(rois).filter((name) => name !== activeRoi).forEach((name) => drawRoi(name, rois[name], false));
    drawRoi(activeRoi, roiDraft === null ? rois[activeRoi] : roiDraft, true);
  });
}

function drawCenterLoupe() {
  fitCanvas(loupeCanvas, loupeContext, (width, height) => {
    loupeContext.clearRect(0, 0, width, height);
    loupeContext.fillStyle = "#15191c";
    loupeContext.fillRect(0, 0, width, height);
    if (latestImage === null) {
      loupeCanvas.setAttribute("aria-label", "No center loupe available");
      return;
    }
    const sourceWidth = latestImage.naturalWidth / 10;
    const sourceHeight = latestImage.naturalHeight / 10;
    const sourceX = (latestImage.naturalWidth - sourceWidth) / 2;
    const sourceY = (latestImage.naturalHeight - sourceHeight) / 2;
    const scale = Math.min(width / sourceWidth, height / sourceHeight);
    const targetWidth = sourceWidth * scale;
    const targetHeight = sourceHeight * scale;
    loupeContext.imageSmoothingEnabled = true;
    loupeContext.imageSmoothingQuality = "high";
    loupeContext.drawImage(latestImage, sourceX, sourceY, sourceWidth, sourceHeight, (width - targetWidth) / 2, (height - targetHeight) / 2, targetWidth, targetHeight);
    loupeCanvas.setAttribute("aria-label", "10x center crop of the latest camera image");
  });
}

function drawRoi(name, roi, active) {
  const x = captureBounds.x + roi.x * captureBounds.width;
  const y = captureBounds.y + roi.y * captureBounds.height;
  const width = roi.width * captureBounds.width;
  const height = roi.height * captureBounds.height;
  captureContext.save();
  captureContext.strokeStyle = ROI_COLORS[name];
  captureContext.lineWidth = active ? 2 : 1;
  captureContext.globalAlpha = active ? 1 : .6;
  captureContext.setLineDash(active ? [] : [5, 4]);
  captureContext.strokeRect(x, y, width, height);
  if (active) {
    captureContext.fillStyle = `${ROI_COLORS[name]}24`;
    captureContext.fillRect(x, y, width, height);
    const size = 6;
    [[x, y], [x + width, y], [x, y + height], [x + width, y + height]].forEach(([px, py]) => {
      captureContext.fillStyle = ROI_COLORS[name];
      captureContext.fillRect(px - size / 2, py - size / 2, size, size);
    });
  }
  captureContext.restore();
}

function loadLatestImage(path, profile) {
  const image = new Image();
  image.onload = () => {
    latestPath = path;
    latestImage = image;
    if (SCAN_DPI_PROFILES.has(profile)) {
      scanCaptureSize = { width: image.naturalWidth, height: image.naturalHeight };
    }
    byId("capture-file").textContent = path.split("/").pop();
    byId("empty-capture").hidden = true;
    drawCapture();
    drawCenterLoupe();
    analyzeExposure();
    renderEstimatedDpi();
  };
  image.onerror = () => showError(new Error(`Failed to load latest capture: ${path}`));
  image.src = `/api/latest.jpg?path=${encodeURIComponent(path)}&t=${Date.now()}`;
}

function renderEstimatedDpi() {
  const output = byId("scan-estimated-dpi");
  if (scanCaptureSize === null) {
    output.textContent = "Take an analysis or RAW capture to estimate DPI";
    return;
  }
  const frameWidth = byId("scan-frame-width").valueAsNumber;
  const frameHeight = byId("scan-frame-height").valueAsNumber;
  if (!Number.isFinite(frameWidth) || !Number.isFinite(frameHeight) || frameWidth <= 0 || frameHeight <= 0) {
    output.textContent = "Enter a valid footprint";
    return;
  }
  const dpiX = scanCaptureSize.width / frameWidth * 25.4;
  const dpiY = scanCaptureSize.height / frameHeight * 25.4;
  const nominalDpi = (dpiX + dpiY) / 2;
  output.textContent = `Nominal ${Math.round(nominalDpi).toLocaleString()} DPI · X ${Math.round(dpiX).toLocaleString()} · Y ${Math.round(dpiY).toLocaleString()}`;
}

function normalizedCapturePoint(event) {
  if (captureBounds === null) return null;
  const rect = captureCanvas.getBoundingClientRect();
  const x = Math.max(captureBounds.x, Math.min(captureBounds.x + captureBounds.width, event.clientX - rect.left));
  const y = Math.max(captureBounds.y, Math.min(captureBounds.y + captureBounds.height, event.clientY - rect.top));
  return { x: (x - captureBounds.x) / captureBounds.width, y: (y - captureBounds.y) / captureBounds.height };
}

function roiFromPoints(first, second) {
  return {
    x: Math.min(first.x, second.x),
    y: Math.min(first.y, second.y),
    width: Math.abs(second.x - first.x),
    height: Math.abs(second.y - first.y),
  };
}

function updateRoiReadout() {
  const roi = rois[activeRoi];
  const name = `${activeRoi[0].toUpperCase()}${activeRoi.slice(1)} ROI`;
  byId("roi-name").textContent = name;
  byId("roi-swatch").className = `roi-swatch ${activeRoi}`;
  byId("roi-value").innerHTML = `x ${Math.round(roi.x * 100)}% &nbsp; y ${Math.round(roi.y * 100)}% &nbsp; w ${Math.round(roi.width * 100)}% &nbsp; h ${Math.round(roi.height * 100)}%`;
}

function selectRoi(name) {
  activeRoi = name;
  document.querySelectorAll("[data-roi-mode]").forEach((button) => {
    const selected = button.dataset.roiMode === name;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", selected ? "true" : "false");
  });
  updateRoiReadout();
  drawCapture();
}

function roiPointerDown(event) {
  const point = normalizedCapturePoint(event);
  if (point === null) return;
  roiPointer = event.pointerId;
  roiStart = point;
  roiDraft = { x: point.x, y: point.y, width: 0, height: 0 };
  captureCanvas.setPointerCapture(event.pointerId);
  drawCapture();
}

function roiPointerMove(event) {
  if (event.pointerId !== roiPointer) return;
  const point = normalizedCapturePoint(event);
  roiDraft = roiFromPoints(roiStart, point);
  drawCapture();
}

function roiPointerEnd(event) {
  if (event.pointerId !== roiPointer) return;
  const point = normalizedCapturePoint(event);
  const selected = roiFromPoints(roiStart, point);
  if (selected.width >= .01 && selected.height >= .01) rois[activeRoi] = selected;
  roiPointer = null;
  roiStart = null;
  roiDraft = null;
  updateRoiReadout();
  drawCapture();
  if (activeRoi === "exposure") analyzeExposure();
}

function roiPointerCancel(event) {
  if (event.pointerId !== roiPointer) return;
  roiPointer = null;
  roiStart = null;
  roiDraft = null;
  drawCapture();
}

function scanAxis(start, stop, step) {
  const span = stop - start;
  if (span <= 1e-9) return [start];
  const intervals = Math.max(1, Math.ceil(span / step));
  const actualStep = span / intervals;
  return Array.from(
    { length: intervals + 1 },
    (_value, index) => index === intervals ? stop : start + index * actualStep,
  );
}

function localScanPlan() {
  const xMin = byId("scan-x-min").valueAsNumber;
  const xMax = byId("scan-x-max").valueAsNumber;
  const yMin = byId("scan-y-min").valueAsNumber;
  const yMax = byId("scan-y-max").valueAsNumber;
  const frameWidth = byId("scan-frame-width").valueAsNumber;
  const frameHeight = byId("scan-frame-height").valueAsNumber;
  const overlap = byId("scan-overlap").valueAsNumber;
  const values = [xMin, xMax, yMin, yMax, frameWidth, frameHeight, overlap];
  const valid = values.every(Number.isFinite)
    && frameWidth > 0
    && frameHeight > 0
    && xMin >= 0
    && xMax <= BED_MAX_X
    && yMin >= 0
    && yMax <= BED_MAX_Y
    && xMax - xMin >= frameWidth
    && yMax - yMin >= frameHeight
    && overlap > 0
    && overlap < 90;
  if (!valid) {
    return {
      points: [],
      completed: 0,
      currentIndex: null,
      phase: "Adjust scan settings",
      xMin,
      xMax,
      yMin,
      yMax,
      frameWidth,
      frameHeight,
    };
  }
  const xs = scanAxis(
    xMin + frameWidth / 2,
    xMax - frameWidth / 2,
    frameWidth * (1 - overlap / 100),
  );
  const ys = scanAxis(
    yMin + frameHeight / 2,
    yMax - frameHeight / 2,
    frameHeight * (1 - overlap / 100),
  );
  const points = [];
  ys.forEach((y, row) => {
    const columns = xs.map((x, col) => ({ x, y, row, col }));
    points.push(...(row % 2 === 0 ? columns : columns.reverse()));
  });
  return {
    points,
    completed: 0,
    currentIndex: null,
    phase: "Planned",
    xMin,
    xMax,
    yMin,
    yMax,
    frameWidth,
    frameHeight,
  };
}

function displayedScanPlan() {
  const local = localScanPlan();
  if (snapshot === null || !["scanning", "stopping"].includes(snapshot.state)) {
    return { ...local, active: false, stepProgress: null };
  }
  const geometry = snapshot.scan_progress;
  return {
    ...local,
    points: geometry === null ? local.points : geometry.points,
    completed: geometry === null ? 0 : geometry.completed,
    currentIndex: geometry === null ? null : geometry.current_index,
    active: true,
    stepProgress: snapshot.step_progress,
  };
}

function formatEta(seconds) {
  if (seconds === null) return "unavailable";
  const total = Math.max(0, Math.ceil(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total % 3600 / 60);
  const remainder = total % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${remainder}s`;
  return `${remainder}s`;
}

function formatStepProgress(progress) {
  if (progress === null) return "No active step · 0 units · ETA unavailable";
  const count = progress.total === null
    ? `${progress.completed} ${progress.unit}`
    : `${progress.completed}/${progress.total} ${progress.unit}`;
  return `${progress.label} · ${count} · ETA ${formatEta(progress.eta_seconds)}`;
}

function renderOperationProgress(progress) {
  const line = formatStepProgress(progress);
  const bar = byId("operation-progress");
  byId("step-progress-line").textContent = line;
  byId("step-progress-line").title = line;
  bar.max = 1;
  if (progress === null) {
    bar.value = 0;
  } else if (progress.total === null) {
    bar.removeAttribute("value");
  } else {
    bar.value = progress.total === 0 ? 1 : progress.completed / progress.total;
  }
}

function renderScanPlanReadout(plan, mapLabel) {
  const total = plan.points.length;
  const completed = Math.max(0, Math.min(total, plan.completed));
  const text = plan.active
    ? `${total} tiles · ${completed}/${total} captured · ${formatStepProgress(plan.stepProgress)}`
    : `${total} tiles · ${plan.phase}`;
  byId("scan-plan-readout").textContent = text;
  bedCanvas.setAttribute("aria-label", `${mapLabel}. Scan plan: ${text}.`);
}

function drawRoute(points, left, top, size, color, width, dash = []) {
  if (points.length === 0) return;
  bedContext.strokeStyle = color;
  bedContext.lineWidth = width;
  bedContext.setLineDash(dash);
  bedContext.beginPath();
  points.forEach((point, index) => {
    const canvasPoint = bedToCanvas(point.x, point.y, left, top, size);
    if (index === 0) bedContext.moveTo(canvasPoint.x, canvasPoint.y);
    else bedContext.lineTo(canvasPoint.x, canvasPoint.y);
  });
  bedContext.stroke();
  bedContext.setLineDash([]);
}

function drawScanPlan(plan, left, top, size) {
  if (plan.points.length === 0) return;
  const completed = Math.max(0, Math.min(plan.points.length, plan.completed));
  const coverageLowerLeft = bedToCanvas(plan.xMin, plan.yMin, left, top, size);
  const coverageUpperRight = bedToCanvas(plan.xMax, plan.yMax, left, top, size);
  bedContext.strokeStyle = "rgba(46, 109, 154, .8)";
  bedContext.lineWidth = 1.5;
  bedContext.strokeRect(
    coverageLowerLeft.x,
    coverageUpperRight.y,
    coverageUpperRight.x - coverageLowerLeft.x,
    coverageLowerLeft.y - coverageUpperRight.y,
  );

  plan.points.forEach((point, index) => {
    const lowerLeft = bedToCanvas(
      point.x - plan.frameWidth / 2,
      point.y - plan.frameHeight / 2,
      left,
      top,
      size,
    );
    const upperRight = bedToCanvas(
      point.x + plan.frameWidth / 2,
      point.y + plan.frameHeight / 2,
      left,
      top,
      size,
    );
    const current = index === plan.currentIndex;
    const captured = index < completed;
    bedContext.fillStyle = current
      ? "rgba(155, 103, 24, .25)"
      : captured
        ? "rgba(23, 106, 85, .18)"
        : "rgba(46, 109, 154, .035)";
    bedContext.strokeStyle = current
      ? "#9b6718"
      : captured
        ? "rgba(23, 106, 85, .7)"
        : "rgba(46, 109, 154, .32)";
    bedContext.lineWidth = current ? 2.5 : 1;
    bedContext.fillRect(
      lowerLeft.x,
      upperRight.y,
      upperRight.x - lowerLeft.x,
      lowerLeft.y - upperRight.y,
    );
    bedContext.strokeRect(
      lowerLeft.x,
      upperRight.y,
      upperRight.x - lowerLeft.x,
      lowerLeft.y - upperRight.y,
    );
  });

  drawRoute(plan.points, left, top, size, "rgba(63, 78, 87, .6)", 1.2, [4, 4]);
  drawRoute(plan.points.slice(0, completed), left, top, size, "#176a55", 2);
  if (plan.currentIndex !== null) {
    const current = plan.points[plan.currentIndex];
    const previous = completed > 0 ? plan.points[completed - 1] : null;
    if (previous !== null && previous !== current) {
      drawRoute([previous, current], left, top, size, "#9b6718", 2);
    }
    const point = bedToCanvas(current.x, current.y, left, top, size);
    bedContext.fillStyle = "#9b6718";
    bedContext.beginPath();
    bedContext.arc(point.x, point.y, 5, 0, Math.PI * 2);
    bedContext.fill();
    bedContext.strokeStyle = "#fff";
    bedContext.lineWidth = 1.5;
    bedContext.stroke();
  }
}

function drawBed() {
  const focusSurface = activeFocusSurface();
  const mesh = focusSurface === null ? null : focusSurface.mesh;
  const scanPlan = displayedScanPlan();
  const mapLabel = renderMeshLegend(focusSurface);
  renderScanPlanReadout(scanPlan, mapLabel);
  fitCanvas(bedCanvas, bedContext, (width, height) => {
    const padding = 29;
    const size = Math.max(1, Math.min(width, height) - padding * 2);
    const left = (width - size) / 2;
    const top = (height - size) / 2;
    bedContext.clearRect(0, 0, width, height);
    bedContext.fillStyle = "#f8faf9";
    bedContext.fillRect(0, 0, width, height);
    if (mesh !== null) drawFocusMesh(mesh, left, top, size);
    bedContext.strokeStyle = "#dce1e1";
    bedContext.lineWidth = 1;
    for (let step = 0; step <= 220; step += 20) {
      const offset = step / 220 * size;
      bedContext.beginPath();
      bedContext.moveTo(left + offset, top);
      bedContext.lineTo(left + offset, top + size);
      bedContext.moveTo(left, top + size - offset);
      bedContext.lineTo(left + size, top + size - offset);
      bedContext.stroke();
    }
    bedContext.strokeStyle = "#7f8b90";
    bedContext.lineWidth = 1.5;
    bedContext.strokeRect(left, top, size, size);
    drawScanPlan(scanPlan, left, top, size);
    bedContext.fillStyle = "#758087";
    bedContext.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
    bedContext.fillText("0", left - 3, top + size + 17);
    bedContext.fillText("220 X", left + size - 30, top + size + 17);
    bedContext.fillText("Y 220", left - 27, top - 8);

    const target = bedToCanvas(numberValue("move-x"), numberValue("move-y"), left, top, size);
    bedContext.strokeStyle = "#ae302e";
    bedContext.lineWidth = 2;
    bedContext.beginPath();
    bedContext.moveTo(target.x - 7, target.y);
    bedContext.lineTo(target.x + 7, target.y);
    bedContext.moveTo(target.x, target.y - 7);
    bedContext.lineTo(target.x, target.y + 7);
    bedContext.stroke();

    if (snapshot !== null && snapshot.printer.position !== null) {
      const position = snapshot.printer.position;
      const current = bedToCanvas(position.x, position.y, left, top, size);
      bedContext.fillStyle = "#176a55";
      bedContext.beginPath();
      bedContext.arc(current.x, current.y, 5, 0, Math.PI * 2);
      bedContext.fill();
      bedContext.strokeStyle = "#fff";
      bedContext.lineWidth = 1.5;
      bedContext.stroke();
    }
    if (focusSurface !== null) drawFocusMeasurements(focusSurface.measurements, focusSurface.mesh, left, top, size);
  });
}

function focusMeshZAt(mesh, x, y) {
  const xSpan = mesh.x_max - mesh.x_min;
  const ySpan = mesh.y_max - mesh.y_min;
  const tx = (x - (mesh.x_min + xSpan * .25)) / (xSpan * .5);
  const ty = (y - (mesh.y_min + ySpan * .25)) / (ySpan * .5);
  return (1 - tx) * (1 - ty) * mesh.z00
    + tx * (1 - ty) * mesh.z10
    + (1 - tx) * ty * mesh.z01
    + tx * ty * mesh.z11;
}

function focusMeshRange(mesh) {
  const corners = [
    focusMeshZAt(mesh, mesh.x_min, mesh.y_min),
    focusMeshZAt(mesh, mesh.x_max, mesh.y_min),
    focusMeshZAt(mesh, mesh.x_min, mesh.y_max),
    focusMeshZAt(mesh, mesh.x_max, mesh.y_max),
  ];
  return { minimum: Math.min(...corners), maximum: Math.max(...corners) };
}

function meshColor(position) {
  const stops = [[52, 106, 137], [226, 212, 151], [188, 82, 67]];
  const segment = position < .5 ? 0 : 1;
  const fraction = position < .5 ? position * 2 : (position - .5) * 2;
  const color = stops[segment].map((value, index) => Math.round(value + (stops[segment + 1][index] - value) * fraction));
  return `rgba(${color[0]}, ${color[1]}, ${color[2]}, .68)`;
}

function drawFocusMesh(mesh, left, top, size) {
  const range = focusMeshRange(mesh);
  const span = range.maximum - range.minimum;
  const cells = 32;
  for (let yIndex = 0; yIndex < cells; yIndex += 1) {
    const y0 = mesh.y_min + (mesh.y_max - mesh.y_min) * yIndex / cells;
    const y1 = mesh.y_min + (mesh.y_max - mesh.y_min) * (yIndex + 1) / cells;
    for (let xIndex = 0; xIndex < cells; xIndex += 1) {
      const x0 = mesh.x_min + (mesh.x_max - mesh.x_min) * xIndex / cells;
      const x1 = mesh.x_min + (mesh.x_max - mesh.x_min) * (xIndex + 1) / cells;
      const z = focusMeshZAt(mesh, (x0 + x1) / 2, (y0 + y1) / 2);
      const normalized = span === 0 ? .5 : (z - range.minimum) / span;
      const band = Math.round(normalized * 8) / 8;
      const lowerLeft = bedToCanvas(x0, y0, left, top, size);
      const upperRight = bedToCanvas(x1, y1, left, top, size);
      bedContext.fillStyle = meshColor(band);
      bedContext.fillRect(lowerLeft.x, upperRight.y, upperRight.x - lowerLeft.x + .5, lowerLeft.y - upperRight.y + .5);
    }
  }
  const lowerLeft = bedToCanvas(mesh.x_min, mesh.y_min, left, top, size);
  const upperRight = bedToCanvas(mesh.x_max, mesh.y_max, left, top, size);
  bedContext.strokeStyle = "#38484f";
  bedContext.lineWidth = 1.5;
  bedContext.strokeRect(lowerLeft.x, upperRight.y, upperRight.x - lowerLeft.x, lowerLeft.y - upperRight.y);
}

function drawFocusMeasurements(measurements, mesh, left, top, size) {
  const centerX = (mesh.x_min + mesh.x_max) / 2;
  const centerY = (mesh.y_min + mesh.y_max) / 2;
  bedContext.font = "700 9px ui-monospace, SFMono-Regular, Menlo, monospace";
  bedContext.lineWidth = 3;
  bedContext.textBaseline = "middle";
  measurements.forEach((measurement) => {
    const point = bedToCanvas(measurement.x, measurement.y, left, top, size);
    const alignment = measurement.x < centerX ? "left" : measurement.x > centerX ? "right" : "center";
    const xOffset = alignment === "left" ? 7 : alignment === "right" ? -7 : 0;
    const yOffset = measurement.y < centerY ? 10 : -10;
    const name = measurement.name.split("_").map((part) => part[0].toUpperCase()).join("");
    bedContext.fillStyle = "#fff";
    bedContext.beginPath();
    bedContext.arc(point.x, point.y, 4, 0, Math.PI * 2);
    bedContext.fill();
    bedContext.strokeStyle = "#26343a";
    bedContext.lineWidth = 2;
    bedContext.stroke();
    bedContext.textAlign = alignment;
    const label = `${name} Z ${measurement.z.toFixed(3)}`;
    bedContext.lineWidth = 3;
    bedContext.strokeStyle = "rgba(255, 255, 255, .9)";
    bedContext.strokeText(label, point.x + xOffset, point.y + yOffset);
    bedContext.fillStyle = "#26343a";
    bedContext.fillText(label, point.x + xOffset, point.y + yOffset);
  });
}

function focusMethodLabel(method) {
  if (method === "flat") return "Flat";
  if (method === "grid") return "Grid";
  throw new TypeError(`Unknown focus method: ${method}`);
}

function renderMeshLegend(focusSurface) {
  const mesh = focusSurface === null ? null : focusSurface.mesh;
  const legend = byId("mesh-legend");
  byId("move-z").readOnly = mesh !== null;
  byId("move-z-label").textContent = mesh === null ? "Z (mm)" : "Z mesh (mm)";
  legend.hidden = mesh === null;
  if (mesh === null) {
    return "Printer bed target map, zero to 220 millimeters on X and Y";
  }
  const range = focusMeshRange(mesh);
  byId("mesh-range").textContent = `Z ${range.minimum.toFixed(3)}-${range.maximum.toFixed(3)} mm`;
  const method = focusMethodLabel(focusSurface.method);
  const observations = focusSurface.measurements.map((measurement) => `${measurement.name} X ${measurement.x.toFixed(2)}, Y ${measurement.y.toFixed(2)}, Z ${measurement.z.toFixed(3)}`).join("; ");
  return `Printer bed target map with ${method.toLowerCase()} focus surface from Z ${range.minimum.toFixed(3)} to ${range.maximum.toFixed(3)} millimeters. Measured focus points: ${observations}`;
}

function activeFocusSurface() {
  return snapshot === null ? null : snapshot.focus_grid;
}

function activeFocusMesh() {
  const focusSurface = activeFocusSurface();
  return focusSurface === null ? null : focusSurface.mesh;
}

function constrainMoveTarget(mesh) {
  const bounds = mesh === null
    ? [[byId("move-x"), 0, BED_MAX_X], [byId("move-y"), 0, BED_MAX_Y]]
    : [[byId("move-x"), mesh.x_min, mesh.x_max], [byId("move-y"), mesh.y_min, mesh.y_max]];
  bounds.forEach(([input, minimum, maximum]) => {
    input.min = String(minimum);
    input.max = String(maximum);
    if (Number.isFinite(input.valueAsNumber)) {
      input.value = Math.max(minimum, Math.min(maximum, input.valueAsNumber)).toFixed(2);
    }
  });
}

function syncMoveZToMesh(mesh) {
  const x = byId("move-x").valueAsNumber;
  const y = byId("move-y").valueAsNumber;
  if (mesh !== null && Number.isFinite(x) && Number.isFinite(y)) {
    byId("move-z").value = focusMeshZAt(mesh, x, y).toFixed(3);
  }
}

function syncTargetMesh(mesh) {
  const signature = mesh === null ? null : JSON.stringify(mesh);
  if (signature !== targetMeshSignature) {
    targetMeshSignature = signature;
    constrainMoveTarget(mesh);
    syncMoveZToMesh(mesh);
  }
}

function updateBedTarget() {
  const mesh = activeFocusMesh();
  constrainMoveTarget(mesh);
  syncMoveZToMesh(mesh);
  drawBed();
}

function bedGeometry() {
  const rect = bedCanvas.getBoundingClientRect();
  const padding = 29;
  const size = Math.max(1, Math.min(rect.width, rect.height) - padding * 2);
  return { rect, left: (rect.width - size) / 2, top: (rect.height - size) / 2, size };
}

function bedToCanvas(x, y, left, top, size) {
  const safeX = Math.max(0, Math.min(BED_MAX_X, x));
  const safeY = Math.max(0, Math.min(BED_MAX_Y, y));
  return { x: left + safeX / BED_MAX_X * size, y: top + size - safeY / BED_MAX_Y * size };
}

function selectBedTarget(event) {
  const geometry = bedGeometry();
  const xPixel = Math.max(geometry.left, Math.min(geometry.left + geometry.size, event.clientX - geometry.rect.left));
  const yPixel = Math.max(geometry.top, Math.min(geometry.top + geometry.size, event.clientY - geometry.rect.top));
  byId("move-x").value = ((xPixel - geometry.left) / geometry.size * BED_MAX_X).toFixed(2);
  byId("move-y").value = ((geometry.top + geometry.size - yPixel) / geometry.size * BED_MAX_Y).toFixed(2);
  bedTargetInitialized = true;
  updateBedTarget();
  if (byId("move-on-click").checked && !byId("move-submit").disabled) submitMove();
}

function movePayload() {
  return {
    x: numberValue("move-x"),
    y: numberValue("move-y"),
    z: numberValue("move-z"),
  };
}

function submitMove() {
  if (!byId("move-form").reportValidity()) return;
  action(byId("move-submit"), "/api/printer/move", movePayload());
}

let jogSocket = null;
let jogOpening = null;
let jogVector = [0, 0, 0];
let jogTimer = null;
let jogToken = 0;
const heldJogKeys = new Set();

function ensureJogSocket() {
  if (jogSocket !== null && jogSocket.readyState === WebSocket.OPEN) return Promise.resolve();
  if (jogOpening !== null) return jogOpening;
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  jogSocket = new WebSocket(`${protocol}//${window.location.host}/ws/jog`);
  jogOpening = new Promise((resolve, reject) => {
    jogSocket.onopen = () => {
      jogOpening = null;
      resolve();
    };
    jogSocket.onerror = () => {
      jogOpening = null;
      reject(new Error("Realtime jog connection failed"));
    };
  });
  jogSocket.onclose = () => {
    jogOpening = null;
    if (jogVector.some((value) => value !== 0)) {
      clearJogUi();
      showError(new Error("Realtime jog connection closed"));
    }
  };
  return jogOpening;
}

function sendJog() {
  if (jogSocket === null || jogSocket.readyState !== WebSocket.OPEN) return;
  jogSocket.send(JSON.stringify({
    dx: jogVector[0],
    dy: jogVector[1],
    dz: jogVector[2],
    speed_xy_mm_s: numberValue("jog-speed-xy"),
    speed_z_mm_s: numberValue("jog-speed-z"),
  }));
}

function clearJogUi() {
  jogToken += 1;
  if (jogTimer !== null) window.clearInterval(jogTimer);
  jogTimer = null;
  jogVector = [0, 0, 0];
  heldJogKeys.clear();
  document.querySelectorAll(".jog-button.active").forEach((button) => button.classList.remove("active"));
}

function setJog(vector, activeButtons = []) {
  const token = ++jogToken;
  if (jogTimer !== null) window.clearInterval(jogTimer);
  jogTimer = null;
  jogVector = vector;
  document.querySelectorAll(".jog-button.active").forEach((button) => button.classList.remove("active"));
  activeButtons.forEach((button) => button.classList.add("active"));
  if (vector.every((value) => value === 0)) {
    sendJog();
    return;
  }
  ensureJogSocket()
    .then(() => {
      if (token !== jogToken) return;
      sendJog();
      jogTimer = window.setInterval(sendJog, 200);
    })
    .catch(showError);
}

function stopJog() {
  if (jogVector.some((value) => value !== 0)) {
    jogVector = [0, 0, 0];
    sendJog();
  }
  clearJogUi();
}

const jogKeyVectors = {
  ArrowUp: [0, 1, 0],
  w: [0, 1, 0],
  ArrowDown: [0, -1, 0],
  s: [0, -1, 0],
  ArrowLeft: [-1, 0, 0],
  a: [-1, 0, 0],
  ArrowRight: [1, 0, 0],
  d: [1, 0, 0],
};

function isFormControl(target) {
  return target.matches("input, select, textarea, button");
}

function updateKeyboardJog() {
  const vector = [0, 0, 0];
  heldJogKeys.forEach((key) => {
    const contribution = jogKeyVectors[key];
    vector[0] += contribution[0];
    vector[1] += contribution[1];
  });
  vector[0] = Math.max(-1, Math.min(1, vector[0]));
  vector[1] = Math.max(-1, Math.min(1, vector[1]));
  const buttons = Array.from(document.querySelectorAll("[data-jog]")).filter((button) => {
    const candidate = button.dataset.jog.split(",").map(Number);
    return candidate[0] === vector[0] && candidate[1] === vector[1] && candidate[2] === vector[2];
  });
  setJog(vector, buttons);
}

function showWorkflowPanel(panelId) {
  document.querySelectorAll(".workflow-tab").forEach((tab) => {
    const active = tab.dataset.panel === panelId;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll(".workflow-content").forEach((panel) => { panel.hidden = panel.id !== panelId; });
}

function focusGridPayload() {
  return {
    ...coverageBoundsPayload(),
    focus_roi: { ...rois.focus },
    output_dir: textValue("cal-output"),
    speed_xy_mm_s: numberValue("cal-speed-xy"),
    speed_z_mm_s: numberValue("cal-speed-z"),
  };
}

function calibrationPayload() {
  return {
    ...focusGridPayload(),
    exposure_roi: { ...rois.exposure },
    gray_roi: { ...rois.gray },
  };
}

function scanPayload() {
  return {
    x_min: numberValue("scan-x-min"),
    x_max: numberValue("scan-x-max"),
    y_min: numberValue("scan-y-min"),
    y_max: numberValue("scan-y-max"),
    frame_width_mm: numberValue("scan-frame-width"),
    frame_height_mm: numberValue("scan-frame-height"),
    overlap_percent: numberValue("scan-overlap"),
    output_dir: textValue("scan-output"),
    speed_xy_mm_s: numberValue("scan-speed-xy"),
    speed_z_mm_s: numberValue("scan-speed-z"),
    settle_ms: numberValue("scan-settle"),
    quick_acquisition: byId("scan-quick-acquisition").checked,
  };
}

function coverageBoundsPayload() {
  return {
    x_min: numberValue("cal-x-min"),
    x_max: numberValue("cal-x-max"),
    y_min: numberValue("cal-y-min"),
    y_max: numberValue("cal-y-max"),
  };
}

function setCoverageBound(name, value) {
  document.querySelectorAll(`[data-coverage-bound="${name}"]`).forEach((input) => { input.value = value; });
  drawBed();
}

byId("dismiss-notice").addEventListener("click", clearNotice);
byId("refresh-ports").addEventListener("click", loadPorts);
byId("printer-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!event.currentTarget.reportValidity()) return;
  action(byId("printer-connect"), "/api/printer/connect", { port: textValue("printer-port"), baud: numberValue("printer-baud"), eol: textValue("printer-eol") });
});
byId("printer-disconnect").addEventListener("click", () => action(byId("printer-disconnect"), "/api/printer/disconnect"));
byId("printer-home").addEventListener("click", () => {
  if (window.confirm("Home all printer axes, then move to X 110, Y 110, Z 203? Make sure the full path is clear.")) action(byId("printer-home"), "/api/printer/home");
});
byId("printer-origin").addEventListener("click", () => {
  if (window.confirm("Set the current X, Y, and Z position as zero?")) action(byId("printer-origin"), "/api/printer/origin");
});
byId("printer-restore").addEventListener("click", () => {
  const position = snapshot.printer.remembered_position;
  if (window.confirm(`Restore ${formatPosition(position)} without moving the stage? Continue only if the machine has not physically moved since this position was recorded.`)) {
    action(byId("printer-restore"), "/api/printer/restore-position");
  }
});
byId("camera-control").addEventListener("click", () => action(byId("camera-control"), "/api/camera/take-control"));
byId("camera-connect").addEventListener("click", () => action(byId("camera-connect"), "/api/camera/connect"));
byId("camera-disconnect").addEventListener("click", () => action(byId("camera-disconnect"), "/api/camera/disconnect"));
byId("camera-iso").addEventListener("change", () => action(byId("camera-iso"), "/api/camera/settings", { iso: textValue("camera-iso") }));
byId("test-shutter").addEventListener("change", () => action(byId("test-shutter"), "/api/camera/shutter", { shutter: textValue("test-shutter") }));
byId("camera-test-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!event.currentTarget.reportValidity()) return;
  latestPath = null;
  action(byId("camera-test"), "/api/camera/test", { output_dir: textValue("test-output") });
});
byId("move-form").addEventListener("submit", (event) => { event.preventDefault(); submitMove(); });
byId("stop-operation").addEventListener("click", () => action(byId("stop-operation"), "/api/stop"));
byId("emergency-stop").addEventListener("click", () => action(byId("emergency-stop"), "/api/emergency-stop"));

document.querySelectorAll(".workspace-tab").forEach((tab) => {
  tab.addEventListener("click", () => showApplicationWorkspace(tab.dataset.workspace));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const next = tab.dataset.workspace === "capture" ? "editor" : "capture";
    showApplicationWorkspace(next);
    byId(`${next}-workspace-tab`).focus();
  });
});
byId("editor-refresh-projects").addEventListener("click", () => {
  loadEditorProjects(editorProject === null ? null : editorProject.directory).catch(showError);
});
byId("editor-project").addEventListener("change", () => {
  loadEditorProject(textValue("editor-project")).catch(showError);
});
document.querySelectorAll("[data-editor-source]").forEach((button) => {
  button.addEventListener("click", () => setEditorSource(button.dataset.editorSource));
});
document.querySelectorAll("[data-editor-material]").forEach((button) => {
  button.addEventListener("click", () => setEditorMaterial(button.dataset.editorMaterial));
});
document.querySelectorAll(".editor-control-tab").forEach((tab) => {
  tab.addEventListener("click", () => showEditorControlPanel(tab.dataset.editorControls));
});
document.querySelectorAll("[data-editor-field]").forEach((input) => {
  input.addEventListener("input", () => {
    syncEditorControl(input);
    byId("editor-preview-state").textContent = "Changes not previewed";
  });
});
byId("editor-tile").addEventListener("change", previewEditor);
byId("editor-reset").addEventListener("click", resetEditorRecipe);
byId("editor-preview-button").addEventListener("click", previewEditor);
byId("editor-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const recipe = editorRecipe();
  if (!validEditorRecipe(recipe)) return;
  action(byId("editor-apply"), "/api/editor/apply", {
    project_dir: editorProject.directory,
    recipe,
  });
});

document.querySelectorAll("[data-roi-mode]").forEach((button) => button.addEventListener("click", () => selectRoi(button.dataset.roiMode)));
byId("reset-roi").addEventListener("click", () => {
  rois[activeRoi] = { ...DEFAULT_ROI };
  updateRoiReadout();
  drawCapture();
  analyzeExposure();
});
captureCanvas.addEventListener("pointerdown", roiPointerDown);
captureCanvas.addEventListener("pointermove", roiPointerMove);
captureCanvas.addEventListener("pointerup", roiPointerEnd);
captureCanvas.addEventListener("pointercancel", roiPointerCancel);

bedCanvas.addEventListener("pointerup", selectBedTarget);
bedCanvas.addEventListener("keydown", (event) => {
  const deltas = { ArrowUp: [0, 1], ArrowDown: [0, -1], ArrowLeft: [-1, 0], ArrowRight: [1, 0] };
  if (!(event.key in deltas)) return;
  event.preventDefault();
  const [dx, dy] = deltas[event.key];
  byId("move-x").value = Math.max(0, Math.min(BED_MAX_X, numberValue("move-x") + dx)).toFixed(2);
  byId("move-y").value = Math.max(0, Math.min(BED_MAX_Y, numberValue("move-y") + dy)).toFixed(2);
  updateBedTarget();
});
["move-x", "move-y"].forEach((id) => byId(id).addEventListener("input", updateBedTarget));
document.querySelectorAll("#scan-form input:not([data-coverage-bound])").forEach((input) => input.addEventListener("input", () => {
  drawBed();
  renderEstimatedDpi();
}));
document.querySelectorAll("[data-coverage-bound]").forEach((input) => {
  input.addEventListener("input", () => setCoverageBound(input.dataset.coverageBound, input.value));
});
document.querySelectorAll("[data-current-bound]").forEach((button) => {
  button.addEventListener("click", () => {
    const position = snapshot.printer.position;
    setCoverageBound(button.dataset.currentBound, position[button.dataset.axis].toFixed(2));
  });
});

for (const axis of ["xy", "z"]) {
  byId(`jog-speed-${axis}`).addEventListener("input", () => {
    byId(`jog-speed-${axis}-value`).textContent = `${numberValue(`jog-speed-${axis}`)} mm/s`;
  });
}
document.querySelectorAll("[data-jog]").forEach((button) => {
  button.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    button.setPointerCapture(event.pointerId);
    setJog(button.dataset.jog.split(",").map(Number), [button]);
  });
  button.addEventListener("pointerup", stopJog);
  button.addEventListener("pointercancel", stopJog);
  button.addEventListener("lostpointercapture", stopJog);
});
byId("stop-jog").addEventListener("click", stopJog);
window.addEventListener("keydown", (event) => {
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  if (!(key in jogKeyVectors) || isFormControl(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
  event.preventDefault();
  if (byId("stop-jog").disabled) return;
  heldJogKeys.add(key);
  updateKeyboardJog();
});
window.addEventListener("keyup", (event) => {
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  if (!heldJogKeys.has(key)) return;
  event.preventDefault();
  heldJogKeys.delete(key);
  updateKeyboardJog();
});
window.addEventListener("blur", stopJog);
window.addEventListener("pagehide", stopJog);

document.querySelectorAll(".workflow-tab").forEach((tab) => tab.addEventListener("click", () => showWorkflowPanel(tab.dataset.panel)));
byId("auto-exposure").addEventListener("click", () => {
  if (!byId("cal-output").reportValidity()) return;
  action(byId("auto-exposure"), "/api/calibration/exposure", {
    exposure_roi: { ...rois.exposure },
    output_dir: textValue("cal-output"),
  });
});
byId("auto-focus").addEventListener("click", () => {
  if (!byId("calibration-form").reportValidity()) return;
  action(byId("auto-focus"), "/api/calibration/focus", {
    ...coverageBoundsPayload(),
    focus_roi: { ...rois.focus },
    output_dir: textValue("cal-output"),
    speed_z_mm_s: numberValue("cal-speed-z"),
  });
});
byId("focus-grid").addEventListener("click", () => {
  if (!byId("calibration-form").reportValidity()) return;
  action(byId("focus-grid"), "/api/calibration/focus-grid", focusGridPayload());
});
byId("gray-white-balance").addEventListener("click", () => {
  if (!byId("cal-output").reportValidity()) return;
  action(byId("gray-white-balance"), "/api/calibration/white-balance", {
    gray_roi: { ...rois.gray },
    output_dir: textValue("cal-output"),
  });
});
byId("calibration-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!event.currentTarget.reportValidity()) return;
  action(byId("start-calibration"), "/api/calibration/start", calibrationPayload());
});
byId("scan-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!event.currentTarget.reportValidity()) return;
  action(byId("start-scan"), "/api/scan/start", scanPayload());
});

new ResizeObserver(drawCapture).observe(captureCanvas);
new ResizeObserver(drawCenterLoupe).observe(loupeCanvas);
new ResizeObserver(drawBed).observe(bedCanvas);
updateRoiReadout();
drawCapture();
drawCenterLoupe();
drawExposureAnalysis();
drawBed();
renderEstimatedDpi();
resetEditorRecipe();
loadPorts();
pollStatus();
