const MODES = ["INT A", "INT B", "INT DEV"];
const MODE_PILL_CLASS = {
  "INT A": "active-A",
  "INT B": "active-B",
  "INT DEV": "active-D",
  "PARAM 1": "active-P1",
  "PARAM 2": "active-P2",
  "MEMS": "active-M",
};

// MODES must exactly match the hardcoded ids in index.html (section-<mode>, intensities-<mode>).
// Checked eagerly so a mismatch fails clearly here, not as a null-dereference inside render().
MODES.forEach(mode => {
  for (const prefix of ["section-", "intensities-"]) {
    if (!document.getElementById(prefix + mode)) {
      throw new Error(
        `UI/state mismatch: MODES contains "${mode}" but no element with ` +
        `id="${prefix}${mode}" exists in the HTML. Check that MODES (app.js) and the ` +
        `hardcoded <div id="section-....">/<div id="intensities-...."> elements agree.`
      );
    }
  }
});

function buildIntensityMeters(mode) {
  const row = document.getElementById("intensities-" + mode);
  for (let i = 1; i <= 24; i++) {
    const f = document.createElement("div");
    f.className = "meter";
    f.id = "intensity-" + mode + "-" + i;
    f.innerHTML = `
      <div class="number">${i}</div>
      <div class="value-row">
        <div class="track"><div class="fill" style="height:0%"></div></div>
        <div class="val">0</div>
      </div>
      <div class="name">
        <div class="name-line"></div>
        <div class="name-line"></div>
        <div class="name-line"></div>
      </div>`;
    row.appendChild(f);
  }
}
MODES.forEach(buildIntensityMeters);

function buildPhysicalFaderMeters() {
  const row = document.getElementById("physical-faders");
  for (let i = 1; i <= 24; i++) {
    const f = document.createElement("div");
    f.className = "meter";
    f.id = "physfader-" + i;
    f.innerHTML = `
      <div class="number">${i}</div>
      <div class="value-row">
        <div class="track"><div class="fill" style="height:0%"></div></div>
        <div class="val">0</div>
      </div>
      <div class="fader-light placeholder" id="physfader-${i}-light"></div>
      <div class="name">
        <div class="name-line"></div>
        <div class="name-line"></div>
        <div class="name-line"></div>
      </div>`;
    row.appendChild(f);
  }
}
buildPhysicalFaderMeters();

// Must match the 1600px breakpoint in style.css exactly, so meter/group-bar placement always
// agrees with whichever layout the CSS actually applied.
function currentColumnsPerRow() {
  return window.matchMedia("(max-width: 1600px)").matches ? 12 : 24;
}

// Explicit grid placement, not CSS auto-placement: once a row wraps to two 12-column blocks
// AND has group-bar captions, auto-placement packs wrapped items with no gap between them,
// leaving no row free for the first block's captions. Odd rows (1, 3, ...) hold each wrapped
// block's meters; the even row right after holds that block's captions (see
// projectGroupSegments) and stays empty when nothing is grouped.
function layoutMeterGrid(containerEl, columnsPerRow) {
  containerEl.querySelectorAll(".meter").forEach((el, idx) => {
    el.style.gridColumn = String((idx % columnsPerRow) + 1);
    el.style.gridRow = String(Math.floor(idx / columnsPerRow) * 2 + 1);
  });
}

function pct(v) { return Math.round((v / 255) * 100); }

// Console convention: 100% reads as "F" (Full), never "100". No % sign anywhere.
function pctLabel(v) {
  const p = pct(v);
  return p === 100 ? "F" : String(p);
}

// hideZero: in the 24-wide INT A/B/DEV rows, blanking 0 makes the nonzero values easier to
// scan. Master/Bumps/Independents always show their number -- there's only one of each.
function setBar(el, value, hideZero) {
  if (!el) return;
  const fill = el.querySelector(".fill");
  const val = el.querySelector(".val");
  fill.style.height = pct(value) + "%";
  val.textContent = (hideZero && value === 0) ? "" : pctLabel(value);
  el.classList.toggle("nonzero", value > 0);
}

// The console's name field is 3 short lines, not one string -- render each line as-is to match
// the console's own layout instead of word-wrapping.
function setName(container, lines) {
  const lineEls = container.querySelectorAll(".name-line");
  for (let i = 0; i < lineEls.length; i++) {
    lineEls[i].textContent = lines?.[i] || "";
  }
}

// Physical Faders' per-fader label depends on the fader mode. INT A/B/DEV and PARAM 1/2 use
// fixed labels (fader N always means the same thing in these modes, so nothing is sent on the
// wire for them); MEMS's come live from the console (see physicalFaderLabelLines). Each entry
// is up to 3 lines, matching the console's own name field, so long labels can split across lines.
const p1_f = "Focus";
const p1_c = "Color";
const p1_bc = "Beam Control";
const p1_g1 = "Gobo 1";
const p1_g2 = "Gobo 2";
const p1_b = "Beam";
const PARAM_1_LABELS = [
  ["Intensity"], [p1_f, "Pan"], [p1_f, "Tilt"], [p1_c, "Hue"], [p1_c, "Sat"], [p1_c, "Wheel 1"], [p1_c, "Wheel 2"], [p1_c, "Modify"],
  [p1_c, "Fx"], ["Strobe/", "Shutter"], [p1_bc, "B1"], [p1_bc, "B2"], [p1_g1, "Select"], [p1_g1, "Index"], [p1_g1, "Rotate"], [p1_g1, "Mode"],
  [p1_g2, "Select"], [p1_g2, "Index"], [p1_g2, "Rotate"], [p1_g2, "Mode"], [p1_b, "Zoom"], [p1_b, "Edge"], [p1_b, "Iris"], [p1_b, "Frost"],
];
const p2_ua = "User Attributes";
const p2_b = "Beam";
const p2_scb = "Shutter Cuts / Barndoors";
const p2_ma = "Macro / Animates";
const PARAM_2_LABELS = [
  [p2_ua, "U1"], [p2_ua, "U2"], [p2_ua, "U3"], [p2_ua, "U4"], [p2_ua, "U5"], [p2_ua, "U6"], [p2_ua, "U7"], [p2_ua, "U8"],
  [p2_b, "Fx 1"], [p2_b, "Fx 1 Mod"], [p2_b, "Fx 2"], [p2_b, "Fx 2 Mod"], [p2_scb, "1 A"], [p2_scb, "1 B"], [p2_scb, "2 A"], [p2_scb, "2 B"],
  [p2_scb, "3 A"], [p2_scb, "3 B"], [p2_scb, "4 A"], [p2_scb, "4 B"], [p2_scb, "Rotate"], [p2_ma, "1"], [p2_ma, "2"], [p2_ma, "3"], 
];

function pad3(lines) {
  return [lines?.[0] || "", lines?.[1] || "", lines?.[2] || ""];
}

// Derives visual groups from consecutive entries sharing the same first line (e.g.
// ["Focus","Pan"]/["Focus","Tilt"] become one 2-wide "Focus" group), so groups don't need a
// separate definition to keep in sync by hand. A run only counts as a group at >=2 entries with
// a non-empty shared key -- lone items and PARAM_2_LABELS' still-blank entries never group.
// Returns [{label, start, end}], 1-indexed inclusive, in column order.
function computeLabelGroups(labels) {
  const groups = [];
  let i = 0;
  while (i < labels.length) {
    const key = labels[i]?.[0] || "";
    let j = i;
    while (j < labels.length && (labels[j]?.[0] || "") === key) j++;
    if (key && j - i >= 2) groups.push({ label: key, start: i + 1, end: j });
    i = j;
  }
  return groups;
}

// Projects an absolute 1-24 group range onto whichever layout is active. At 24-per-row there's
// one meter row, with captions in row 2. Once wrapped to 12-per-row, both halves occupy rows 1
// and 2, so each half needs its own caption row (3 and 4) -- a group straddling the 12/13
// boundary is split into two segments, each renumbered to its own row's 1-12 column space.
function projectGroupSegments(g, columnsPerRow) {
  const segments = [];
  for (let rowStart = 1; rowStart <= 24; rowStart += columnsPerRow) {
    const rowEnd = rowStart + columnsPerRow - 1;
    const start = Math.max(g.start, rowStart);
    const end = Math.min(g.end, rowEnd);
    if (start > end) continue;
    const meterRow = (rowStart - 1) / columnsPerRow * 2 + 1;
    segments.push({ label: g.label, start: start - rowStart + 1, end: end - rowStart + 1, row: meterRow + 1 });
  }
  return segments;
}

// (Re)draws a row's group-bar captions from a [{label, start, end}] list -- clears whatever was
// there before (safe to call again whenever groups or layout change) and appends one .group-bar
// per projected segment, each with an explicit grid-column/grid-row.
function renderGroupBars(containerEl, groups, columnsPerRow) {
  containerEl.querySelectorAll(".group-bar").forEach(el => el.remove());
  for (const g of groups) {
    for (const seg of projectGroupSegments(g, columnsPerRow)) {
      const bar = document.createElement("div");
      bar.className = "group-bar";
      bar.style.gridColumn = `${seg.start} / ${seg.end + 1}`;
      bar.style.gridRow = String(seg.row);
      bar.textContent = seg.label;
      containerEl.appendChild(bar);
    }
  }
}

// Computed once, statically, so the group-bar row and each fader label always agree on what's grouped.
const PARAM_GROUPS = { "PARAM 1": computeLabelGroups(PARAM_1_LABELS), "PARAM 2": computeLabelGroups(PARAM_2_LABELS) };

// Declared before relayoutAllRows()'s first call below (a `let` is in its temporal dead zone
// until declared). Set on each mode change in render(); read here and in relayoutAllRows().
let lastFaderGroupMode = null;

// Redraws for whichever mode last rendered, using whatever layout is active right now. Called
// from render() on a mode change, and from relayoutAllRows() since a plain resize can cross the
// 1600px breakpoint with no mode change involved.
function updateFaderGroupBars() {
  renderGroupBars(document.getElementById("physical-faders"),
    PARAM_GROUPS[lastFaderGroupMode] || [], currentColumnsPerRow());
}

// layoutMeterGrid's grid-column/row values are static once set, so any resize crossing the
// 1600px breakpoint needs meters and group-bars redone together, not just at page load.
const ALL_INTENSITY_ROW_IDS = ["intensities-INT A", "intensities-INT B", "intensities-INT DEV", "physical-faders"];
function relayoutAllRows() {
  const columnsPerRow = currentColumnsPerRow();
  for (const id of ALL_INTENSITY_ROW_IDS) layoutMeterGrid(document.getElementById(id), columnsPerRow);
  updateFaderGroupBars();
}
relayoutAllRows();
window.matchMedia("(max-width: 1600px)").addEventListener("change", relayoutAllRows);

// A grouped fader ("Focus"/"Pan") already shows its group name once, in the group-bar below --
// showing only the distinguishing 2nd word here avoids repeating it per fader. Solo entries
// (e.g. "Strobe"/"Shutter") are never grouped (groups need >=2 consecutive same-key entries),
// so they keep showing both words.
function paramFaderLabel(mode, i, entry) {
  const groups = PARAM_GROUPS[mode];
  if (!groups) {
    throw new Error(
      `paramFaderLabel() called with mode "${mode}", but PARAM_GROUPS only has ` +
      `entries for "PARAM 1"/"PARAM 2". Check the caller's mode gate.`
    );
  }
  const inGroup = groups.some(g => i >= g.start && i <= g.end);
  if (!inGroup) return pad3(entry);
  return [entry?.[1] || entry?.[0] || "", "", ""];
}

// INT A/B/DEV show the real patched channel name (state.labels[mode][i]) instead of a generic
// label -- blank, not "?", when the console hasn't sent one for that slot yet. MEMS names come
// from the console's per-page memory names, state.labels["MEMS"][memsPage][i] (see RE notes for
// how the page/slot indexing was confirmed).
function physicalFaderLabelLines(mode, i, labels, memsPage) {
  if (MODES.includes(mode)) return labels?.[mode]?.[i] || ["", "", ""];
  if (mode === "PARAM 1") return paramFaderLabel(mode, i, PARAM_1_LABELS[i - 1]);
  if (mode === "PARAM 2") return paramFaderLabel(mode, i, PARAM_2_LABELS[i - 1]);
  if (mode === "MEMS") return labels?.["MEMS"]?.[memsPage]?.[i] || ["?", "", ""];
  return ["", "", ""];
}

// state is null until the first type=0x16 update (still "placeholder"), then "on"/"off"/"blinking".
// keepLabel: SOLO/BLACKOUT already carry a permanent label in the HTML -- don't overwrite it.
function setIndicator(el, state, keepLabel) {
  if (!el) return;
  el.classList.toggle("placeholder", state === null);
  el.classList.toggle("on", state === "on");
  el.classList.toggle("blinking", state === "blinking");
  if (!keepLabel) {
    el.querySelector(".light").textContent = state === null ? "?" : "";
  }
}

// value is a stored/preset level (stays constant regardless of click state); clicked is whether
// it's currently reaching output. Bar shows `clicked ? value : 0`, same pattern as Bump buttons.
function setIndependent(meterEl, btnEl, value, clicked, lines) {
  setBar(meterEl, (value === null || clicked !== true) ? 0 : value, true);
  const known = clicked !== null;
  btnEl.classList.toggle("placeholder", !known);
  btnEl.classList.toggle("on", known && clicked === true);
  setName(btnEl.querySelector(".light"), known ? lines : ["?", "", ""]);
}

function rgbCss(color) {
  return color ? `rgb(${color[0]}, ${color[1]}, ${color[2]})` : "";
}

// The wire only ever carries a brightness value per fader, not hue -- see RE notes for the
// Bump-LED hue-by-mode finding. Same RGB triples as --accent-green/--bad in style.css, so this
// row stays visually consistent with the rest of the UI.
const FADER_HUE_MEMS = [255, 92, 92];
const FADER_HUE_DEFAULT = [211, 248, 181];
function faderColor(mode, value) {
  const hue = mode === "MEMS" ? FADER_HUE_MEMS : FADER_HUE_DEFAULT;
  const scale = value / 255;
  return hue.map(c => Math.round(c * scale));
}

// light is null until the fader's first type=0x16 update, then either
// {blinking:true, value_a, value_b} (flashing toward its stored value) or {blinking:false, value}.
function setPhysicalFader(i, value, light, mode, labels, memsPage) {
  const meterEl = document.getElementById("physfader-" + i);
  setBar(meterEl, value, true);
  setName(meterEl, physicalFaderLabelLines(mode, i, labels, memsPage));
  const lightEl = document.getElementById("physfader-" + i + "-light");
  lightEl.classList.toggle("placeholder", light == null);
  if (light && light.blinking) {
    lightEl.style.setProperty("--color-a", rgbCss(faderColor(mode, light.value_a)));
    lightEl.style.setProperty("--color-b", rgbCss(faderColor(mode, light.value_b)));
    lightEl.style.background = "";
    lightEl.classList.add("blinking");
  } else {
    lightEl.classList.remove("blinking");
    lightEl.style.background = light ? rgbCss(faderColor(mode, light.value)) : "";
  }
}

let lastUpdate = 0;

function render(state) {
  const dot = document.getElementById("dot");
  const connText = document.getElementById("conn-text");
  dot.classList.toggle("ok", state.connected);
  connText.textContent = state.connected ? "connected" : "waiting for console…";

  const pill = document.getElementById("mode-pill");
  // state.fader_mode is null until a real type=0x17 reply has been seen (e.g. before the
  // console has ever connected) -- show that as "unknown", not a guessed mode.
  const modeKnown = state.fader_mode !== null;
  const modeClass = (modeKnown && MODE_PILL_CLASS[state.fader_mode]) || "";
  pill.className = "mode-pill " + modeClass + (state.fader_mode_confirmed ? "" : " unconfirmed");
  pill.textContent = modeKnown ? state.fader_mode : "unknown";

  MODES.forEach(mode => {
    const badge = document.querySelector("#section-" + CSS.escape(mode) + " .active-badge");
    const isActive = mode === state.fader_mode;
    badge.style.display = isActive ? "inline-block" : "none";
    document.getElementById("section-" + mode).classList.toggle("dim", !isActive);
    for (let i = 1; i <= 24; i++) {
      const el = document.getElementById("intensity-" + mode + "-" + i);
      setBar(el, state.intensities[mode][i - 1], true);
      el.querySelector(".number").textContent = String(i);
      setName(el, state.labels?.[mode]?.[i]);
    }
  });

  setBar(document.getElementById("fader-master"), state.master);
  setBar(document.getElementById("bumps"), state.bumps);

  for (let i = 1; i <= 24; i++) {
    setPhysicalFader(i, state.physical_faders?.[i - 1] ?? 0, state.physical_fader_lights?.[i - 1],
      state.fader_mode, state.labels, state.mems_page);
  }
  // Only PARAM 1/2 have groupable labels; recomputed on mode change only, since PARAM_GROUPS is
  // static and shared with paramFaderLabel() -- the bar and per-fader labels can't disagree.
  if (state.fader_mode !== lastFaderGroupMode) {
    lastFaderGroupMode = state.fader_mode;
    updateFaderGroupBars();
  }

  setIndependent(document.getElementById("ind1"), document.getElementById("ind-1"),
    state.independent1, state.independent1_clicked, state.independent_labels?.[1]);
  setIndependent(document.getElementById("ind2"), document.getElementById("ind-2"),
    state.independent2, state.independent2_clicked, state.independent_labels?.[2]);
  setIndicator(document.getElementById("ind-solo"), state.solo, true);
  setIndicator(document.getElementById("ind-blackout"), state.blackout, true);

  lastUpdate = state.last_update;
}

// Pushed via Server-Sent Events rather than polled: every state change the server
// decodes is sent immediately, so no update is silently skipped between polls.
function connect() {
  const es = new EventSource("/api/events");
  es.onmessage = (ev) => render(JSON.parse(ev.data));
  es.onerror = () => {
    document.getElementById("dot").classList.remove("ok");
    document.getElementById("conn-text").textContent = "server unreachable, retrying…";
    // EventSource auto-retries the connection itself; nothing else to do here.
  };
}
connect();

setInterval(() => {
  const staleEl = document.getElementById("stale-text");
  if (!lastUpdate) return;
  const age = Date.now() / 1000 - lastUpdate;
  staleEl.textContent = age > 3 ? `last update ${age.toFixed(0)}s ago` : "";
}, 1000);
