#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import deque
import json
import logging
import math
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlsplit

from v3se_printer.uvc import UvcCameraConfig, apply_uvc_config, get_capture_info, probe_uvc_indices


UHD_WIDTH = 3840
UHD_HEIGHT = 2160
FHD_WIDTH = 1920
FHD_HEIGHT = 1080
BOUNDARY = "frame"
STALE_AFTER_SECONDS = 5.0
LOGGER = logging.getLogger("camera_server")
DEFAULT_PROFILE = "detail"
PREVIEW_PROFILES = {
    "smooth": (FHD_WIDTH, FHD_HEIGHT, 15),
    "detail": (UHD_WIDTH, UHD_HEIGHT, 15),
}
WHITE_BALANCE_INTERVAL_SECONDS = 0.5
WHITE_BALANCE_MAX_SAMPLES = 4096
WHITE_BALANCE_UNSET = object()

INDEX_HTML = b"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MarlinScan Camera</title>
  <style>
    :root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    html, body { width: 100%; height: 100%; margin: 0; }
    body { height: 100dvh; display: grid; grid-template-rows: auto auto minmax(0, 1fr); overflow: hidden; background: #090a0b; color: #17191b; letter-spacing: 0; }
    header { min-height: 62px; display: flex; align-items: center; gap: 16px; padding: 9px 14px; background: #f4f5f2; border-bottom: 1px solid #c9ccc7; }
    strong { flex: 0 0 auto; font-size: 15px; font-weight: 700; white-space: nowrap; }
    .controls { min-width: 0; display: flex; align-items: center; gap: 8px; }
    label { color: #525854; font-size: 12px; font-weight: 700; }
    select, button { min-height: 38px; border: 1px solid #b8bcb7; border-radius: 5px; background: #fff; color: #17191b; font: 600 13px/1 system-ui, sans-serif; letter-spacing: 0; }
    select { width: min(330px, 28vw); padding: 0 34px 0 10px; text-overflow: ellipsis; }
    select.profile { width: 180px; }
    button { padding: 0 13px; cursor: pointer; }
    button.primary { border-color: #202522; background: #202522; color: #fff; }
    button:hover:not(:disabled) { border-color: #737a75; background: #e9ebe7; }
    button.primary:hover:not(:disabled) { border-color: #303b35; background: #303b35; }
    button:disabled, select:disabled { cursor: not-allowed; opacity: .52; }
    button:focus-visible, input:focus-visible, select:focus-visible { outline: 3px solid #3d83c5; outline-offset: 2px; }
    .telemetry { margin-left: auto; display: flex; align-items: center; gap: 14px; }
    .metric { color: #525854; font: 600 12px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: nowrap; }
    .status { display: inline-flex; align-items: center; gap: 7px; color: #17623b; }
    .status::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: #1d9a59; box-shadow: 0 0 0 3px #d8ecdf; }
    .status.busy { color: #74540c; }
    .status.busy::before { background: #c38a13; box-shadow: 0 0 0 3px #efe3c4; }
    .status.error { color: #9d3029; }
    .status.error::before { background: #c73d34; box-shadow: 0 0 0 3px #f0d9d7; }
    .color-controls { min-height: 48px; display: flex; align-items: center; justify-content: center; gap: 18px; padding: 7px 14px; background: #e6e8e4; border-bottom: 1px solid #afb4ae; }
    .gain { display: grid; grid-template-columns: auto 120px 38px; align-items: center; gap: 7px; }
    .gain input { width: 120px; accent-color: #525854; }
    .gain.red input { accent-color: #ad443d; }
    .gain.green input { accent-color: #278355; }
    .gain.blue input { accent-color: #3d6faa; }
    output { color: #525854; font: 600 11px/1 ui-monospace, SFMono-Regular, Menlo, monospace; }
    .color-actions { display: flex; align-items: center; gap: 7px; }
    .auto-toggle { min-height: 38px; display: inline-flex; align-items: center; gap: 7px; padding: 0 9px; border: 1px solid #b8bcb7; border-radius: 5px; background: #fff; color: #17191b; white-space: nowrap; }
    .auto-toggle input { width: 16px; height: 16px; margin: 0; accent-color: #278355; }
    main { position: relative; min-width: 0; min-height: 0; display: grid; place-items: center; overflow: hidden; }
    img { display: block; width: 100%; height: 100%; object-fit: contain; }
    .white-balance-overlay { position: absolute; z-index: 2; pointer-events: none; }
    .white-balance-overlay.picking { pointer-events: auto; cursor: crosshair; touch-action: none; }
    .white-balance-region { position: absolute; border: 2px solid #fff; background: transparent; box-shadow: 0 0 0 1px #17191b, 0 0 0 3px rgba(255, 255, 255, .28); pointer-events: none; }
    .white-balance-region.draft { background: rgba(255, 255, 255, .14); }
    .white-balance-region.pending { border-style: dashed; }
    .white-balance-region.retry { border-color: #ff9f96; }
    .stage-message { max-width: min(760px, 90vw); padding: 24px; color: #c7cbc8; font: 600 13px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; text-align: center; overflow-wrap: anywhere; }
    .stage-message.error { color: #ff9f96; }
    @media (max-width: 900px) {
      header { display: grid; grid-template-columns: 1fr auto; gap: 8px 12px; }
      .controls { grid-column: 1 / -1; grid-row: 2; flex-wrap: wrap; }
      .telemetry { grid-column: 2; grid-row: 1; }
      select { width: min(100%, 420px); flex: 1 1 220px; }
      select.profile { width: 180px; flex: 0 0 180px; }
      .color-controls { flex-wrap: wrap; gap: 8px 18px; }
    }
    @media (max-width: 700px) {
      header { padding: 9px 10px; }
      .controls { width: 100%; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .controls label { grid-column: 1 / -1; }
      select, button { min-height: 44px; }
      .controls select { width: 100%; min-width: 0; grid-column: 1 / -1; }
      .controls button { width: 100%; }
      .telemetry .metric:not(.status) { display: none; }
      .color-controls { display: grid; grid-template-columns: minmax(0, 1fr); }
      .gain { width: 100%; grid-template-columns: 18px minmax(0, 1fr) 38px; }
      .gain input { width: 100%; }
      .color-actions { width: 100%; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .color-actions button, .auto-toggle { width: 100%; justify-content: center; padding: 0 6px; }
    }
  </style>
</head>
<body>
  <header>
    <strong>MarlinScan Camera</strong>
    <div class="controls" aria-busy="false" id="controls">
      <label for="camera">Camera</label>
      <select id="camera" disabled><option value="" disabled>Scanning cameras...</option></select>
      <select class="profile" id="profile" aria-label="Preview mode">
        <option value="smooth">Smooth 1080p</option>
        <option value="detail">Detail 4K</option>
      </select>
      <button type="button" id="scan">Rescan</button>
      <button type="button" class="primary" id="preview" disabled>Preview</button>
      <button type="button" id="stop" hidden>Stop</button>
    </div>
    <div class="telemetry">
      <span class="metric" id="resolution">NO SIGNAL</span>
      <span class="metric" id="fps">0.0 fps</span>
      <span class="metric" id="bitrate">0.0 Mbps</span>
      <span class="metric status busy" id="status">SCANNING</span>
    </div>
  </header>
  <section class="color-controls" aria-label="Color balance">
    <label class="gain red" for="red-gain">R <input id="red-gain" type="range" min="0.25" max="2" step="0.01" value="1"><output id="red-value">1.00</output></label>
    <label class="gain green" for="green-gain">G <input id="green-gain" type="range" min="0.25" max="2" step="0.01" value="1"><output id="green-value">1.00</output></label>
    <label class="gain blue" for="blue-gain">B <input id="blue-gain" type="range" min="0.25" max="2" step="0.01" value="1"><output id="blue-value">1.00</output></label>
    <div class="color-actions">
      <label class="auto-toggle" for="auto-white-balance"><input id="auto-white-balance" type="checkbox" disabled>Auto WB</label>
      <button type="button" id="pick-gray" disabled>Pick gray</button>
      <button type="button" id="reset-color">Reset</button>
    </div>
  </section>
  <main>
    <img id="preview-image" alt="" hidden>
    <div class="white-balance-overlay" id="white-balance-overlay" hidden><div class="white-balance-region" id="white-balance-region" hidden></div></div>
    <div class="stage-message" id="stage-message" role="status" aria-live="polite">SCANNING CAMERAS</div>
  </main>
  <script>
    const autoWhiteBalance = document.querySelector("#auto-white-balance");
    const camera = document.querySelector("#camera");
    const bitrate = document.querySelector("#bitrate");
    const blueGain = document.querySelector("#blue-gain");
    const blueValue = document.querySelector("#blue-value");
    const controls = document.querySelector("#controls");
    const fps = document.querySelector("#fps");
    const greenGain = document.querySelector("#green-gain");
    const greenValue = document.querySelector("#green-value");
    const main = document.querySelector("main");
    const pickGrayButton = document.querySelector("#pick-gray");
    const previewButton = document.querySelector("#preview");
    const previewImage = document.querySelector("#preview-image");
    const profile = document.querySelector("#profile");
    const redGain = document.querySelector("#red-gain");
    const redValue = document.querySelector("#red-value");
    const resetColorButton = document.querySelector("#reset-color");
    const resolution = document.querySelector("#resolution");
    const scanButton = document.querySelector("#scan");
    const stageMessage = document.querySelector("#stage-message");
    const status = document.querySelector("#status");
    const stopButton = document.querySelector("#stop");
    const whiteBalanceOverlay = document.querySelector("#white-balance-overlay");
    const whiteBalanceRegion = document.querySelector("#white-balance-region");
    let activeCamera = null;
    let activeProfile = null;
    let actualFrameHeight = 0;
    let actualFrameWidth = 0;
    let busy = false;
    let colorEditing = false;
    let colorTimer = null;
    let draftWhiteBalanceRoi = null;
    let frameHealthy = false;
    let pickNeedsRetry = false;
    let pickPointer = null;
    let pickStart = null;
    let pickingGray = false;
    let previewGeneration = null;
    let profileInitialized = false;
    let serverAutoWhiteBalance = false;
    let serverBusy = false;
    let statusEpoch = 0;
    let whiteBalanceBusy = false;
    let whiteBalanceRoi = null;

    function setStatus(text, kind = "ready", detail = "") {
      status.textContent = text;
      status.classList.toggle("busy", kind === "busy");
      status.classList.toggle("error", kind === "error");
      if (detail) status.title = detail;
      else status.removeAttribute("title");
    }

    function showStage(text, isError = false) {
      cancelGrayPick();
      previewImage.hidden = true;
      previewImage.removeAttribute("src");
      previewGeneration = null;
      actualFrameHeight = 0;
      actualFrameWidth = 0;
      frameHealthy = false;
      whiteBalanceOverlay.hidden = true;
      stageMessage.hidden = false;
      stageMessage.textContent = text;
      stageMessage.classList.toggle("error", isError);
      stageMessage.setAttribute("role", isError ? "alert" : "status");
    }

    function selectedIndex() {
      const value = Number(camera.value);
      return camera.value !== "" && Number.isInteger(value) && value >= 0 ? value : null;
    }

    function updateControls() {
      const selected = selectedIndex();
      const switching = activeCamera !== null && (selected !== activeCamera || profile.value !== activeProfile);
      const locked = busy || serverBusy;
      controls.setAttribute("aria-busy", locked ? "true" : "false");
      camera.disabled = locked || camera.options.length === 0;
      profile.disabled = locked;
      scanButton.disabled = locked || activeCamera !== null;
      previewButton.disabled = locked || selected === null || (activeCamera !== null && !switching);
      previewButton.textContent = switching ? "Switch" : "Preview";
      stopButton.hidden = activeCamera === null;
      stopButton.disabled = locked;
      updateColorControlState();
    }

    async function readResponse(response) {
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || `Request failed (${response.status})`);
      }
      return payload;
    }

    function renderMutationStatus(data) {
      statusEpoch += 1;
      renderStatus(data);
    }

    function renderStatus(data) {
      const actual = data.actual;
      const hasFrame = data.jpeg.sequence > 0;
      if (previewGeneration !== null && data.generation !== previewGeneration) cancelGrayPick();
      serverBusy = data.state === "connecting" || data.state === "scanning";
      actualFrameWidth = hasFrame ? actual.width : 0;
      actualFrameHeight = hasFrame ? actual.height : 0;
      frameHealthy = data.state === "streaming" && data.healthy && hasFrame;
      resolution.textContent = hasFrame ? `${actual.width} x ${actual.height}` : "NO SIGNAL";
      fps.textContent = `${actual.fps.toFixed(1)} fps`;
      bitrate.textContent = `${data.jpeg.mbps.toFixed(1)} Mbps`;
      if (!profileInitialized && data.selected_profile) {
        profile.value = data.selected_profile;
        profileInitialized = true;
      }
      if (data.state === "streaming") {
        activeCamera = data.camera_index;
        activeProfile = data.profile;
        if (pickingGray) {
          setStatus(pickNeedsRetry ? "PICK AGAIN" : "SELECT GRAY", pickNeedsRetry ? "error" : "busy");
        } else if (whiteBalanceBusy) {
          setStatus("WHITE BALANCE", "busy");
        } else if (data.color.auto_white_balance && data.color.auto_white_balance_state === "waiting") {
          setStatus("WB WAITING", "error", data.color.white_balance_error || "Waiting for a usable gray sample");
        } else if (data.color.auto_white_balance) {
          setStatus("AUTO WB");
        } else {
          setStatus("STREAMING");
        }
        if (previewImage.hidden || previewGeneration !== data.generation) {
          previewImage.alt = `Live preview from camera ${activeCamera}`;
          previewImage.src = `/stream.mjpg?generation=${data.generation}`;
          previewGeneration = data.generation;
          previewImage.hidden = false;
          stageMessage.hidden = true;
        }
      } else if (data.state === "connecting") {
        activeCamera = null;
        activeProfile = null;
        setStatus("CONNECTING", "busy");
        if (!previewImage.hidden) showStage(`OPENING CAMERA ${data.camera_index}`);
      } else if (data.state === "scanning") {
        activeCamera = null;
        activeProfile = null;
        setStatus("SCANNING", "busy");
        if (!previewImage.hidden) showStage("SCANNING CAMERAS");
      } else if (data.state === "stalled" || data.state === "error") {
        activeCamera = hasFrame ? data.camera_index : null;
        activeProfile = hasFrame ? data.profile : null;
        setStatus(data.state.toUpperCase(), "error");
        showStage(data.error || data.state.toUpperCase(), true);
      } else {
        activeCamera = null;
        activeProfile = null;
        setStatus("IDLE");
        if (!previewImage.hidden) showStage("NO PREVIEW");
      }
      renderColorState(data.color);
      updateWhiteBalanceOverlay();
      updateControls();
    }

    function setColorControls(color) {
      redGain.value = color.red_gain;
      greenGain.value = color.green_gain;
      blueGain.value = color.blue_gain;
      redValue.value = Number(redGain.value).toFixed(2);
      greenValue.value = Number(greenGain.value).toFixed(2);
      blueValue.value = Number(blueGain.value).toFixed(2);
    }

    function renderColorState(color) {
      serverAutoWhiteBalance = Boolean(color.auto_white_balance);
      if (!colorEditing) setColorControls(color);
      if (!whiteBalanceBusy && !pickingGray) autoWhiteBalance.checked = serverAutoWhiteBalance;
      if (!pickingGray) whiteBalanceRoi = color.white_balance_roi;
      updateColorControlState();
      renderWhiteBalanceRegion();
    }

    function updateColorControlState() {
      const manualLocked = autoWhiteBalance.checked || whiteBalanceBusy;
      for (const input of [redGain, greenGain, blueGain]) input.disabled = manualLocked;
      autoWhiteBalance.disabled = !frameHealthy || whiteBalanceBusy;
      pickGrayButton.disabled = !frameHealthy || main.clientWidth <= 0 || main.clientHeight <= 0 || whiteBalanceBusy;
      resetColorButton.disabled = whiteBalanceBusy;
      pickGrayButton.textContent = pickingGray ? "Cancel" : "Pick gray";
    }

    async function updateColor() {
      colorTimer = null;
      try {
        const response = await fetch("/settings", {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-MarlinScan-Request": "1"},
          body: JSON.stringify({
            red_gain: Number(redGain.value),
            green_gain: Number(greenGain.value),
            blue_gain: Number(blueGain.value),
          }),
        });
        const data = await readResponse(response);
        colorEditing = false;
        renderMutationStatus(data);
      } catch (error) {
        statusEpoch += 1;
        colorEditing = false;
        autoWhiteBalance.checked = serverAutoWhiteBalance;
        updateColorControlState();
        setStatus("COLOR ERROR", "error");
      }
    }

    function scheduleColorUpdate() {
      cancelGrayPick();
      autoWhiteBalance.checked = false;
      colorEditing = true;
      redValue.value = Number(redGain.value).toFixed(2);
      greenValue.value = Number(greenGain.value).toFixed(2);
      blueValue.value = Number(blueGain.value).toFixed(2);
      clearTimeout(colorTimer);
      colorTimer = setTimeout(updateColor, 120);
      updateColorControlState();
    }

    function updateWhiteBalanceOverlay() {
      if (previewImage.hidden || actualFrameWidth <= 0 || actualFrameHeight <= 0) {
        whiteBalanceOverlay.hidden = true;
        return;
      }
      const stageWidth = main.clientWidth;
      const stageHeight = main.clientHeight;
      const scale = Math.min(stageWidth / actualFrameWidth, stageHeight / actualFrameHeight);
      const width = actualFrameWidth * scale;
      const height = actualFrameHeight * scale;
      if (width <= 0 || height <= 0) {
        whiteBalanceOverlay.hidden = true;
        return;
      }
      whiteBalanceOverlay.style.left = `${(stageWidth - width) / 2}px`;
      whiteBalanceOverlay.style.top = `${(stageHeight - height) / 2}px`;
      whiteBalanceOverlay.style.width = `${width}px`;
      whiteBalanceOverlay.style.height = `${height}px`;
      whiteBalanceOverlay.hidden = false;
      renderWhiteBalanceRegion();
    }

    function renderWhiteBalanceRegion() {
      const roi = draftWhiteBalanceRoi || whiteBalanceRoi;
      if (!pickingGray || !roi) {
        whiteBalanceRegion.hidden = true;
        return;
      }
      whiteBalanceRegion.style.left = `${roi.x * 100}%`;
      whiteBalanceRegion.style.top = `${roi.y * 100}%`;
      whiteBalanceRegion.style.width = `${roi.width * 100}%`;
      whiteBalanceRegion.style.height = `${roi.height * 100}%`;
      whiteBalanceRegion.classList.toggle("draft", draftWhiteBalanceRoi !== null);
      whiteBalanceRegion.classList.toggle("pending", whiteBalanceBusy);
      whiteBalanceRegion.classList.toggle("retry", pickNeedsRetry);
      whiteBalanceRegion.hidden = false;
    }

    function beginGrayPick() {
      if (!frameHealthy) return;
      pickingGray = true;
      pickNeedsRetry = false;
      draftWhiteBalanceRoi = null;
      whiteBalanceOverlay.classList.add("picking");
      updateColorControlState();
      renderWhiteBalanceRegion();
      setStatus("SELECT GRAY", "busy");
    }

    function cancelGrayPick() {
      if (pickPointer !== null && whiteBalanceOverlay.hasPointerCapture(pickPointer)) {
        whiteBalanceOverlay.releasePointerCapture(pickPointer);
      }
      pickPointer = null;
      pickStart = null;
      pickingGray = false;
      pickNeedsRetry = false;
      draftWhiteBalanceRoi = null;
      whiteBalanceOverlay.classList.remove("picking");
      if (autoWhiteBalance.checked && !serverAutoWhiteBalance && whiteBalanceRoi === null) {
        autoWhiteBalance.checked = false;
      }
      updateColorControlState();
      renderWhiteBalanceRegion();
    }

    async function requestWhiteBalance(enabled, roi) {
      const payload = {
        auto_white_balance: enabled,
        generation: previewGeneration,
      };
      if (roi !== undefined) payload.white_balance_roi = roi;
      const response = await fetch("/white-balance", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-MarlinScan-Request": "1"},
        body: JSON.stringify(payload),
      });
      return readResponse(response);
    }

    async function changeAutoWhiteBalance() {
      clearTimeout(colorTimer);
      colorTimer = null;
      colorEditing = false;
      if (autoWhiteBalance.checked && whiteBalanceRoi === null) {
        beginGrayPick();
        return;
      }
      if (!autoWhiteBalance.checked && pickingGray) cancelGrayPick();
      whiteBalanceBusy = true;
      updateColorControlState();
      try {
        const data = await requestWhiteBalance(autoWhiteBalance.checked);
        renderMutationStatus(data);
        setStatus(autoWhiteBalance.checked ? "AUTO WB" : "WB FIXED");
      } catch (error) {
        statusEpoch += 1;
        autoWhiteBalance.checked = serverAutoWhiteBalance;
        setStatus("WB ERROR", "error", error.message);
      } finally {
        whiteBalanceBusy = false;
        updateColorControlState();
        renderWhiteBalanceRegion();
      }
    }

    async function applyPickedRegion(roi) {
      whiteBalanceBusy = true;
      draftWhiteBalanceRoi = roi;
      updateColorControlState();
      renderWhiteBalanceRegion();
      try {
        const data = await requestWhiteBalance(autoWhiteBalance.checked, roi);
        pickingGray = false;
        draftWhiteBalanceRoi = null;
        whiteBalanceOverlay.classList.remove("picking");
        renderMutationStatus(data);
        setStatus(autoWhiteBalance.checked ? "AUTO WB" : "WHITE BALANCED");
      } catch (error) {
        statusEpoch += 1;
        draftWhiteBalanceRoi = roi;
        pickNeedsRetry = true;
        setStatus("PICK AGAIN", "error", error.message);
      } finally {
        whiteBalanceBusy = false;
        updateColorControlState();
        renderWhiteBalanceRegion();
      }
    }

    function pointInWhiteBalanceOverlay(event) {
      const bounds = whiteBalanceOverlay.getBoundingClientRect();
      return {
        x: Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width)),
        y: Math.min(1, Math.max(0, (event.clientY - bounds.top) / bounds.height)),
      };
    }

    function roiBetween(start, end) {
      return {
        x: Math.min(start.x, end.x),
        y: Math.min(start.y, end.y),
        width: Math.abs(end.x - start.x),
        height: Math.abs(end.y - start.y),
      };
    }

    function resetPickDrag() {
      pickPointer = null;
      pickStart = null;
      draftWhiteBalanceRoi = null;
      renderWhiteBalanceRegion();
    }

    function populateCameras(data) {
      const previous = camera.value;
      camera.replaceChildren();
      for (const item of data.cameras) {
        const option = document.createElement("option");
        option.value = String(item.index);
        option.textContent = `Camera ${item.index} - ${item.info}${item.frame_ok ? "" : " - no frame during scan"}`;
        camera.append(option);
      }
      const preferred = data.selected_camera === null ? previous : String(data.selected_camera);
      if (preferred && [...camera.options].some(option => option.value === preferred)) {
        camera.value = preferred;
      }
    }

    async function scanCameras() {
      busy = true;
      setStatus("SCANNING", "busy");
      showStage("SCANNING CAMERAS");
      updateControls();
      try {
        const response = await fetch("/cameras/scan", {
          method: "POST",
          headers: {"X-MarlinScan-Request": "1"},
        });
        const data = await readResponse(response);
        populateCameras(data);
        if (data.cameras.length > 0) {
          setStatus("READY");
          showStage("NO PREVIEW");
        } else {
          setStatus("NO CAMERAS", "error");
          showStage("NO CAMERAS AVAILABLE", true);
        }
      } catch (error) {
        setStatus("SCAN ERROR", "error");
        showStage(error.message, true);
      } finally {
        busy = false;
        serverBusy = false;
        updateControls();
      }
    }

    async function initialize() {
      try {
        const response = await fetch("/cameras.json", {cache: "no-store"});
        const data = await readResponse(response);
        populateCameras(data);
        if (data.selected_profile) {
          profile.value = data.selected_profile;
          profileInitialized = true;
        }
        if (data.active_camera !== null) {
          await refreshStatus();
        } else if (data.cameras.length > 0) {
          setStatus("READY");
          showStage("NO PREVIEW");
          updateControls();
        } else {
          await scanCameras();
        }
      } catch (error) {
        await scanCameras();
      }
    }

    async function startPreview() {
      const index = selectedIndex();
      if (index === null) return;
      busy = true;
      setStatus("CONNECTING", "busy");
      showStage(`OPENING CAMERA ${index}`);
      updateControls();
      try {
        const response = await fetch("/camera", {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-MarlinScan-Request": "1"},
          body: JSON.stringify({camera_index: index, profile: profile.value}),
        });
        const data = await readResponse(response);
        activeCamera = index;
        activeProfile = profile.value;
        renderMutationStatus(data);
      } catch (error) {
        activeCamera = null;
        activeProfile = null;
        setStatus("PREVIEW ERROR", "error");
        showStage(error.message, true);
      } finally {
        busy = false;
        serverBusy = false;
        updateControls();
      }
    }

    async function stopPreview() {
      busy = true;
      setStatus("STOPPING", "busy");
      updateControls();
      try {
        const response = await fetch("/camera", {
          method: "DELETE",
          headers: {"X-MarlinScan-Request": "1"},
        });
        const data = await readResponse(response);
        activeCamera = null;
        activeProfile = null;
        showStage("NO PREVIEW");
        renderMutationStatus(data);
      } catch (error) {
        setStatus("STOP ERROR", "error");
        showStage(error.message, true);
      } finally {
        busy = false;
        serverBusy = false;
        updateControls();
      }
    }

    async function refreshStatus() {
      const epoch = statusEpoch;
      try {
        const response = await fetch("/status.json", {cache: "no-store"});
        const data = await readResponse(response);
        if (epoch === statusEpoch) renderStatus(data);
      } catch (error) {
        if (epoch === statusEpoch) setStatus("OFFLINE", "error");
      }
    }

    autoWhiteBalance.addEventListener("change", changeAutoWhiteBalance);
    camera.addEventListener("change", () => {
      cancelGrayPick();
      updateControls();
    });
    profile.addEventListener("change", () => {
      cancelGrayPick();
      updateControls();
    });
    for (const input of [redGain, greenGain, blueGain]) input.addEventListener("input", scheduleColorUpdate);
    pickGrayButton.addEventListener("click", () => {
      if (pickingGray) {
        cancelGrayPick();
        setStatus(serverAutoWhiteBalance ? "AUTO WB" : "STREAMING");
      } else {
        beginGrayPick();
      }
    });
    previewButton.addEventListener("click", startPreview);
    previewImage.addEventListener("error", () => showStage("PREVIEW CONNECTION LOST", true));
    previewImage.addEventListener("load", updateWhiteBalanceOverlay);
    scanButton.addEventListener("click", scanCameras);
    resetColorButton.addEventListener("click", () => {
      setColorControls({red_gain: 1, green_gain: 1, blue_gain: 1});
      scheduleColorUpdate();
    });
    stopButton.addEventListener("click", stopPreview);
    whiteBalanceOverlay.addEventListener("pointerdown", event => {
      if (
        !pickingGray
        || whiteBalanceBusy
        || pickPointer !== null
        || !event.isPrimary
        || (event.pointerType === "mouse" && event.button !== 0)
      ) return;
      event.preventDefault();
      pickNeedsRetry = false;
      pickPointer = event.pointerId;
      const point = pointInWhiteBalanceOverlay(event);
      pickStart = {
        ...point,
        clientX: event.clientX,
        clientY: event.clientY,
        pointerType: event.pointerType,
      };
      draftWhiteBalanceRoi = {x: point.x, y: point.y, width: 0, height: 0};
      whiteBalanceOverlay.setPointerCapture(event.pointerId);
      renderWhiteBalanceRegion();
    });
    whiteBalanceOverlay.addEventListener("pointermove", event => {
      if (event.pointerId !== pickPointer || pickStart === null) return;
      event.preventDefault();
      draftWhiteBalanceRoi = roiBetween(pickStart, pointInWhiteBalanceOverlay(event));
      renderWhiteBalanceRegion();
    });
    whiteBalanceOverlay.addEventListener("pointerup", async event => {
      if (event.pointerId !== pickPointer || pickStart === null) return;
      event.preventDefault();
      const start = pickStart;
      const end = pointInWhiteBalanceOverlay(event);
      const distance = Math.hypot(event.clientX - start.clientX, event.clientY - start.clientY);
      if (whiteBalanceOverlay.hasPointerCapture(event.pointerId)) {
        whiteBalanceOverlay.releasePointerCapture(event.pointerId);
      }
      resetPickDrag();
      let roi;
      if (distance < 12 && start.pointerType === "touch") {
        const bounds = whiteBalanceOverlay.getBoundingClientRect();
        const width = Math.min(1, Math.max(0.01, 48 / bounds.width));
        const height = Math.min(1, Math.max(0.01, 48 / bounds.height));
        roi = {
          x: Math.min(1 - width, Math.max(0, end.x - width / 2)),
          y: Math.min(1 - height, Math.max(0, end.y - height / 2)),
          width,
          height,
        };
      } else {
        roi = roiBetween(start, end);
      }
      if ((distance < 12 && start.pointerType !== "touch") || roi.width < 0.01 || roi.height < 0.01) {
        pickNeedsRetry = true;
        setStatus("PICK AGAIN", "error");
        return;
      }
      await applyPickedRegion(roi);
    });
    whiteBalanceOverlay.addEventListener("pointercancel", event => {
      if (event.pointerId === pickPointer) resetPickDrag();
    });
    whiteBalanceOverlay.addEventListener("lostpointercapture", event => {
      if (event.pointerId === pickPointer) resetPickDrag();
    });
    new ResizeObserver(() => {
      updateWhiteBalanceOverlay();
      updateColorControlState();
    }).observe(main);
    updateColorControlState();
    initialize();
    setInterval(refreshStatus, 1000);
  </script>
</body>
</html>
"""


class WhiteBalanceError(RuntimeError):
    pass


class CameraStream:
    def __init__(
        self,
        camera_index: int,
        fps: int,
        fourcc: str,
        jpeg_quality: int,
        *,
        width: int = UHD_WIDTH,
        height: int = UHD_HEIGHT,
        stream_fps: int | None = None,
        red_gain: float = 1.0,
        green_gain: float = 1.0,
        blue_gain: float = 1.0,
        auto_white_balance: bool = False,
        white_balance_roi: tuple[float, float, float, float] | None = None,
        cv2_module: object | None = None,
        capture_factory: Callable[[int, int | None], object] | None = None,
        configurer: Callable[[object, UvcCameraConfig], None] = apply_uvc_config,
        capture_info: Callable[[object], str] = get_capture_info,
        negotiation_timeout: float = 4.0,
    ) -> None:
        self.camera_index = camera_index
        self.config = UvcCameraConfig(
            width=width,
            height=height,
            fps=fps,
            fourcc=fourcc,
            auto_white_balance=False,
        )
        self.stream_fps = fps if stream_fps is None else stream_fps
        self.jpeg_quality = jpeg_quality
        self._color_gains = (red_gain, green_gain, blue_gain)
        self._color_lut: object | None = None
        self._auto_white_balance = auto_white_balance
        self._white_balance_roi = white_balance_roi
        self._white_balance_state = "waiting" if auto_white_balance else "disabled"
        self._white_balance_error: str | None = None
        self._white_balance_sample: tuple[float, float, float] | None = None
        self._white_balance_revision = 0
        self._white_balance_last_attempt = 0.0
        self._cv2 = cv2_module
        self._capture_factory = capture_factory
        self._configurer = configurer
        self._capture_info = capture_info
        self._negotiation_timeout = negotiation_timeout
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._encoder_thread: threading.Thread | None = None
        self._raw_frame: object | None = None
        self._raw_sequence = 0
        self._jpeg: bytes | None = None
        self._jpeg_sequence = 0
        self._jpeg_at = 0.0
        self._capture_times: deque[float] = deque(maxlen=max(30, fps * 4))
        self._jpeg_times: deque[float] = deque(maxlen=max(30, self.stream_fps * 4))
        self._actual_width = 0
        self._actual_height = 0
        self._backend = "?"
        self._capture_description = "?"
        self._active_capture: object | None = None
        self._error: Exception | None = None
        self._started = False

    def start(self, timeout: float = 12.0) -> None:
        if self._started:
            raise RuntimeError("Camera stream has already been started")
        self._started = True
        self._load_cv2()
        self._encoder_thread = threading.Thread(target=self._encoder_worker, name="camera-encoder", daemon=True)
        self._capture_thread = threading.Thread(target=self._capture_worker, name="camera-capture", daemon=True)
        self._encoder_thread.start()
        self._capture_thread.start()

        deadline = time.monotonic() + timeout
        with self._condition:
            while self._jpeg is None and self._error is None and not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            error = self._error
            ready = self._jpeg is not None
            stopped = self._stop.is_set()

        if error is not None:
            self.stop()
            raise RuntimeError(f"Camera startup failed: {error}") from error
        if stopped:
            raise RuntimeError("Camera startup was stopped")
        if not ready:
            self.stop()
            raise TimeoutError(f"Camera did not produce a JPEG within {timeout:g} seconds")

    def stop(self) -> bool:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        capture_thread = self._capture_thread
        if capture_thread is not None and capture_thread is not threading.current_thread():
            capture_thread.join(timeout=1.0)
            if capture_thread.is_alive():
                capture = self._detach_capture()
                if capture is not None:
                    capture.release()
                capture_thread.join(timeout=2.0)
        encoder_thread = self._encoder_thread
        if encoder_thread is not None and encoder_thread is not threading.current_thread():
            encoder_thread.join(timeout=3.0)
        stopped = all(
            thread is None or not thread.is_alive()
            for thread in (capture_thread, encoder_thread)
        )
        if not stopped:
            LOGGER.warning("Camera worker did not stop within the shutdown timeout")
        return stopped

    def raise_if_failed(self) -> None:
        with self._condition:
            error = self._error
        if error is not None:
            raise RuntimeError(f"Camera pipeline failed: {error}") from error

    def wait_for_jpeg(self, after_sequence: int, timeout: float) -> tuple[int, bytes] | None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._jpeg_sequence <= after_sequence and self._error is None and not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            if self._error is not None:
                raise RuntimeError(f"Camera pipeline failed: {self._error}") from self._error
            if self._jpeg is None or self._jpeg_sequence <= after_sequence:
                return None
            return self._jpeg_sequence, self._jpeg

    def latest_jpeg(self) -> bytes:
        with self._condition:
            if self._error is not None:
                raise RuntimeError(f"Camera pipeline failed: {self._error}") from self._error
            if self._jpeg is None:
                raise RuntimeError("No camera frame is available")
            return self._jpeg

    def set_color_gains(self, red: float, green: float, blue: float, *, disable_auto: bool = True) -> None:
        with self._condition:
            self._color_gains = (red, green, blue)
            self._color_lut = None
            if disable_auto:
                self._auto_white_balance = False
                self._white_balance_state = "disabled"
                self._white_balance_error = None
                self._white_balance_sample = None
                self._white_balance_last_attempt = 0.0
            self._white_balance_revision += 1

    def color_gains(self) -> tuple[float, float, float]:
        with self._condition:
            return self._color_gains

    def color_settings(self) -> tuple[tuple[float, float, float], bool, tuple[float, float, float, float] | None]:
        with self._condition:
            return self._color_gains, self._auto_white_balance, self._white_balance_roi

    def configure_white_balance(
        self,
        enabled: bool,
        roi: tuple[float, float, float, float] | None | object = WHITE_BALANCE_UNSET,
        *,
        calibrate: bool = True,
    ) -> None:
        with self._condition:
            selected_roi = self._white_balance_roi if roi is WHITE_BALANCE_UNSET else roi
            frame = self._raw_frame
            revision = self._white_balance_revision
        if enabled and selected_roi is None:
            raise ValueError("Pick a gray region before enabling auto white balance")
        estimate = None
        if calibrate and selected_roi is not None and (roi is not WHITE_BALANCE_UNSET or enabled):
            if frame is None:
                raise WhiteBalanceError("No camera frame is available for white balance")
            estimate = self._estimate_white_balance(frame, selected_roi)
        with self._condition:
            if revision != self._white_balance_revision:
                raise WhiteBalanceError("White balance settings changed while sampling")
            self._auto_white_balance = enabled
            self._white_balance_roi = selected_roi
            self._white_balance_state = "waiting" if enabled else "disabled"
            self._white_balance_error = None
            self._white_balance_sample = None
            self._white_balance_last_attempt = 0.0
            if estimate is not None:
                gains, sample = estimate
                self._color_gains = gains
                self._color_lut = None
                self._white_balance_sample = sample
                self._white_balance_last_attempt = time.monotonic()
                if enabled:
                    self._white_balance_state = "active"
            self._white_balance_revision += 1

    def status(self) -> dict[str, object]:
        now = time.monotonic()
        with self._condition:
            capture_times = list(self._capture_times)
            jpeg_times = list(self._jpeg_times)
            age_ms = None if self._jpeg is None else max(0.0, (now - self._jpeg_at) * 1000.0)
            capture_fps = self._rate(capture_times)
            stream_fps = self._rate(jpeg_times)
            red_gain, green_gain, blue_gain = self._color_gains
            white_balance_roi = self._white_balance_roi
            white_balance_sample = self._white_balance_sample
            if self._error is not None:
                state = "error"
            elif self._stop.is_set():
                state = "stopped"
            elif self._jpeg is not None and age_ms is not None and age_ms >= STALE_AFTER_SECONDS * 1000.0:
                state = "stalled"
            elif self._jpeg is not None:
                state = "streaming"
            else:
                state = "starting"
            if state == "stalled":
                stream_fps = 0.0
            healthy = (
                state == "streaming"
                and self._actual_width > 0
                and self._actual_height > 0
                and age_ms is not None
                and age_ms < 5000.0
            )
            return {
                "state": state,
                "healthy": healthy,
                "camera_index": self.camera_index,
                "requested": {
                    "width": self.config.width,
                    "height": self.config.height,
                    "fps": self.config.fps,
                    "stream_fps": self.stream_fps,
                    "fourcc": self.config.fourcc,
                },
                "actual": {
                    "width": self._actual_width,
                    "height": self._actual_height,
                    "fps": stream_fps,
                    "capture_fps": capture_fps,
                    "backend": self._backend,
                    "capture": self._capture_description,
                },
                "jpeg": {
                    "quality": self.jpeg_quality,
                    "sequence": self._jpeg_sequence,
                    "bytes": 0 if self._jpeg is None else len(self._jpeg),
                    "age_ms": age_ms,
                    "mbps": 0.0 if self._jpeg is None else len(self._jpeg) * stream_fps * 8.0 / 1_000_000.0,
                },
                "color": {
                    "red_gain": red_gain,
                    "green_gain": green_gain,
                    "blue_gain": blue_gain,
                    "auto_white_balance": self._auto_white_balance,
                    "white_balance_roi": white_balance_roi_payload(white_balance_roi),
                    "auto_white_balance_state": self._white_balance_state,
                    "white_balance_error": self._white_balance_error,
                    "white_balance_sample": white_balance_sample_payload(white_balance_sample),
                },
                "error": None if self._error is None else str(self._error),
            }

    def _load_cv2(self) -> object:
        if self._cv2 is None:
            try:
                import cv2
            except ImportError as exc:
                raise RuntimeError(
                    "OpenCV is required. Install it with: conda run -n 3dprinter python -m pip install opencv-python"
                ) from exc
            self._cv2 = cv2
        return self._cv2

    def _capture_worker(self) -> None:
        capture = None
        try:
            capture, first_frame, backend, description = self._open_camera()
            with self._condition:
                self._backend = backend
                self._capture_description = description
            self._publish_raw(first_frame)
            failed_reads = 0
            while not self._stop.is_set():
                ok, frame = capture.read()
                if not ok or frame is None:
                    failed_reads += 1
                    if failed_reads >= 10:
                        raise RuntimeError("Camera returned 10 consecutive empty frames")
                    time.sleep(0.02)
                    continue
                failed_reads = 0
                self._record_resolution(frame)
                self._publish_raw(frame)
        except Exception as exc:
            if not self._stop.is_set():
                self._fail(exc)
        finally:
            if capture is not None:
                owned_capture = self._detach_capture(capture)
                if owned_capture is not None:
                    owned_capture.release()

    def _encoder_worker(self) -> None:
        cv2 = self._load_cv2()
        last_raw_sequence = 0
        next_encode_at = 0.0
        frame_interval = 1.0 / self.stream_fps
        try:
            while not self._stop.is_set():
                with self._condition:
                    while (
                        self._raw_sequence <= last_raw_sequence
                        and self._error is None
                        and not self._stop.is_set()
                    ):
                        self._condition.wait(0.5)
                    if self._error is not None or self._stop.is_set():
                        return
                delay = next_encode_at - time.monotonic()
                if delay > 0.0 and self._stop.wait(delay):
                    return
                with self._condition:
                    if self._error is not None or self._stop.is_set():
                        return
                    frame = self._raw_frame
                    raw_sequence = self._raw_sequence
                if raw_sequence <= last_raw_sequence:
                    continue
                last_raw_sequence = raw_sequence
                if frame is None:
                    continue
                encode_started = time.monotonic()
                next_encode_at = encode_started + frame_interval
                self._update_auto_white_balance(frame, encode_started)
                frame = self._apply_color_gains(frame)
                ok, encoded = cv2.imencode(
                    ".jpg",
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality],
                )
                if not ok:
                    raise RuntimeError("OpenCV failed to encode a JPEG frame")
                jpeg = encoded.tobytes()
                now = time.monotonic()
                with self._condition:
                    self._jpeg = jpeg
                    self._jpeg_sequence += 1
                    self._jpeg_at = now
                    self._jpeg_times.append(now)
                    self._condition.notify_all()
        except Exception as exc:
            if not self._stop.is_set():
                self._fail(exc)

    def _apply_color_gains(self, frame: object) -> object:
        cv2 = self._load_cv2()
        with self._condition:
            gains = self._color_gains
            lut = self._color_lut
        if gains == (1.0, 1.0, 1.0):
            return frame
        if lut is None:
            import numpy as np

            red, green, blue = gains
            values = np.arange(256, dtype=np.float32)[:, None]
            lut = np.clip(values * np.array([[blue, green, red]], dtype=np.float32), 0, 255).astype(np.uint8)
            lut = lut.reshape(256, 1, 3)
            with self._condition:
                if gains == self._color_gains:
                    self._color_lut = lut
        return cv2.LUT(frame, lut)

    def _update_auto_white_balance(self, frame: object, now: float) -> None:
        with self._condition:
            if (
                not self._auto_white_balance
                or self._white_balance_roi is None
                or now - self._white_balance_last_attempt < WHITE_BALANCE_INTERVAL_SECONDS
            ):
                return
            roi = self._white_balance_roi
            revision = self._white_balance_revision
            had_sample = self._white_balance_sample is not None
            self._white_balance_last_attempt = now
        try:
            target, sample = self._estimate_white_balance(frame, roi)
        except WhiteBalanceError as exc:
            with self._condition:
                if revision == self._white_balance_revision and self._auto_white_balance:
                    self._white_balance_state = "waiting"
                    self._white_balance_error = str(exc)
            return
        with self._condition:
            if revision != self._white_balance_revision or not self._auto_white_balance:
                return
            if had_sample:
                target = tuple(
                    current + 0.25 * (wanted - current)
                    for current, wanted in zip(self._color_gains, target)
                )
            self._color_gains = target
            self._color_lut = None
            self._white_balance_sample = sample
            self._white_balance_state = "active"
            self._white_balance_error = None

    @staticmethod
    def _estimate_white_balance(
        frame: object,
        roi: tuple[float, float, float, float],
    ) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        import numpy as np

        height, width = CameraStream._frame_size(frame)
        x, y, roi_width, roi_height = roi
        x0 = min(width - 1, max(0, math.floor(x * width)))
        y0 = min(height - 1, max(0, math.floor(y * height)))
        x1 = min(width, max(x0 + 1, math.ceil((x + roi_width) * width)))
        y1 = min(height, max(y0 + 1, math.ceil((y + roi_height) * height)))
        pixels = frame[y0:y1, x0:x1, :3]
        pixel_count = int(pixels.shape[0] * pixels.shape[1])
        stride = max(1, math.ceil(math.sqrt(pixel_count / WHITE_BALANCE_MAX_SAMPLES)))
        samples = np.asarray(pixels[::stride, ::stride], dtype=np.float32).reshape(-1, 3)
        valid = samples[np.all((samples > 3.0) & (samples < 252.0), axis=1)]
        if len(valid) < max(16, math.ceil(len(samples) * 0.05)):
            raise WhiteBalanceError("Selected region is too dark, bright, or clipped")
        blue, green, red = (float(value) for value in np.median(valid, axis=0))
        target = (red + green + blue) / 3.0
        gains = tuple(min(2.0, max(0.25, target / value)) for value in (red, green, blue))
        return gains, (red, green, blue)

    def _open_camera(self) -> tuple[object, object, str, str]:
        cv2 = self._load_cv2()
        backends: list[int | None] = [None]
        avfoundation = getattr(cv2, "CAP_AVFOUNDATION", None)
        if avfoundation is not None:
            backends.append(int(avfoundation))
        failures: list[str] = []

        for backend in backends:
            if self._stop.is_set():
                raise RuntimeError("Camera startup was stopped")
            label = "default" if backend is None else "AVFoundation"
            capture = None
            try:
                if self._capture_factory is not None:
                    capture = self._capture_factory(self.camera_index, backend)
                elif backend is None:
                    capture = cv2.VideoCapture(self.camera_index)
                else:
                    capture = cv2.VideoCapture(self.camera_index, backend)
                if not capture.isOpened():
                    raise RuntimeError("could not open device")
                with self._condition:
                    self._active_capture = capture
                buffer_size = getattr(cv2, "CAP_PROP_BUFFERSIZE", None)
                if buffer_size is not None:
                    capture.set(int(buffer_size), 1.0)
                self._configurer(capture, self.config)
                frame = self._read_frame(capture)
                description = self._capture_info(capture)
                return capture, frame, label, description
            except Exception as exc:
                failures.append(f"{label}: {exc}")
                if capture is not None:
                    owned_capture = self._detach_capture(capture)
                    if owned_capture is not None:
                        owned_capture.release()

        detail = "; ".join(failures)
        raise RuntimeError(f"Could not open camera index {self.camera_index} ({detail})")

    def _read_frame(self, capture: object) -> object:
        deadline = time.monotonic() + self._negotiation_timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            ok, frame = capture.read()
            if ok and frame is not None:
                self._record_resolution(frame)
                return frame
            time.sleep(0.05)
        raise RuntimeError("device opened but produced no frames")

    def _record_resolution(self, frame: object) -> None:
        height, width = self._frame_size(frame)
        if width <= 0 or height <= 0:
            raise RuntimeError("Camera returned a frame without valid dimensions")
        with self._condition:
            self._actual_width = width
            self._actual_height = height

    @staticmethod
    def _frame_size(frame: object) -> tuple[int, int]:
        try:
            height, width = frame.shape[:2]
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError("Camera returned a frame without valid dimensions") from exc
        return int(height), int(width)

    def _publish_raw(self, frame: object) -> None:
        now = time.monotonic()
        with self._condition:
            self._raw_frame = frame
            self._raw_sequence += 1
            self._capture_times.append(now)
            self._condition.notify_all()

    def _detach_capture(self, expected: object | None = None) -> object | None:
        with self._condition:
            if expected is not None and self._active_capture is not expected:
                return None
            capture = self._active_capture
            self._active_capture = None
            return capture

    @staticmethod
    def _rate(times: list[float]) -> float:
        if len(times) < 2 or times[-1] <= times[0]:
            return 0.0
        return (len(times) - 1) / (times[-1] - times[0])

    def _fail(self, error: Exception) -> None:
        with self._condition:
            if self._error is None:
                self._error = error
            self._stop.set()
            self._condition.notify_all()


class CameraBusyError(RuntimeError):
    pass


class CameraSelectionError(RuntimeError):
    pass


class CameraManager:
    def __init__(
        self,
        fps: int,
        fourcc: str,
        jpeg_quality: int,
        scan_limit: int,
        *,
        profile: str = DEFAULT_PROFILE,
        red_gain: float = 1.0,
        green_gain: float = 1.0,
        blue_gain: float = 1.0,
        stream_factory: Callable[[int, str], CameraStream] | None = None,
        probe: Callable[[int], list[object]] | None = None,
    ) -> None:
        if profile not in PREVIEW_PROFILES:
            raise ValueError(f"Unknown preview profile: {profile}")
        self.fps = fps
        self.fourcc = fourcc
        self.jpeg_quality = jpeg_quality
        self.scan_limit = scan_limit
        self._default_profile = profile
        self._red_gain = normalize_color_gain(red_gain, "red_gain")
        self._green_gain = normalize_color_gain(green_gain, "green_gain")
        self._blue_gain = normalize_color_gain(blue_gain, "blue_gain")
        self._auto_white_balance = False
        self._white_balance_roi: tuple[float, float, float, float] | None = None
        self._stream_factory = stream_factory
        self._probe = probe or (
            lambda limit: list(probe_uvc_indices(max_index=limit, read_tries=3))
        )
        self._state_lock = threading.Lock()
        self._operation_lock = threading.Lock()
        self._stream: CameraStream | None = None
        self._candidate: CameraStream | None = None
        self._preview_state = "idle"
        self._selected_camera: int | None = None
        self._selected_profile = profile
        self._generation = 0
        self._last_error: str | None = None
        self._scan_state = "idle"
        self._scan_error: str | None = None
        self._cameras: list[dict[str, object]] = []
        self._closed = False

    def scan(self) -> dict[str, object]:
        if not self._operation_lock.acquire(blocking=False):
            raise CameraBusyError("Another camera operation is in progress")
        try:
            with self._state_lock:
                if self._closed:
                    raise CameraBusyError("Camera service is shutting down")
                if self._stream is not None or self._candidate is not None:
                    raise CameraBusyError("Stop the preview before rescanning cameras")
                self._preview_state = "idle"
                self._last_error = None
                self._scan_state = "scanning"
                self._scan_error = None
            try:
                probes = self._probe(self.scan_limit)
                cameras = [
                    {
                        "index": int(getattr(item, "index")),
                        "opened": bool(getattr(item, "opened")),
                        "frame_ok": bool(getattr(item, "frame_ok")),
                        "info": str(getattr(item, "info")),
                    }
                    for item in probes
                ]
            except Exception as exc:
                with self._state_lock:
                    self._scan_state = "error"
                    self._scan_error = str(exc)
                raise CameraSelectionError(f"Camera scan failed: {exc}") from exc
            with self._state_lock:
                self._cameras = cameras
                self._scan_state = "ready"
                self._scan_error = None
            return self.cameras()
        finally:
            self._operation_lock.release()

    def cameras(self) -> dict[str, object]:
        with self._state_lock:
            active_camera = None if self._stream is None else self._stream.camera_index
            return {
                "state": self._scan_state,
                "cameras": [dict(item) for item in self._cameras],
                "selected_camera": self._selected_camera,
                "selected_profile": self._selected_profile,
                "active_camera": active_camera,
                "generation": self._generation,
                "error": self._scan_error,
            }

    def select(self, camera_index: int, profile: str | None = None) -> dict[str, object]:
        if isinstance(camera_index, bool) or not isinstance(camera_index, int):
            raise ValueError("Camera index must be an integer")
        if not 0 <= camera_index < self.scan_limit:
            raise ValueError(f"Camera index must be between 0 and {self.scan_limit - 1}")
        selected_profile = self._default_profile if profile is None else profile
        if selected_profile not in PREVIEW_PROFILES:
            raise ValueError(f"Unknown preview profile: {selected_profile}")
        if not self._operation_lock.acquire(blocking=False):
            raise CameraBusyError("Another camera operation is in progress")
        candidate = None
        try:
            with self._state_lock:
                if self._closed:
                    raise CameraBusyError("Camera service is shutting down")
                previous = self._stream
                if previous is not None:
                    self._sync_color_settings_locked(previous)
                previous_camera = self._selected_camera
                if (
                    previous_camera is not None
                    and previous_camera != camera_index
                    and self._white_balance_roi is not None
                ):
                    self._red_gain = 1.0
                    self._green_gain = 1.0
                    self._blue_gain = 1.0
                    self._auto_white_balance = False
                    self._white_balance_roi = None
                self._stream = None
                self._preview_state = "connecting"
                self._selected_camera = camera_index
                self._selected_profile = selected_profile
                self._last_error = None
            if previous is not None:
                if not previous.stop():
                    with self._state_lock:
                        self._preview_state = "error"
                        self._last_error = "The previous camera did not stop cleanly"
                    raise CameraSelectionError(self._last_error)
            try:
                if self._stream_factory is None:
                    candidate = self._create_stream(camera_index, selected_profile)
                else:
                    candidate = self._stream_factory(camera_index, selected_profile)
                with self._state_lock:
                    if self._closed:
                        raise CameraBusyError("Camera service is shutting down")
                    candidate.set_color_gains(
                        self._red_gain,
                        self._green_gain,
                        self._blue_gain,
                        disable_auto=False,
                    )
                    candidate.configure_white_balance(
                        self._auto_white_balance,
                        self._white_balance_roi,
                        calibrate=False,
                    )
                    self._candidate = candidate
                candidate.start()
            except CameraBusyError:
                if candidate is not None:
                    candidate.stop()
                raise
            except Exception as exc:
                if candidate is not None:
                    candidate.stop()
                with self._state_lock:
                    if self._candidate is candidate:
                        self._candidate = None
                    if not self._closed:
                        self._preview_state = "error"
                        self._last_error = str(exc)
                raise CameraSelectionError(str(exc)) from exc
            with self._state_lock:
                self._candidate = None
                if self._closed:
                    publish = False
                else:
                    self._stream = candidate
                    self._preview_state = "streaming"
                    self._scan_state = "ready" if self._cameras else "idle"
                    self._scan_error = None
                    self._generation += 1
                    publish = True
            if not publish:
                candidate.stop()
                raise CameraBusyError("Camera service is shutting down")
            return self.status()
        finally:
            self._operation_lock.release()

    def stop(self) -> dict[str, object]:
        if not self._operation_lock.acquire(blocking=False):
            raise CameraBusyError("Another camera operation is in progress")
        try:
            with self._state_lock:
                stream = self._stream
                if stream is not None:
                    self._sync_color_settings_locked(stream)
                self._stream = None
                self._preview_state = "idle"
                self._last_error = None
            if stream is not None:
                if not stream.stop():
                    with self._state_lock:
                        self._preview_state = "error"
                        self._last_error = "The camera did not stop cleanly"
                    raise CameraSelectionError(self._last_error)
            return self.status()
        finally:
            self._operation_lock.release()

    def current_stream(self) -> CameraStream | None:
        with self._state_lock:
            return self._stream

    def set_color_gains(self, red: object, green: object, blue: object) -> dict[str, object]:
        gains = (
            normalize_color_gain(red, "red_gain"),
            normalize_color_gain(green, "green_gain"),
            normalize_color_gain(blue, "blue_gain"),
        )
        with self._state_lock:
            self._red_gain, self._green_gain, self._blue_gain = gains
            self._auto_white_balance = False
            for stream in dict.fromkeys(item for item in (self._stream, self._candidate) if item is not None):
                stream.set_color_gains(*gains)
        return self.status()

    def configure_white_balance(
        self,
        enabled: object,
        roi: object = WHITE_BALANCE_UNSET,
        generation: object = None,
    ) -> dict[str, object]:
        if not isinstance(enabled, bool):
            raise ValueError("auto_white_balance must be true or false")
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise ValueError("generation must be an integer")
        normalized_roi = WHITE_BALANCE_UNSET if roi is WHITE_BALANCE_UNSET else normalize_white_balance_roi(roi)
        with self._state_lock:
            if generation != self._generation:
                raise WhiteBalanceError("The preview changed; pick the gray region again")
            stream = self._stream
            if stream is None:
                raise WhiteBalanceError("No camera preview is active")
            stream.configure_white_balance(enabled, normalized_roi)
            self._sync_color_settings_locked(stream)
        return self.status()

    def status(self) -> dict[str, object]:
        with self._state_lock:
            stream = self._stream
            state = self._preview_state
            selected = self._selected_camera
            selected_profile = self._selected_profile
            generation = self._generation
            error = self._last_error
            scan_state = self._scan_state
            scan_error = self._scan_error
            red_gain = self._red_gain
            green_gain = self._green_gain
            blue_gain = self._blue_gain
            auto_white_balance = self._auto_white_balance
            white_balance_roi = self._white_balance_roi
        if stream is not None:
            payload = stream.status()
            payload["selected_camera"] = selected
            payload["profile"] = selected_profile
            payload["selected_profile"] = selected_profile
            payload["generation"] = generation
            return payload
        if state == "idle" and scan_state == "scanning":
            state = "scanning"
        elif state == "idle" and scan_state == "error":
            state = "error"
            error = scan_error
        return {
            "state": state,
            "healthy": False,
            "camera_index": selected,
            "selected_camera": selected,
            "profile": selected_profile,
            "selected_profile": selected_profile,
            "generation": generation,
            "requested": {
                "width": PREVIEW_PROFILES[selected_profile][0],
                "height": PREVIEW_PROFILES[selected_profile][1],
                "fps": self.fps,
                "stream_fps": PREVIEW_PROFILES[selected_profile][2],
                "fourcc": self.fourcc,
            },
            "actual": {
                "width": 0,
                "height": 0,
                "fps": 0.0,
                "capture_fps": 0.0,
                "backend": "?",
                "capture": "?",
            },
            "jpeg": {
                "quality": self.jpeg_quality,
                "sequence": 0,
                "bytes": 0,
                "age_ms": None,
                "mbps": 0.0,
            },
            "color": {
                "red_gain": red_gain,
                "green_gain": green_gain,
                "blue_gain": blue_gain,
                "auto_white_balance": auto_white_balance,
                "white_balance_roi": white_balance_roi_payload(white_balance_roi),
                "auto_white_balance_state": "waiting" if auto_white_balance else "disabled",
                "white_balance_error": None,
                "white_balance_sample": None,
            },
            "error": error,
        }

    def shutdown(self) -> None:
        with self._state_lock:
            self._closed = True
            stream = self._stream
            candidate = self._candidate
            if stream is not None:
                self._sync_color_settings_locked(stream)
            self._stream = None
            self._candidate = None
            self._preview_state = "stopped"
        if candidate is not None:
            candidate.stop()
        if stream is not None and stream is not candidate:
            stream.stop()

    def _create_stream(self, camera_index: int, profile: str) -> CameraStream:
        width, height, stream_fps = PREVIEW_PROFILES[profile]
        return CameraStream(
            camera_index=camera_index,
            fps=self.fps,
            fourcc=self.fourcc,
            jpeg_quality=self.jpeg_quality,
            width=width,
            height=height,
            stream_fps=stream_fps,
            red_gain=self._red_gain,
            green_gain=self._green_gain,
            blue_gain=self._blue_gain,
            auto_white_balance=self._auto_white_balance,
            white_balance_roi=self._white_balance_roi,
        )

    def _sync_color_settings_locked(self, stream: CameraStream) -> None:
        gains, enabled, roi = stream.color_settings()
        self._red_gain, self._green_gain, self._blue_gain = gains
        self._auto_white_balance = enabled
        self._white_balance_roi = roi


class APIRequestError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class CameraHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, address: tuple[str, int], manager: CameraManager, max_stream_clients: int = 4) -> None:
        self.manager = manager
        self._stream_slots = threading.BoundedSemaphore(max_stream_clients)
        self._request_slots = threading.BoundedSemaphore(max_stream_clients + 8)
        super().__init__(address, CameraRequestHandler)

    def acquire_stream(self) -> bool:
        return self._stream_slots.acquire(blocking=False)

    def release_stream(self) -> None:
        self._stream_slots.release()

    def process_request(self, request: object, client_address: object) -> None:
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: object, client_address: object) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class CameraRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MarlinScanCamera/1"

    @property
    def manager(self) -> CameraManager:
        return self.server.manager  # type: ignore[attr-defined]

    def setup(self) -> None:
        self.request.settimeout(10.0)
        super().setup()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self._send_bytes(200, "text/html; charset=utf-8", INDEX_HTML)
        elif path == "/stream.mjpg":
            self._send_stream()
        elif path in {"/snapshot.jpg", "/frame.jpg"}:
            self._send_snapshot()
        elif path == "/status.json":
            self._send_json(200, self.manager.status())
        elif path == "/cameras.json":
            self._send_json(200, self.manager.cameras())
        elif path == "/healthz":
            status = self.manager.status()
            self._send_json(200 if status["healthy"] else 503, status)
        elif path == "/favicon.ico":
            self._send_bytes(204, "image/x-icon", b"")
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            self._require_mutation_header()
            if path == "/cameras/scan":
                self._send_json(200, self.manager.scan())
            elif path == "/camera":
                payload = self._read_json()
                camera_index = payload.get("camera_index")
                if isinstance(camera_index, bool) or not isinstance(camera_index, int):
                    raise APIRequestError(400, "camera_index must be an integer")
                profile = payload.get("profile")
                if profile is not None and not isinstance(profile, str):
                    raise APIRequestError(400, "profile must be a string")
                self._send_json(200, self.manager.select(camera_index, profile))
            elif path == "/settings":
                payload = self._read_json()
                self._send_json(
                    200,
                    self.manager.set_color_gains(
                        payload.get("red_gain"),
                        payload.get("green_gain"),
                        payload.get("blue_gain"),
                    ),
                )
            elif path == "/white-balance":
                payload = self._read_json()
                roi = payload.get("white_balance_roi", WHITE_BALANCE_UNSET)
                self._send_json(
                    200,
                    self.manager.configure_white_balance(
                        payload.get("auto_white_balance"),
                        roi,
                        payload.get("generation"),
                    ),
                )
            else:
                self._send_json(404, {"error": "not found"})
        except APIRequestError as exc:
            self.close_connection = True
            self._send_json(exc.status, {"error": str(exc)})
        except CameraBusyError as exc:
            self._send_json(409, {"error": str(exc)})
        except CameraSelectionError as exc:
            self._send_json(422, {"error": str(exc)})
        except WhiteBalanceError as exc:
            self._send_json(422, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})

    def do_DELETE(self) -> None:
        path = urlsplit(self.path).path
        try:
            self._require_mutation_header()
            if path == "/camera":
                self._send_json(200, self.manager.stop())
            else:
                self._send_json(404, {"error": "not found"})
        except APIRequestError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except CameraBusyError as exc:
            self._send_json(409, {"error": str(exc)})
        except CameraSelectionError as exc:
            self._send_json(422, {"error": str(exc)})

    def _send_stream(self) -> None:
        stream = self.manager.current_stream()
        if stream is None:
            self._send_json(503, {"error": "No camera preview is active"})
            return
        server = self.server
        if not server.acquire_stream():  # type: ignore[attr-defined]
            self._send_json(503, {"error": "maximum stream clients reached"})
            return
        try:
            self.connection.settimeout(10.0)
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            sequence = 0
            while True:
                item = stream.wait_for_jpeg(sequence, timeout=5.0)
                if item is None:
                    if stream.status()["state"] != "streaming":
                        return
                    continue
                sequence, jpeg = item
                header = (
                    f"--{BOUNDARY}\r\n"
                    "Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpeg)}\r\n\r\n"
                ).encode("ascii")
                self.wfile.write(header)
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, RuntimeError, socket.timeout):
            return
        finally:
            server.release_stream()  # type: ignore[attr-defined]

    def _send_snapshot(self) -> None:
        stream = self.manager.current_stream()
        if stream is None:
            self._send_json(503, {"error": "No camera preview is active"})
            return
        try:
            jpeg = stream.latest_jpeg()
        except RuntimeError as exc:
            self._send_json(503, {"error": str(exc)})
            return
        self._send_bytes(200, "image/jpeg", jpeg)

    def _read_json(self) -> dict[str, object]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise APIRequestError(415, "Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise APIRequestError(411, "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise APIRequestError(400, "Invalid Content-Length") from exc
        if not 1 <= length <= 1024:
            raise APIRequestError(413, "JSON body must be between 1 and 1024 bytes")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise APIRequestError(400, "Incomplete JSON body")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise APIRequestError(400, "Invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise APIRequestError(400, "JSON body must be an object")
        return payload

    def _require_mutation_header(self) -> None:
        if self.headers.get("X-MarlinScan-Request") != "1":
            raise APIRequestError(403, "Missing X-MarlinScan-Request header")

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", body)

    def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.info("%s - %s", self.address_string(), format % args)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def normalize_color_gain(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    parsed = float(value)
    if not math.isfinite(parsed) or not 0.25 <= parsed <= 2.0:
        raise ValueError(f"{name} must be between 0.25 and 2.0")
    return parsed


def normalize_white_balance_roi(value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
        raise ValueError("white_balance_roi must contain x, y, width, and height")
    parsed: list[float] = []
    for name in ("x", "y", "width", "height"):
        item = value[name]
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"white_balance_roi.{name} must be a number")
        number = float(item)
        if not math.isfinite(number):
            raise ValueError(f"white_balance_roi.{name} must be finite")
        parsed.append(number)
    x, y, width, height = parsed
    if x < 0.0 or y < 0.0 or width < 0.01 or height < 0.01:
        raise ValueError("white_balance_roi must be inside the frame and at least 1% wide and high")
    if x + width > 1.0 + 1e-9 or y + height > 1.0 + 1e-9:
        raise ValueError("white_balance_roi must stay inside the frame")
    return x, y, width, height


def white_balance_roi_payload(
    roi: tuple[float, float, float, float] | None,
) -> dict[str, float] | None:
    if roi is None:
        return None
    x, y, width, height = roi
    return {"x": x, "y": y, "width": width, "height": height}


def white_balance_sample_payload(
    sample: tuple[float, float, float] | None,
) -> dict[str, float] | None:
    if sample is None:
        return None
    red, green, blue = sample
    return {"red": red, "green": green, "blue": blue}


def color_gain(value: str) -> float:
    try:
        return normalize_color_gain(float(value), "color gain")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def jpeg_quality(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("must be between 1 and 100")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pick and preview a UVC camera over HTTP.")
    parser.add_argument("--host", default="127.0.0.1", help="listen address (default: 127.0.0.1)")
    parser.add_argument("--port", type=positive_int, default=8000, help="HTTP port (default: 8000)")
    parser.add_argument("--fps", type=positive_int, default=30, help="requested camera FPS (default: 30)")
    parser.add_argument("--fourcc", default="MJPG", help="requested camera FourCC (default: MJPG)")
    parser.add_argument("--profile", choices=PREVIEW_PROFILES, default=DEFAULT_PROFILE, help="initial preview profile")
    parser.add_argument("--jpeg-quality", type=jpeg_quality, default=80, help="stream JPEG quality 1-100 (default: 80)")
    parser.add_argument("--red-gain", type=color_gain, default=1.0, help="software red gain (default: 1.0)")
    parser.add_argument("--green-gain", type=color_gain, default=1.0, help="software green gain (default: 1.0)")
    parser.add_argument("--blue-gain", type=color_gain, default=1.0, help="software blue gain (default: 1.0)")
    parser.add_argument("--max-clients", type=positive_int, default=4, help="maximum MJPEG viewers (default: 4)")
    parser.add_argument("--scan-limit", type=positive_int, default=6, help="camera indices to scan (default: 6)")
    args = parser.parse_args(argv)
    args.fourcc = args.fourcc.strip().upper()
    if len(args.fourcc) != 4:
        parser.error("--fourcc must contain exactly four characters")
    return args


def serve(args: argparse.Namespace) -> int:
    manager = CameraManager(
        fps=args.fps,
        fourcc=args.fourcc,
        jpeg_quality=args.jpeg_quality,
        scan_limit=args.scan_limit,
        profile=args.profile,
        red_gain=args.red_gain,
        green_gain=args.green_gain,
        blue_gain=args.blue_gain,
    )
    server: CameraHTTPServer | None = None
    previous_handlers: dict[int, object] = {}
    stop = threading.Event()

    try:
        server = CameraHTTPServer((args.host, args.port), manager, args.max_clients)
        server.timeout = 0.5

        def stop_server(_signum: int, _frame: object) -> None:
            stop.set()

        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, stop_server)

        LOGGER.info("Viewer: http://127.0.0.1:%d/", server.server_address[1])
        LOGGER.info("Listening on http://%s:%d/", server.server_address[0], server.server_address[1])
        LOGGER.info("Open the viewer to scan, pick, and preview a camera")

        while not stop.is_set():
            server.handle_request()
    finally:
        manager.shutdown()
        if server is not None:
            server.server_close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return serve(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
