"use strict";

// The display is noVNC's live framebuffer. No mock desktop is rendered here.
const ui = Object.fromEntries([
  "console-status", "console-error", "connect-button", "focus-button", "fullscreen-button",
  "disconnect-button", "console-display", "console-empty", "console-empty-title", "console-detail",
  "console-surface", "credentials-form", "vnc-password", "start-menu-button", "run-app-button",
  "shortcut-status", "ctrl-alt-del-button"
].map((id) => [id, document.getElementById(id)]));
const token = document.querySelector('meta[name="nutcracker-token"]')?.content || "";
const isLocalLauncher = ["localhost", "127.0.0.1", "[::1]", "::1"].includes(location.hostname)
  && location.protocol === "http:" && token && token !== "__NUTCRACKER_TOKEN__";
let client = null;
let connected = false;
let connecting = false;
let intentionalDisconnect = false;
let connectionTimer = null;
let fullscreenRequest = 0;

function unlockKeyboard() {
  try { navigator.keyboard?.unlock?.(); } catch { /* Ordinary input stays available. */ }
}

function releaseGuestModifiers() {
  if (!connected || !client) return;
  for (const [keysym, code] of [
    [0xffe1, "ShiftLeft"], [0xffe2, "ShiftRight"],
    [0xffe3, "ControlLeft"], [0xffe4, "ControlRight"],
    [0xffe9, "AltLeft"], [0xffea, "AltRight"],
    [0xffeb, "MetaLeft"], [0xffec, "MetaRight"]
  ]) client.sendKey(keysym, code, false);
}

function releaseGuestKeys() {
  if (!connected || !client) return;
  client.releaseKeys();
  releaseGuestModifiers();
}

function leaveFullscreen() {
  fullscreenRequest++;
  unlockKeyboard();
  if (document.fullscreenElement) return document.exitFullscreen().catch(() => {});
  return Promise.resolve();
}

function status(text, tone = "") {
  const badge = ui["console-status"];
  badge.className = `status-pill${tone ? ` ${tone}` : ""}`;
  const dot = document.createElement("span");
  dot.className = "tiny-dot";
  badge.replaceChildren(dot, document.createTextNode(text));
}

function error(message = "") {
  ui["console-error"].textContent = message;
  ui["console-error"].hidden = !message;
}

function controls() {
  ui["connect-button"].disabled = !isLocalLauncher || connecting || connected;
  ui["connect-button"].textContent = client || connected ? "Connected" : "Connect";
  ui["disconnect-button"].disabled = !client;
  ui["focus-button"].disabled = !connected;
  ui["start-menu-button"].disabled = !connected;
  ui["run-app-button"].disabled = !connected;
  ui["ctrl-alt-del-button"].disabled = !connected;
  ui["fullscreen-button"].disabled = !connected || !document.fullscreenEnabled;
  ui["console-display"].tabIndex = connected ? 0 : -1;
  ui["console-surface"].setAttribute("aria-busy", String(connecting));
}

function empty(title, message) {
  ui["console-empty"].hidden = false;
  ui["console-empty-title"].textContent = title;
  ui["console-detail"].textContent = message;
}

function clearConnectionTimer() {
  clearTimeout(connectionTimer);
  connectionTimer = null;
}

function safeWebSocketUrl(value) {
  if (typeof value !== "string") throw new Error("The launcher did not provide a console connection.");
  const url = new URL(value);
  // Only the launcher's signed, fixed local bridge is accepted. No host input exists.
  if (url.protocol !== "ws:" || url.hostname !== location.hostname || url.pathname !== "/websockify" || url.username || url.password) {
    throw new Error("The launcher returned an unexpected console address. Restart the local launcher.");
  }
  return url.href;
}

async function connect() {
  if (!isLocalLauncher || connecting || connected) return;
  error();
  connecting = true;
  intentionalDisconnect = false;
  status("Checking console", "transitioning");
  empty("Checking your local console…", "The launcher will connect only to the configured VM on this Mac.");
  ui["credentials-form"].hidden = true;
  ui["vnc-password"].value = "";
  controls();
  const controller = new AbortController();
  const requestTimer = setTimeout(() => controller.abort(), 25000);
  try {
    const response = await fetch("/api/console", { credentials: "same-origin", cache: "no-store", signal: controller.signal });
    if (!(response.headers.get("content-type") || "").includes("application/json")) throw new Error("The local launcher did not return a console status.");
    const data = await response.json();
    if (!response.ok || data?.available !== true || data.transport !== "vnc") {
      throw new Error(typeof data?.error === "string" && data.error ? data.error : "The console is unavailable. Start the configured VM in UTM, then connect again.");
    }
    const socketUrl = safeWebSocketUrl(data.url);
    const { default: RFB, initLogging } = await import("./vendor/novnc/rfb.bundle.js");
    // Keep signed bridge URLs and session details out of application logging.
    initLogging("none");
    status("Connecting", "transitioning");
    empty("Connecting to your VM…", "Waiting for the live display from UTM. Windows may still be starting.");
    // Adapter for the pinned noVNC 1.7.0 runtime: release held keys before
    // moving focus, including ordinary game keys whose key-up could be missed.
    class LocalRFB extends RFB {
      releaseKeys() { this._keyboard._allKeysUp(); }
    }
    const rfb = new LocalRFB(ui["console-display"], socketUrl, { shared: true });
    client = rfb;
    rfb.scaleViewport = true;
    rfb.resizeSession = false;
    rfb.focusOnClick = true;
    rfb.viewOnly = false;
    rfb.background = "#080a0f";
    rfb.addEventListener("connect", () => {
      if (client !== rfb) return;
      clearConnectionTimer();
      connecting = false;
      connected = true;
      status("Live VM display", "running");
      ui["console-empty"].hidden = true;
      ui["credentials-form"].hidden = true;
      ui["vnc-password"].value = "";
      controls();
    });
    rfb.addEventListener("disconnect", (event) => {
      if (client !== rfb) return;
      clearConnectionTimer();
      leaveFullscreen();
      client = null;
      connected = false;
      connecting = false;
      ui["credentials-form"].hidden = true;
      ui["vnc-password"].value = "";
      status("Disconnected");
      empty("Console disconnected.", intentionalDisconnect ? "The viewer is closed. Your VM keeps running; connect again to reopen its display." : "The live connection ended. Check the VM in UTM, then connect again.");
      if (!intentionalDisconnect && !event.detail.clean && ui["console-error"].hidden) error("The connection to UTM ended unexpectedly. Your VM may still be running.");
      controls();
    });
    rfb.addEventListener("credentialsrequired", (event) => {
      if (client !== rfb) return;
      clearConnectionTimer();
      if (event.detail.types.some((type) => type !== "password")) {
        error("This VNC server requires credentials that this local console does not support. Open your VM in UTM.");
        rfb.disconnect();
        return;
      }
      status("Password required", "transitioning");
      empty("VNC password required.", "Enter the VM’s VNC password in the form below. It is not stored.");
      ui["credentials-form"].hidden = false;
      ui["vnc-password"].focus();
    });
    rfb.addEventListener("securityfailure", () => {
      if (client !== rfb) return;
      error("VNC authentication failed. Check the VM’s VNC settings in UTM and connect again.");
    });
    rfb.addEventListener("serververification", () => {
      if (client !== rfb) return;
      error("This console cannot verify the VNC server’s requested identity. Open the configured VM in UTM.");
      rfb.disconnect();
    });
    connectionTimer = setTimeout(() => {
      if (client !== rfb || connected) return;
      error("The VM did not complete the display connection. Check UTM and connect again.");
      rfb.disconnect();
    }, 30000);
    controls();
  } catch (cause) {
    connecting = false;
    connected = false;
    client = null;
    const message = cause.name === "AbortError" ? "The local launcher did not respond. Check that it is running, then connect again." : cause.message || "Could not connect to the local console.";
    error(message);
    status("Unavailable");
    empty("Console unavailable.", "No VM display is connected. Check the launcher and UTM, then try again.");
    controls();
  } finally {
    clearTimeout(requestTimer);
  }
}

ui["connect-button"].addEventListener("click", connect);
ui["focus-button"].addEventListener("click", () => {
  if (!connected) return;
  releaseGuestKeys();
  client.focus();
});
ui["start-menu-button"].addEventListener("click", () => {
  if (!connected) return;
  releaseGuestKeys();
  client.sendKey(0xffeb, "MetaLeft");
  client.focus();
});
ui["run-app-button"].addEventListener("click", () => {
  if (!connected) return;
  releaseGuestKeys();
  const current = client;
  current.sendKey(0xffeb, "MetaLeft", true);
  try { current.sendKey(0x72, "KeyR"); }
  finally { current.sendKey(0xffeb, "MetaLeft", false); }
  current.focus();
});
ui["ctrl-alt-del-button"].addEventListener("click", () => {
  if (!connected) return;
  releaseGuestKeys();
  client.sendCtrlAltDel();
  client.focus();
});
ui["console-display"].addEventListener("focus", () => { if (connected) client.focus(); });
ui["disconnect-button"].addEventListener("click", () => {
  if (!client) return;
  intentionalDisconnect = true;
  status("Disconnecting", "transitioning");
  clearConnectionTimer();
  releaseGuestKeys();
  leaveFullscreen();
  client.disconnect();
});
ui["fullscreen-button"].addEventListener("click", async () => {
  if (!connected || !document.fullscreenEnabled) return;
  if (document.fullscreenElement) { await leaveFullscreen(); return; }
  const request = ++fullscreenRequest;
  const current = client;
  // Request capture from this user gesture. It only takes effect in fullscreen.
  let lock = Promise.resolve(false);
  try {
    if (navigator.keyboard?.lock) lock = navigator.keyboard.lock().then(() => true, () => false);
  } catch { /* Browser-reserved keys stay with the host if capture is unavailable. */ }
  try {
    await ui["console-display"].requestFullscreen({ navigationUI: "hide" });
    if (request !== fullscreenRequest) {
      await lock;
      if (!document.fullscreenElement) unlockKeyboard();
      return;
    }
    if (!connected || client !== current) {
      await leaveFullscreen();
      await lock;
      if (!document.fullscreenElement) unlockKeyboard();
      return;
    }
    current.focus();
    const captured = await lock;
    if (request !== fullscreenRequest || !document.fullscreenElement || !connected) {
      if (request === fullscreenRequest || !document.fullscreenElement) unlockKeyboard();
      return;
    }
    ui["shortcut-status"].textContent = captured
      ? "Fullscreen captures keyboard shortcuts allowed by your Mac. Hold Esc for two seconds or press Ctrl + Alt + Shift + M to leave."
      : "Use Control for Windows shortcuts. This browser may keep some system shortcuts; use the Windows shortcut buttons when needed. Press Esc or Ctrl + Alt + Shift + M to leave fullscreen.";
  } catch {
    if (request === fullscreenRequest) {
      fullscreenRequest++;
      unlockKeyboard();
      error("The browser could not open fullscreen. You can keep using the display in this window.");
    }
    await lock;
    if (!document.fullscreenElement) unlockKeyboard();
  }
});
document.addEventListener("fullscreenchange", () => {
  ui["fullscreen-button"].lastChild.textContent = document.fullscreenElement ? "Exit fullscreen" : "Fullscreen";
  if (!document.fullscreenElement) {
    fullscreenRequest++;
    unlockKeyboard();
    releaseGuestKeys();
  } else if (connected) client.focus();
});
ui["credentials-form"].addEventListener("submit", (event) => {
  event.preventDefault();
  if (!client || !ui["vnc-password"].value) return;
  client.sendCredentials({ password: ui["vnc-password"].value });
  ui["vnc-password"].value = "";
  ui["credentials-form"].hidden = true;
  status("Authenticating", "transitioning");
  empty("Authenticating to VNC…", "Waiting for the VM to accept this connection.");
  clearConnectionTimer();
  const current = client;
  connectionTimer = setTimeout(() => {
    if (client !== current || connected) return;
    error("VNC authentication did not finish. Check UTM and connect again.");
    current.disconnect();
  }, 30000);
});
document.addEventListener("keydown", (event) => {
  if (connected && event.ctrlKey && event.altKey && event.shiftKey && event.code === "KeyM") {
    event.preventDefault();
    event.stopImmediatePropagation();
    releaseGuestKeys();
    client.blur();
    leaveFullscreen().then(() => ui["focus-button"].focus());
  }
}, true);
window.addEventListener("pagehide", () => {
  clearConnectionTimer();
  fullscreenRequest++;
  unlockKeyboard();
  releaseGuestKeys();
  ui["vnc-password"].value = "";
  if (client) client.disconnect();
});
controls();
if (isLocalLauncher) connect();
