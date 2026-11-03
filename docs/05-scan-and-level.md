# 5. Surface scan and auto-level

For engraving or V-carving on stock that isn't flat: probe a grid of points, then bend the G-code's Z to follow
the surface. **Tools → Surface Scan…** shows the Scan panel.

## The probe

The probe ("the nano") is an Arduino board that drives its own Z, on a USB port such as `/dev/ttyACM0`
at 115200 baud. Its commands are:

| Command | Reply | Meaning |
|---|---|---|
| `H` | `homed` | retract fully; the next touch reads 0.0 |
| `ZR:0.200` | `OK ZR=0.200` | how far it backs off after a touch (in) |
| `P` | `-0.0090` | probe down: Z in inches, relative to the first touch after `H`. `E` means over travel |
| `A` | — | stop moving at once |

## Scan

1. Zero X, Y and Z as you will for the job.
2. In the **Scan** panel, **Connect** the probe and then **Home Probe**.
3. Set the grid:
   - **Min X / Max X, Min Y / Max Y** (in): the area to probe
   - **X points / Y points**: for example 15 × 5 = 75 points
   - **Z retract** (in): how high to lift between points, 0.2 by default
4. **Start Scan**. The scan is **refused if work Z is below 0.2 in**, so the bit can't be dragged.
   It probes in UGS's zigzag order, and the progress shows the point number and the last Z.
5. **Stop** aborts the scan and the probe at once, then **homes the probe (H)** by itself, so its tip goes up
   without anyone having to remember. If the probe doesn't confirm, the message says so: home it before moving.
   Then, once the machine has stopped, **Z goes all the way up** (`G53 G0 Z` to the top of FluidNC's Z travel,
   less 0.04 in). Not in an ALARM ("Unlock first, then raise Z"), nor when FluidNC's Z travel is unknown ("Raise Z
   by hand"). The machine's Stop (Soft Reset) and the pendant's Stop do the same.

The result auto-saves to `scans/last-scan.xyz` in UGS `.xyz` format. **File → Save Scan…** saves a copy, for
example next to the job. Open a saved scan with **File → Open…** (any `.xyz` file); **Close Scan** removes it.

> If the probe could not be homed (an abort, or a home that failed), its tip may still be down.
> FluidSender warns before X/Y moves.

## Touch probe (the bit as the probe)

No auto probe? The Scan panel's **Touch probe** tab measures the surface with the bit itself, through FluidNC's
probe input (the user's FluidNC: `probe: pin: gpio.2:pd`). For a blank copper-clad PCB, or any conductive stock.
The two tabs are completely separate: their own settings, buttons and progress. Only the result is shared (see
below).

**Wiring:** spindle **OFF**. One lead clipped on the copper, the other on the bit (or the collet). When the bit
touches the copper the circuit closes, and FluidNC's status shows `Pn:P`. The tab shows it live: *Probe input:
open* or *CLOSED*.

1. Zero X, Y and Z with the bit as usual (Z on the copper).
2. In the **Touch probe** tab set the grid (**Min / Max X and Y**, **Points**, or **Use G-code area**). There is
   **no offset**: the bit touches where it is. Also set:
   - **Clearance** (in, 0.1): the work Z the bit travels at between points. It must clear the clamps.
   - **Probe depth** (in, 0.1): how far below work Z 0 a touch keeps looking before it gives up.
   - **Probe feed** (in/min, 2): how fast the bit comes down.
3. **Probe check**, before **every** touch scan: touch the clip to the bit, then let go. FluidSender must see
   the probe input go open → CLOSED → open in FluidNC's own reports; the tab then says **Probe check passed ✓**.
   There is no button for it. Each scan uses it up. A reset, an alarm, a disconnect, or 10 minutes clear it.
4. **Start Touch Scan**. It is off (and the server refuses it) until the check has passed, and while the probe
   input is closed, the spindle is on (or FluidNC still shows a speed: after M5 it does until something moves), or
   the area is all 0. If the spindle comes on during the scan, the scan ends and the machine is stopped.
   At each point: `G0` to X Y at the clearance, `G38.2 Z-<depth> F<feed>` down until the bit touches, then back up
   to the clearance.
5. **Pause** takes effect between points: once the bit is back up at the clearance, it waits there (*Paused at
   point n of N*). **Resume** goes on with the next point.
6. **Stop** stops the machine safely (Hold, then Soft Reset once it has stopped), then **raises Z all the way
   up**, like the auto probe tab. It never sends anything to the auto probe.

**No contact** (the bit went down to −depth without touching): FluidNC raises ALARM:5 and the scan ends at once:
*No contact at point 3 (X0.5000 Y0.2500): is the clip on the copper?* **Unlock**, then raise Z.

**The heights** are the copper's **work Z** at each point (FluidNC's `[PRB:]` position less the work offset): 0 where
the copper is at work Z 0. The auto probe's heights are relative to its first touch, on which you zero Z, so both
mean the same thing with **Z surface 0**. A touch scan stays right even if Z was zeroed elsewhere on the copper.

**Shared:** both tabs write the same `scans/last-scan.xyz` (UGS format, with FluidSender's setup line), and
"the last scan" for **Apply loaded scan** is whichever scan was made most recently, of either kind. Each tab's
**Save Scan…** saves its own scan.

**Before you run:** after a touch scan, Run's question starts with **⚠ Remove the probe clip from the bit before
running** (until FluidNC is disconnected).

### Touch off Z (touch plate)

Sets work **Z 0 on the stock's top** with a touch plate (the metal "puck" with a lead), in the same tab's
**Touch off Z** column. Spindle **OFF**.

1. Once: measure the plate's thickness (calipers) and type it in **Plate** (in). It is remembered; until it is set
   (0) Touch off Z is off. Also there: **Search** (in, 1; up to 3): how far down the bit looks for the plate;
   **Feed** (in/min, 2); **Lift** (in, 0.25): how far it goes back up afterwards.
2. Put the plate on the stock **under the bit**, its lead plugged in; the **clip on the bit**. Jog the bit down
   to a little above the plate (less than the Search distance).
3. **Probe check**: touch the plate to the bit, then let go (*Probe check passed ✓*), as for a touch scan.
4. **Touch off Z**: two clicks (the second 0.5 to 3 s after the first; a key never presses it). The bit comes down
   (`G38.2`, every line `G20 G90`) until it touches the plate; work Z there becomes the plate's thickness
   (`G10 L20 P0 Z<plate>`), so **Z 0 is the stock's top**; then it lifts by **Lift**.
5. Take the plate away and the clip off the bit (Run's question reminds you, as after a touch scan).

**Undo Zero** puts the Z zero from before back, like any zero (the Machine panel says *(touch off)*).
**No contact** within the Search distance ends it at once with nothing set (ALARM:5: **Unlock**, then raise Z, or
start closer to the plate). **Stop** (the column's, the machine's or the pendant's) stops safely and raises Z all the
way up. The probe check is used up by each touch-off, as by each scan.

## Apply the scan (level)

1. Open the job's **original** G-code (not a saved leveled one, see below).
2. **Z surface** (in) is the Z of the G-code's surface, usually 0.
   **Arc segment** (mm) is the length G2/G3 arcs are cut into, 0.1 by default, so arcs follow the surface too.
3. Under **Apply to G-code** (or **Tools → Auto-level…** to get there) there are two boxes, and **only one can be
   on**:
   - **Apply loaded scan (name)**: levels with the scan on screen: the scan just made here, or a `.xyz` you
     opened with **File → Open…** (or by clicking it in the Files panel). Its label names that scan.
   - **Apply .xyz file to G-code…**: opens the file dialog; the `.xyz` you pick **replaces the loaded scan** (the
     visualizer shows its data) and levels the G-code at once.

   The one that is on **blinks bright red and names its scan** (*Loaded scan scan.xyz: ON*, or *other.xyz: ON*).
   Click it again to go back to the original G-code. Click the other box to level with that scan instead. If more
   than a quarter of the G-code is outside the scan, a red **⚠ WRONG SCAN?** line says so, and Run's question
   does too.

   Opening a scan only draws it in the visualizer (the **Scan** checkbox); nothing is leveled until you click
   **Apply loaded scan**.
4. **Save leveled G-code…** writes the leveled file (default `<name>-autoleveled.gcode`). Or just **Run**: while
   Apply is on, the leveled version is what gets sent, and Run's question says **LEVELED**.

Leveled Z = G-code Z + scan height − Z surface: where the scan reads higher, the cut is raised by that much.
The result matches UGS's auto-leveler byte for byte.

### Saved leveled files are never leveled twice

**Save leveled G-code…** writes a line in the file's header, right after a first `%`:

```
(FluidSender: auto-leveled, do not level again: scan.xyz 30x30 pts Zsurf 0 2026-10-27 03:13)
```

When you open a file with that line, or one whose name ends in `-autoleveled` (UGS's and FluidSender's default
name), FluidSender treats it as **already leveled**:

- both **Apply** boxes are greyed (their tooltips say why), and the server refuses to level it;
- Run's question says **ALREADY LEVELED: … It runs as it is**;

To level with a new scan, open the original G-code instead.

### The probe offset: offset the start corner, never move the zero

The probe sits **3 in toward −Y** from the spindle centerline. Don't shift X/Y zero to scan. Set **Offset from
spindle** in the Scan panel's **Probe** section instead (it is saved in settings.json), and the scan offsets the
start corner for you and shifts every point back to the spindle centerline:

1. Zero X, Y and Z with the bit as usual (zero Z on the surface at the scan's first point, Min X / Min Y,
   for the most exact depth: the scan's heights are relative to that first touch).
2. In the Probe section set **Offset from spindle X 0, Y 3** (once: it is remembered). It is how far the spindle is from the probe: the probe touches at the bit's position minus it.
3. Set **Min / Max X and Y to the job's own area**, where the bit will cut (for example X 1–8, Y 1–11.5).
4. **Start Scan**. The bit goes to Y + 3 (here Y 4–14.5) so the probe lands on that area. The saved scan is
   in the job's coordinates, ready to level.
5. **Apply**, then **Run**. Don't re-zero X, Y or Z between the scan and the end of the job.

**First run:** FluidSender starts with Min / Max X and Y all **0** and the offset at **0, 0**, so nothing
made up can send the machine somewhere. **Start Scan is refused** while the area is all 0 ("Set the scan area
first"). With the offset at 0, 0 the panel shows an amber note, and Start Scan's question opens with a
**WARNING** you must OK: 0, 0 is right only if the probe is on the spindle centerline.

**Why:** a leveled G-code has the scan's heights built in at fixed X/Y spots. Re-zero X or Y after leveling and
the heights land in the wrong places: the cut has high and low spots that aren't on the stock. If you must
re-zero, open the original G-code and level it again.

With the offset at 0, 0 you would have to type the box 3 in higher in Y, and the saved scan would then be 3 in
off in Y for leveling (high spots in the wrong places). Use the offset.

The old way (jog to Y +3, Zero Y, scan, put Y back) still works but is easy to get wrong. If X/Y zero has
changed since the scan, Apply **warns with the amount but still levels**. Only a different work coordinate system
(G55, etc.) is refused.
