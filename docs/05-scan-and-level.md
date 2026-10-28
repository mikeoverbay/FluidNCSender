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
5. **Stop** aborts the scan and the probe at once.

The result auto-saves to `scans/last-scan.xyz` in UGS `.xyz` format. **File → Save Scan…** saves a copy, for
example next to the job. Open a saved scan with **File → Open…** (any `.xyz` file); **Close Scan** removes it.

> Until the probe is homed again after a scan Stop, abort or failed home, its tip may still be down.
> FluidSender warns before X/Y moves.

## Apply the scan (level)

1. Open the job's **original** G-code (not a saved leveled one, see below).
2. In the Scan panel, under **Apply to G-code**, pick the scan (the last one, or a `.xyz` file).
3. **Z surface** (in) is the Z of the G-code's surface, usually 0.
   **Arc segment** (mm) is the length G2/G3 arcs are cut into, 0.1 by default, so arcs follow the surface too.
4. Click the **Apply** box (or **Tools → Auto-level…** to get to it). While the G-code is leveled the box reads
   **Apply: ON** and **blinks bright red**. Click it again to go back to the original G-code.
5. **Save leveled G-code…** writes the leveled file (default `<name>-autoleveled.gcode`). Or just **Run**: while
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

- the **Apply** box is greyed (its tooltip says why), and the server refuses to level it;
- Run's question says **ALREADY LEVELED: … It runs as it is**;
- the visualizer's **Leveled** checkbox stays off (it would draw the heights twice).

To level with a new scan, open the original G-code instead.

### The probe offset: shift Y, scan, put Y back

The probe sits 3 in from the bit in Y. To scan where the bit will cut:

1. Zero the job with the bit as usual (zero Z on the surface at the scan's first point, Min X / Min Y,
   for the most exact depth: the scan's heights are relative to that first touch).
2. Jog the bit to **Y +3** and **Zero Y** there, so the probe is over the job's coordinates.
3. **Start Scan**.
4. Put Y back: **Undo Zero** (exact), or jog to Y −3 and Zero Y.
5. **Apply**. A note says *X/Y zero moved since the scan (Y by +3.0000 in)*: that is expected here.

If X/Y zero has changed since the scan, Apply **warns with the amount but still levels**. Only a different work
coordinate system (G55, etc.) is refused. If you forget step 4, the leveling is off by the amount the note shows.
