# 2. Connect to FluidNC

1. Plug in the controller's USB cable (usually `/dev/ttyUSB0`).
2. Close UGS or anything else that might have the port open. FluidSender refuses a port another program holds,
   and the error names that program.
3. **Machine → Connect…**, pick the port, then **Connect** (115200 baud).

Ports that look like the probe or the pendant (`/dev/ttyACM*`, `/dev/rfcomm*`) ask
*"This looks like the probe/pendant, connect anyway?"* first.

## What FluidSender sends on its own

FluidSender never connects by itself. After you connect, it sends only these commands:

- `?` status, every 200 ms
- `$I` build info, `$G` modal state
- `G20`, so the machine is in inches; it is sent again after any reset banner, once the machine is Idle
- quiet reads of `$/axes/<axis>/max_rate`, `acceleration` and `soft_limits`, and `$/planner_blocks`.
  These cap the jog speed.

The DRO always shows **inches**, and positions FluidNC reports in mm are converted.

## Disconnect

Use **Machine → Disconnect**. If the machine is moving, FluidSender does a safe stop first.

When you close the window, the port is released **10 seconds after the last window closes**, but only when
the machine is at rest: no job, no scan, no motion, and no alarm with the spindle or vacuum still on.
The pendant and probe ports are released at the same time.

FluidNC's port is opened **exclusively**, so UGS can't open it at the same time.
