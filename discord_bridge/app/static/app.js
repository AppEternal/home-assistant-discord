"use strict";

const $ = (id) => document.getElementById(id);
const STYLES = ["primary", "secondary", "success", "danger", "link"];
let sets = [];
let current = null;      // the set being edited (working copy)
let originalId = null;   // id of the saved set being edited, null when new
let status = {};
let dirty = false;

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null) el.append(c);
  return el;
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

function slug(text) {
  return text.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 64);
}

function showMessage(text, error = false) {
  const el = $("message");
  el.textContent = text;
  el.className = "message" + (error ? " error" : "");
  clearTimeout(showMessage.t);
  showMessage.t = setTimeout(() => el.classList.add("hidden"), error ? 12000 : 6000);
}

// ---------------------------------------------------------------- status
async function loadStatus() {
  try { status = await api("GET", "api/status"); } catch { status = {}; }
  const chips = [];
  chips.push(status.discord_connected
    ? h("span", { class: "chip ok" }, `Discord: ${status.bot_user}`)
    : h("span", { class: "chip bad", title: status.bot_error || "" }, `Discord: ${status.bot_error || "connecting…"}`));
  chips.push(status.ha_connected
    ? h("span", { class: "chip ok" }, "Home Assistant: connected")
    : h("span", { class: "chip bad", title: status.ha_error || "" }, `Home Assistant: ${status.ha_error || "connecting…"}`));
  $("status").replaceChildren(...chips);
  $("channel-hint").textContent = status.default_channel_id ? `(empty = default ${status.default_channel_id})` : "";
}

// ---------------------------------------------------------------- sets
async function loadSets(selectId) {
  sets = await api("GET", "api/sets");
  renderList();
  if (selectId) select(selectId);
}

function renderList() {
  $("set-list").replaceChildren(...sets.map((s) =>
    h("li", { class: s.id === originalId ? "active" : "", onclick: () => select(s.id) },
      s.name, h("small", {}, `${s.id} · ${s.buttons.length} button(s)`))));
}

function confirmDiscard() {
  return !dirty || confirm("Discard unsaved changes?");
}

function select(id) {
  const found = sets.find((s) => s.id === id);
  if (!found || !confirmDiscard()) return;
  edit(structuredClone(found), found.id);
}

function edit(set, id) {
  current = set;
  originalId = id;
  dirty = false;
  $("empty").classList.add("hidden");
  $("editor").classList.remove("hidden");
  $("f-name").value = set.name || "";
  $("f-id").value = set.id || "";
  $("f-channel").value = set.channel_id || "";
  $("f-content").value = set.content || "";
  $("f-embed-title").value = set.embed?.title || "";
  $("f-embed-description").value = set.embed?.description || "";
  $("f-embed-use-color").checked = !!set.embed?.color;
  $("f-embed-color").value = (set.embed?.color || "#5865f2").toLowerCase();
  $("delete").disabled = !id;
  $("duplicate").disabled = !id;
  $("send-channel").value = "";
  renderButtons();
  renderList();
  refresh();
}

function readForm() {
  current.name = $("f-name").value.trim();
  current.id = $("f-id").value.trim();
  current.channel_id = $("f-channel").value.trim();
  current.content = $("f-content").value;
  const title = $("f-embed-title").value.trim();
  const description = $("f-embed-description").value.trim();
  current.embed = title || description
    ? { title, description, color: $("f-embed-use-color").checked ? $("f-embed-color").value : null }
    : null;
}

function renderButtons() {
  const rows = current.buttons.map((b, i) => {
    const update = (key) => (e) => {
      b[key] = e.target.type === "checkbox" ? e.target.checked : e.target.value;
      if (key === "style") {
        if (b.style === "link") delete b.form;
        renderButtons();
      }
      changed();
    };
    const isLink = b.style === "link";
    const formField = (key, label, maxlength) => h("label", {}, label, h("input", {
      value: b.form[key] || "", maxlength,
      oninput: (e) => { b.form[key] = e.target.value; changed(); },
    }));
    return h("div", { class: "btn-row" },
      h("label", {}, `Label`, h("input", { value: b.label || "", oninput: update("label"),
        onblur: () => { if (!isLink && !b.custom_id && b.label) { b.custom_id = uniqueId(slug(b.label)); renderButtons(); changed(); } } })),
      h("label", {}, "Emoji", h("input", { value: b.emoji || "", placeholder: "e.g. 🚨", oninput: update("emoji") })),
      h("label", {}, "Style", h("select", { onchange: update("style") },
        STYLES.map((s) => h("option", { value: s, selected: s === b.style }, s)))),
      isLink
        ? h("label", {}, "URL", h("input", { value: b.url || "", placeholder: "https://…", oninput: update("url") }))
        : h("label", {}, "Custom ID (event)", h("input", { value: b.custom_id || "", placeholder: "alarm_1", oninput: update("custom_id") })),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: !!b.disabled, onchange: update("disabled") }), "off"),
      h("div", { class: "tools" },
        h("button", { class: "small", title: "Move up", disabled: i === 0, onclick: () => move(i, -1) }, "↑"),
        h("button", { class: "small", title: "Move down", disabled: i === current.buttons.length - 1, onclick: () => move(i, 1) }, "↓"),
        h("button", { class: "small danger", title: "Remove", onclick: () => { current.buttons.splice(i, 1); renderButtons(); changed(); } }, "✕")),
      !isLink ? h("div", { class: "button-form" },
        h("label", { class: "inline" }, h("input", {
          type: "checkbox", checked: !!b.form,
          onchange: (e) => {
            if (e.target.checked) b.form = {
              custom_id: `${(b.custom_id || "button").slice(0, 95)}_form`,
              title: "Verification", label: "Message", placeholder: "",
            };
            else delete b.form;
            renderButtons();
            changed();
          },
        }), "Open text form on click"),
        b.form ? [
          formField("custom_id", "Form ID (submission event)", 100),
          formField("title", "Form title", 45),
          formField("label", "Input label", 45),
          formField("placeholder", "Input hint (optional)", 100),
          h("small", {}, "The click fires the button ID. Submission fires the form ID with values.message."),
        ] : null) : null);
  });
  $("buttons").replaceChildren(...rows);
  $("button-count").textContent = `(${current.buttons.length}/25, 5 per row)`;
  $("add-button").disabled = current.buttons.length >= 25;
}

function uniqueId(base) {
  base = base || "button";
  const used = new Set(current.buttons.map((b) => b.custom_id));
  let id = base, n = 2;
  while (used.has(id)) id = `${base}_${n++}`;
  return id;
}

function move(i, delta) {
  const [b] = current.buttons.splice(i, 1);
  current.buttons.splice(i + delta, 0, b);
  renderButtons();
  changed();
}

function changed() {
  dirty = true;
  refresh();
}

function refresh() {
  readForm();
  $("content-count").textContent = `(${current.content.length}/2000)`;
  renderPreview();
  renderYaml();
}

function renderPreview() {
  const parts = [h("div", { class: "pv-name" }, status.bot_user ? status.bot_user.split("#")[0] : "Your bot", h("span", {}, "APP"))];
  if (current.content.trim()) parts.push(h("div", { class: "pv-content" }, current.content));
  if (current.embed) {
    const el = h("div", { class: "pv-embed" },
      current.embed.title ? h("b", {}, current.embed.title) : null,
      current.embed.description ? h("div", {}, current.embed.description) : null);
    if (current.embed.color) el.style.borderLeftColor = current.embed.color;
    parts.push(el);
  }
  for (let r = 0; r < current.buttons.length; r += 5) {
    parts.push(h("div", { class: "pv-row" }, current.buttons.slice(r, r + 5).map((b) =>
      h("span", { class: `pv-btn ${b.style}${b.disabled ? " disabled" : ""}` },
        b.emoji || null, b.label || null, b.style === "link" ? " ↗" : null))));
  }
  $("preview").replaceChildren(...parts);
}

function renderYaml() {
  const id = current.id || "my_set";
  const clickable = current.buttons.filter((b) => b.style !== "link" && b.custom_id);
  const ids = clickable.map((b) => b.custom_id);
  let y = `# Send this message from an automation or script\naction: discord_bridge.send_message\ndata:\n  set_id: ${id}\n`;
  y += `  # channel_id: "123456789012345678"   # optional override\n  # content: "Custom text"              # optional override\nresponse_variable: sent\n`;
  if (ids.length) {
    const events = clickable.flatMap((b) => [
      { custom_id: b.custom_id, type: "button" },
      ...(b.form ? [{ custom_id: b.form.custom_id, type: "modal_submit" }] : []),
    ]);
    y += `\n# Button clicks and form submissions have separate IDs\ntriggers:\n${events.map((event) => `  - trigger: event\n    event_type: discord_bridge_interaction\n    event_data:\n      custom_id: ${JSON.stringify(event.custom_id)}\n      interaction_type: ${event.type}\n    id: ${JSON.stringify(`${event.type}:${event.custom_id}`)}\n`).join("")}`;
    y += `actions:\n  # 1. Acknowledge the click (within ~2 s, otherwise the app does it for you)\n  - action: discord_bridge.respond\n    data:\n      interaction_id: "{{ trigger.event.data.interaction_id }}"\n      type: reply           # defer_update | defer_reply | reply | update_message\n      ephemeral: true\n      content: "Got it, {{ trigger.event.data.user_display_name }}!"\n`;
    y += `  # 2. Do your thing per button or form\n  - choose:\n${events.map((event) => `      - conditions:\n          - condition: trigger\n            id: ${JSON.stringify(`${event.type}:${event.custom_id}`)}\n        sequence: []   # your actions for ${event.type}\n`).join("")}`;
    if (clickable.some((b) => b.form)) {
      y += `  # For form submissions, use the entered text in your actions:\n  # "{{ trigger.event.data['values']['message'] }}"\n`;
    }
    y += `  # 3. Optionally tell them it is done\n  - action: discord_bridge.followup\n    data:\n      interaction_id: "{{ trigger.event.data.interaction_id }}"\n      ephemeral: true\n      content: "Done ✅"\n`;
  }
  $("yaml").textContent = y;
}

async function save() {
  readForm();
  if (!current.id && current.name) { current.id = slug(current.name); $("f-id").value = current.id; }
  const saved = originalId
    ? await api("PUT", `api/sets/${encodeURIComponent(originalId)}`, current)
    : await api("POST", "api/sets", current);
  dirty = false;
  await loadSets();
  edit(structuredClone(saved), saved.id);
  return saved;
}

async function run(button, fn) {
  button.disabled = true;
  try { await fn(); } catch (err) { showMessage(err.message, true); } finally { button.disabled = false; }
}

// ---------------------------------------------------------------- history
async function loadHistory() {
  const records = await api("GET", "api/history");
  if (!records.length) {
    $("history-rows").replaceChildren(h("tr", {}, h("td", { colspan: 6 }, "Nothing sent yet.")));
    return;
  }
  $("history-rows").replaceChildren(...records.map((r) => {
    const setChoice = h("select", { class: "small" },
      sets.map((s) => h("option", { value: s.id, selected: s.id === r.set_id }, s.name)));
    return h("tr", {},
      h("td", {}, new Date(r.sent_at).toLocaleString(), r.edited_at ? h("small", {}, h("br"), `edited ${new Date(r.edited_at).toLocaleString()}`) : null),
      h("td", {}, r.set_name || r.set_id || h("small", {}, "(custom)")),
      h("td", {}, h("code", {}, r.channel_id)),
      h("td", {}, r.jump_url ? h("a", { href: r.jump_url, target: "_blank", rel: "noopener" }, r.message_id) : r.message_id,
        h("details", {}, h("summary", {}, "what was sent"), h("pre", {}, JSON.stringify(r.payload, null, 2)))),
      h("td", {}, r.source + (r.edited_by ? ` / edited by ${r.edited_by}` : "")),
      h("td", {}, h("div", { class: "row-actions" },
        sets.length ? setChoice : null,
        sets.length ? h("button", { class: "small primary", title: "Edit the Discord message to match the chosen set",
          onclick: (e) => run(e.target, async () => { await api("POST", `api/history/${r.id}/update`, { set_id: setChoice.value }); await loadHistory(); }) }, "Update message") : null,
        h("button", { class: "small", title: "Remove from this list (does not delete it in Discord)",
          onclick: (e) => run(e.target, async () => { if (confirm("Forget this record?")) { await api("DELETE", `api/history/${r.id}`); await loadHistory(); } }) }, "Forget"))));
  }));
}

// ---------------------------------------------------------------- wiring
for (const id of ["f-name", "f-id", "f-channel", "f-content", "f-embed-title", "f-embed-description", "f-embed-use-color", "f-embed-color"]) {
  $(id).addEventListener("input", changed);
}
$("f-name").addEventListener("blur", () => {
  if (!originalId && !$("f-id").value.trim()) { $("f-id").value = slug($("f-name").value); changed(); }
});
$("new-set").onclick = () => {
  if (!confirmDiscard()) return;
  edit({ id: "", name: "", channel_id: "", content: "", embed: null, buttons: [] }, null);
};
$("add-button").onclick = () => {
  const n = current.buttons.length + 1;
  current.buttons.push({ label: `Button ${n}`, style: "primary", custom_id: uniqueId(`button_${n}`), emoji: "", disabled: false });
  renderButtons();
  changed();
};
$("save").onclick = (e) => run(e.target, async () => { await save(); showMessage("Saved"); });
$("send").onclick = (e) => run(e.target, async () => {
  const saved = await save();
  const result = await api("POST", `api/sets/${encodeURIComponent(saved.id)}/send`, { channel_id: $("send-channel").value.trim() });
  showMessage(`Sent! Message ID ${result.message_id}`);
});
$("duplicate").onclick = () => {
  if (!confirmDiscard()) return;
  const copy = structuredClone(current);
  copy.id = `${copy.id}_copy`.slice(0, 64);
  copy.name = `${copy.name} (copy)`;
  edit(copy, null);
  dirty = true;
};
$("delete").onclick = (e) => run(e.target, async () => {
  if (!originalId || !confirm(`Delete message set "${current.name}"? Messages already sent stay in Discord.`)) return;
  await api("DELETE", `api/sets/${encodeURIComponent(originalId)}`);
  originalId = null; current = null; dirty = false;
  $("editor").classList.add("hidden");
  $("empty").classList.remove("hidden");
  await loadSets();
});
document.querySelectorAll(".tabs button").forEach((btn) => btn.onclick = () => {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b === btn));
  $("tab-sets").classList.toggle("hidden", btn.dataset.tab !== "sets");
  $("tab-history").classList.toggle("hidden", btn.dataset.tab !== "history");
  if (btn.dataset.tab === "history") loadHistory().catch((err) => alert(err.message));
});
window.addEventListener("beforeunload", (e) => { if (dirty) e.preventDefault(); });

loadStatus();
setInterval(loadStatus, 10000);
loadSets().catch((err) => alert(err.message));
