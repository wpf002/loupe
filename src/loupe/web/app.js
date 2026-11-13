"use strict";

const TOKEN = document.querySelector('meta[name="loupe-token"]').content;
const ALL_TABS = ["library", "search", "brand", "hooks", "week", "everything"];
const LIBRARY_TABS = ["library", "search"];
let tabs = ALL_TABS.filter((tab) => !LIBRARY_TABS.includes(tab));
const DEFAULT_LENGTH = 30;
const TOAST_MS = 1800;
let mode = "app";
let canSync = false;

/* DOM builder. Text goes through textContent only, never innerHTML. */
function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(method, path, body) {
  const options = { method, headers: {} };
  if (method === "POST") {
    options.headers["Content-Type"] = "application/json";
    options.headers["X-Loupe-Token"] = TOKEN;
    options.body = JSON.stringify(body || {});
  }
  const response = await fetch(path, options);
  if (response.status === 401 && mode === "hosted") location.assign("/login");
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function toast(message) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), TOAST_MS);
}

function alertBox(message) {
  return h("div", { class: "alert", role: "alert" }, h("b", { text: "Error" }), h("span", { text: message }));
}

function emptyState(title, command, action) {
  return h("div", { class: "empty" },
    h("span", { class: "lens", "aria-hidden": "true" }),
    h("h2", { text: title }),
    command ? h("code", { text: command }) : null,
    action || null);
}

function skeleton(...heights) {
  return h("div", { class: "stack", "aria-busy": "true", "aria-label": "Loading" },
    heights.map((height) => {
      const block = h("div", { class: "skeleton" });
      block.style.height = `${height}px`;
      return block;
    }));
}

function button(label, onclick, primary = false) {
  return h("button", { class: primary ? "button primary" : "button", type: "button", onclick }, label);
}

/* Runs a POST behind a button: busy state while it runs, error banner if it fails. */
async function busy(control, panel, work) {
  control.disabled = true;
  control.setAttribute("aria-busy", "true");
  panel.querySelector(".alert")?.remove();
  try {
    return await work();
  } catch (error) {
    panel.prepend(alertBox(error.message));
    return null;
  } finally {
    control.disabled = false;
    control.removeAttribute("aria-busy");
  }
}

function seconds(value) {
  const whole = Math.round(value);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function weekLabel(start) {
  const day = new Date(`${start}T12:00:00`);
  return day.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function head(title, ...actions) {
  return h("div", { class: "head" }, h("h1", { text: title }), h("div", { class: "actions" }, actions));
}

/* Brand */
async function renderBrand(panel) {
  const brand = await api("GET", "/api/brand");
  const names = { ink: "Ink", paper: "Paper", accent: "Accent", secondary: "Secondary" };
  const swatches = Object.entries(names).map(([key, label]) => {
    const fill = h("div", { class: "fill" });
    fill.style.background = brand.colors[key];
    return h("div", { class: "swatch" }, fill, h("span", { class: "label", text: label }),
      h("span", { class: "mono", text: brand.colors[key].toUpperCase() }));
  });
  panel.replaceChildren(
    head(brand.name),
    h("div", { class: "brand-grid" },
      h("div", { class: "card specimen" },
        h("div", { class: "display", text: brand.voice.samples[0] }),
        h("p", { class: "body-sample", text: brand.voice.samples[1] || "" }),
        h("div", { class: "font-row" },
          h("div", {}, h("div", { class: "label", text: "Display" }), h("div", { class: "mono", text: brand.fonts.display })),
          h("div", {}, h("div", { class: "label", text: "Body" }), h("div", { class: "mono", text: brand.fonts.body }))),
        h("div", { class: "swatches" }, swatches)),
      h("div", { class: "stack" },
        h("div", { class: "card" },
          h("div", { class: "label", text: "Voice" }),
          h("div", { class: "chips" }, brand.voice.traits.map((trait) => h("span", { class: "chip", text: trait })))),
        h("div", { class: "card" },
          h("div", { class: "label", text: "Avoid" }),
          h("div", { class: "chips" }, brand.voice.avoid.map((word) => h("span", { class: "chip off", text: word })))),
        h("div", { class: "card" },
          h("div", { class: "label", text: "Comment Word" }),
          h("div", { class: "chips" }, h("span", { class: "chip soft", text: brand.voice.comment_word.toUpperCase() }))),
        h("div", { class: "card" },
          h("div", { class: "label", text: "Captions" }),
          h("div", { class: "samples" }, brand.voice.samples.map((sample) => h("blockquote", { text: sample })))))));
}

/* Hooks */
const hookState = { uuid: null, hook: 1, length: DEFAULT_LENGTH };

async function renderHooks(panel) {
  const { takes } = await api("GET", "/api/takes");
  if (!takes.length) {
    panel.replaceChildren(head("Hooks"), emptyState("No Takes", "loupe transcribe"));
    return;
  }
  if (!takes.some((take) => take.uuid === hookState.uuid)) hookState.uuid = takes[0].uuid;
  const center = h("div", {});
  const preview = h("div", { class: "preview" });
  const rail = h("div", { class: "card rail", role: "list", "aria-label": "Takes" },
    takes.map((take) => h("button", {
      class: "take", type: "button", role: "listitem",
      "aria-current": take.uuid === hookState.uuid ? "true" : "false",
      onclick: () => { hookState.uuid = take.uuid; hookState.hook = 1; renderHooks(panel); },
    },
    h("span", { class: "meta" },
      h("span", { class: "mono", text: `${seconds(take.seconds)} · ${take.words} words` }),
      h("span", { class: take.has_hooks ? "dot on" : "dot", title: take.has_hooks ? "Has hooks" : "No hooks" })),
    h("span", { class: "opening", text: take.opening }))));
  panel.replaceChildren(head("Hooks"), h("div", { class: "studio" }, rail, center, preview));
  const take = takes.find((row) => row.uuid === hookState.uuid);
  center.append(skeleton(56, 72, 72, 72));
  const data = await api("GET", `/api/takes/${take.uuid}/hooks`);
  drawHookColumn(panel, center, take, data);
  drawPreview(panel, preview, take, data);
}

function drawHookColumn(panel, center, take, data) {
  const write = button(data.hooks ? "New Hooks" : "Write Hooks", async () => {
    const result = await busy(write, panel, () =>
      api("POST", `/api/takes/${take.uuid}/hooks`, { fresh: Boolean(data.hooks) }));
    if (result) { hookState.hook = 1; renderHooks(panel); }
  }, !data.hooks);
  const top = h("div", { class: "take-head" },
    h("span", { class: "label", text: `${seconds(take.seconds)} · ${take.words} words` }), write);
  if (!data.hooks) {
    center.replaceChildren(top, emptyState("No Hooks Yet", null));
    return;
  }
  const rendered = new Set(data.reels.map((reel) => reel.hook));
  const list = h("div", { class: "hook-list", role: "radiogroup", "aria-label": "Hooks" },
    data.hooks.map((line, index) => {
      const number = index + 1;
      return h("button", {
        class: "hook", type: "button", role: "radio",
        "aria-checked": number === hookState.hook ? "true" : "false",
        onclick: () => { hookState.hook = number; drawHookColumn(panel, center, take, data); drawPreview(panel, panel.querySelector(".preview"), take, data); },
      },
      h("span", { class: "number", text: String(number).padStart(2, "0") }),
      h("span", { class: "line", text: line }),
      rendered.has(number) ? h("span", { class: "rendered", text: "Rendered" }) : null);
    }));
  center.replaceChildren(top, list);
}

function drawPreview(panel, preview, take, data) {
  const reel = data.reels.find((item) => item.hook === hookState.hook);
  const phone = h("div", { class: "phone" },
    reel ? h("video", { src: reel.url, controls: true, playsinline: true, preload: "metadata" }) : h("span", { text: "No Reel" }));
  const output = h("output", { for: "length", text: `${hookState.length}s` });
  const slider = h("input", {
    id: "length", type: "range", min: "5", max: "90", step: "5", value: String(hookState.length),
    oninput: (event) => { hookState.length = Number(event.target.value); output.textContent = `${hookState.length}s`; },
  });
  const render = button("Render", async () => {
    const result = await busy(render, panel, () =>
      api("POST", `/api/takes/${take.uuid}/reel`, { hook: hookState.hook, length: hookState.length }));
    if (result) { toast(`Reel ${seconds(result.seconds)}`); renderHooks(panel); }
  }, true);
  render.disabled = !data.hooks;
  preview.replaceChildren(phone, h("div", { class: "controls" },
    h("label", { class: "range", for: "length" },
      h("span", { class: "row" }, h("span", { class: "label", text: "Length" }), output), slider),
    render));
}

/* Week */
async function renderWeek(panel) {
  const { week } = await api("GET", "/api/week");
  const plan = button(week ? "Replan" : "Plan Week", async () => {
    const result = await busy(plan, panel, () =>
      api("POST", "/api/week", { start: week ? week.start : null, fresh: Boolean(week) }));
    if (result) renderWeek(panel);
  }, !week);
  if (!week) {
    panel.replaceChildren(head("Week"), emptyState("No Week Planned", null, plan));
    return;
  }
  const columns = week.posts.map((post) => h("article", { class: "card day" },
    h("div", { class: "top" },
      h("span", { class: "label", text: post.day }),
      h("span", { class: `chip format-${post.format}`, text: post.format[0].toUpperCase() + post.format.slice(1) })),
    h("div", { class: "angle", text: post.angle }),
    h("div", { class: "pillar", text: post.pillar }),
    post.assets.length ? h("div", { class: "thumbs" }, post.assets.slice(0, 4).map((asset) =>
      h("figure", {},
        h("img", { src: `/media/thumb/${asset.uuid}`, alt: "", loading: "lazy" }),
        asset.media_type === "video" ? h("span", { class: "kind", text: "Video" }) : null))) : null,
    h("details", { class: "caption" }, h("summary", { text: "Caption" }), h("p", { text: post.caption }))));
  panel.replaceChildren(head(`Week of ${weekLabel(week.start)}`, plan), h("div", { class: "board" }, columns));
}

/* Everything */
async function renderEverything(panel) {
  const data = await api("GET", "/api/everything");
  if (!data.start) {
    panel.replaceChildren(head("Everything"), emptyState("No Week Planned", null,
      button("Open Week", () => { location.hash = "#week"; }, true)));
    return;
  }
  const built = data.carousels.length || data.stories.length || data.email;
  const build = button(built ? "Rebuild" : "Build", async () => {
    const result = await busy(build, panel, () => api("POST", "/api/everything", { start: data.start, fresh: Boolean(built) }));
    if (result) { toast("Built"); renderEverything(panel); }
  }, !built);
  if (!built) {
    panel.replaceChildren(head(`Everything · ${weekLabel(data.start)}`), emptyState("Nothing Built", null, build));
    return;
  }
  const setGroup = (title, sets, tall) => sets.length ? h("section", { class: "set" },
    h("div", { class: "top" }, h("h3", { text: title }), h("span", { class: "label", text: `${sets.length} Days` })),
    sets.map((set) => h("div", { class: "set" },
      h("div", { class: "top" }, h("span", { class: "label", text: set.day }), h("span", { class: "muted", text: set.pillar })),
      h("div", { class: tall ? "strip tall" : "strip" }, set.files.map((file, index) =>
        h("a", { href: file, target: "_blank", rel: "noopener", "aria-label": `${set.day} ${index + 1}` },
          h("img", { src: file, alt: "", loading: "lazy" }))))))) : null;
  const email = data.email;
  const copy = email ? button("Copy", async () => {
    await navigator.clipboard.writeText(`${email.subject}\n\n${email.body}`);
    toast("Copied");
  }) : null;
  const letter = email ? h("aside", { class: "card letter" },
    h("span", { class: "label", text: "Email" }),
    h("div", { class: "subject", text: email.subject }),
    h("div", { class: "muted", text: email.preview }),
    h("div", { class: "body", text: email.body }),
    h("div", { class: "actions" }, copy)) : null;
  panel.replaceChildren(
    head(`Everything · ${weekLabel(data.start)}`, build),
    h("div", { class: "everything" },
      h("div", { class: "sets" }, setGroup("Carousels", data.carousels, false), setGroup("Stories", data.stories, true)),
      letter));
}

/* Library: embed, train and score in one job, then add confident picks to Photos. */
const LIBRARY_POLL_MS = 1000;
const RUNNING_STAGES = ["embed", "train", "classify", "albums"];
const STAGE_LABELS = { embed: "Reading Photos", train: "Training", classify: "Scoring", albums: "Adding To Photos" };
let confirmAdd = false;

function percent(value) {
  return value === null ? "–" : `${Math.round(value * 100)}%`;
}

function stageMeter(status, labels = STAGE_LABELS) {
  const fill = h("span");
  const known = status.total > 0;
  if (known) fill.style.transform = `scaleX(${Math.min(1, status.done / status.total)})`;
  return h("div", { class: "progress" },
    h("div", { class: "row" },
      h("span", { class: "label", text: labels[status.stage] }),
      known ? h("span", { class: "mono", text: `${count(status.done)} / ${count(status.total)}` }) : null),
    h("div", { class: known ? "meter known" : "meter", role: "progressbar", "aria-label": labels[status.stage],
      "aria-valuemin": known ? "0" : null, "aria-valuemax": known ? String(status.total) : null,
      "aria-valuenow": known ? String(status.done) : null }, fill));
}

function pillarTable(status) {
  const cell = (value, trained) => h("td", { class: "number", text: trained ? count(value) : "–" });
  return h("div", { class: "card table-wrap" }, h("table", { class: "pillar-table" },
    h("thead", {}, h("tr", {}, ["Pillar", "Examples", "Precision", "Confident", "Borderline", "In Photos", "To Add"]
      .map((title, index) => h("th", { scope: "col", class: index ? "number" : null, text: title })))),
    h("tbody", {}, status.pillars.map((row) => h("tr", {},
      h("th", { scope: "row", text: row.album }),
      h("td", { class: "number" }, count(row.examples),
        row.examples < status.min_examples
          ? h("span", { class: "short", title: `Needs ${status.min_examples} to sort`, text: ` / ${status.min_examples}` })
          : null),
      h("td", { class: "number", text: percent(row.precision) }),
      cell(row.confident, row.trained),
      cell(row.borderline, row.trained),
      cell(row.in_album, row.trained),
      h("td", { class: row.to_add ? "number add" : "number", text: row.trained ? count(row.to_add) : "–" }))))));
}

function libraryActions(panel, status, sync) {
  if (mode === "hosted") return [];
  const syncButton = sync ? syncControl(panel, status, sync) : null;
  const running = RUNNING_STAGES.includes(status.stage);
  const toAdd = status.pillars.reduce((sum, row) => sum + row.to_add, 0);
  if (confirmAdd && !running && toAdd) {
    const add = button(`Add ${count(toAdd)}`, async () => {
      confirmAdd = false;
      const result = await busy(add, panel, () => api("POST", "/api/library/albums"));
      if (result) renderLibrary(panel);
    }, true);
    return [button("Cancel", () => { confirmAdd = false; renderLibrary(panel); }), add];
  }
  confirmAdd = false;
  const sort = button(status.sorted ? "Resort" : "Sort", async () => {
    const result = await busy(sort, panel, () => api("POST", "/api/library/sort"));
    if (result) renderLibrary(panel);
  }, !status.sorted);
  const addToPhotos = button("Add To Photos", () => { confirmAdd = true; renderLibrary(panel); }, status.sorted);
  sort.disabled = running || !status.pillars.length;
  addToPhotos.disabled = running || !toAdd;
  addToPhotos.title = toAdd ? `Adds to albums in the Loupe folder` : "Nothing new to add";
  return [syncButton, sort, status.sorted ? addToPhotos : null].filter(Boolean);
}

const SYNC_STAGES = ["library", "settings", "previews", "vectors"];
const SYNC_LABELS = { library: "Sending Library", settings: "Sending Settings", previews: "Sending Previews", vectors: "Sending Search Data" };

function syncControl(panel, status, sync) {
  const control = button("Sync", async () => {
    const result = await busy(control, panel, () => api("POST", "/api/library/sync"));
    if (result) renderLibrary(panel);
  });
  control.disabled = SYNC_STAGES.includes(sync.stage) || RUNNING_STAGES.includes(status.stage) || !status.items;
  control.title = sync.url || "";
  return control;
}


async function renderLibrary(panel) {
  const [status, sync] = await Promise.all([
    api("GET", "/api/library"),
    canSync ? api("GET", "/api/sync") : Promise.resolve(null),
  ]);
  if (panel.hidden) return;
  const syncing = Boolean(sync && SYNC_STAGES.includes(sync.stage));
  const running = RUNNING_STAGES.includes(status.stage) || syncing;
  const stats = h("dl", { class: "stats" },
    [["Items", status.items], ["Read", status.embedded], ["Pillars", status.pillars.length]].map(([label, value]) =>
      h("div", {}, h("dt", { class: "label", text: label }), h("dd", { text: count(value) }))));
  const body = status.pillars.length
    ? pillarTable(status)
    : emptyState("No Pillar Albums", "CC Travel", null);
  if (!status.pillars.length) body.title = "Albums named with CC at the start become pillars";
  panel.replaceChildren(...[
    head("Library", ...libraryActions(panel, status, sync)),
    status.stage === "failed" && status.error ? alertBox(status.error) : null,
    sync && sync.stage === "failed" && sync.error ? alertBox(sync.error) : null,
    RUNNING_STAGES.includes(status.stage) ? stageMeter(status) : null,
    syncing ? stageMeter(sync, SYNC_LABELS) : null,
    stats,
    body,
  ].filter(Boolean));
  clearTimeout(renderLibrary.timer);
  if (running) renderLibrary.timer = setTimeout(() => { if (!panel.hidden) renderLibrary(panel).catch(() => {}); }, LIBRARY_POLL_MS);
}

/* Search: text query over the embeddings, with pillar, type and facet filters. */
const FACET_OPTIONS = {
  light: [["natural", "Natural"], ["artificial", "Artificial"]],
  shot: [["close-up", "Close-Up"], ["medium", "Medium"], ["wide", "Wide"], ["selfie", "Selfie"]],
  setting: [["indoor", "Indoor"], ["outdoor", "Outdoor"]],
};
const searchState = { q: "", pillar: "", media: "", light: "", shot: "", setting: "", hits: null };

function choice(label, name, options) {
  const id = `search-${name}`;
  const select = h("select", { id, name },
    h("option", { value: "", text: "Any" }),
    options.map(([value, text]) => h("option", { value, text, selected: searchState[name] === value })));
  return h("div", { class: "field" }, h("label", { class: "label", for: id, text: label }), select);
}

function drawHits(results) {
  const hits = searchState.hits;
  if (hits === null) { results.replaceChildren(); return; }
  if (!hits.length) { results.replaceChildren(emptyState("No Matches", null, null)); return; }
  results.replaceChildren(
    h("div", { class: "label", text: `${count(hits.length)} Results` }),
    h("div", { class: "thumbs grid" }, hits.map((hit) => h("figure", {},
      h("img", { src: `/media/thumb/${hit.uuid}`, alt: "", loading: "lazy" }),
      hit.media_type === "video" ? h("span", { class: "kind", text: "Video" }) : null))));
}

async function renderSearch(panel) {
  const status = await api("GET", "/api/library");
  if (!status.sorted) {
    panel.replaceChildren(head("Search"), emptyState("Not Sorted", null,
      button("Open Library", () => { location.hash = "#library"; }, true)));
    return;
  }
  const results = h("div", { class: "results", "aria-live": "polite" });
  const submit = h("button", { class: "button primary", type: "submit" }, "Search");
  const form = h("form", { class: "search", role: "search" },
    h("div", { class: "field query" },
      h("label", { class: "label", for: "search-q", text: "Search" }),
      h("input", { id: "search-q", name: "q", type: "search", required: true, maxlength: "200",
        autocomplete: "off", value: searchState.q })),
    choice("Pillar", "pillar", status.pillars.filter((row) => row.trained).map((row) => [row.pillar, row.album])),
    choice("Type", "media", [["photo", "Photos"], ["video", "Videos"]]),
    choice("Light", "light", FACET_OPTIONS.light),
    choice("Shot", "shot", FACET_OPTIONS.shot),
    choice("Setting", "setting", FACET_OPTIONS.setting),
    submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form));
    Object.assign(searchState, values);
    const params = new URLSearchParams(Object.entries(values).filter(([, value]) => value));
    const found = await busy(submit, panel, () => api("GET", `/api/search?${params}`));
    if (found) { searchState.hits = found.hits; drawHits(results); }
  });
  panel.replaceChildren(head("Search"), form, results);
  drawHits(results);
  form.querySelector("input").focus();
}

/* Tabs */
const RENDERERS = {
  library: renderLibrary, search: renderSearch,
  brand: renderBrand, hooks: renderHooks, week: renderWeek, everything: renderEverything,
};

function setTabs(library) {
  tabs = library ? ALL_TABS : ALL_TABS.filter((tab) => !LIBRARY_TABS.includes(tab));
  for (const tab of ALL_TABS) document.getElementById(`tab-${tab}`).hidden = !tabs.includes(tab);
}

async function show(name) {
  const tab = tabs.includes(name) ? name : tabs[0];
  for (const other of ALL_TABS) {
    const control = document.getElementById(`tab-${other}`);
    control.setAttribute("aria-selected", other === tab ? "true" : "false");
    control.tabIndex = other === tab ? 0 : -1;
    document.getElementById(other).hidden = other !== tab;
  }
  const panel = document.getElementById(tab);
  panel.replaceChildren(skeleton(64, 240, 120));
  try {
    await RENDERERS[tab](panel);
  } catch (error) {
    panel.replaceChildren(alertBox(error.message));
  }
}

function wireTabs() {
  const list = document.querySelector(".tabs");
  list.addEventListener("click", (event) => {
    const tab = event.target.closest("[data-tab]");
    if (tab) location.hash = `#${tab.dataset.tab}`;
  });
  list.addEventListener("keydown", (event) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    if (!step) return;
    const current = Math.max(0, tabs.indexOf(location.hash.slice(1)));
    const next = tabs[(current + step + tabs.length) % tabs.length];
    location.hash = `#${next}`;
    document.getElementById(`tab-${next}`).focus();
  });
  window.addEventListener("hashchange", () => {
    if (!list.hidden) show(location.hash.slice(1));
  });
}

async function loadState(setup) {
  const who = document.getElementById("who");
  try {
    const state = await api("GET", "/api/state");
    canSync = Boolean(state.sync);
    setTabs(state.library);
    const parts = [state.brand, `${state.takes} Takes`].filter(Boolean);
    if (state.week) parts.push(`Week of ${weekLabel(state.week)}`);
    who.replaceChildren(h("span", { text: parts.join(" · ") }));
  } catch (error) {
    who.replaceChildren(h("span", { text: "Offline" }));
  }
  if ((setup.mode === "app" || setup.mode === "hosted") && setup.account) {
    who.append(h("span", { text: setup.account.email }), button("Sign Out", signOut));
  }
}

async function signOut(event) {
  event.currentTarget.disabled = true;
  try {
    await api("POST", mode === "hosted" ? "/auth/sign-out" : "/api/setup/sign-out");
  } catch (error) {
    toast(error.message);
  }
  if (mode === "hosted") location.assign("/login");
  else location.reload();
}

/* Setup: Loupe.app first run. Account, then Photos, then Library. */
const SCAN_POLL_MS = 1000;
const BLOCKED_ACCESS = ["denied", "restricted"];

function count(value) {
  return Number(value).toLocaleString("en-US");
}

function step(number, title, state, status, ...body) {
  return h("li", { class: "card step", "data-state": state, "aria-current": state === "active" ? "step" : null },
    h("div", { class: "row" },
      h("span", { class: "number", "aria-hidden": "true", text: state === "done" ? "✓" : String(number) }),
      h("h2", { text: title }),
      status ? h("span", { class: "status", text: status }) : null),
    state === "active" ? body : null);
}

function field(label, attrs) {
  const id = `field-${attrs.name}`;
  return h("div", { class: "field" },
    h("label", { class: "label", for: id, text: label }),
    h("input", { id, required: true, ...attrs }));
}

/* Runs one setup step. Redraws with the new summary, or shows the error in that step. */
async function setupStep(control, path, body) {
  const card = control.closest(".step");
  const summary = await busy(control, card, () => api("POST", `/api/setup/${path}`, body));
  if (summary) drawSetup(summary);
}

function accountStep(setup) {
  if (setup.account) return step(1, "Account", "done", setup.account.email);
  const form = h("form", {},
    field("Email", { name: "email", type: "email", autocomplete: "username", maxlength: "320" }),
    field("Password", { name: "password", type: "password", autocomplete: "current-password", maxlength: "1024" }),
    h("div", { class: "actions" },
      h("button", { class: "button primary", type: "submit", value: "sign-in" }, "Sign In"),
      h("button", { class: "button", type: "submit", value: "create-account" }, "Create Account")));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const control = event.submitter;
    const values = Object.fromEntries(new FormData(form));
    setupStep(control, control.value, { email: values.email, password: values.password });
  });
  return step(1, "Account", "active", null, form);
}

function photosStep(setup) {
  if (!setup.account) return step(2, "Photos", "waiting", null);
  if (setup.photos.ready) {
    return step(2, "Photos", "done", setup.photos.access === "limited" ? "Selected Photos" : "Connected");
  }
  const actions = h("div", { class: "actions" });
  if (BLOCKED_ACCESS.includes(setup.photos.access)) {
    actions.append(
      button("Open Settings", (event) => setupStep(event.currentTarget, "photos-settings"), true),
      button("Check Again", (event) => setupStep(event.currentTarget, "photos")));
    return step(2, "Photos", "active", "Access Off", actions);
  }
  actions.append(button("Connect Photos", (event) => setupStep(event.currentTarget, "photos"), true));
  return step(2, "Photos", "active", null, actions);
}

function libraryStep(setup) {
  const scan = setup.library.scan;
  if (!setup.photos.ready) return step(3, "Library", "waiting", null);
  if (scan.state === "running") {
    return step(3, "Library", "active", `${count(scan.seen)} Scanned`,
      h("div", { class: "meter", role: "progressbar", "aria-label": "Scanning" }, h("span")));
  }
  if (setup.library.items > 0) return step(3, "Library", "done", `${count(setup.library.items)} Items`);
  const label = scan.state === "failed" ? "Scan Again" : "Scan Library";
  return step(3, "Library", "active", null,
    scan.error ? alertBox(scan.error) : null,
    h("div", { class: "actions" }, button(label, (event) => setupStep(event.currentTarget, "scan"), true)));
}

function drawSetup(setup) {
  if (setup.ready) {
    enterStudio(setup);
    return;
  }
  const panel = document.getElementById("setup");
  const focused = document.activeElement?.name;
  panel.replaceChildren(
    h("h1", { id: "setup-title", text: "Set Up Loupe" }),
    h("ol", { class: "steps" }, accountStep(setup), photosStep(setup), libraryStep(setup)));
  panel.hidden = false;
  if (focused) panel.querySelector(`[name="${focused}"]`)?.focus();
  else panel.querySelector('[aria-current="step"] input, [aria-current="step"] .button')?.focus();
  clearTimeout(drawSetup.timer);
  if (setup.library.scan.state === "running") drawSetup.timer = setTimeout(pollSetup, SCAN_POLL_MS);
}

async function pollSetup() {
  try {
    drawSetup(await api("GET", "/api/setup"));
  } catch (error) {
    document.getElementById("setup").prepend(alertBox(error.message));
  }
}

async function enterStudio(setup) {
  document.getElementById("setup").hidden = true;
  await loadState(setup);
  document.querySelector(".tabs").hidden = false;
  show(location.hash.slice(1));
}

async function boot() {
  wireTabs();
  let setup;
  try {
    setup = await api("GET", "/api/setup");
    mode = setup.mode;
  } catch (error) {
    const panel = document.getElementById("setup");
    panel.replaceChildren(alertBox(error.message));
    panel.hidden = false;
    return;
  }
  drawSetup(setup);
}

boot();
