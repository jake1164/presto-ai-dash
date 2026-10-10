"use strict";
(() => {
  const element = (id) => document.getElementById(id);
  let token = "", catalog = [], sources = [], editing = null;
  let busy = false;
  function message(text, error = false) {
    element("message").textContent = text;
    element("message").classList.toggle("error", error);
  }
  async function request(path, method = "GET", data) {
    const headers = { Authorization: "Bearer " + token };
    if (data !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, { method, headers, cache: "no-store",
      body: data === undefined ? undefined : JSON.stringify(data) });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || "Configuration request failed");
    return body;
  }
  function selectedType() {
    return catalog.find((type) => type.id === element("source-type").value);
  }
  function renderFields(settings = {}) {
    const type = selectedType();
    element("source-fields").replaceChildren();
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
      button.addEventListener("click", () => { if (!busy) edit(source); });
      head.append(name, button);
      const detail = document.createElement("p");
      detail.textContent = source.type + " · " + (source.enabled ? source.status : "disabled") +
        " · " + (source.has_key ? "key stored" : "no stored key");
      row.append(head, detail); list.append(row);
    }
  }
  async function reload() {
    const data = await request("/api/admin/sources");
    catalog = data.types; sources = data.sources; renderList();
  }
  function lock() {
    token = ""; sources = []; catalog = []; editing = null;
    element("admin-token").value = ""; element("source-form").reset();
    element("source-list").replaceChildren(); element("source-fields").replaceChildren();
    element("workspace").hidden = true; element("login-panel").hidden = false;
    element("logout").hidden = true; message("Configuration locked.");
  }
  element("login-form").addEventListener("submit", async (event) => {
    event.preventDefault(); token = element("admin-token").value.trim();
    try {
      await reload();
      element("source-type").replaceChildren(...catalog.map((type) => {
        const option = document.createElement("option"); option.value = type.id;
        option.textContent = type.name; return option;
      }));
      edit(); element("admin-token").value = "";
      element("login-panel").hidden = true; element("workspace").hidden = false;
      element("logout").hidden = false; message("Configuration unlocked.");
    } catch (error) { token = ""; message(error.message, true); }
  });
  element("logout").addEventListener("click", lock);
  element("add-source").addEventListener("click", () => { if (!busy) { edit(); message(""); } });
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
      await reload(); edit(); message("Source deleted.");
    } catch (error) { message(error.message, true); }
    finally { busy = false; }
  });
  setInterval(() => { if (token && !busy) reload().catch((error) => message(error.message, true)); }, 10000);
})();
