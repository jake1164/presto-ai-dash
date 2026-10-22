"use strict";
(() => {
  const element = (id) => document.getElementById(id);
  const TABS = ["services", "status", "settings"];
  // The URL of each tab. The hash is what a link points at.
  const TAB_HASHES = { services: "#services", status: "#status", settings: "#settings" };
  const STORAGE_KEY = "presto-ai-dash.tab";
  let csrfToken = "", unlocked = false, catalog = [], sources = [], editing = null;
  let busy = false;
  let dragging = null;
  let ingestState = null, tokenReveal = null, displayState = null;
  let alertsState = null;
  // The master switch: alerts on or off for every service. It is kept apart
  // from the form, so a background refresh never rewrites what the reader
  // is typing.
  let masterOn = true;
  // What the master switch was the last time the page was drawn, so a change
  // of it can repaint the account cards at once.
  let lastMasterOn = null;
  // One service's own alert settings, by service id, and the values the
  // reader has typed but not saved yet. Both survive the ten-second refresh.
  const serviceAlertStates = new Map();
  const serviceAlertDrafts = new Map();
  const serviceAlertLoading = new Set();
  let statusState = null, configState = null;
  let activeTab = "services";
  // Which account cards are open. Kept across the ten-second refresh, so the
  // list does not collapse under the reader.
  const expanded = new Set();

  // -- help links ------------------------------------------------------------

  // The documentation site. One base URL and one map: every help link is
  // built from these two, so there is nowhere else to change a link and no
  // URL anywhere else in the page.
  const DOCS_BASE = "https://jake1164.github.io/presto-ai-dash/";
  const HELP = {
    "tab-services": {page: "guides/admin-page.md", anchor: "services"},
    "tab-status": {page: "guides/admin-page.md", anchor: "status"},
    "tab-settings": {page: "guides/admin-page.md", anchor: "settings"},
    "services": {page: "guides/admin-page.md", anchor: "adding-and-editing-an-account"},
    "add-source": {page: "guides/admin-page.md", anchor: "adding-and-editing-an-account"},
    "account": {page: "guides/admin-page.md", anchor: "the-accounts-own-actions"},
    "status": {page: "guides/admin-page.md", anchor: "status"},
    "settings-panel": {page: "guides/admin-page.md", anchor: "settings"},
    "home-display": {page: "guides/home-display.md", anchor: "the-setting"},
    "remote-reports": {page: "concepts/remote-reports.md", anchor: "turning-it-on"},
    "remote-token": {page: "guides/remote-collectors.md", anchor: "rotating-and-revoking"},
    "alerts": {page: "guides/alerts-and-the-bell.md", anchor: "the-settings-behind-it"},
    "alert-thresholds": {page: "concepts/alerts.md", anchor: "what-raises-an-alert"},
    "alert-sound": {page: "concepts/alerts.md", anchor: "the-sound"},
    "alert-mute": {page: "concepts/alerts.md", anchor: "the-mute-that-expires"},
    "effective-config": {page: "reference/configuration.md", anchor: "where-each-value-comes-from"},
    "about": {page: "getting-started/what-it-does.md", anchor: "what-it-collects"},
    "claude-export": {page: "guides/claude.md", anchor: "the-terminal-status-line"},
    "source-claude": {page: "guides/claude.md", anchor: "the-terminal-status-line"},
    "source-codex": {page: "guides/codex.md", anchor: "set-it-up"},
    "source-opencode-go": {page: "guides/opencode-go.md", anchor: "set-it-up"},
    "source-ollama-cloud": {page: "guides/ollama-cloud.md", anchor: "set-it-up"},
    "source-ollama": {page: "guides/ollama.md", anchor: "set-it-up"},
    "source-litellm": {page: "guides/litellm.md", anchor: "set-it-up"},
    "source-fake": {page: "getting-started/add-the-first-source.md",
                     anchor: "the-first-source-pick-something-local"},
  };

  function helpLink(key, label) {
    // A plain anchor: it works with JavaScript, with the keyboard and with
    // a middle click. Nothing identifying goes into the URL - no query
    // string, no install id, no host, no token, no account - and the page's
    // own address is never sent as a Referer.
    const entry = HELP[key];
    if (!entry) return null;
    const link = document.createElement("a");
    link.className = "help-link";
    link.href = DOCS_BASE + entry.page.replace(/\.md$/, "/") + "#" + entry.anchor;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.referrerPolicy = "no-referrer";
    link.textContent = "?";
    link.setAttribute("aria-label", "Help: " + label);
    link.title = "Help: " + label;
    return link;
  }

  function mountHelp() {
    // Every placeholder in the page gets its link. The parts the page
    // builds itself call helpLink directly.
    for (const slot of document.querySelectorAll("[data-help]")) {
      const label = slot.dataset.helpLabel || slot.dataset.help;
      const link = helpLink(slot.dataset.help, label);
      if (link) slot.append(link);
    }
  }

  // -- small helpers --------------------------------------------------------

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function message(text, error = false) {
    element("message").textContent = text;
    element("message").classList.toggle("error", error);
  }
  function ageText(value) {
    if (!value) return null;
    const moment = Date.parse(value);
    if (Number.isNaN(moment)) return null;
    const seconds = Math.max(0, Math.round((Date.now() - moment) / 1000));
    if (seconds < 60) return seconds + "s";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + "m";
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return hours + "h";
    return Math.floor(hours / 24) + "d";
  }
  function whenText(value) {
    if (!value) return "never";
    const moment = Date.parse(value);
    if (Number.isNaN(moment)) return "unknown";
    return new Date(moment).toLocaleString();
  }
  function agoText(value) {
    const age = ageText(value);
    return age ? age + " ago" : "unknown";
  }
  function chip(text, className) {
    return el("span", "chip " + (className || ""), text);
  }
  function buildTable(headers, rows) {
    const table = el("table", "grid compact");
    const head = el("thead");
    const headRow = el("tr");
    for (const header of headers) headRow.append(el("th", "", header));
    head.append(headRow);
    const body = el("tbody");
    for (const row of rows) {
      const line = el("tr");
      for (const cell of row) line.append(el("td", "", cell));
      body.append(line);
    }
    table.append(head, body);
    // A wide table scrolls inside its own box, so the page never widens.
    const wrapper = el("div", "table-wrap");
    wrapper.append(table);
    return wrapper;
  }

  // -- requests -------------------------------------------------------------

  async function request(path, method = "GET", data, adminToken) {
    const headers = {};
    if (adminToken) headers.Authorization = "Bearer " + adminToken;
    else if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
    if (data !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, { method, headers, cache: "no-store", credentials: "same-origin",
      body: data === undefined ? undefined : JSON.stringify(data) });
    const body = await response.json();
    if (!response.ok) {
      if (response.status === 401 && unlocked) lock();
      const error = new Error(body.error || "Configuration request failed");
      error.status = response.status; throw error;
    }
    return body;
  }

  // -- the tabs -------------------------------------------------------------

  function readStoredTab() {
    // Private browsing and a full disk can both refuse localStorage.
    try { return window.localStorage.getItem(STORAGE_KEY); } catch (_) { return null; }
  }
  function storeTab(name) {
    try { window.localStorage.setItem(STORAGE_KEY, name); } catch (_) { /* not available */ }
  }
  function tabFromHash() {
    const name = (window.location.hash || "").replace(/^#/, "");
    return TABS.includes(name) ? name : null;
  }
  function showTab(name, updateHash = true) {
    const tab = TABS.includes(name) ? name : "services";
    activeTab = tab;
    for (const other of TABS) {
      const button = element("tab-" + other);
      const panel = element("panel-" + other);
      const selected = other === tab;
      button.setAttribute("aria-selected", selected ? "true" : "false");
      button.tabIndex = selected ? 0 : -1;
      panel.hidden = !selected;
    }
    storeTab(tab);
    if (updateHash) {
      try { window.history.replaceState(null, "", TAB_HASHES[tab]); }
      catch (_) { window.location.hash = TAB_HASHES[tab]; }
    }
    if (tab === "status") refreshStatus();
    if (tab === "settings") { refreshConfig(); loadAlerts(); }
  }
  function moveTabFocus(from, step) {
    const index = TABS.indexOf(from);
    let next = index + step;
    if (next < 0) next = TABS.length - 1;
    if (next >= TABS.length) next = 0;
    showTab(TABS[next]);
    element("tab-" + TABS[next]).focus();
  }

  // -- settings that belong to the whole daemon ------------------------------

  function selectedType() {
    return catalog.find((type) => type.id === element("source-type").value);
  }
  async function loadIngest() {
    // A separate call so a toggle change is visible without waiting for the next poll.
    try {
      ingestState = await request("/api/admin/ingest");
      renderIngest();
    } catch (_) { /* keep the last known switch state */ }
  }
  function renderIngest() {
    element("ingest-enabled").checked = !!ingestState?.enabled;
    element("ingest-enabled").disabled = !!ingestState?.locked_by_environment;
    element("ingest-lock").hidden = !ingestState?.locked_by_environment;
  }
  async function loadDisplay() {
    // The Home display settings travel with the status payload, so the
    // control is read on its own, like the ingest switch.
    try {
      displayState = await request("/api/admin/display");
      renderDisplay();
    } catch (_) { /* keep the last known setting */ }
  }
  function renderDisplay() {
    const value = displayState?.overflow_rows;
    element("overflow-rows").value = String(value ?? 1);
    element("overflow-rows").disabled = !!displayState?.locked_by_environment;
    element("display-lock").hidden = !displayState?.locked_by_environment;
  }
  // The reason the reader sees in words when the master switch is off.
  // It is text, not only a colour, so it is never missed.
  const MASTER_OFF_REASON = "Alerts are off for all services: no service chirps, lights up or " +
    "scrolls Home to an alarm. Turn the master switch on to use the settings below.";
  const MASTER_OFF_BANNER = "Alerts are off for all services";

  async function loadAlerts(quiet = false) {
    // The alert settings travel with the status payload, so they are read on
    // their own, like the display settings. A quiet read only refreshes the
    // master switch and the banners: it never rewrites the form the reader
    // is filling in.
    try {
      const previous = alertsState;
      alertsState = await request("/api/admin/alerts");
      if (quiet) { masterOn = alertsState.enabled !== false; renderMasterState(); }
      else renderAlerts();
      // A service's own thresholds are resolved against the global ones, so
      // the open cards are read again when the global ones move.
      if (!previous || previous.enabled !== alertsState.enabled
        || previous.warning_percent !== alertsState.warning_percent
        || previous.critical_percent !== alertsState.critical_percent) {
        await refreshOpenServiceAlerts();
      }
    } catch (_) { /* keep the last known settings */ }
  }
  function renderAlerts() {
    const state = alertsState || {};
    masterOn = state.enabled !== false;
    element("alert-master").checked = masterOn;
    element("alert-warning").value = String(state.warning_percent ?? 80);
    element("alert-critical").value = String(state.critical_percent ?? 95);
    element("alert-sound").checked = state.sound === true;
    element("alert-mute-hours").value = String(state.mute_hours ?? 4);
    element("alert-quiet").checked = !!state.quiet_hours;
    element("alert-quiet-start").value = state.quiet_start ?? "22:00";
    element("alert-quiet-end").value = state.quiet_end ?? "07:00";
    element("alert-pop").checked = state.pop_to_top !== false;
    element("alert-quiet-now").hidden = !state.quiet;
    renderMasterState();
  }
  // Everything that depends on the master switch: the banners, the reason in
  // words, the controls that cannot be used, and the summary line.
  function renderMasterState() {
    const off = !masterOn;
    const changed = lastMasterOn !== masterOn;
    lastMasterOn = masterOn;
    element("alert-master").checked = masterOn;
    element("alerts-banner").textContent = MASTER_OFF_BANNER;
    element("services-alerts-banner").textContent = MASTER_OFF_BANNER;
    element("alerts-banner").hidden = !off;
    element("services-alerts-banner").hidden = !off;
    element("alert-master-reason").hidden = !off;
    element("alert-master-summary").textContent =
      off ? "Alerts off for all services" : "Alerts on for every service";
    // The sound, the quiet hours and the scroll to an alarm are all the
    // master's business. They keep their values: only their use is stopped.
    for (const id of ["alert-sound", "alert-quiet", "alert-quiet-start",
      "alert-quiet-end", "alert-pop"]) setDisabled(id, off);
    for (const group of document.querySelectorAll(".service-alerts")) {
      applyServiceAlertDisabled(group);
    }
    // The chips on the account cards say whether a service alerts, so they
    // follow the master switch at once.
    if (changed) renderList();
  }
  function setDisabled(id, off) {
    const node = element(id);
    if (!node) return;
    node.disabled = off;
    if (off) node.setAttribute("aria-disabled", "true");
    else node.removeAttribute("aria-disabled");
  }
  function percentValue(id, fallback) {
    // An empty field must not send a broken value: the stored one is used.
    const raw = String(element(id).value || "").trim();
    if (raw === "") return fallback;
    const value = Number(raw);
    return Number.isFinite(value) ? value : fallback;
  }
  function muteHoursValue() {
    // The mute length is a whole number of hours, from 0 (until the user
    // presses again) to 48. Anything else is refused in words.
    const raw = String(element("alert-mute-hours").value || "").trim();
    if (raw === "") return null;
    const value = Number(raw);
    if (!Number.isInteger(value) || value < 0 || value > 48) return undefined;
    return value;
  }
  function alertValues() {
    return {
      enabled: element("alert-master").checked,
      warning_percent: percentValue("alert-warning", alertsState?.warning_percent ?? 80),
      critical_percent: percentValue("alert-critical", alertsState?.critical_percent ?? 95),
      sound: element("alert-sound").checked,
      quiet_hours: element("alert-quiet").checked,
      quiet_start: element("alert-quiet-start").value || "00:00",
      quiet_end: element("alert-quiet-end").value || "00:00",
      pop_to_top: element("alert-pop").checked,
      mute_hours: muteHoursValue(),
    };
  }
  async function saveAlerts() {
    if (busy) { renderAlerts(); return; }
    const wanted = alertValues();
    if (wanted.mute_hours === null) {
      message("The mute length must be a whole number of hours between 0 and 48.", true);
      renderAlerts();
      return;
    }
    if (wanted.mute_hours === undefined) {
      message("The mute length must be a whole number of hours between 0 and 48.", true);
      return;
    }
    const wasOn = masterOn;
    busy = true;
    try {
      alertsState = await request("/api/admin/alerts", "PUT", wanted);
      // The open cards are resolved against these values, so they are read
      // again: the master switch and the thresholds both moved.
      await refreshOpenServiceAlerts();
      if (wanted.enabled !== wasOn) {
        message(wanted.enabled ? "Alerts are on for every service." :
          "Alerts are off for all services. The settings below are kept for later.");
      } else {
        message("Alerts saved: warning at " + alertsState.warning_percent + "%, critical at " +
          alertsState.critical_percent + "%.");
      }
    } catch (error) { message(error.message, true); }
    finally { busy = false; renderAlerts(); }
  }
  async function refreshConfig() {
    try {
      configState = await request("/api/admin/config");
      renderConfig();
    } catch (error) { message(error.message, true); }
  }
  function renderConfig() {
    const container = element("config-table");
    container.replaceChildren();
    const settings = configState?.settings || [];
    if (!settings.length) {
      container.append(el("p", "note", "No effective configuration to show."));
      return;
    }
    container.append(buildTable(["Setting", "Value", "Comes from"],
      settings.map((entry) => [entry.name, String(entry.value), entry.origin])));
  }

  // -- one service's alert settings ------------------------------------------

  function globalPercent(key, fallback) {
    const value = alertsState?.[key];
    return typeof value === "number" ? value : fallback;
  }
  function serviceAlertDraft(id, state) {
    // What the form shows: the saved values, or what the reader has typed.
    let draft = serviceAlertDrafts.get(id);
    if (!draft) {
      draft = {
        enabled: state ? state.enabled !== false : true,
        warning: state && state.warning_percent !== null && state.warning_percent !== undefined
          ? String(state.warning_percent) : "",
        critical: state && state.critical_percent !== null && state.critical_percent !== undefined
          ? String(state.critical_percent) : "",
      };
      serviceAlertDrafts.set(id, draft);
    }
    return draft;
  }
  async function loadServiceAlerts(id, force = false) {
    // One service's own settings, read when its card is opened.
    if ((!force && serviceAlertStates.has(id)) || serviceAlertLoading.has(id)) return;
    serviceAlertLoading.add(id);
    try {
      const state = await request("/api/admin/sources/" + id + "/alerts");
      serviceAlertStates.set(id, state);
      serviceAlertDraft(id, state);
      renderList();
    } catch (error) { message(error.message, true); }
    finally { serviceAlertLoading.delete(id); }
  }
  async function refreshOpenServiceAlerts() {
    // The open cards, read again: their own thresholds are resolved against
    // the global ones, which may have moved.
    for (const id of [...expanded]) {
      if (serviceAlertStates.has(id)) await loadServiceAlerts(id, true);
    }
  }
  function numberField(id, labelText, ariaText, value, placeholder) {
    const label = document.createElement("label");
    label.htmlFor = id; label.textContent = labelText;
    const input = document.createElement("input");
    input.id = id; input.type = "number"; input.min = "1"; input.max = "100"; input.step = "1";
    input.inputMode = "numeric"; input.value = value;
    if (placeholder) input.placeholder = placeholder;
    input.setAttribute("aria-label", ariaText);
    return {label, input};
  }
  function renderServiceAlerts(source, detail) {
    // The Alerts group inside one expanded account card.
    const group = document.createElement("div");
    group.className = "service-alerts";
    group.dataset.id = source.id;
    group.append(el("h4", "", "Alerts"));
    const state = serviceAlertStates.get(source.id);
    if (!state) {
      group.append(el("p", "note", "Reading this service's alert settings..."));
      detail.append(group);
      loadServiceAlerts(source.id);
      return;
    }
    const draft = serviceAlertDraft(source.id, state);
    const summary = el("p", "note");
    if (!masterOn || state.master_off) {
      summary.textContent = "Alerts are off for all services, so this service does not alert.";
    } else if (state.effective?.enabled) {
      summary.textContent = "Alerts on for this service. Warning at " +
        state.effective.warning_percent + "%, critical at " + state.effective.critical_percent + "%.";
    } else {
      summary.textContent = "Alerts off for this service. Its thresholds are kept for later.";
    }
    group.append(summary);
    if (state.override_ignored) {
      const note = el("p", "note service-alerts-override");
      note.textContent = "override ignored: this service's own thresholds cannot be used, so the " +
        "global ones are used instead (" + state.effective.warning_percent + "% warning, " +
        state.effective.critical_percent + "% critical).";
      group.append(note);
    }
    const reason = el("p", "note service-alerts-reason");
    reason.textContent = MASTER_OFF_REASON;
    group.append(reason);

    const enabledLabel = document.createElement("label");
    enabledLabel.className = "check";
    const enabled = document.createElement("input");
    enabled.type = "checkbox"; enabled.id = "svc-alert-" + source.id + "-enabled";
    enabled.checked = draft.enabled;
    enabled.setAttribute("aria-label", "Alerts for this service (" + source.name + ")");
    enabled.addEventListener("change", () => { draft.enabled = enabled.checked; });
    enabledLabel.append(enabled, document.createTextNode("Alerts for this service"));
    group.append(enabledLabel);

    const warning = numberField("svc-alert-" + source.id + "-warning", "Warning percent",
      "Warning percent for " + source.name, draft.warning,
      "global " + globalPercent("warning_percent", 80));
    warning.input.addEventListener("input", () => { draft.warning = warning.input.value; });
    const critical = numberField("svc-alert-" + source.id + "-critical", "Critical percent",
      "Critical percent for " + source.name, draft.critical,
      "global " + globalPercent("critical_percent", 95));
    critical.input.addEventListener("input", () => { draft.critical = critical.input.value; });
    group.append(warning.label, warning.input, critical.label, critical.input);
    group.append(el("p", "note", "An empty field inherits the global value."));

    const actions = el("div", "actions");
    const save = document.createElement("button");
    save.type = "button"; save.id = "svc-alert-" + source.id + "-save";
    save.textContent = "Save";
    save.addEventListener("click", () => saveServiceAlerts(source));
    actions.append(save);
    group.append(actions);

    detail.append(group);
    applyServiceAlertDisabled(group);
  }
  function applyServiceAlertDisabled(group) {
    const off = !masterOn;
    for (const node of group.querySelectorAll("input, button")) {
      node.disabled = off;
      if (off) node.setAttribute("aria-disabled", "true");
      else node.removeAttribute("aria-disabled");
    }
    const reason = group.querySelector(".service-alerts-reason");
    if (reason) reason.hidden = !off;
  }
  function parsePercent(text) {
    // An empty field means "inherit the global value"; anything else must be
    // a whole number between 1 and 100.
    if (String(text).trim() === "") return null;
    const value = Number(text);
    if (!Number.isInteger(value) || value < 1 || value > 100) return undefined;
    return value;
  }
  async function saveServiceAlerts(source) {
    if (busy) return;
    const draft = serviceAlertDrafts.get(source.id);
    if (!draft) return;
    const warning = parsePercent(draft.warning);
    const critical = parsePercent(draft.critical);
    if (warning === undefined) {
      message("The warning percent must be a whole number between 1 and 100.", true); return;
    }
    if (critical === undefined) {
      message("The critical percent must be a whole number between 1 and 100.", true); return;
    }
    const body = {enabled: !!draft.enabled, warning_percent: warning, critical_percent: critical};
    busy = true;
    try {
      const saved = await request("/api/admin/sources/" + source.id + "/alerts", "PUT", body);
      serviceAlertStates.set(source.id, saved);
      serviceAlertDrafts.set(source.id, {
        enabled: saved.enabled !== false,
        warning: saved.warning_percent === null || saved.warning_percent === undefined
          ? "" : String(saved.warning_percent),
        critical: saved.critical_percent === null || saved.critical_percent === undefined
          ? "" : String(saved.critical_percent),
      });
      await reload();
      message("Alerts for " + saved.name + " saved: warning at " +
        saved.effective.warning_percent + "%, critical at " + saved.effective.critical_percent + "%.");
    } catch (error) { message(error.message, true); }
    finally { busy = false; renderList(); }
  }

  // -- the editor -----------------------------------------------------------

  function renderFields(settings = {}) {
    const type = selectedType();
    element("source-fields").replaceChildren();
    element("source-help").textContent = type?.id === "ollama" ?
      "Monitors local Ollama health and loaded models. The local API needs no key; this does not monitor Ollama Cloud subscription quotas." :
      type?.id === "opencode_go" ? "Read-only subscription meters for your OpenCode Go account. This does not make model requests or sum tokens into quotas." :
      type?.id === "ollama_cloud" ? "Reads dated cloud activity. Monthly allowance and remaining credits are not returned by the current API; local Ollama is a separate source." :
      type?.id === "codex" ? "Reads Codex subscription quotas using your ChatGPT login in Codex. Leave both paths blank for automatic discovery and the default profile. Separate accounts need separate Codex profile directories. OAuth stays with Codex; no pay-per-use API key is needed. Browser and device-code sign-in, cancellation, and logout are available through the Codex login controls." :
      type?.id === "claude" ? "Claude Code exports 5h/weekly subscription quotas after normal terminal responses. Save this source, then set up its exporter below. Readings go straight to the database; existing status-line output is preserved. No API key is needed. Idle readings become stale; the VS Code panel does not run the status line, but the optional usage cache reader reads the numbers Claude Code caches on disk for its /usage dialog." :
      type?.id === "fake" ? "Synthetic demo/test data. Disable it before enabling a live source." : "";
    // The guide for the source type, beside the type chooser.
    const guide = element("source-type-help");
    guide.replaceChildren();
    const guideLink = helpLink("source-" + (type?.id || "fake"),
                               "The guide for " + (type?.name || "this source"));
    if (guideLink) guide.append(guideLink);
    if (!type) return;
    for (const field of type.fields) {
      const label = document.createElement("label");
      label.htmlFor = "field-" + field.key; label.textContent = field.label;
      const input = document.createElement("input");
      input.id = label.htmlFor; input.dataset.key = field.key;
      input.type = field.type === "int" ? "number" : field.type === "url" ? "url"
        : field.type === "bool" ? "checkbox" : "text";
      if (field.type === "bool") {
        input.checked = Boolean(settings[field.key] ?? field.default);
      } else {
        input.value = settings[field.key] ?? field.default;
      }
      if (field.type === "int") {
        input.min = field.minimum; input.max = field.maximum; input.step = "1"; input.required = true;
      } else if (field.type === "url") input.required = true;
      element("source-fields").append(label, input);
    }
    element("key-section").hidden = !type.has_secret;
    if (type.id === "claude") element("source-fields").append(...vscodePreset());
  }
  function vscodePreset() {
    // One button for panel users: it only ticks the two local readers in the
    // form. The user still presses Save, and un-ticking works as before.
    const preset = document.createElement("button");
    preset.type = "button";
    preset.textContent = "Set up for VS Code";
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = "Ticks both readers: the activity reader shows that Claude Code "
      + "is in use, from the session logs Claude Code writes on this machine, and the "
      + "usage cache reader shows the quota numbers Claude Code caches on this machine "
      + "for its own /usage dialog.";
    const status = document.createElement("p");
    status.className = "note";
    status.textContent = "";
    preset.addEventListener("click", () => {
      element("field-activity_logs").checked = true;
      element("field-usage_cache").checked = true;
      status.textContent = "Both readers are ticked. Press Save source to apply.";
      message("Both Claude readers are ticked in the form. Press Save source to apply.");
    });
    return [preset, note, status];
  }
  function openEditor(source = null) {
    edit(source);
    element("editor-modal").hidden = false;
  }
  function closeEditor() {
    editing = null;
    element("editor-modal").hidden = true;
    element("source-form").reset();
    element("source-fields").replaceChildren();
  }
  function edit(source = null) {
    editing = source ? { id: source.id, revision: source.revision } : null;
    element("editor-title").textContent = source ? "Edit source" : "Add source";
    element("source-type").disabled = !!source;
    if (source && !catalog.some((type) => type.id === source.type)) {
      const option = document.createElement("option");
      option.value = source.type; option.textContent = source.type + " (unavailable)";
      element("source-type").append(option);
    }
    element("source-type").value = source?.type ??
      catalog.find((type) => type.id !== "fake")?.id ?? catalog[0]?.id ?? "";
    element("source-name").value = source?.name ?? selectedType()?.name ?? "";
    element("source-enabled").checked = source?.enabled ?? false;
    element("api-key").value = "";
    element("clear-key").checked = false;
    element("clear-key-label").hidden = !source?.has_key;
    element("key-help").textContent = source?.has_key ?
      "A key is stored. Leave blank to keep it, or enter a replacement." :
      "Enter a key if this endpoint requires one.";
    element("delete-source").hidden = !source;
    renderFields(source?.config ?? {});
    element("save-source").disabled = !selectedType();
    if (!selectedType()) {
      element("key-section").hidden = true;
      message("This source type is unavailable. Restore its module or delete this source.", true);
    }
  }

  // -- order, enable, edit --------------------------------------------------

  async function saveOrder(order, focusId) {
    const expected = sources.map(source => source.id);
    if (order.every((id, index) => id === expected[index])) return;
    busy = true;
    element("source-list").setAttribute("aria-busy", "true");
    message("Saving account order...");
    try {
      const data = await request("/api/admin/source-order", "PUT", {order, expected_order: expected});
      catalog = data.types; sources = data.sources;
      message("Account order saved. Presto will update on its next poll.");
    } catch (error) {
      if (unlocked) {
        try { await reload(); } catch (_) { /* Keep the last confirmed list. */ }
      }
      message(error.message + ". Could not confirm the move; showing the last confirmed account order.", true);
    } finally {
      busy = false;
      element("source-list").removeAttribute("aria-busy");
      renderList();
      focusAccount(focusId);
    }
  }
  function focusAccount(id, action = "reorder") {
    const row = [...element("source-list").children].find(row => row.dataset.id === id);
    row?.querySelector('[data-action="' + action + '"]')?.focus({preventScroll: true});
  }
  async function setEnabled(source, checkbox) {
    if (busy) { checkbox.checked = source.enabled; return; }
    const enabled = checkbox.checked;
    busy = true; checkbox.disabled = true;
    element("source-list").setAttribute("aria-busy", "true");
    message("Saving source status...");
    try {
      const saved = await request("/api/admin/sources/" + source.id, "PUT",
        {enabled, revision: source.revision});
      sources = sources.map(item => item.id === saved.id ? saved : item);
      message(source.name + (enabled ? " enabled." : " disabled. Settings and key kept.") +
        " Presto will update on its next poll.");
    } catch (error) {
      if (unlocked) {
        try { await reload(); } catch (_) { /* Keep the last confirmed state. */ }
      }
      message(error.message + ". Showing the last confirmed source status.", true);
    } finally {
      busy = false; element("source-list").removeAttribute("aria-busy");
      renderList(); focusAccount(source.id, "enabled");
    }
  }
  function moveAccount(id, direction) {
    if (busy) return;
    const order = sources.map(source => source.id), index = order.indexOf(id);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= order.length) return;
    [order[index], order[target]] = [order[target], order[index]];
    saveOrder(order, id);
  }
  function updateDrag() {
    const drag = dragging;
    if (!drag?.active) return;
    if (drag.y < 80) window.scrollBy(0, -12);
    else if (drag.y > window.innerHeight - 80) window.scrollBy(0, 12);
    const target = document.elementFromPoint(drag.x, drag.y)?.closest(".source");
    if (target && target !== drag.row) {
      const box = target.getBoundingClientRect();
      target.parentNode.insertBefore(drag.row,
        drag.y < box.top + box.height / 2 ? target : target.nextSibling);
    }
    drag.frame = requestAnimationFrame(updateDrag);
  }
  function finishDrag(cancel = false) {
    const drag = dragging;
    if (!drag) return;
    dragging = null; cancelAnimationFrame(drag.frame);
    const list = element("source-list");
    if (list.hasPointerCapture(drag.pointerId)) list.releasePointerCapture(drag.pointerId);
    drag.row.classList.remove("dragging");
    busy = false;
    if (cancel) { renderList(); focusAccount(drag.row.dataset.id); message("Account move cancelled."); return; }
    const order = [...element("source-list").children].map(row => row.dataset.id);
    saveOrder(order, drag.row.dataset.id);
  }

  // -- the account cards ----------------------------------------------------

  function summaryFigures(source) {
    const quota = source.quota || {};
    const parts = [];
    const activity = quota.local_activity;
    if (activity && activity.available && activity.last_request_at) {
      const age = ageText(activity.last_request_at);
      const minutes = age && age.endsWith("m") ? parseInt(age, 10) : 9999;
      const hours = age && age.endsWith("h") ? parseInt(age, 10) : 9999;
      if (minutes <= Math.round((quota.active_seconds || 300) / 60)) parts.push("in use " + age);
      else if (hours < 24) parts.push("used " + age + " ago");
    }
    if (quota.windows && quota.windows.length) {
      parts.push(quota.windows.map(window => window.label + " " + windowPercent(window)).join(" / "));
    } else if (quota.activity && typeof quota.activity.request_count === "number") {
      parts.push(quota.activity.request_count + " req");
    }
    if (!parts.length) parts.push(source.enabled ? (source.status || "no data") : "disabled");
    return parts.join(" · ");
  }
  function windowPercent(window) {
    return typeof window.used_percent === "number" ? Math.round(window.used_percent) + "%" : "--";
  }
  function freshnessChip(source) {
    const quota = source.quota || {};
    const measured = (quota.windows || []).filter(window => typeof window.used_percent === "number");
    if (!measured.length) return chip("no data", "chip-muted");
    const newest = measured.map(window => Date.parse(window.observed_at))
      .filter(moment => !Number.isNaN(moment)).sort((a, b) => b - a)[0];
    const age = newest ? ageText(new Date(newest).toISOString()) : null;
    const stale = measured.some(window => window.state === "stale");
    return chip((stale ? "stale " : "fresh ") + (age || "?"), stale ? "chip-stale" : "chip-fresh");
  }
  function sourceChip(source) {
    const quota = source.quota || {};
    const measured = (quota.windows || []).filter(window => typeof window.used_percent === "number");
    if (measured.length) {
      const newest = measured.slice().sort((a, b) =>
        Date.parse(b.observed_at) - Date.parse(a.observed_at))[0];
      if (newest.source) return chip(newest.source, "chip-source");
    }
    if (quota.local_activity && quota.local_activity.available) return chip("local logs", "chip-source");
    return null;
  }
  function alertsChip(source) {
    // A chip on the collapsed card line: it says the service does not alert,
    // and why. It is information, not a control. The master switch is read
    // from the alert settings, so the chip follows it at once and does not
    // wait for the next list refresh.
    const alerts = source.alerts || {};
    if (!masterOn) return chip("alerts off (global)", "chip-muted");
    if (alerts.service_enabled === false) return chip("alerts off", "chip-muted");
    return null;
  }
  function toggleCard(id) {
    const open = !expanded.has(id);
    if (open) expanded.add(id); else expanded.delete(id);
    const card = [...element("source-list").children].find(row => row.dataset.id === id);
    if (!card) return;
    const detail = card.querySelector(".card-detail");
    const toggle = card.querySelector('[data-action="toggle"]');
    if (detail) detail.hidden = !open;
    if (toggle) {
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      const chevron = toggle.querySelector(".chevron");
      if (chevron) chevron.textContent = open ? "▾" : "▸";
    }
    // An open card reads its own alert settings.
    if (open) loadServiceAlerts(id);
  }
  function copyCell(text) {
    const container = document.createElement("div"); container.className = "ingest-line";
    const code = document.createElement("code"); code.className = "ingest-code";
    code.textContent = text;
    const copy = document.createElement("button"); copy.type = "button";
    copy.textContent = "Copy"; copy.setAttribute("aria-label", "Copy to the clipboard");
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(text); copy.textContent = "Copied";
        setTimeout(() => { copy.textContent = "Copy"; }, 2000);
      } catch (_) { message("Copying is blocked; select the text instead.", true); }
    });
    container.append(code, copy);
    return container;
  }
  function revealToken(source) {
    // tokenReveal holds the one-time response; it disappears on hide or revoke.
    if (!tokenReveal || tokenReveal.id !== source.id || !source.ingest.token_exists) return null;
    const box = document.createElement("div"); box.className = "token-box";
    const title = document.createElement("p"); title.className = "badge";
    title.textContent = "Remote token — shown only once, copy it now:";
    box.append(title);
    box.append(copyCell(tokenReveal.token));
    for (const [name, snippet] of [["POSIX (sh/bash) settings.json status line:",
                                    tokenReveal.posix_snippet],
                                   ["Windows PowerShell settings.json status line:",
                                    tokenReveal.powershell_snippet]]) {
      const label = document.createElement("p"); label.className = "note";
      label.textContent = name + " edit the URL after pasting:";
      box.append(label, copyCell(snippet));
    }
    const dismiss = document.createElement("button"); dismiss.type = "button";
    dismiss.textContent = "Hide";
    dismiss.addEventListener("click", () => { tokenReveal = null; renderList(); });
    box.append(dismiss);
    return box;
  }
  function renderList() {
    const list = element("source-list"), active = document.activeElement;
    const focusedId = list.contains(active) ? active.closest(".source")?.dataset.id : null;
    const focusedAction = active?.dataset.action;
    // The alert controls of an open card keep the keyboard focus when the
    // list is rebuilt underneath the reader.
    const focusedField = active?.id && active.id.startsWith("svc-alert-") ? active.id : null;
    list.replaceChildren();
    if (!sources.length) {
      const empty = document.createElement("p"); empty.textContent = "No sources configured. Add your first source.";
      list.append(empty);
    }
    for (const [index, source] of sources.entries()) {
      list.append(buildCard(source, index));
    }
    if (focusedId && focusedAction) focusAccount(focusedId, focusedAction);
    else if (focusedField) document.getElementById(focusedField)?.focus({preventScroll: true});
  }
  function buildCard(source, index) {
    const card = document.createElement("article");
    card.className = "source card";
    card.classList.toggle("is-disabled", !source.enabled);
    card.dataset.id = source.id;

    const head = document.createElement("div"); head.className = "source-head";
    const toggle = document.createElement("button");
    toggle.className = "card-toggle"; toggle.type = "button"; toggle.dataset.action = "toggle";
    toggle.setAttribute("aria-expanded", expanded.has(source.id) ? "true" : "false");
    toggle.setAttribute("aria-controls", "detail-" + source.id);
    toggle.setAttribute("aria-label", "Show the details of " + source.name);
    const dot = document.createElement("span");
    dot.className = "dot dot-" + (source.enabled ? (source.status || "unknown") : "disabled");
    dot.setAttribute("aria-hidden", "true");
    const name = document.createElement("strong"); name.textContent = source.name;
    const figures = document.createElement("span");
    figures.className = "figures"; figures.textContent = summaryFigures(source);
    toggle.append(dot, name, figures, freshnessChip(source));
    const origin = sourceChip(source);
    if (origin) toggle.append(origin);
    const alerting = alertsChip(source);
    if (alerting) toggle.append(alerting);
    const chevron = document.createElement("span");
    chevron.className = "chevron"; chevron.setAttribute("aria-hidden", "true");
    chevron.textContent = expanded.has(source.id) ? "▾" : "▸";
    toggle.append(chevron);
    toggle.addEventListener("click", () => toggleCard(source.id));

    const handle = document.createElement("button"); handle.className = "drag-handle";
    handle.dataset.action = "reorder";
    handle.type = "button"; handle.textContent = "⠿";
    handle.setAttribute("aria-label", "Reorder " + source.name + ", position " + (index + 1) +
      " of " + sources.length + ". Drag or use Up and Down arrow keys.");
    handle.title = "Drag to reorder, or use Up / Down arrow keys";
    handle.disabled = sources.length < 2;
    handle.addEventListener("keydown", event => {
      if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        event.preventDefault(); moveAccount(source.id, event.key === "ArrowUp" ? -1 : 1);
      }
    });
    handle.addEventListener("pointerdown", event => {
      if (busy || event.button !== 0 || sources.length < 2) return;
      event.preventDefault(); handle.focus(); busy = true;
      dragging = {row: card, handle, pointerId: event.pointerId, x: event.clientX, y: event.clientY,
        startY: event.clientY, active: false};
      // Capture on the stable list: moving a captured row would lose capture.
      element("source-list").setPointerCapture(event.pointerId);
    });
    const enabledLabel = document.createElement("label"); enabledLabel.className = "check source-enabled";
    const enabled = document.createElement("input"); enabled.type = "checkbox";
    enabled.checked = source.enabled; enabled.dataset.action = "enabled";
    enabled.setAttribute("aria-label", "Enable " + source.name);
    enabled.addEventListener("change", () => setEnabled(source, enabled));
    enabledLabel.append(enabled, document.createTextNode("Enabled"));
    const ordering = document.createElement("div"); ordering.className = "order-controls";
    for (const [direction, label, text] of [[-1, "up", "↑"], [1, "down", "↓"]]) {
      const move = document.createElement("button"); move.type = "button";
      move.className = "move-account"; move.textContent = text;
      move.dataset.action = label;
      move.title = "Move " + source.name + " " + label;
      move.setAttribute("aria-label", move.title);
      move.disabled = direction < 0 ? index === 0 : index === sources.length - 1;
      move.addEventListener("click", () => moveAccount(source.id, direction));
      ordering.append(move);
    }
    const button = document.createElement("button"); button.textContent = "Edit";
    button.dataset.action = "edit";
    button.addEventListener("click", () => { if (!busy) openEditor(source); });
    // The help for this account's own actions.
    const cardHelp = helpLink("account", "Account actions");
    if (cardHelp) head.append(cardHelp);
    head.append(toggle, handle, enabledLabel, ordering, button);

    const detail = document.createElement("div");
    detail.className = "card-detail"; detail.id = "detail-" + source.id;
    detail.hidden = !expanded.has(source.id);
    card.append(head, detail);
    renderCardDetail(source, detail);
    return card;
  }
  function renderCardDetail(source, detail) {
    // Everything the page showed before the cards were collapsed.
    const type = catalog.find((item) => item.id === source.type);
    const keyStatus = source.has_key ? "key stored" :
      type && !type.has_secret ? (source.type === "claude" ? "Claude Code quota export" :
        source.type === "codex" ? "Codex-managed subscription login" :
        source.type === "ollama" ? "local API · no key required" :
        "no API key required") : "no stored key";
    const summary = document.createElement("p");
    summary.textContent = source.type + " · " + (source.enabled ? source.status : "disabled") +
      " · " + keyStatus;
    detail.append(summary);
    if (source.type === "claude") {
      const setup = document.createElement("button");
      setup.textContent = source.export_installed ? "Remove Claude export setup" : "Set up Claude export";
      setup.addEventListener("click", async () => {
        if (busy) return;
        busy = true; setup.disabled = true;
        try {
          await request("/api/admin/sources/" + source.id + "/claude-export", "POST",
            {revision: source.revision, action: source.export_installed ? "remove" : "install"});
          await reload();
          message(source.export_installed ? "Claude export removed; previous status line restored where applicable." :
            "Claude export configured. Reopen Claude Code in this profile and use it normally; quotas appear after a response.");
        } catch (error) { message(error.message, true); }
        finally { busy = false; setup.disabled = false; }
      });
      detail.append(setup);
      // The guide for the Claude export, beside the button that sets it up.
      const exportHelp = helpLink("claude-export", "The Claude export");
      if (exportHelp) detail.append(exportHelp);
    }
    if (source.ingest) {
      const reported = document.createElement("p"); reported.className = "note";
      reported.textContent = source.ingest.collector_seen_at ?
        "Last report: " + new Date(source.ingest.collector_seen_at).toLocaleString() +
        " from " + source.ingest.collector_host : "No reports yet";
      detail.append(reported);
      const controls = document.createElement("div"); controls.className = "actions";
      const create = document.createElement("button"); create.type = "button";
      create.textContent = source.ingest.token_exists ? "Rotate remote token" : "Create remote token";
      create.addEventListener("click", async () => {
        if (busy) return;
        busy = true; create.disabled = true;
        try {
          tokenReveal = await request("/api/admin/sources/" + source.id + "/ingest-token", "POST");
          tokenReveal.id = source.id;
          await reload();
          message("Remote token ready. Copy it now: it is shown only once.");
        } catch (error) { message(error.message, true); }
        finally { busy = false; create.disabled = false; renderList(); }
      });
      controls.append(create);
      if (source.ingest.token_exists) {
        const revoke = document.createElement("button"); revoke.type = "button";
        revoke.className = "danger"; revoke.textContent = "Revoke";
        revoke.addEventListener("click", async () => {
          if (!window.confirm("Revoke this account's remote token? Collectors using it are refused.")) return;
          if (busy) return;
          busy = true; revoke.disabled = true;
          try {
            await request("/api/admin/sources/" + source.id + "/ingest-token", "DELETE");
            tokenReveal = null;
            await reload();
            message("Remote token revoked. Install a new one to report again.");
          } catch (error) { message(error.message, true); }
          finally { busy = false; revoke.disabled = false; }
        });
        controls.append(revoke);
      }
      detail.append(controls);
      // The guide for the remote token, beside the buttons that manage it.
      const tokenHelp = helpLink("remote-token", "Remote tokens");
      if (tokenHelp) detail.append(tokenHelp);
      const reveal = revealToken(source);
      if (reveal) detail.append(reveal);
    }
    if (source.quota) {
      for (const quota of source.quota.windows) {
        const meter = document.createElement("div"); meter.className = "quota " + quota.state;
        const text = document.createElement("p");
        const percentage = typeof quota.used_percent === "number" ? quota.used_percent.toFixed(1) + "%" : "";
        // A passed reset means the value is the last known one, not a reading.
        const passed = quota.reset_passed === true;
        const state = quota.state === "available" ? percentage :
          quota.state === "stale" ? percentage + (passed ? " (stale, last known)" : " (stale)") :
          quota.state;
        text.textContent = quota.label + " · " + state;
        if (quota.source) {
          // Every number says where it came from.
          text.textContent += " · " + quota.source;
        }
        if (quota.currency && typeof quota.used_amount === "number") {
          text.textContent += " · $" + quota.used_amount.toFixed(2);
          if (typeof quota.limit_amount === "number") text.textContent += " / $" + quota.limit_amount.toFixed(2);
        }
        meter.append(text);
        if (percentage && (quota.state === "available" || quota.state === "stale")) {
          const bar = document.createElement("progress"); bar.max = 100; bar.value = quota.used_percent;
          bar.setAttribute("aria-label", quota.label + " allowance used"); meter.append(bar);
        }
        if (quota.resets_at) {
          const reset = document.createElement("p"); reset.className = "note";
          reset.textContent = "Reset: " + new Date(quota.resets_at).toLocaleString() +
            (passed ? " (passed)" : "");
          meter.append(reset);
        }
        detail.append(meter);
      }
      if (source.quota.error) {
        const error = document.createElement("p"); error.textContent = source.quota.error; detail.append(error);
      }
      if (source.quota.activity) {
        const activity = source.quota.activity;
        const summary = document.createElement("p");
        summary.textContent = (activity.state === "stale" ? "Stale activity: " : "Reported activity: ") + activity.request_count + " requests · $" +
          activity.usage_usd.toFixed(6) + " · " + (activity.input_tokens + activity.output_tokens) + " tokens";
        detail.append(summary);
        const range = document.createElement("p"); range.className = "note";
        range.textContent = "Period: " + new Date(activity.from).toLocaleString() + " – " +
          new Date(activity.until).toLocaleString(); detail.append(range);
      }
      if (source.quota.note) {
        const note = document.createElement("p"); note.className = "note";
        note.textContent = source.quota.note; detail.append(note);
      }
    }
    if (source.provenance) {
      // Labelled lines: each one says where its number comes from.
      for (const key of ["quota", "activity", "usage_cache", "usage_cache_refreshes",
                         "credit", "status_note"]) {
        const line = source.provenance[key];
        if (!line) continue;
        const paragraph = document.createElement("p"); paragraph.className = "note";
        paragraph.textContent = line; detail.append(paragraph);
      }
    }
    // This service's own alert settings: its own switch and its own
    // thresholds, or the global ones when it sets none.
    renderServiceAlerts(source, detail);
  }

  // -- the status tab -------------------------------------------------------

  async function refreshStatus() {
    if (!unlocked || busy) return;
    try {
      statusState = await request("/api/admin/status");
      renderStatus();
    } catch (error) { message(error.message, true); }
  }
  function renderStatus() {
    const state = statusState;
    if (!state) return;
    const daemon = element("status-daemon");
    daemon.replaceChildren();
    const facts = [
      ["Version", state.daemon.version],
      ["Database schema", "v" + state.daemon.schema_version],
      ["Uptime", state.daemon.uptime],
      ["Poll interval", state.daemon.poll_interval + " s"],
      ["Started", whenText(state.daemon.started_at)],
      ["Listening on", state.daemon.host + ":" + state.daemon.port],
      ["Log level", state.daemon.log_level],
    ];
    for (const [name, value] of facts) {
      daemon.append(el("dt", "", name), el("dd", "", String(value)));
    }
    const clients = state.clients || [];
    const clientBox = element("status-clients");
    clientBox.replaceChildren();
    if (!clients.length) {
      clientBox.append(el("p", "note", "No client has fetched the status endpoint yet."));
    } else {
      clientBox.append(buildTable(["Name", "Address", "Last seen", "Fetches", "Revision", "State"],
        clients.map(client => [client.name, client.address,
          whenText(client.last_seen) + " (" + agoText(client.last_seen) + ")",
          String(client.fetches), client.revision,
          client.connected ? "connected" : "not connected"])));
    }
    const accounts = state.accounts || [];
    const accountBox = element("status-accounts");
    accountBox.replaceChildren();
    if (!accounts.length) {
      accountBox.append(el("p", "note", "No accounts are configured."));
    } else {
      accountBox.append(buildTable(["Account", "Last collection", "Status", "Last problem"],
        accounts.map(account => [account.name + (account.enabled ? "" : " (disabled)"),
          whenText(account.last_collection), account.status, account.last_problem])));
    }
    const readerBox = element("status-readers");
    readerBox.replaceChildren();
    const readers = state.readers || [];
    const collectors = state.collectors || [];
    if (!readers.length && !collectors.length) {
      readerBox.append(el("p", "note", "No local readers and no remote collectors are in use."));
    } else {
      if (readers.length) {
        readerBox.append(el("h4", "", "Local readers"));
        readerBox.append(buildTable(["Account", "Reader", "State", "Detail"],
          readers.map(reader => [reader.account, reader.reader, reader.state, reader.detail])));
      }
      if (collectors.length) {
        readerBox.append(el("h4", "", "Remote collectors"));
        readerBox.append(buildTable(["Account", "Host", "Last seen", "State"],
          collectors.map(collector => [collector.account, collector.host,
            whenText(collector.last_seen), collector.state])));
      }
    }
    const alertBox = element("status-alerts");
    alertBox.replaceChildren();
    const alerts = state.alerts || {};
    const master = alerts.master !== false;
    alertBox.append(el("p", master ? "note" : "banner",
      master ? "Alerts are on for every service" : "Alerts are off for all services"));
    const offServices = alerts.services || [];
    if (!offServices.length) {
      alertBox.append(el("p", "note", "Every service is alerting."));
    } else {
      for (const service of offServices) {
        const line = el("p", "note");
        line.textContent = service.name + ": alerts are off " +
          (service.master_off ? "because the master switch is off for all services."
            : "for this service only.");
        alertBox.append(line);
      }
    }
    const problemBox = element("status-problems");
    problemBox.replaceChildren();
    const problems = state.problems || [];
    if (!problems.length) {
      problemBox.append(el("p", "note", "No warnings or errors since the daemon started."));
    } else {
      for (const problem of problems) {
        const line = el("p", "problem");
        line.append(el("strong", "", problem.level + ": "));
        line.append(document.createTextNode(problem.message));
        line.append(el("span", "note", " · " + whenText(problem.time) +
          (problem.count > 1 ? " · " + problem.count + " times" : "")));
        problemBox.append(line);
      }
    }
    element("status-updated").textContent = "Updated " + new Date().toLocaleTimeString();
  }

  // -- loading, locking, unlocking -----------------------------------------

  async function reload(poll = false) {
    const data = await request("/api/admin/sources");
    if (poll && busy) return;
    catalog = data.types; sources = data.sources; renderList();
  }
  function lock() {
    csrfToken = ""; unlocked = false; sources = []; catalog = []; editing = null;
    ingestState = null; tokenReveal = null; displayState = null;
    alertsState = null; masterOn = true; lastMasterOn = null;
    serviceAlertStates.clear(); serviceAlertDrafts.clear(); serviceAlertLoading.clear();
    statusState = null; configState = null; expanded.clear();
    element("admin-token").value = ""; element("source-form").reset();
    element("source-list").replaceChildren(); element("source-fields").replaceChildren();
    element("workspace").hidden = true; element("login-panel").hidden = false;
    element("logout").hidden = true; element("editor-modal").hidden = true; message("Configuration locked.");
  }
  async function unlock() {
    await reload();
    await loadIngest();
    await loadDisplay();
    await loadAlerts();
    element("source-type").replaceChildren(...catalog.map((type) => {
        const option = document.createElement("option"); option.value = type.id;
        option.textContent = type.id === "fake" ? "Fake (demo/test data)" : type.name; return option;
      }));
    const planned = document.createElement("optgroup"); planned.label = "Planned — not implemented";
    for (const [id, name] of [["codex", "OpenAI / Codex subscription"], ["claude", "Claude subscription"],
      ["opencode_go", "OpenCode Go subscription"], ["ollama_cloud", "Ollama Cloud credits"]]) {
      if (catalog.some(type => type.id === id)) continue;
      const option = document.createElement("option"); option.textContent = name;
      option.disabled = true; planned.append(option);
    }
    element("source-type").append(planned);
    edit(); element("admin-token").value = ""; unlocked = true;
    element("login-panel").hidden = true; element("workspace").hidden = false;
    element("logout").hidden = false; message("Configuration unlocked. Login remembered for seven days.");
    showTab(tabFromHash() || readStoredTab() || "services");
  }

  // -- events ---------------------------------------------------------------

  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && dragging) { event.preventDefault(); finishDrag(true); }
  });
  element("source-list").addEventListener("pointermove", event => {
    if (!dragging || event.pointerId !== dragging.pointerId) return;
    dragging.x = event.clientX; dragging.y = event.clientY;
    if (!dragging.active && Math.abs(event.clientY - dragging.startY) > 5) {
      dragging.active = true; dragging.row.classList.add("dragging"); updateDrag();
    }
  });
  element("source-list").addEventListener("pointerup", event => {
    if (event.pointerId === dragging?.pointerId) finishDrag();
  });
  element("source-list").addEventListener("pointercancel", event => {
    if (event.pointerId === dragging?.pointerId) finishDrag(true);
  });
  element("source-list").addEventListener("lostpointercapture", () => { if (dragging) finishDrag(true); });
  element("tablist").addEventListener("keydown", event => {
    const tab = event.target.closest('[role="tab"]');
    if (!tab) return;
    if (event.key === "ArrowRight") { event.preventDefault(); moveTabFocus(tab.dataset.tab, 1); }
    else if (event.key === "ArrowLeft") { event.preventDefault(); moveTabFocus(tab.dataset.tab, -1); }
    else if (event.key === "Home") { event.preventDefault(); showTab("services"); element("tab-services").focus(); }
    else if (event.key === "End") { event.preventDefault(); showTab("settings"); element("tab-settings").focus(); }
  });
  for (const tab of TABS) {
    element("tab-" + tab).addEventListener("click", () => showTab(tab));
  }
  window.addEventListener("hashchange", () => {
    const name = tabFromHash();
    if (name && name !== activeTab) showTab(name, false);
  });
  element("login-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (busy) return;
    busy = true; element("unlock").disabled = true;
    try {
      const session = await request("/api/admin/session", "POST", undefined,
        element("admin-token").value.trim());
      csrfToken = session.csrf_token; element("admin-token").value = "";
      await unlock();
    } catch (error) { lock(); message(error.message, true); }
    finally { busy = false; element("unlock").disabled = false; }
  });
  element("logout").addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    try { await request("/api/admin/session", "DELETE"); lock(); }
    catch (error) { message(error.message, true); }
    finally { busy = false; }
  });
  element("ingest-enabled").addEventListener("change", async () => {
    if (busy) { renderIngest(); return; }
    busy = true;
    try {
      ingestState = await request("/api/admin/ingest", "PUT",
        {enabled: element("ingest-enabled").checked});
      message(element("ingest-enabled").checked ?
        "Remote reports accepted. Each remote account needs its own token on the Services tab." :
        "Remote reports refused. Existing tokens are kept.");
    } catch (error) { message(error.message, true); }
    finally { busy = false; renderIngest(); }
  });
  element("overflow-rows").addEventListener("change", async () => {
    if (busy) { renderDisplay(); return; }
    busy = true;
    const lines = Number(element("overflow-rows").value);
    try {
      displayState = await request("/api/admin/display", "PUT", {overflow_rows: lines});
      message("Home overflow strip: " + displayState.overflow_rows +
        " line(s) of names for services that do not fit.");
    } catch (error) { message(error.message, true); }
    finally { busy = false; renderDisplay(); }
  });
  for (const id of ["alert-master", "alert-warning", "alert-critical", "alert-sound",
    "alert-quiet", "alert-quiet-start", "alert-quiet-end", "alert-pop",
    "alert-mute-hours"]) {
    element(id).addEventListener("change", saveAlerts);
  }
  element("add-source").addEventListener("click", () => { if (!busy) { openEditor(); message(""); } });  element("close-editor").addEventListener("click", () => { if (!busy) closeEditor(); });
  element("source-type").addEventListener("change", () => {
    element("source-name").value = selectedType()?.name ?? "";
    element("api-key").value = ""; element("clear-key").checked = false; renderFields();
  });
  element("source-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (busy) return;
    const type = selectedType(), config = {};
    if (!type) { message("Restore the source module before saving.", true); return; }
    for (const field of type.fields) {
      const input = element("field-" + field.key);
      config[field.key] = field.type === "int" ? Number(input.value)
        : field.type === "bool" ? input.checked : input.value;
    }
    const data = { type: type.id, name: element("source-name").value,
      enabled: element("source-enabled").checked, config };
    if (editing) data.revision = editing.revision;
    if (type.has_secret) {
      data.api_key = element("api-key").value;
      data.clear_key = element("clear-key").checked;
    }
    busy = true; element("save-source").disabled = true;
    try {
      const result = await request("/api/admin/sources" + (editing ? "/" + editing.id : ""),
        editing ? "PUT" : "POST", data);
      element("api-key").value = ""; await reload(); edit(result);
      message("Saved. Collection will update automatically.");
    } catch (error) { message(error.message, true); }
    finally { busy = false; element("save-source").disabled = false; }
  });
  element("delete-source").addEventListener("click", async () => {
    if (!editing || busy || !window.confirm("Delete this source and its stored key? Past observations remain.")) return;
    busy = true;
    try {
      await request("/api/admin/sources/" + editing.id, "DELETE", {revision: editing.revision});
      await reload(); closeEditor(); message("Source deleted.");
    } catch (error) { message(error.message, true); }
    finally { busy = false; }
  });
  // The account list refreshes every ten seconds, as before. The alert
  // settings are read quietly with it: the master switch and the banners
  // stay current, and the form the reader is filling in is left alone.
  setInterval(() => {
    if (!unlocked || busy) return;
    reload(true).catch((error) => message(error.message, true));
    loadAlerts(true);
  }, 10000);
  // The status tab refreshes itself while it is open and the browser tab is in
  // the foreground. Nothing is fetched while another tab is showing.
  setInterval(() => {
    if (!unlocked || busy || activeTab !== "status") return;
    if (document.visibilityState !== "visible") return;
    refreshStatus();
  }, 10000);
  async function restoreLogin() {
    busy = true; element("unlock").disabled = true;
    try {
      const session = await request("/api/admin/session");
      csrfToken = session.csrf_token; await unlock();
    } catch (error) {
      lock();
      message(error.status === 401 ? "Enter the admin token shown at startup." : error.message,
        error.status !== 401);
    } finally { busy = false; element("unlock").disabled = false; }
  }
  restoreLogin();
  mountHelp();
})();
