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
      <div class="meter-label">${i}</div>
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
      <div class="meter-label">${i}</div>
      <div class="value-row">
        <div class="track"><div class="fill" style="height:0%"></div></div>
        <div class="val">0</div>
      </div>
      <div class="fader-light" id="physfader-${i}-light" data-bump="${i}"></div>
      <div class="name">
        <div class="name-line"></div>
        <div class="name-line"></div>
        <div class="name-line"></div>
      </div>`;
    row.appendChild(f);
  }
}
buildPhysicalFaderMeters();

// DMX Outputs tab: one cell per address, 512 per universe. The column count (32 / 16 / 8) is
// --dmx-cols in style.css, so this file never needs to know where the grid wraps.
const DMX_UNIVERSES = 2;
const DMX_CHANNELS = 512;
const dmxCells = [];  // dmxCells[universe][address - 1]
const dmxShown = [];  // last level drawn per cell, so an update only touches cells that changed
function buildDmxGrids() {
  for (let u = 0; u < DMX_UNIVERSES; u++) {
    const grid = document.querySelector("#dmx-universe-" + (u + 1) + " .dmx-grid");
    dmxCells.push([]);
    dmxShown.push(new Array(DMX_CHANNELS).fill(null));
    for (let a = 1; a <= DMX_CHANNELS; a++) {
      const cell = document.createElement("div");
      cell.className = "dmx-cell";
      cell.innerHTML = `<span class="addr">${a}</span><span class="lvl"></span>`;
      grid.appendChild(cell);
      dmxCells[u].push(cell);
    }
  }
}
buildDmxGrids();

// dmx is state.dmx: null before the first type=0x0d, else [universe 1, universe 2] of raw levels.
function renderDmx(dmx) {
  document.getElementById("dmx-note").style.display = dmx ? "none" : "";
  for (let u = 0; u < DMX_UNIVERSES; u++) {
    for (let i = 0; i < DMX_CHANNELS; i++) {
      const value = dmx ? dmx[u][i] : 0;
      if (dmxShown[u][i] === value) continue;
      dmxShown[u][i] = value;
      const cell = dmxCells[u][i];
      cell.querySelector(".lvl").textContent = value === 0 ? "" : pctLabel(value);
      cell.style.setProperty("--lvl", String(value / 255));
      cell.classList.toggle("nonzero", value > 0);
      cell.classList.toggle("bright", value >= 128);  // dark text once the green is strong
    }
  }
}

// Tabs. The last one used is remembered; a hidden tab isn't rendered, it catches up when shown.
const TAB_KEY = "consolelink.tab";
const TABS = ["playback", "dmx"];
let activeTab = "playback";
function showTab(name) {
  activeTab = name;
  TABS.forEach(t => {
    document.getElementById("tab-" + t).hidden = t !== name;
    document.getElementById("tab-btn-" + t).setAttribute("aria-selected", String(t === name));
  });
  if (name === "dmx" && lastState) renderDmx(lastState.dmx);
}
TABS.forEach(t => document.getElementById("tab-btn-" + t).addEventListener("click", () => {
  try { localStorage.setItem(TAB_KEY, t); } catch (e) { /* not persisted */ }
  showTab(t);
}));

// Read from style.css's --cols, which its media queries set per window width -- the breakpoints
// live only there, so meter/group-bar placement always agrees with the layout the CSS applied.
function currentColumnsPerRow() {
  return parseInt(getComputedStyle(document.documentElement).getPropertyValue("--cols"), 10) || 24;
}

// Explicit grid placement, not CSS auto-placement: once a row wraps into several blocks AND
// has group-bar captions, auto-placement packs wrapped items with no gap between them,
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
// one meter row, with captions in row 2. Once wrapped (e.g. 12-per-row), each block of meters
// gets its own caption row right below it (rows 1/2, 3/4, ...) -- a group straddling a block
// boundary (e.g. 12/13) is split into one segment per block, each renumbered to its own
// block's 1..columnsPerRow column space.
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
// from render() on a mode change, and from relayoutAllRows() since a plain resize can change
// the column count with no mode change involved.
function updateFaderGroupBars() {
  renderGroupBars(document.getElementById("physical-faders"),
    PARAM_GROUPS[lastFaderGroupMode] || [], currentColumnsPerRow());
}

// layoutMeterGrid's grid-column/row values are static once set, so any resize that changes
// --cols needs meters and group-bars redone together, not just at page load.
const ALL_INTENSITY_ROW_IDS = ["intensities-INT A", "intensities-INT B", "intensities-INT DEV", "physical-faders"];
let laidOutColumns = null;
function relayoutAllRows() {
  const columnsPerRow = currentColumnsPerRow();
  if (columnsPerRow === laidOutColumns) return;
  laidOutColumns = columnsPerRow;
  for (const id of ALL_INTENSITY_ROW_IDS) layoutMeterGrid(document.getElementById(id), columnsPerRow);
  updateFaderGroupBars();
}
relayoutAllRows();
window.addEventListener("resize", relayoutAllRows);

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
// from the console's per-page memory names, state.labels["MEMS"][memsPage][i].
function physicalFaderLabelLines(mode, i, labels, memsPage) {
  if (MODES.includes(mode)) return labels?.[mode]?.[i] || ["", "", ""];
  if (mode === "PARAM 1") return paramFaderLabel(mode, i, PARAM_1_LABELS[i - 1]);
  if (mode === "PARAM 2") return paramFaderLabel(mode, i, PARAM_2_LABELS[i - 1]);
  if (mode === "MEMS") return labels?.["MEMS"]?.[memsPage]?.[i] || ["?", "", ""];
  return ["", "", ""];
}

// state is null until the first type=0x16 update (still "placeholder"), then "on"/"off"/"blinking".
// light is the console's own LED colors for it (decode_0x16_indicator_lights), used while on or
// blinking; off keeps the page's own inactive style.
// keepLabel: SOLO/BLACKOUT already carry a permanent label in the HTML -- don't overwrite it.
function setIndicator(el, state, light, keepLabel) {
  if (!el) return;
  el.classList.toggle("placeholder", state === null);
  el.classList.toggle("on", state === "on");
  el.classList.toggle("blinking", state === "blinking");
  setLightColors(el.querySelector(".light"), state === "off" ? null : light);
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

// Colors the console itself sends (bump LEDs, FADERS bars, Solo/BlackOut) and the page's
// LED-matching palette (PALETTE_TOKENS below) all go through softenColor(). saturation: the
// header's Soft <-> Native slider, 1 = the LED's own color, lower = softer.
const SATURATION_KEY = "consolelink.saturation";
let saturation = 0.6;
try {
  const stored = parseFloat(localStorage.getItem(SATURATION_KEY));
  if (stored >= 0 && stored <= 1) saturation = stored;
} catch (e) { /* storage unavailable -- keep the default */ }

// Scales the color's chroma in OKLab, leaving its perceived lightness alone: a dim 0x46 MEMS LED
// stays dim at any saturation. (Plain sRGB luma would instead darken red and blue as they
// desaturate -- pure blue's luma is only 18/255.) White/grey have no chroma to take away, so
// Soft dims them a little instead: lightness drops by up to NEUTRAL_SOFTEN * (1 - saturation).
const NEUTRAL_SOFTEN = 0.25;
const srgbToLinear = c => { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
const linearToSrgb = c => {
  c = c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055;
  return Math.round(Math.min(1, Math.max(0, c)) * 255);
};
function softenColor(rgb) {
  const [r, g, b] = rgb.map(srgbToLinear);
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  const L0 = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s;
  const A0 = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s;
  const B0 = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s;
  const neutral = Math.hypot(A0, B0) < 0.02;
  const L = neutral ? L0 * (1 - NEUTRAL_SOFTEN * (1 - saturation)) : L0;
  const A = A0 * saturation;
  const B = B0 * saturation;
  const l2 = (L + 0.3963377774 * A + 0.2158037573 * B) ** 3;
  const m2 = (L - 0.1055613458 * A - 0.0638541728 * B) ** 3;
  const s2 = (L - 0.0894841775 * A - 1.2914855480 * B) ** 3;
  return rgbCss([
    4.0767416621 * l2 - 3.3077115913 * m2 + 0.2309699292 * s2,
    -1.2684380046 * l2 + 2.6097574011 * m2 - 0.3413193965 * s2,
    -0.0041960863 * l2 - 0.7034186147 * m2 + 1.7076147010 * s2,
  ].map(linearToSrgb));
}

// Sets --color-a/--color-b (the LED's two blink phases, equal when solid) from a type=0x16 light
// ({blinking, color} / {blinking, color_a, color_b}), or clears them when light is null so the
// CSS fallbacks apply.
function setLightColors(el, light) {
  const a = light && (light.blinking ? light.color_a : light.color);
  const b = light && (light.blinking ? light.color_b : light.color);
  el.style.setProperty("--color-a", a ? softenColor(a) : "");
  el.style.setProperty("--color-b", b ? softenColor(b) : "");
}

// The FADERS bar takes only the hue of its bump LED, at full brightness: in INT modes the LED
// dims with the fader, which would otherwise make a low fader's bar near-black. "" (CSS
// fallback) while the LED is unknown or dark.
function barColor(light) {
  const c = light && (light.blinking ? light.color_a : light.color);
  const max = c ? Math.max(...c) : 0;
  return max ? softenColor(c.map(v => Math.round(v * 255 / max))) : "";
}

// The page's own LED-matching colors (style.css :root) get the same Soft <-> Native treatment.
// Their base values are read once, before the first override, so style.css stays the source of
// truth; applyPalette() then writes the softened versions back as inline :root overrides.
const PALETTE_TOKENS = ["--master", "--bumps", "--accent-green", "--accent-red"];
function hexToRgb(hex, token) {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  if (!m) {
    throw new Error(`${token} in style.css must be a 6-digit hex color for softenColor(), ` +
      `got "${hex}".`);
  }
  return m.slice(1).map(h => parseInt(h, 16));
}
const rootStyle = getComputedStyle(document.documentElement);
const paletteBase = Object.fromEntries(PALETTE_TOKENS.map(
  token => [token, hexToRgb(rootStyle.getPropertyValue(token).trim(), token)]));
function applyPalette() {
  PALETTE_TOKENS.forEach(token =>
    document.documentElement.style.setProperty(token, softenColor(paletteBase[token])));
}

// light is null until the fader's first type=0x16 update, then either
// {blinking:true, color_a, color_b} (flashing toward its stored value) or {blinking:false, color}
// -- [r, g, b] straight from the console (green, or red in MEMS), through softenColor().
function setPhysicalFader(i, value, light, mode, labels, memsPage) {
  const meterEl = document.getElementById("physfader-" + i);
  setBar(meterEl, value, true);
  setName(meterEl, physicalFaderLabelLines(mode, i, labels, memsPage));
  const lightEl = document.getElementById("physfader-" + i + "-light");
  lightEl.classList.toggle("blinking", !!light?.blinking);
  setLightColors(lightEl, light);
  meterEl.style.setProperty("--bar-color", barColor(light));
}

// lines is null until the first type=0x15, then [LCD 1 line 1, LCD 1 line 2, LCD 2 line 1,
// LCD 2 line 2], 20 chars each, shown as-is (spacing included) in a monospace panel.
function setLcds(lines) {
  ["lcd-1", "lcd-2"].forEach((id, n) => {
    const el = document.getElementById(id);
    el.classList.toggle("placeholder", !lines);
    el.querySelectorAll(".lcd-line").forEach((lineEl, i) => {
      lineEl.textContent = lines?.[n * 2 + i] ?? "";
    });
  });
}

let lastUpdate = 0;
let lastState = null;  // re-rendered as-is when the saturation slider moves

function render(state) {
  lastState = state;
  document.body.classList.toggle("writable", state.write_enabled === true);
  const dot = document.getElementById("dot");
  const connText = document.getElementById("conn-text");
  dot.classList.toggle("ok", state.connected);
  connText.textContent = state.connected
    ? (state.device ? `connected · ${state.device}` : "connected")
    : "waiting for console…";
  if (state.version) document.getElementById("version").textContent = `v${state.version}`;

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
      el.querySelector(".meter-label").textContent = String(i);
      setName(el, state.labels?.[mode]?.[i]);
    }
  });

  setBar(document.getElementById("fader-master"), state.master);
  setBar(document.getElementById("bumps"), state.bumps);
  setBar(document.getElementById("xfade-live"), state.crossfader_live);
  setBar(document.getElementById("xfade-next"), state.crossfader_next);

  document.getElementById("section-physical").classList.toggle("mems", state.fader_mode === "MEMS");
  document.getElementById("mems-page-badge").textContent = "PAGE " + (state.mems_page ?? "—");
  if (state.fader_mode === "MEMS" && state.mems_page && document.activeElement !== pageSelect) {
    pageSelect.value = String(state.mems_page);
  }
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

  setIndependent(document.getElementById("ind1"), document.getElementById("btn-ind1"),
    state.independent1, state.independent1_clicked, state.independent_labels?.[1]);
  setIndependent(document.getElementById("ind2"), document.getElementById("btn-ind2"),
    state.independent2, state.independent2_clicked, state.independent_labels?.[2]);
  setIndicator(document.getElementById("btn-solo"), state.solo, state.indicator_lights?.solo, true);
  setIndicator(document.getElementById("btn-blackout"), state.blackout,
    state.indicator_lights?.blackout, true);

  setLcds(state.lcd);

  if (activeTab === "dmx") renderDmx(state.dmx);

  lastUpdate = state.last_update;
}

const saturationInput = document.getElementById("saturation");
// Clamped to the slider's own range, so a value saved under an older range can't go past it.
saturation = Math.min(Math.max(saturation, parseFloat(saturationInput.min)),
  parseFloat(saturationInput.max));
saturationInput.value = saturation;
applyPalette();
saturationInput.addEventListener("input", () => {
  saturation = parseFloat(saturationInput.value);
  try { localStorage.setItem(SATURATION_KEY, String(saturation)); } catch (e) { /* not persisted */ }
  applyPalette();
  if (lastState) render(lastState);
});

// Header "INT A/B" checkbox: INT DEV may be all that is needed, so the two 24-wide INT A/B rows
// can be hidden. Display only -- they keep rendering while hidden. With no saved choice yet,
// they start hidden on a phone in portrait (6 columns), where each one is 4 meter rows tall.
const SHOW_INT_AB_KEY = "consolelink.showIntAB";
const showIntAbInput = document.getElementById("show-int-ab");
function applyShowIntAb() {
  ["INT A", "INT B"].forEach(mode => document.getElementById("section-" + mode)
    .classList.toggle("hidden-row", !showIntAbInput.checked));
}
const defaultShowIntAb = currentColumnsPerRow() > 6;
try {
  const stored = localStorage.getItem(SHOW_INT_AB_KEY);
  showIntAbInput.checked = stored === null ? defaultShowIntAb : stored !== "0";
}
catch (e) { showIntAbInput.checked = defaultShowIntAb; }
applyShowIntAb();
showIntAbInput.addEventListener("change", () => {
  try { localStorage.setItem(SHOW_INT_AB_KEY, showIntAbInput.checked ? "1" : "0"); } catch (e) { /* not persisted */ }
  applyShowIntAb();
});

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
try {
  const storedTab = localStorage.getItem(TAB_KEY);
  if (TABS.includes(storedTab)) showTab(storedTab);
} catch (e) { /* stays on Playback */ }

// Buttons marked data-button toggle that console function (BlackOut, Solo, Independents) when
// the server runs with --allow-write. The new state comes back through the normal SSE stream
// (a button only lights when the console reports it), not set locally.
document.querySelectorAll("[data-button]").forEach((el) => {
  el.addEventListener("click", () => {
    if (!document.body.classList.contains("writable")) return;
    fetch(`/api/button/${el.dataset.button}`, { method: "POST" }).catch(() => {});
  });
});

// A fader's Bump LED doubles as its Bump button: the console holds the bump for as long as the
// button is down, so the press goes out on pointerdown and the release on pointerup, with a
// keepalive in between -- the server releases for us if the keepalive stops (page closed, phone
// off Wi-Fi), because the console never releases a button by itself. Like the toggle buttons,
// the LED only lights when the console reports it.
const BUMP_KEEPALIVE_MS = 250;
const heldBumps = new Map();  // fader -> keepalive timer
function postBump(fader, action) {
  fetch(`/api/bump/${fader}/${action}`, { method: "POST" }).catch(() => {});
}
function pressBump(fader) {
  if (heldBumps.has(fader) || !document.body.classList.contains("writable")) return;
  postBump(fader, "press");
  heldBumps.set(fader, setInterval(() => postBump(fader, "keepalive"), BUMP_KEEPALIVE_MS));
}
function releaseBump(fader) {
  if (!heldBumps.has(fader)) return;
  clearInterval(heldBumps.get(fader));
  heldBumps.delete(fader);
  postBump(fader, "release");
}
function releaseAllBumps() {
  [...heldBumps.keys()].forEach(releaseBump);
}
document.querySelectorAll("[data-bump]").forEach((el) => {
  const fader = Number(el.dataset.bump);
  el.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    el.setPointerCapture(e.pointerId);  // the release arrives here even if the pointer slides off
    pressBump(fader);
  });
  ["pointerup", "pointercancel"].forEach(t => el.addEventListener(t, () => releaseBump(fader)));
});
window.addEventListener("blur", releaseAllBumps);
window.addEventListener("pagehide", releaseAllBumps);
document.addEventListener("visibilitychange", () => { if (document.hidden) releaseAllBumps(); });

// MEMS page select (SmartSoft's PAGE dropdown), shown only in MEMS mode (see style.css); without
// --allow-write the read-only badge shows the page instead. The console confirms with its own
// page number, so both follow state.mems_page.
const pageSelect = document.getElementById("mems-page");
for (let p = 1; p <= 12; p++) pageSelect.add(new Option(String(p), String(p)));
pageSelect.addEventListener("change", () => {
  if (!document.body.classList.contains("writable")) return;
  fetch(`/api/mems_page/${pageSelect.value}`, { method: "POST" }).catch(() => {});
});

connect();

setInterval(() => {
  const staleEl = document.getElementById("stale-text");
  if (!lastUpdate) return;
  const age = Date.now() / 1000 - lastUpdate;
  staleEl.textContent = age > 3 ? `last update ${age.toFixed(0)}s ago` : "";
}, 1000);
