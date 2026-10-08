"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const ui = {
    refresh: byId("refresh-button"), offline: byId("offline-notice"), error: byId("error-notice"),
    notice: byId("action-notice"), state: byId("machine-state"), description: byId("machine-description"),
    select: byId("vm-select"), start: byId("start-button"), shutdown: byId("shutdown-button"),
    hint: byId("machine-hint"), host: byId("host-description"), architecture: byId("host-architecture"),
    memory: byId("host-memory"), storage: byId("host-storage"), utm: byId("utm-status"), openUtm: byId("open-utm-button"),
    console: byId("console-button"), consoleHint: byId("console-hint"), logout: byId("logout-button")
  };
  const token = document.querySelector('meta[name="nutcracker-token"]')?.content || "";
  const localHost = ["localhost", "127.0.0.1", "[::1]", "::1"].includes(location.hostname);
  const isLauncher = ((localHost && location.protocol === "http:") || location.protocol === "https:")
    && token && token !== "__NUTCRACKER_TOKEN__";
  let snapshot = null;
  let busy = false;
  let connected = false;
  let consoleAvailable = false;
  const labels = {
    stopped: "Stopped", started: "Running", running: "Running", starting: "Starting", pausing: "Pausing",
    paused: "Paused", resuming: "Resuming", stopping: "Shutting down", unknown: "Unknown"
  };

  function selectedVm() {
    return snapshot?.vms.find((vm) => vm.id === ui.select.value) || null;
  }

  function setMachineState(label, style = "") {
    ui.state.className = `status-pill${style ? ` ${style}` : ""}`;
    ui.state.replaceChildren();
    const dot = document.createElement("span");
    dot.className = "tiny-dot";
    ui.state.append(dot, document.createTextNode(label));
  }

  function showError(message) {
    ui.error.textContent = message || "";
    ui.error.hidden = !message;
  }

  function updateControls() {
    const vm = selectedVm();
    ui.refresh.disabled = !isLauncher || busy;
    ui.select.disabled = !connected || !snapshot?.utm_installed || !snapshot.vms.length || busy;
    ui.start.disabled = !connected || busy || !snapshot?.utm_installed || !vm || vm.status !== "stopped";
    ui.shutdown.disabled = !connected || busy || !snapshot?.utm_installed || !vm || !["started", "running", "paused"].includes(vm.status);
    ui.openUtm.disabled = !connected || busy || !snapshot?.utm_installed;
    ui.console.disabled = !connected || busy || !consoleAvailable;
    ui.refresh.setAttribute("aria-busy", String(busy));
    if (!connected) return;
    if (!snapshot.utm_installed) {
      setMachineState("Setup required");
      ui.description.textContent = "Install the free version of UTM to create your Windows VM.";
      ui.hint.textContent = "Start with step 1 below, then refresh this page after installing UTM.";
    } else if (!snapshot.vms.length) {
      setMachineState("Setup required");
      ui.description.textContent = "UTM is installed. Create a Windows VM to get started.";
      ui.hint.textContent = "Follow steps 2 and 3 below, then refresh to find your new VM.";
    } else if (!vm) {
      setMachineState("Select a VM", "ready");
      ui.description.textContent = "Choose your Windows VM from UTM to see its current state.";
      ui.hint.textContent = "Choose a Windows VM. This launcher also lists any other VMs you have in UTM.";
    } else {
      const running = ["started", "running"].includes(vm.status);
      const transitioning = ["starting", "pausing", "resuming", "stopping"].includes(vm.status);
      setMachineState(labels[vm.status] || "Unknown", running ? "running" : transitioning ? "transitioning" : "ready");
      ui.description.textContent = `${vm.name} · ${labels[vm.status] || "Unknown state"}`;
      if (running) ui.hint.textContent = "Your VM is running. Open Windows in your browser to use the desktop and launch Steam.";
      else if (vm.status === "paused") ui.hint.textContent = "Resume this VM in UTM to use Windows. A graceful shutdown requests that Windows close normally.";
      else if (vm.status === "stopped") ui.hint.textContent = "Start opens your VM in UTM. Steam runs inside Windows, after you install it.";
      else if (transitioning) ui.hint.textContent = "UTM is changing this VM’s state. Refresh status after a moment to check the result.";
      else ui.hint.textContent = "UTM returned an unknown state. Open UTM to check the VM before using its controls.";
    }
  }

  function formatGb(value) {
    return typeof value === "number" && Number.isFinite(value) && value >= 0
      ? `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(value)} GB`
      : "Unavailable";
  }

  function renderStatus(data) {
    const previous = ui.select.value;
    snapshot = data;
    connected = true;
    ui.offline.hidden = true;
    ui.host.textContent = typeof data.host?.os === "string" ? data.host.os : "Host operating system unavailable";
    const architecture = data.host?.architecture;
    ui.architecture.textContent = architecture === "arm64" || architecture === "aarch64" ? "Apple silicon" : typeof architecture === "string" ? architecture : "Unavailable";
    ui.memory.textContent = formatGb(data.host?.memory_gb);
    ui.storage.textContent = formatGb(data.host?.disk_free_gb);
    ui.utm.textContent = data.utm_installed ? "UTM · Installed" : "UTM · Setup required";
    ui.select.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = !data.utm_installed ? "Install UTM to continue" : data.vms.length ? "Select your Windows VM…" : "No VMs found in UTM";
    ui.select.append(placeholder);
    for (const vm of data.vms) {
      const option = document.createElement("option");
      option.value = vm.id;
      option.textContent = `${vm.name} (${labels[vm.status] || "Unknown"})`;
      ui.select.append(option);
    }
    if (data.vms.some((vm) => vm.id === previous)) ui.select.value = previous;
    showError(typeof data.error === "string" ? data.error : "");
    updateControls();
  }

  function setOffline() {
    connected = false;
    snapshot = null;
    consoleAvailable = false;
    ui.consoleHint.textContent = "Connect the local launcher to check the live browser console.";
    ui.offline.hidden = false;
    setMachineState("Offline");
    ui.description.textContent = "Connect the local launcher to see your Windows VMs.";
    ui.hint.textContent = "Open the live Windows screen in your browser after starting the configured VM.";
    ui.select.replaceChildren(new Option("Local launcher offline", ""));
    ui.host.textContent = "Host details appear when connected.";
    ui.architecture.textContent = "—";
    ui.memory.textContent = "—";
    ui.storage.textContent = "—";
    ui.utm.textContent = "UTM · Not connected";
    updateControls();
  }

  async function request(path, body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), body ? 45000 : 25000);
    try {
      const options = { signal: controller.signal, credentials: "same-origin", cache: "no-store" };
      if (body) {
        options.method = "POST";
        options.headers = { "Content-Type": "application/json", "X-Nutcracker-Token": token };
        options.body = JSON.stringify(body);
      }
      const response = await fetch(path, options);
      if (response.status === 401) {
        location.replace("/login.html");
        throw new Error("Your session ended. Sign in again.");
      }
      const contentType = response.headers.get("content-type") || "";
      if (!contentType.includes("application/json")) throw new Error("The local launcher did not return a valid response.");
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.error === "string" ? data.error : `The local launcher could not complete the request (${response.status}).`);
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The local launcher took too long to respond. Check UTM, then refresh status.");
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  async function refreshStatus({ quiet = false } = {}) {
    if (!isLauncher) return;
    busy = true;
    updateControls();
    if (!quiet) ui.notice.textContent = "Checking your Mac and UTM…";
    try {
      const data = await request("/api/status");
      if (!data || !data.host || typeof data.utm_installed !== "boolean" || !Array.isArray(data.vms)) throw new Error("The local launcher returned an incomplete status. Restart the launcher and try again.");
      if (data.vms.some((vm) => !vm || typeof vm.id !== "string" || typeof vm.name !== "string" || typeof vm.status !== "string")) throw new Error("The local launcher returned an invalid VM list. Check UTM and refresh status.");
      renderStatus(data);
      await refreshConsoleAvailability();
      if (!quiet) ui.notice.textContent = "Status refreshed from your Mac.";
    } catch (error) {
      setOffline();
      showError(error.message || "Could not reach the local launcher. Start it on your Mac and refresh status.");
      if (!quiet) ui.notice.textContent = "";
    } finally {
      busy = false;
      updateControls();
    }
  }

  async function refreshConsoleAvailability() {
    consoleAvailable = false;
    try {
      const data = await request("/api/console");
      consoleAvailable = data?.available === true && data.transport === "vnc" && typeof data.url === "string";
      ui.consoleHint.textContent = consoleAvailable
        ? "Live display of the configured VM. No audio. Browser access does not confirm Steam compatibility."
        : typeof data?.error === "string" ? data.error : "Browser console unavailable. Start the configured VM, then refresh status.";
    } catch (error) {
      ui.consoleHint.textContent = "Browser console unavailable. Check the local launcher and refresh status.";
    }
    updateControls();
  }

  async function performAction(path, body, progress) {
    if (!connected || busy) return;
    busy = true;
    updateControls();
    showError("");
    ui.notice.textContent = progress;
    try {
      const data = await request(path, body);
      if (data.ok !== true) throw new Error(typeof data.error === "string" ? data.error : "UTM did not confirm the request. Open UTM to check your VM.");
      ui.notice.textContent = typeof data.message === "string" ? data.message : "Request sent to UTM.";
      await refreshStatus({ quiet: true });
    } catch (error) {
      showError(error.message || "Could not complete the request. Open UTM to check your VM.");
      ui.notice.textContent = "";
    } finally {
      busy = false;
      updateControls();
    }
  }

  ui.refresh.addEventListener("click", () => refreshStatus());
  ui.logout.addEventListener("click", async () => {
    ui.logout.disabled = true;
    try {
      await request("/api/logout", {});
      location.replace("/login.html");
    } catch (error) {
      showError(error.message || "Could not sign out. Try again.");
      ui.logout.disabled = false;
    }
  });
  ui.select.addEventListener("change", () => { ui.notice.textContent = ""; updateControls(); });
  ui.start.addEventListener("click", () => {
    const vm = selectedVm();
    if (vm && !ui.start.disabled) performAction("/api/vm/start", { id: vm.id }, `Requesting startup for ${vm.name}…`);
  });
  ui.shutdown.addEventListener("click", () => {
    const vm = selectedVm();
    if (vm && !ui.shutdown.disabled) performAction("/api/vm/shutdown", { id: vm.id }, `Requesting a graceful shutdown for ${vm.name}…`);
  });
  ui.openUtm.addEventListener("click", () => {
    if (!ui.openUtm.disabled) performAction("/api/open-utm", {}, "Opening UTM on your Mac…");
  });
  ui.console.addEventListener("click", () => {
    if (!ui.console.disabled) location.assign("console.html");
  });

  const navLinks = [...document.querySelectorAll(".nav-link")];
  function activateNavigation(hash) {
    for (const link of navLinks) {
      const active = link.getAttribute("href") === hash;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "location");
      else link.removeAttribute("aria-current");
    }
    const active = navLinks.find((link) => link.getAttribute("href") === hash);
    if (active) document.querySelector(".breadcrumb strong").textContent = hash === "#setup" ? "Setup guide" : hash === "#compatibility" ? "Game compatibility" : "Overview";
  }
  window.addEventListener("hashchange", () => activateNavigation(location.hash || "#overview"));
  activateNavigation(location.hash || "#overview");
  if (isLauncher) refreshStatus({ quiet: true });
  else setOffline();
})();
