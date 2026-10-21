// Short explanations for FluidNC's error:N and ALARM:N replies (Grbl numbering, plus FluidNC's own; 3.9.4 and
// 4.1.1: the same as machine.py's ERRORS and ALARMS).

const ERRORS = {
  1: 'expected a command letter', 2: 'bad number format', 3: 'unknown $ command',
  4: 'negative value', 5: 'homing is not enabled', 6: 'step pulse too short',
  7: 'settings read failed', 8: '$ command only works when Idle', 9: 'locked out during alarm or jog',
  10: 'soft limits need homing', 11: 'line too long', 12: 'step rate too high',
  13: 'safety door is open', 14: 'line too long (FluidNC takes 127 characters of G-code)',
  15: 'jog target is beyond the travel', 16: 'invalid jog command', 17: 'laser mode needs PWM',
  18: 'no homing cycle defined in the settings', 19: 'single axis homing not allowed',
  20: 'unsupported or invalid G-code', 21: 'two G-codes from the same group', 22: 'feed rate not set',
  23: 'needs a whole number', 24: 'two G-codes both need the axis words', 25: 'repeated G-code word',
  26: 'axis words missing', 27: 'line number out of range', 28: 'P or L value missing',
  29: 'unsupported coordinate system', 30: 'G53 needs G0 or G1', 31: 'axis words while G80 is active',
  32: 'arc without axis words in the plane', 33: 'invalid motion target', 34: 'arc radius error',
  35: 'arc offset (I/J/K) missing', 36: 'unused words in the line', 37: 'G43.1 on the wrong axis',
  38: 'tool number too high', 39: 'P value too big', 40: 'a startup pin (door, control or limit) is on',
  120: 'FluidNC is busy with a macro or file of its own (after_reset/after_homing/after_unlock...); try again when it is done',
  177: 'loops (do/while/repeat) need a FluidNC file job: not over USB',
};

const ALARMS = {
  1: 'hard limit: position likely lost, home again', 2: 'soft limit: target beyond the travel',
  3: 'reset while moving: position likely lost, home again', 4: 'probe fail: probe not in its start state',
  5: 'probe fail: no contact', 6: 'homing fail: reset during homing', 7: 'homing fail: door opened',
  8: 'homing fail: could not pull off the switch', 9: 'homing fail: switch not found',
  10: 'spindle control', 11: 'an input pin (door, control or limit) was on', 12: 'homing: ambiguous switch',
  13: 'hard stop', 14: 'unhomed: home the machine', 15: 'init', 16: 'I/O expander reset: reboot FluidNC',
  17: 'G-code error in a FluidNC file job', 18: 'probe touched during a move (probe hard limit)',
};

/** "error:20" -> "unsupported or invalid G-code"; '' for anything else. */
export function explain(line) {
  const m = /^(error|ALARM):(\d+)\s*$/.exec(line);
  if (!m) return '';
  return (m[1] === 'error' ? ERRORS : ALARMS)[+m[2]] || '';
}
