# 3. Jog, home, zero and macros

## Home and unlock

- **Machine → Home All ($H)…**, or Home X / Y / Z: asks for confirmation first.
- **Machine → Unlock ($X)** clears an alarm without homing. Only use it if you know where the machine is.
- After an alarm that says *Reset to continue*, do **Soft Reset** first.
- In the Machine panel, **Home ($H) / Home Z ($HZ) / Unlock ($X)** sit right under the X/Y/Z position
  (in an alarm they move up under Run / Hold / Resume / Soft Reset).

## Motion control → Jog

The **Motion control** panel (under the Machine panel) has two tabs: **Jog** and **Macros** (below). It
remembers the one you used last.

- **3×3 pad** for X/Y, with diagonals, plus **Z+ / Z−** and **Stop**.
- **Step buttons** along the bottom: **10, 1, 0.1, 0.01, 0.001 in**.
- **Click** a direction to move one step when you release the button.
- **Hold** it for half a second or more to jog continuously. It stops when you let go. A 0.4 s deadman on the
  server stops the jog if the page goes quiet.
- **Speed slider:** one jog speed, capped at the X/Y max rate FluidNC reports. Z jogs are capped at Z's max.
- FluidNC clamps jogs at the soft limits.

## Zero (the "2nd chance")

Zero and Go-to buttons need **two clicks**: click once, and the button arms. Then click again
**between 0.5 and 3 seconds later**. Double-clicks and Enter/Space never count, so a bumped button does nothing.

- **Zero X / Y / Z** or **Zero All**, in the Machine panel or the Machine menu.
- **Click a DRO value** to type a value for that axis (for example, set Z to 0.75 above the spoilboard).
- **Undo Zero…** goes back to the zero before. The last 5 are kept in `settings.json`, through resets and restarts.

## Go to zero

- **Go to XY 0…** moves X and Y to work zero.
- **Go to Z 0…** moves Z to work zero.

Both use the 2nd-chance click. Raise Z before going to XY 0 if there are clamps in the way.

## Vacuum and AUX

Under the speed slider in Motion control → Jog:

- **Vac** toggles M8 (flood), which runs the vacuum relay.
- **AUX** toggles M7 (mist), which runs the AUX relay.

The button state follows the program: an M7/M8/M9 in a running job updates it.
A toggle is refused while the program's own M8/M9 is still waiting, so the two can't fight.

> FluidNC leaves the spindle and coolant **ON** after an alarm. Turn them off by hand or reset.

## Macros

**Motion control → Macros** holds buttons that run your own G-code lines. FluidSender keeps them in
`settings.json`, so every window shows the same ones.

The defaults:

| Macro | G-code | What it does |
|---|---|---|
| **Park X axis** | `G53 G0 X0` | Rapid to machine X 0 |
| **Park Y axis** | `G53 G0 Y0` | Rapid to machine Y 0 |
| **Park Z axis** | `G53 G0 Z0` | Rapid to machine Z 0 (all the way up) |

These are rapid moves to machine zero, **not** a homing cycle: they don't touch the switches (use Home for that).

- **Two clicks** run a macro, like Zero and Go to: the first turns it amber ("Park Z axis? click again"), the
  second runs it. Mouse only: no key presses it.
- A macro runs only when connected and **Idle**, with no job, scan or jog going. Otherwise its button is grey,
  and its tooltip says why. The tooltip also shows its G-code.
- Its lines go one after another, as if typed in the Console (the Console shows them, and the tab says
  "Running macro …: line 2 of 3"). The first error or ALARM stops it; **Hold**, **Stop** and **Soft Reset** do too
  (the lines not sent yet are dropped).
- **+ New macro** writes one: a name (up to 40 characters) and its G-code, one line each (up to 50 macros).
  **✎** edits one, **🗑** deletes it (asks first). A line the Console wouldn't send (one with `?`, `!` or `~`, or
  too long for FluidNC) is refused when you save. Editing works while not connected.
- **Restore defaults** puts the Park macros back (asks first). Your own macros stay.

> **`G53 G0 X0` moves at the height Z is at now.** With the bit down in the work, park Z first, or write a
> macro that raises Z before it moves X and Y, for example *Park all*:
>
> ```
> G53 G0 Z0
> G53 G0 X0 Y0
> ```
