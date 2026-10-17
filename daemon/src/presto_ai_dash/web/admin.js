"use strict";
(() => {
  const element = (id) => document.getElementById(id);
  let csrfToken = "", unlocked = false, catalog = [], sources = [], editing = null;
  let busy = false;
  function message(text, error = false) {
    element("message").textContent = text;
    element("message").classList.toggle("error", error);
  }
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
  function selectedType() {
    return catalog.find((type) => type.id === element("source-type").value);
  }
  function renderFields(settings = {}) {
    const type = selectedType();
    element("source-fields").replaceChildren();
    element("source-help").textContent = type?.id === "ollama" ?
      "Monitors local Ollama health and loaded models. The local API needs no key; this does not monitor Ollama Cloud subscription quotas." :
      type?.id === "opencode_go" ? "Read-only subscription meters for your OpenCode Go account. This does not make model requests or sum tokens into quotas." :
      type?.id === "ollama_cloud" ? "Reads dated cloud activity. Monthly allowance and remaining credits are not returned by the current API; local Ollama is a separate source." :
      type?.id === "codex" ? "Reads Codex subscription quotas using your ChatGPT login in Codex. Leave both paths blank for automatic discovery and the default profile. Separate accounts need separate Codex profile directories. OAuth stays with Codex; no pay-per-use API key is needed. Browser and device-code sign-in, cancellation, and logout are available through the Codex login controls." :
      type?.id === "claude" ? "Claude Code exports 5h/weekly subscription quotas after normal terminal responses. Save this source, then set up its exporter below. Readings go straight to the database; existing status-line output is preserved. No API key is needed. Idle readings become stale; VS Code graphical-panel coverage is unverified." :
      type?.id === "fake" ? "Synthetic demo/test data. Disable it before enabling a live source." : "";
    if (!type) return;
    for (const field of type.fields) {
      const label = document.createElement("label");
      label.htmlFor = "field-" + field.key; label.textContent = field.label;
      const input = document.createElement("input");
      input.id = label.htmlFor; input.dataset.key = field.key;
      input.type = field.type === "int" ? "number" : field.type === "url" ? "url" : "text";
      input.value = settings[field.key] ?? field.default;
      if (field.type === "int") {
        input.min = field.minimum; input.max = field.maximum; input.step = "1"; input.required = true;
      } else if (field.type === "url") input.required = true;
      element("source-fields").append(label, input);
    }
    element("key-section").hidden = !type.has_secret;
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
  function renderList() {
    const list = element("source-list"); list.replaceChildren();
    if (!sources.length) {
      const empty = document.createElement("p"); empty.textContent = "No sources configured. Add your first source.";
      list.append(empty);
    }
    for (const source of sources) {
      const row = document.createElement("div"); row.className = "source";
      const head = document.createElement("div"); head.className = "source-head";
      const name = document.createElement("strong"); name.textContent = source.name;
      const button = document.createElement("button"); button.textContent = "Edit";
      button.addEventListener("click", () => { if (!busy) openEditor(source); });
      head.append(name, button);
      const detail = document.createElement("p");
      const type = catalog.find((item) => item.id === source.type);
      const keyStatus = source.has_key ? "key stored" :
        type && !type.has_secret ? (source.type === "claude" ? "Claude Code quota export" :
          source.type === "codex" ? "Codex-managed subscription login" :
          source.type === "ollama" ? "local API · no key required" :
          "no API key required") : "no stored key";
      detail.textContent = source.type + " · " + (source.enabled ? source.status : "disabled") +
        " · " + keyStatus;
      row.append(head, detail);
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
        row.append(setup);
      }
      if (source.quota) {
        for (const quota of source.quota.windows) {
          const meter = document.createElement("div"); meter.className = "quota " + quota.state;
          const text = document.createElement("p");
          const percentage = typeof quota.used_percent === "number" ? quota.used_percent.toFixed(1) + "%" : "";
          text.textContent = quota.label + " · " + (quota.state === "available" ? percentage :
            quota.state === "stale" ? percentage + " (stale)" : quota.state);
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
            reset.textContent = "Reset: " + new Date(quota.resets_at).toLocaleString(); meter.append(reset);
          }
          row.append(meter);
        }
        if (source.quota.error) {
          const error = document.createElement("p"); error.textContent = source.quota.error; row.append(error);
        }
        if (source.quota.activity) {
          const activity = source.quota.activity;
          const summary = document.createElement("p");
          summary.textContent = (activity.state === "stale" ? "Stale activity: " : "Reported activity: ") + activity.request_count + " requests · $" +
            activity.usage_usd.toFixed(6) + " · " + (activity.input_tokens + activity.output_tokens) + " tokens";
          row.append(summary);
          const range = document.createElement("p"); range.className = "note";
          range.textContent = "Period: " + new Date(activity.from).toLocaleString() + " – " +
            new Date(activity.until).toLocaleString(); row.append(range);
        }
        if (source.quota.note) {
          const note = document.createElement("p"); note.className = "note";
          note.textContent = source.quota.note; row.append(note);
        }
      }
      list.append(row);
    }
  }
  async function reload() {
    const data = await request("/api/admin/sources");
    catalog = data.types; sources = data.sources; renderList();
  }
  function lock() {
    csrfToken = ""; unlocked = false; sources = []; catalog = []; editing = null;
    element("admin-token").value = ""; element("source-form").reset();
    element("source-list").replaceChildren(); element("source-fields").replaceChildren();
    element("workspace").hidden = true; element("login-panel").hidden = false;
    element("logout").hidden = true; element("editor-modal").hidden = true; message("Configuration locked.");
  }
  async function unlock() {
    await reload();
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
  }
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
  element("add-source").addEventListener("click", () => { if (!busy) { openEditor(); message(""); } });
  element("close-editor").addEventListener("click", () => { if (!busy) closeEditor(); });
  element("source-type").addEventListener("change", () => {
    element("source-name").value = selectedType()?.name ?? "";
    element("api-key").value = ""; element("clear-key").checked = false; renderFields();
  });
  element("source-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (busy) return;
    const type = selectedType(), config = {};
    if (!type) { message("Restore the source module before saving.", true); return; }
    for (const field of type.fields) {
      const value = element("field-" + field.key).value;
      config[field.key] = field.type === "int" ? Number(value) : value;
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
  setInterval(() => { if (unlocked && !busy) reload().catch((error) => message(error.message, true)); }, 10000);
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
})();
