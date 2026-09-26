# Significant Leg ZigZag (TradingView / Pine Script v6)

Draws a fast (high-sensitivity) ZigZag on the chart and highlights only its **significant legs**: fast legs that exactly match a leg of a hidden slow (low-sensitivity) ZigZag. A significant leg is a clean move between two major pivots, with no smaller pivot in between.

## How to use it

1. In TradingView, open **Pine Editor** and create a new indicator.
2. Replace its contents with [`SignificantLegZigZag.pine`](SignificantLegZigZag.pine).
3. Click **Add to chart**.

## Inputs

| Group | Input | Default | Meaning |
|---|---|---|---|
| Fast ZigZag | Left / Right length | 2 / 2 | Pivot window of the displayed ZigZag |
| Slow ZigZag | Left length | 25 | Controls how important a slow pivot is |
| Slow ZigZag | Right length | 5 | Controls how quickly a slow pivot is confirmed |
| Display | Normal leg | gray, 1 | Color and width of normal legs |
| Display | Significant leg | cyan, 3 | Color and width of significant legs |
| Display | Show pending legs | on | Draws candidate legs as dashed lines |
| Debug | Show slow ZigZag | off | Draws the slow ZigZag as a dotted line, to check matches by eye |
| Debug | Show stats table | on | Counts of fast legs, final slow legs and significant legs |

The slow lengths must be at least the fast lengths. Otherwise the script stops with an error.

## Leg states

| State | Look | Meaning |
|---|---|---|
| Normal | gray, thin | No matching slow leg |
| Pending | dashed, significant color | The fast leg that starts at the latest slow pivot. The slow ZigZag has not confirmed its end yet |
| Confirmed | solid, significant style | Matches the latest slow leg. Its end can still move if the slow ZigZag replaces its last pivot, and it then goes back to normal |
| Final | solid, significant style | Matches a slow leg that is followed by another slow pivot. It never changes again |

## Specification

### 1. Purpose
Display a high-sensitivity ZigZag and highlight only its significant legs: legs that exactly match a leg of a hidden, low-sensitivity ZigZag. Scoring is out of scope for this version.

### 2. General
- Pine Script v6, `indicator()` with `overlay = true`.
- Both ZigZags are built with pivot logic only. No ATR, no percentage deviation.
- Both ZigZags use identical construction code, so pivots land on the same bars and prices.

### 3. Inputs
See the table above. Validation: slow left ≥ fast left and slow right ≥ fast right, enforced with `runtime.error()`.

### 4. Pivot detection
- The pivot's bar is `bar_index - right`, and its price is that bar's high or low.
- A pivot becomes known `right` bars after it forms.
- **Decision:** a custom pivot function replaces `ta.pivothigh` / `ta.pivotlow`, so the tie rule is explicit. A pivot high must be *strictly higher* than every bar in the left window and *at least as high* as every bar in the right window (the mirror applies to lows). Among equal extremes the earliest bar wins. The conditions only get stricter as the windows grow, so every slow pivot is also a fast pivot.

### 5. ZigZag construction (same for both)
- **Opposite type to the last pivot:** append it. A new leg is created.
- **Same type:** replace the last pivot only if the new one is strictly more extreme. On equal prices the earlier pivot is kept.
- **Pivot high and pivot low on the same bar:** process the one opposite to the last pivot first, then the other. **Decision:** both are kept, which can make a vertical leg on that bar. The rule is identical in both ZigZags.
- Pivots always alternate high/low, and only the last pivot can change.

### 6. Leg definition
A leg is the segment between two consecutive pivots.

### 7. Significance rule (strict, "Rule A")
A fast leg is significant if and only if a slow leg has the same start bar and price and the same end bar and price. **Decision:** matching compares bar and type. Both ZigZags read the price from the same bar's high or low, so the prices are identical.

### 8. Leg states
See the table above. **Decision:** "pending" is the fast leg that starts at the latest slow pivot, even when newer fast legs follow it. The slow ZigZag confirms later than the fast one (5 bars vs 2), so that leg can still become a match.

### 9. Update and re-evaluation
On every closed bar, update the fast ZigZag, then the slow one. After every append or replace, re-evaluate the legs whose state can still change.

**Decision (fixes a gap in the original spec):** re-checking "the latest one or two legs" is not enough. When the slow ZigZag replaces its last pivot, the fast leg that matched the old pivot can be several fast legs back. The script keeps direct references to the Confirmed and Pending lines, so those lines are always updated, however far back they are.

### 10. Drawing
- Fast legs are drawn with `line.new()` using bar indices. A replaced last pivot moves its line with `line.set_xy2()`.
- `max_lines_count = 500`. The script keeps at most 420 fast pivots and 60 slow pivots, and deletes the oldest line when a pivot is dropped.
- The slow ZigZag is not drawn unless the debug toggle is on.

### 11. Repainting
- Final legs never repaint.
- Only the most recent leg of each ZigZag can change.
- Pending legs are dashed, so any change is visible.
- The ZigZags update only on closed bars, so nothing flickers inside a realtime bar.

### 12. Out of scope (future versions)
- Leg scoring (length, duration, speed, retracement)
- Alerts when a leg becomes confirmed or final
- Relaxed matching mode ("Rule B")

## What to expect

With the defaults, few legs will be significant. A move between two 25-bar pivots usually contains at least one 2-bar pullback, and that pullback breaks the match. Use the stats table ("Clean slow legs" %) to tune the lengths.
