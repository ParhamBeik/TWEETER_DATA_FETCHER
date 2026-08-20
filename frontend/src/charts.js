// Chart tokens, kept in one place so every chart reads as one system.
//
// These are the reference dark-mode categorical steps, validated against this
// app's chart surface (#121a2d) with the dataviz validator: lightness band,
// chroma floor, CVD separation, normal-vision floor and contrast all pass.
// The teal UI accent (--accent) is deliberately NOT used for data marks -- at
// OKLCH L 0.85 it sits far outside the dark-mode band. It stays UI chrome.
export const SERIES = ["#3987e5", "#199e70", "#d95926"];

export const AXIS = "#91a1bd";
export const SURFACE = "#121a2d";
export const LINE = "#26334d";

// 4px rounded data-ends anchored to the baseline (vertical vs horizontal bars).
export const BAR_RADIUS_Y = [4, 4, 0, 0];
export const BAR_RADIUS_X = [0, 4, 4, 0];

export const TOOLTIP_STYLE = {
  background: SURFACE,
  border: `1px solid ${LINE}`,
  borderRadius: 8,
  color: "#e5edf9",
};

export const AXIS_PROPS = {
  stroke: AXIS,
  tick: { fill: AXIS, fontSize: 12 },
  tickLine: false,
};
