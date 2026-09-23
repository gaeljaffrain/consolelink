const MODES = ["INT A", "INT B", "INT DEV"];
const MODE_PILL_CLASS = {
  "INT A": "active-A",
  "INT B": "active-B",
  "INT DEV": "active-D",
  "PARAM 1": "active-P1",
  "PARAM 2": "active-P2",
  "MEMS": "active-M",
};

// MODES is the source of truth for both the hardcoded HTML sections in index.html (id="section-<mode>"
// with a nested id="intensities-<mode>" row) and every id built dynamically at runtime
// ("intensity-<mode>-<i>", "section-<mode>"). A drift between the two -- e.g. renaming a MODES
// entry without updating the matching HTML -- used to surface as a cryptic null-dereference deep
// inside buildIntensityMeters() or render(), aborting silently partway through. Check it here,
// before anything else runs, so a mismatch fails immediately with a message that names exactly
// which mode and which expected id is missing.
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

// .intensity-row wraps from 24 columns to two 12-column rows below 1600px (see style.css) --
// matches that breakpoint exactly, so layout/group-bar placement always agrees with whichever
// one actually applied.
function currentColumnsPerRow() {
  return window.matchMedia("(max-width: 1600px)").matches ? 12 : 24;
}

// Explicitly places a 24-cell row's .meter children at (row, column) for the given
// columns-per-row, instead of leaving them to plain CSS Grid auto-placement. Needed once a
// row can have group-bar captions AND wrap to two 12-column rows at once: auto-placement packs
// wrapped items straight into consecutive rows with no gap, so there's no row left free
// between them for the first wrapped block's own captions -- confirmed live (meters 13+
// visibly collided with the first block's caption bars) before this existed. Odd rows (1, 3,
// 5, ...) hold each wrapped block's meters; the even row right after each is reserved for that
// block's captions (see projectGroupSegments below) and simply stays empty -- 0 height -- for
// any row with nothing grouped, e.g. every INT A/B/DEV row today.
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

// hideZero: for the 24-cell INT A/B/DEV intensity rows, a 0 value is left blank rather than
// printed as "0" -- with 24 columns per row, scanning for the nonzero ones (which dimmer/
// device is actually on) is much faster when the many zeros don't visually compete with
// them. Master/Bumps/Independents always show their number, even at 0 -- there's only one of
// each, so there's no scanning problem to solve, and always-blank-at-0 would just look broken
// there.
function setBar(el, value, hideZero) {
  if (!el) return;
  const fill = el.querySelector(".fill");
  const val = el.querySelector(".val");
  fill.style.height = pct(value) + "%";
  val.textContent = (hideZero && value === 0) ? "" : pctLabel(value);
  el.classList.toggle("nonzero", value > 0);
}

// The console's own name field is 3 short lines (6/6/5 chars), not one long string -- render
// each on its own row exactly as the console lays it out, rather than word-wrapping a joined
// string to whatever width this column happens to be.
function setName(container, lines) {
  const lineEls = container.querySelectorAll(".name-line");
  for (let i = 0; i < lineEls.length; i++) {
    lineEls[i].textContent = lines?.[i] || "";
  }
}

// The Physical Faders row's per-fader label: what slider 1-24 actually means changes with
// the fader mode, so the label does too. INT A/INT B/INT DEV and PARAM 1/2 are fixed,
// deterministic labels -- not sent on the wire, since fader N always means the same thing in
// these modes -- confirmed against the console's own physical/manual labeling by the
// console's owner. Each entry is up to 3 lines (same 3-line label area the console's own
// fields use elsewhere in this UI), so a wide name like "Strobe/Shutter" can be split instead
// of clipping in the narrow 24-column layout -- pad3() below fills in any missing lines.
// PARAM 2's list is a placeholder (same slot count as PARAM 1, all blank) for the console's
// owner to fill in by hand once confirmed; leave PARAM_1_LABELS as the template to follow.

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

// Generic: derives visual groups from a set of 1-indexed 3-line labels, by finding runs of
// consecutive entries that share the same first line -- e.g. PARAM_1_LABELS' ["Focus","Pan"]
// and ["Focus","Tilt"] are consecutive and share "Focus", so they become one 2-wide group
// without a separate group definition to keep in sync by hand. A run only becomes a group if
// it's at least 2 items AND its shared key is non-empty -- a lone item (e.g. "Intensity",
// "Strobe") doesn't need a caption to group it with anything, and PARAM_2_LABELS' still-blank
// entries (first line "") must never be treated as one giant 24-wide group.
// Returns [{label, start, end}], both 1-indexed inclusive, in column order.
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

// Projects an absolute 1-24 group range onto whichever layout is actually active. At 24
// columns-per-row there's exactly one meter row (row 1) and the caption goes in row 2, same
// as before. Once wrapped to 12-per-row, the 24 meters occupy BOTH row 1 (items 1-12) and row
// 2 (items 13-24) -- there's no longer a free row directly under either half for a
// grid-row-less .group-bar to auto-place into (it would land in row 3 for EITHER half, since
// that's the first row where 1-12 is free either way, merging both halves' captions into one
// row). So each row's captions get an explicit grid-row of their own (3 for row 1's groups, 4
// for row 2's), and a group that would have straddled the 12/13 boundary is split into two
// segments -- one per wrapped row -- each re-numbered to that row's own 1-12 column space.
// (Confirmed live: before this, the FADERS row's group-bars had explicit grid-column values up
// to 24, which forced CSS Grid to create implicit columns to fit them even under the 12-column
// media query, silently keeping the whole row 24-wide and defeating the wrap entirely -- the
// bug this function exists to fix.)
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

// Generic: (re)draws a row's group-bar captions from a [{label, start, end}] list (1-indexed,
// absolute 1-24 column range) -- removes whatever was there before (so this is safe to call
// again whenever the applicable groups OR the wrap layout change) and appends one .group-bar
// per projected segment (see projectGroupSegments), each with an explicit grid-column AND
// grid-row so its placement doesn't depend on auto-placement guessing correctly.
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

// Computed once (PARAM_1_LABELS/PARAM_2_LABELS don't change at runtime) so both the group-bar
// row and each individual fader label can agree on exactly which faders are actually grouped,
// without recomputing/duplicating that logic in two places.
const PARAM_GROUPS = { "PARAM 1": computeLabelGroups(PARAM_1_LABELS), "PARAM 2": computeLabelGroups(PARAM_2_LABELS) };

// Set in render() whenever the mode changes; read here and by relayoutAllRows() below. Declared
// before its first use (relayoutAllRows() runs immediately, once, right after this) rather than
// down next to render() -- a `let` is in its temporal dead zone until its declaration actually
// runs, and that immediate call would otherwise hit it before render() ever gets a chance to.
let lastFaderGroupMode = null;

// Draws the FADERS row's group-bars for whichever mode last rendered (lastFaderGroupMode,
// updated in render()), using the layout that's ACTUALLY active right now. Called both from
// render() (on a mode change) and from relayoutAllRows() below (on a wrap-layout change) --
// a plain resize can cross the 1600px breakpoint with no mode change involved at all, and the
// bars need to be re-projected either way.
function updateFaderGroupBars() {
  renderGroupBars(document.getElementById("physical-faders"),
    PARAM_GROUPS[lastFaderGroupMode] || [], currentColumnsPerRow());
}

// Every 24-cell row's .meter positions (layoutMeterGrid) and the FADERS row's group-bars both
// depend on the SAME columns-per-row layout, so both need redoing together whenever it changes
// -- not just at page load, but on any resize that crosses the 1600px breakpoint, since
// layoutMeterGrid's explicit grid-column/grid-row values are static once set and don't
// magically update themselves the way plain CSS auto-placement would have.
const ALL_INTENSITY_ROW_IDS = ["intensities-INT A", "intensities-INT B", "intensities-INT DEV", "physical-faders"];
function relayoutAllRows() {
  const columnsPerRow = currentColumnsPerRow();
  for (const id of ALL_INTENSITY_ROW_IDS) layoutMeterGrid(document.getElementById(id), columnsPerRow);
  updateFaderGroupBars();
}
relayoutAllRows();
window.matchMedia("(max-width: 1600px)").addEventListener("change", relayoutAllRows);

// A ["Group","Item"] entry's group name is already shown once, in the group-bar underneath the
// whole group -- repeating it on every member fader's own label too (the original behavior)
// just says "Focus" four times over for a 2-wide Focus group. So when this fader is actually
// part of a group (checked against the same PARAM_GROUPS used to draw the bars, not just
// "does this entry have a 2nd word" -- see below for why that distinction matters), only the
// distinguishing 2nd word is shown here. A solo entry (e.g. ["Strobe","Shutter"]) that ISN'T
// part of any real group -- computeLabelGroups only forms a group from >=2 consecutive
// same-key entries, so a lone 2-word entry never gets a group-bar of its own -- keeps showing
// both words exactly as before, since there's no group-bar standing in for the first one.
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

// INT A/INT B/INT DEV show the same patched channel name as that mode's own detail row below
// (state.labels[mode][i], decode_0x09_labels in protocol.py) instead of a generic "Int A:1" --
// fader N always means "channel N of this mode" so the generic label was pure redundancy once
// the real name is known; left blank (not a "?" placeholder) when the console hasn't sent a
// name for that slot, since an unnamed channel isn't an error state, just unlabeled.
//
// MEMS's per-fader names come from the console's own memory ("Look") names -- decoded by
// decode_0x00_memory_name in protocol.py and fed into state.labels["MEMS"][page][slot]
// (both 1-indexed) by server.py. MEMS has (at least) 4 memory pages -- hold the MEMS button,
// press Bump 1-12 to pick one -- and state.mems_page (from decode_0x17_mems_page) tracks
// which one is current, confirmed live across all 4 by matching the console's own LCD text.
// The page/slot split itself is confirmed against real named memories on two different pages
// (traces/mems_names_page1-2-3.log: "page2 mem1" decoded at page 2 slot 1, self-describing by
// design) -- so unlike the earlier page-1-only guess, this now looks up the label under
// whichever page is actually current.
function physicalFaderLabelLines(mode, i, labels, memsPage) {
  if (MODES.includes(mode)) return labels?.[mode]?.[i] || ["", "", ""];
  if (mode === "PARAM 1") return paramFaderLabel(mode, i, PARAM_1_LABELS[i - 1]);
  if (mode === "PARAM 2") return paramFaderLabel(mode, i, PARAM_2_LABELS[i - 1]);
  if (mode === "MEMS") return labels?.["MEMS"]?.[memsPage]?.[i] || ["?", "", ""];
  return ["", "", ""];
}

// state is null until the first type=0x16 message arrives (still "placeholder" until then),
// then "on"/"off"/"blinking" (the console's own indicator LEDs are driven by a pair of colors
// on the wire, and "blinking" is just the case where that pair disagrees).
// keepLabel is for SOLO/BLACKOUT, whose .light already has a permanent text label baked into
// the HTML -- skip overwriting it with the "?"/empty placeholder text.
function setIndicator(el, state, keepLabel) {
  if (!el) return;
  el.classList.toggle("placeholder", state === null);
  el.classList.toggle("on", state === "on");
  el.classList.toggle("blinking", state === "blinking");
  if (!keepLabel) {
    el.querySelector(".light").textContent = state === null ? "?" : "";
  }
}

// IND 1/2 show a live 0-255 value, like Master/Bumps -- via the same generic bar widget --
// plus a click button showing the Independent's patched name. `value` and `clicked` are two
// genuinely separate bits on the wire (confirmed against a real capture): `value` is a
// stored/preset level that stays constant regardless of click state -- it does NOT drop to 0
// on its own -- while `clicked` is whether that level is currently reaching output. So the
// bar shows `clicked ? value : 0`, the same "stored level only reaches output while engaged"
// pattern already confirmed for Bump buttons, not the raw value unconditionally.
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

// The wire only ever confirmed a single brightness byte per fader (see decode_0x16_bump_catch
// in protocol.py for why reading it as RGB was wrong) -- the LED's actual hue isn't sent
// per-fader at all. Per the console's owner, checked against real hardware: the Bump LED is
// green in every fader mode except MEMS, where it's red. So hue is picked here from the
// currently known fader_mode, not from the wire value, and the wire byte only scales
// brightness. Same RGB triples as --accent-green/--bad in style.css, so this row's colors
// stay visually consistent with the rest of the UI.
const FADER_HUE_MEMS = [255, 92, 92];
const FADER_HUE_DEFAULT = [211, 248, 181];
function faderColor(mode, value) {
  const hue = mode === "MEMS" ? FADER_HUE_MEMS : FADER_HUE_DEFAULT;
  const scale = value / 255;
  return hue.map(c => Math.round(c * scale));
}

// light is null until the first type=0x16 block for this fader has arrived, then either
// {"blinking": true, "value_a": 0-255, "value_b": 0-255} (fader hasn't caught its stored value
// yet -- the console's own Bump LED is genuinely flashing between these two brightness levels)
// or {"blinking": false, "value": 0-255} (solid, latched/at rest).
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
  // Only PARAM 1/2 have groupable labels (a shared first word across consecutive faders) --
  // INT A/B/DEV show real patched channel names (nothing to group) and MEMS's are free-form
  // memory names. Recomputed only when the mode actually changes, not on every render tick --
  // PARAM_GROUPS is static, never depends on live state. Same PARAM_GROUPS paramFaderLabel()
  // uses, so the bar and the per-fader labels can never disagree about what's grouped.
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
