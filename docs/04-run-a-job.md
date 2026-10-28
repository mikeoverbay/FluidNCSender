# 4. Run a job

## Open

**File → Open…** (Ctrl+O), or double-click a file in the **Files** panel. The G-code shows in the editor and
the toolpath view, kept in sync with each other. **Open Recent** lists the last files.

You can edit the file. **Save** (Ctrl+S) keeps its CRLF line endings, and **Save As…** is Ctrl+Shift+S.
**Reload from Disk** reloads the file if CarveMaker wrote a new copy.

## Before you run

1. Home, then zero X, Y and Z ([guide 3](03-jog-and-zero.md)).
2. If the surface isn't flat, scan and level ([guide 5](05-scan-and-level.md)).
3. Check the toolpath in the viewer: Top / Front / Iso views are in the View menu.

## Run

**Machine → Run…** (or the Run button). Every job starts with `G20 G90`.
If Run is greyed out, hover over it to see why (not connected, alarm, already running…).

FluidNC 4.x refuses G-code lines over **127 characters**. If the file has one, Run refuses the job and names
the line, so shorten it (drop comments and spaces) and run again.

The job streams with character counting: up to 128 bytes are kept in FluidNC's buffer.
Progress shows in the Machine panel and on the pendant.

## While running

| Action | What it does |
|---|---|
| **Hold (!)** | feed hold: it slows to a stop and keeps its place |
| **Resume (~)** | carries on after a hold or an `M0` |
| **Stop / SAFE STOP** | hold, wait until fully stopped (Hold:0), then soft reset. The job ends. |
| **Soft Reset (Ctrl-X)…** | immediate reset. You may lose position, so re-home if it was moving. |

On an `error:` reply, the job **pauses** so you can decide what to do.

## Overrides

In the Machine panel:

- **Feed:** 10–200 % of the programmed feed (G1/G2/G3)
- **Rapid:** 100 / 50 / 25 % (G0)
- **Spindle (S):** 10–200 %

**Machine → Overrides to 100%** resets all three.

## Vacuum / AUX

The program's `M8` / `M7` / `M9` switch the relays, and the Vac/AUX buttons show their state.
Watch out for one FluidNC quirk: it reads `M8.1` from a later number on the same line, so **`M8 X0.1` turns
the vacuum OFF**. Keep M7/M8/M9 on their own lines.
