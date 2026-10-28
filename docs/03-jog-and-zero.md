# 3. Jog, home and zero

## Home and unlock

- **Machine → Home All ($H)…**, or Home X / Y / Z: asks for confirmation first.
- **Machine → Unlock ($X)** clears an alarm without homing. Only use it if you know where the machine is.
- After an alarm that says *Reset to continue*, do **Soft Reset** first.

## Jog panel

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

Under the speed slider in the Jog panel:

- **Vac** toggles M8 (flood), which runs the vacuum relay.
- **AUX** toggles M7 (mist), which runs the AUX relay.

The button state follows the program: an M7/M8/M9 in a running job updates it.
A toggle is refused while the program's own M8/M9 is still waiting, so the two can't fight.

> FluidNC leaves the spindle and coolant **ON** after an alarm. Turn them off by hand or reset.
