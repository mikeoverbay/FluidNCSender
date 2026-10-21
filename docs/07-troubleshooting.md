# 7. Troubleshooting

### "Port is in use by …"
Another program has the port, usually UGS. Close it, or in UGS disconnect first. FluidSender never shares a port.

### `/dev/ttyUSB0` doesn't appear, or disappears
- Check the cable: `ls /dev/ttyUSB*`, and `dmesg | tail` right after plugging in.
- On Ubuntu, `brltty` steals CH340 adapters: `sudo apt remove brltty`.
- Permission denied: `sudo usermod -aG dialout $USER`, then log out and back in.

### ALARM after connect or after a limit hit
- Home it (**Home All**), or **Unlock ($X)** if you know the position is good.
- If it says *Reset to continue*, do **Soft Reset** first.
- In an alarm, Hold/Resume are refused, because `!` can leave a critical alarm without homing. Stop just resets.
- FluidNC leaves the spindle and vacuum **on** in an alarm.

### Run is greyed out
Hover over it to see the reason: not connected, alarm, a job or scan already running, or a hold.

### Run refuses: line too long
FluidNC 4.x refuses G-code lines **over 127 characters** (error:14). Remove comments or spaces on the line it names.

### `error:` during a job
The job pauses. Read the console: FluidNC 4.x prints `[MSG:ERR: …]` with the reason.

### The vacuum turned off by itself
A line like `M8 X0.1` reads as `M8.1` in FluidNC, which turns the vacuum off. Put M7/M8/M9 on their own line.

### Level says the X/Y zero changed
That's a warning, not an error: it still levels. Re-scan if you moved the stock itself rather than the zero.

### The window closed but it's still connected
The ports are released 10 s after the last window closes, only when the machine is at rest
(no job, scan, motion, or alarm with outputs on). Reopen the window and **Disconnect** if you need the port now.

### The viewer is blank
Use **View → Reset Layout**, or reload with Ctrl+R. WebGL needs hardware acceleration enabled in Chrome.

### The new version doesn't show up after `git pull`
Disconnect and close every FluidSender window. The launcher only restarts a server that isn't connected.
To restart by hand while **disconnected**:

```bash
pkill -f 'fluidsender/server.py'; ~/fluidsender/fluidsender.sh
```

### Where are the logs?
The **Console** panel shows everything sent and received. The current state is at
`curl -s -H 'X-FluidSender: 1' http://localhost:8765/api/machine/state`.
