# Triaging a Fluke 5700A/5720A with `caldmm-5700a-console`

A worked service session, in the order you will actually do it, for a
calibrator that arrives with a suspected **A14 High Voltage Control PCA**
fault.

Every transcript below is real output from the app driving a simulated 5720A
(a `FakeTransport` scripted with faults 3101, 3117, 3122 and 3128, 412-day-old
dc zeros and 8 minutes of warm-up). Numbers from your unit will differ; the
shape of the screens will not.

```bash
caldmm-5700a-console                      # REPL on GPIB 4
caldmm-5700a-console --address 6          # a different address
caldmm-5700a-console --bus 1 --address 4  # a second USB-GPIB adapter
caldmm-5700a-console --port /dev/ttyUSB0  # the rear-panel RS-232C port instead
caldmm-5700a-console --service            # unlock the guide §16 service commands
caldmm-5700a-console --status             # one-shot: print the status screen and exit
caldmm-5700a-console --faults             # one-shot: drain the fault queue and exit
caldmm-5700a-console --diag D5700         # one-shot: run a diagnostic and exit
```

Exit status: `0` every requested action succeeded, `1` one was refused or
failed (the rest are abandoned), `2` a bad flag combination.

---

## Read this before you plug anything in

**There is no remote abort for the long-term commands.** `*TST?`, `DIAG`,
`CAL_ZERO`, `CAL_CHK`, `CAL_REF` and `CAL_ADJ` block for minutes, and `STBY`
sent while one runs is rejected with fault 2002 (guide §19.2). Once started,
**the front-panel STBY key or the line switch is the only stop.** Keep one
within reach. Ctrl-C during a long-term command is noted by the console and
deliberately *not* obeyed — obeying it would leave the calibrator running a
command nobody is watching.

**`OPER` is silently inhibited above 22 V while the fault queue is
non-empty.** On a unit with a latched A14/A15 fault you can ask for 100 V,
get no error, and have no output. That is the expected behaviour of a sick
5700A, not a bug in the console. Drain the queue (`faults`) before concluding
anything about the high-voltage section.

**The console refuses to start a long-term command while ≥22 V is live**, for
the first reason above: a command you cannot abort must not be started with a
dangerous output on the terminals.

**Energizing ≥22 V requires a typed `yes`.** A bare Enter is not enough. This
now includes the three commands that edit a **live** output without going
through `oper`: `mult`, `incr` and `newref` read `ISR?` first, and above 22 V
(or 1 A, where the firmware has no auto-standby of its own) they ask for the
same typed `yes` before anything is sent. Declining sends nothing.

**Nothing on the bus can see which terminals a cable is plugged into.** Every
other interlock — meter range, input ratings, volt-hertz product — is blind to
this, and sourcing volts into a DMM's *current* input drives the calibrator
into roughly 0.1 Ω: a blown fuse at best, a damaged shunt and protection
network at worst. The calibrator will do it without complaint, because from its
side a short is just a load. This is what protects `caldmm-cal-verify` and the
adjustment procedures (`--adjust`) — not this console, which is a manual REPL
where the operator is the one deciding what is on the terminals for every
command:

1. **Declare the wiring, with no default.** A plan's connection (2-wire,
   4-wire, …) is fixed at generation time and is what chooses the wiring
   prompt's text; nothing infers it from an instrument name or a range.
2. **Prove it before trusting it, every run.** Before the first point, at
   every wiring-group boundary, on every resume, and whenever the calibrator's
   own range changes, the engine sources a level that cannot hurt anything —
   `max(10 mV, 0.1 % of range)` for volts (at the point's own frequency for ac
   and wideband), `max(100 µA, 0.1 % of range)` for current, the standard
   itself for resistance — and requires the meter to read it back within
   10 %. 10 mV is chosen for what it does when the leads are wrong: into a
   current shunt it is about 100 mA, harmless to a 1 A input, while into the
   voltage input it is ten thousand times the lead thermal EMF and
   unmistakable; the voltage and current terminals are a different pair, so a
   misplaced cable reads ≈ 0 V. This also catches an open lead, a lead in
   SENSE only, and a meter left in the wrong function. A proof that fails
   writes its row flagged `wiring-proof-failed` and stops the run with the
   output in standby; no CLI flag disables it. The complementary backstop
   runs on every reading, proof or point: an error more than **ten times**
   the point's own tolerance (`GROSS_ERROR_FACTOR = 10`) stops the run too,
   flagged `gross-error-abort` — a real out-of-tolerance instrument is a
   `FAIL`, not this.

The engine also never relies on meter autorange: the meter is placed on a
fixed range chosen from the point's nominal *before* the calibrator goes to
operate, and above the hazard thresholds (**> 22 V** or **> 220 mA**) that
range is read back and compared with what was applied — a mismatch aborts
before `OPERATE` (`meter-range-mismatch`). Full detail, with the exact
sequencing, is `docs/calibration-howto.md`'s "What every run does before it
trusts the wiring" and "Ranges are chosen before anything is energised".

**Never `timeout N` a script that energizes an output.** SIGTERM's default
action terminates the interpreter without running `finally`, so the output is
left live and the meter left free-running — and a free-running 3458A wedges the
bus hard enough that a device clear will *not* recover it. Board-level
`gpib.interface_clear(0)` is then required. Install a SIGTERM handler that
raises instead, or run detached and poll.

**The console itself does install them.** `caldmm-5700a-console` handles
SIGTERM *and* SIGHUP (`install_termination_handlers`), so a `kill`, a
`timeout`, a systemd stop or a dropped ssh session unwinds through the same
`finally` a Ctrl-C does and the output goes to STANDBY. Your own scripts still
have to do this for themselves.

**Choosing the 3458A's ac technique matters more than it looks.** `SETACV SYNC`
needs a repetitive signal and locks with a level-crossing detector
(`SSRC LEVEL,AUTO`); below roughly 100 mV it cannot hold lock and returns
readings that look like enormous calibrator errors — measured on a 1.9 mV tone:
−17.8 % with a standard deviation of 28 % of the signal under SYNC, versus
+0.374 % with 0.1 µV of noise under `SETACV ANA`. Above ~190 mV the preference
reverses, SYNC being five times the better method (0.010 % against 0.03 %).

**The display cannot be turned off, and driving the unit remotely keeps it
lit.** There is no display, blank, dim or brightness command anywhere in the
5700A set — checked against [OM Table 5-1/5-2], the consolidated remote
programming guide, and the service manual. The only power-saving feature is
the front-panel screen saver, which blanks the Control Display after 30
minutes of inactivity **except** when ([OM §3-3]):

- a Setup Menu or submenu is displayed,
- **the calibrator is under remote control**, or
- **the calibrator is calibrating or running diagnostics.**

A long `CAL_CHK` trips the last two at once, so on an hour-long run the VFDs
stay on and nothing can be done about it. What *does* help is that the driver
sends **GTL on `close()`**, so a cleanly-exited session drops the panel back
to local and the 30-minute saver becomes eligible again during idle time. A
script killed mid-session leaves the unit in remote with the display lit
indefinitely — reason enough to let scripts finish rather than Ctrl-C them.

---

## 1. Link check and the opening screen

Start with the cheapest possible end-to-end test — `ECHO?` round-trips a
string through the instrument's parser without touching a single setting:

```
5700A> echo link
  ECHO? -> 'link'
```

If that answers, the address, the cable, the interface and the instrument's
command parser are all alive. If it does not, nothing below will tell you
anything useful.

### Over RS-232C instead of GPIB

`--port /dev/ttyUSB0` (plus `--baud` if the port has been set faster than the
factory 9600) uses the rear-panel serial port. Everything in this document
works the same over it; four things differ underneath, and all four are
handled for you:

| GPIB | RS-232C |
|---|---|
| Device Clear is a bus message | Control/C ([OM §5-7]) |
| Remote comes from the REN line | the `REMOTE` command, sent at open |
| serial poll reads and clears RQS | `*STB?`, where bit 6 is MSS and needs no clearing |
| responses are always machine-readable | the port must be in `COMP` style |

That last one is the only one you can trip over. The factory-default response
style is `TERM`, whose replies are prose for a human at a terminal — nothing
can parse them. The console sends `SP_SET COMP` before it reads anything, so
a factory-default unit just works; the setting is saved in the calibrator's
nonvolatile memory (port configuration, not calibration memory), so it is a
one-time change. If it cannot take — most often because the front panel has
GPIB, not RS-232C, selected as the remote-control interface — you get:

```
the serial port is still in TERM response style after SP_SET COMP, so no
response can be parsed. ...
```

Two other serial notes. There is **no SRQ over the serial port** (guide §2.5),
so nothing can interrupt you; the status registers still work and the console
polls them exactly as it does over GPIB. And `CAL_PR` report printouts can
only go *out* the serial port when the serial port is not the control port —
if you are controlling over RS-232C, use `calrpt` (which reads the report back
over the link) rather than printing it.

The console prints an identity/warm-up/zeros banner as it opens, which is the
first real triage screen:

```
── Fluke 5700A/5720A service console ────────
  FLUKE 5720A  s/n 5248000  firmware 1.0+B+*
  options: 5725A Attached
  powered on for 8 min this session
  ! WARM-UP: specifications need twice the time since last warmed up, up to 30 min (guide §11.6). Readings taken now are indicative only.
  days since dc zeros : 412
  ! DC ZEROS ARE 412 DAYS OLD (limit 30). The calibrator logs fault 1303 and its specifications do not apply until 'calzero' is run (guide §11.6).
  days since calibration: 512 of a 365 day interval
  ! calibration is 147 days past its interval; UNCERT? no longer describes this unit.
  type 'help' for the command list, 'quit' to leave (the output is dropped to standby on exit)
```

Three things to take from it before you go further:

- **warm-up** — specifications assume twice the off-time, up to 30 minutes.
  Anything measured in the first half hour is indicative only.
- **dc zeros** — over 30 days the calibrator logs fault 1303 and *its dc
  specifications do not apply*. A dc number read off a unit with 412-day-old
  zeros is a statement about the zeros, not about the instrument.
- **calibration interval** — past it, `UNCERT?` no longer describes the unit.

The individual queries, if you want them separately:

```
5700A> idn
  FLUKE,5720A,5248000,1.0+B+*
  FLUKE 5720A  s/n 5248000  firmware 1.0+B+*
5700A> opt
  *OPT? 5725A Attached
5700A> ontime
  ONTIME? 8 min since power-up
  ! specifications need twice the time since last warmed up, up to 30 min (guide §11.6)
5700A> caldays
  CAL_DAYS? CAL    512 days
  CAL_DAYS? ZERO   412 days
  ! the dc zeros are past their 30 day limit (fault 1303 territory); specifications do not apply until 'calzero' is run.
```

`--no-banner` skips the opening screen when you are scripting.

## 2. `FATALITY?` — the separate fatal-fault list

The fault queue (`faults`) and the **fatal**-fault list are two different
things. A fatal fault is one the instrument recorded as having stopped it, and
it survives in its own list; draining the fault queue does not clear it.

`FATALITY?` is a guide §16 service-only command — the driver refuses it unless
you started with `--service`, because the calibrator answers fault 2217
*"Command Reserved For Service"* to an operator-level session:

```
5700A> fatality
  ! Fluke5700AError: FATALITY? is a service-only command (guide §16; the calibrator answers fault 2217 'Command Reserved For Service'). Construct the driver with service=True if you really mean to use it — it is not part of the documented operator command set.
```

With `caldmm-5700a-console --service`:

```
5700A> fatality
  0
```

`0` means the list is empty. `fatalclr` clears it, also `--service` only.

## 3. `*TST?` — and time it

```
5700A> test

── *TST? self-test: long-term command ──
  There is NO REMOTE ABORT. STBY is rejected with fault 2002 while a
  long-term command runs (guide §19.2 gap G6), so this console
  cannot stop it once started.

  The front-panel STBY key or line power is the only stop — keep one
  within reach. Ctrl-C is noted but will NOT interrupt the poll.
  *TST? blocks the bus for its whole duration and its runtime is undocumented (gap G9); timing it.
  *TST? PASSED in 0.0 s
```

**The 5700A programming guide gives no duration for `*TST?`.** Neither does it
for `DIAG` or `CAL_CHK`. The console measures and prints the elapsed time for
exactly that reason — **write the numbers down from your first real run and
feed them back into the driver's timeout constants**, which are currently
guesses.

Note in the transcript above that `*TST?` **passed** on a unit that `DIAG`
then found four faults on. The self-test is coarse. It is not a substitute for
diagnostics and a pass is not evidence of health.

`self_test()` in the driver returns `True` for **pass**, which is the inverse
of the instrument's `*TST?` answer of `0`. The console does not re-invert it.

## 4. `DIAG` and `diagstep`

`diag` runs a diagnostic to completion and then groups whatever it queued:

```
5700A> diag D5700

── DIAG D5700: long-term command ──
  There is NO REMOTE ABORT. STBY is rejected with fault 2002 while a
  long-term command runs (guide §19.2 gap G6), so this console
  cannot stop it once started.

  The front-panel STBY key or line power is the only stop — keep one
  within reach. Ctrl-C is noted but will NOT interrupt the poll.
    STATE 1 Self diagnostics — high voltage tests
    STATE 0 Operating — dc volts
  DIAG D5700 finished after 0.0 s
  4 fault(s), earliest first (the earliest points at the cause — guide §8.5):
   3101  A14: 8255 Port A Falt  (A14)
   3117  A14/A15: HV +DC Current Error Amp Fault  (A14/A15)
   3122  A14/A15: HV AC 1KHZ, Preamp Fault (lo)  (A14/A15)
   3128  A14/A15: HV AC 1KHZ, Output Fault (lo)  (A14/A15)

  4 fault(s) across 2 group(s); a fault naming two assemblies appears under both:
    A14  — 4 fault(s)
       3101  A14: 8255 Port A Falt  (A14)
       3117  A14/A15: HV +DC Current Error Amp Fault  (A14/A15)
       3122  A14/A15: HV AC 1KHZ, Preamp Fault (lo)  (A14/A15)
       3128  A14/A15: HV AC 1KHZ, Output Fault (lo)  (A14/A15)
    A15  — 3 fault(s)
       3117  A14/A15: HV +DC Current Error Amp Fault  (A14/A15)
       3122  A14/A15: HV AC 1KHZ, Preamp Fault (lo)  (A14/A15)
       3128  A14/A15: HV AC 1KHZ, Output Fault (lo)  (A14/A15)
```

**Faults are listed earliest first, and the earliest one points at the
cause** (guide §8.5). Everything after it may well be a consequence.

`diagstep` is the board-isolation workflow: it sets `DIAGFLT HALT`, so the
diagnostic **stops at every fault and waits**, and steps you through them one
at a time.

> **`DIAGFLT` is stored in NVRAM.** It is not a per-run option — whatever you
> set it to survives a power cycle and governs every later diagnostic,
> including one somebody else runs from the front panel. `diagstep` therefore
> saves the current setting, selects `HALT` for its own run, and **restores the
> original when it ends** — on the normal path, on operator abort, and on an
> exception. It says so as it starts, and `diagflt` on its own shows you the
> current value so you can confirm it was put back.

```
5700A> diagstep D5700

── DIAG D5700, stepping at every fault: long-term command ──
  There is NO REMOTE ABORT. STBY is rejected with fault 2002 while a
  long-term command runs (guide §19.2 gap G6), so this console
  cannot stop it once started.

  The front-panel STBY key or line power is the only stop — keep one
  within reach. Ctrl-C is noted but will NOT interrupt the poll.
  DIAGFLT was ABORT; HALT is selected for this run and ABORT is restored when it ends.
    STATE 1 Self diagnostics — high voltage tests
    STATE 2 Self diagnostics halted by a fault — halted at 3101

── diagnostic HALTED at a fault ──
  faults so far:
     3101  A14: 8255 Port A Falt  (A14)

  Enter or 'y'  → DIAG CONT: step to the next fault
  anything else → DIAG ABORT: stop the diagnostic here

  Ctrl-C is deferred here on purpose: the diagnostic is halted
  inside the calibrator and one of these two answers is what releases it.
  → OK with stepping to the next fault (DIAG CONT)? [Enter or y = continue / anything else = abort] y
    STATE 1 Self diagnostics — high voltage tests
    STATE 2 Self diagnostics halted by a fault — halted at 3117

── diagnostic HALTED at a fault ──
  faults so far:
     3101  A14: 8255 Port A Falt  (A14)
     3117  A14/A15: HV +DC Current Error Amp Fault  (A14/A15)

  Enter or 'y'  → DIAG CONT: step to the next fault
  anything else → DIAG ABORT: stop the diagnostic here

  Ctrl-C is deferred here on purpose: the diagnostic is halted
  inside the calibrator and one of these two answers is what releases it.
  → OK with stepping to the next fault (DIAG CONT)? [Enter or y = continue / anything else = abort] y
    STATE 1 Self diagnostics — high voltage tests
    STATE 2 Self diagnostics halted by a fault — halted at 3122

── diagnostic HALTED at a fault ──
  faults so far:
     3101  A14: 8255 Port A Falt  (A14)
     3117  A14/A15: HV +DC Current Error Amp Fault  (A14/A15)
     3122  A14/A15: HV AC 1KHZ, Preamp Fault (lo)  (A14/A15)

  Enter or 'y'  → DIAG CONT: step to the next fault
  anything else → DIAG ABORT: stop the diagnostic here

  Ctrl-C is deferred here on purpose: the diagnostic is halted
  inside the calibrator and one of these two answers is what releases it.
  → OK with stepping to the next fault (DIAG CONT)? [Enter or y = continue / anything else = abort] n

── diagstep summary — DIAG D5700 ABORTED by the operator ──
  3 fault(s), earliest first (the earliest points at the cause — guide §8.5):
   3101  A14: 8255 Port A Falt  (A14)
   3117  A14/A15: HV +DC Current Error Amp Fault  (A14/A15)
   3122  A14/A15: HV AC 1KHZ, Preamp Fault (lo)  (A14/A15)

  3 fault(s) across 2 group(s); a fault naming two assemblies appears under both:
    A14  — 3 fault(s)
       3101  A14: 8255 Port A Falt  (A14)
       3117  A14/A15: HV +DC Current Error Amp Fault  (A14/A15)
       3122  A14/A15: HV AC 1KHZ, Preamp Fault (lo)  (A14/A15)
    A15  — 2 fault(s)
       3117  A14/A15: HV +DC Current Error Amp Fault  (A14/A15)
       3122  A14/A15: HV AC 1KHZ, Preamp Fault (lo)  (A14/A15)
```

Why stepping is worth the tedium: the diagnostic keeps going after a fault by
default, and later tests run through hardware the first fault already broke.
Halting at each one lets you probe the board *in the state that produced that
specific fault*, which is the difference between "the HV section is bad" and
"the ±DC current error amplifier is bad".

Afterwards, confirm the NVRAM setting was put back:

```
5700A> diagflt
  DIAGFLT? ABORT
  (HALT = stop and wait at each fault, ABORT = stop, CONT = carry on)
```

If a `DIAG CONT` times out or you hit EOF at the halt prompt, the console
sends `DIAG ABORT` to release the halted calibrator, says so, and labels the
run `INTERRUPTED` rather than claiming it ran to the end. If the abort itself
fails you get `STILL HALTED … clear it from the front panel`, which is a state
you must not learn about later.

## 5. Reading the result

### Faults grouped by assembly

The console files a fault under **every** assembly its text names, and orders
the groups largest-first, because the question the view answers is *"which
board is implicated in everything I am seeing?"*. In the session above that
answer is `A14 — 4, A15 — 3` at a glance.

Of the 692 shipped fault codes (the guide's reviewed table merged with the
Service Manual's complete chapter-4 list — see
`docs/5700a/fluke5700a-fault-codes.md`), 220 name an assembly at all: 151 name
exactly one, **62 name a pair** (`A14/A15:`) and **7 name a triple**
(`A14/A15/A16:`). Every multi-assembly fault appears under each named board —
so tracing A14 finds it, and so does tracing A15. A fault naming no assembly
lands in a `(no assembly named)` group, always printed last. One code (3135,
*"Assembly A14 Not Responding"*) names its board in prose rather than as a
prefix and is still grouped correctly.

### What the 3100–3142 range means

| codes | assembly | what the diagnostic was testing |
|---|---|---|
| 3100–3103 | A14 | **8255 parallel-port faults** — control word and ports A/B/C, the digital interface to the HV control board itself. A fault here means the CPU cannot drive the board properly, so treat everything after it with suspicion. |
| 3104–3108 | A15 | HV oven and HV I oven regulation; dc HV amplifier noise, offset and gain |
| 3109–3116 | A15 | HV ±DC preamplifier, series pass & current, high-voltage output, reference/error amplifier |
| 3117–3120 | A14/A15 | HV ±DC **current error amplifier** and absolute value |
| 3122–3133 | A14/A15 | HV AC **preamp** (3122–3127) and **output** (3128–3133) faults, each tested at **1 kHz and 100 Hz**, low/mid/high |
| 3135–3136 | A14 | assembly not responding; 8255 was reset |
| 3137–3142 | A14/A15/A16 | 2.2 A ac and dc range: magnitude, compliance, amplitude, absolute value, dummy load |

(3121 and 3134 are not assigned.)

`explain <code>` prints the offline table and the instrument's own `EXPLAIN?`
side by side, and calls out a disagreement — the offline table is transcribed
verbatim from the manual's appendix *including its typos*, and a sick unit's
`EXPLAIN?` can misbehave too, so neither is authoritative alone:

```
5700A> explain 3101
  fault 3101
    offline table : A14: 8255 Port A Fault
    EXPLAIN?      : A14: 8255 Port A Falt
    ! the two sources DISAGREE. The offline table is transcribed verbatim from [OM Appendix A] (typos and all, guide §13 item 31); a sick unit's EXPLAIN? can also misbehave. Trust the numeric code, not either text.
```

## 6. `watch` — for an intermittent fault

`watch` polls `ISR?` and the fault queue and prints **only on change**, which
is what makes it usable while you are flexing a connector or heating a board
with a hot-air gun:

```
5700A> watch 0
  watching ISR + the fault queue every 0 s; only changes are printed. Ctrl-C stops.
  (this DRAINS the fault queue as it goes — faults are reported here and then gone)
  10:10:17  ISR 0x1800  SETTLED REMOTE
  10:10:17  ISR 0x1800 -> 0x1801  +OPER
  10:10:17  new faults:
     3117  A14/A15: HV +DC Current Error Amp Fault  (A14/A15)
  10:10:17  ISR 0x1801 -> 0x1800  -OPER
  watch stopped
```

Two things to know:

- **`watch` drains the fault queue as it polls.** That is the only way to see
  a *new* fault, but it means the faults it reports are gone from the queue
  afterwards — copy them out of the terminal.
- Ctrl-C stops the loop and returns you to the prompt; it does not end the
  session. `watch <seconds>` sets the poll interval; the default is 2 s.

Also worth knowing about the destructive-read registers: **`status` clears the
ESR** and the serial poll clears RQS. If something else is polling ESR, do not
interleave a human typing `status`.

## 7. Manual sourcing, to probe the HV section

```
5700A> out 100mV
  staged 100.000000mV dc  (STANDBY — 'oper' to energize)
5700A> outq
  OUT?    100.000000mV dc
```

`out` **never energizes** — the driver sends `STBY` first. `oper` is a
separate, deliberate act, and above 22 V it asks:

```
5700A> out 100V
  staged 100.000000V dc  (STANDBY — 'oper' to energize)
  ! this output is at or above 22 V: 'oper' will ask you to type 'yes' before it energizes.
5700A> oper

── HIGH VOLTAGE ──
  About to energize 100.000000V dc.
  Check the leads, the DUT and everyone's hands first.
  → Override and accept energizing 100.000000V dc on the output terminals? [type "yes" to accept / Enter to repeat] yes
  OPERATE — live at 100.000000V dc
```

`oper` reads `OUT?` first, so it gates on what the calibrator is *actually*
staged to source, not on what you last typed.

And with that live, a long-term command is refused rather than started:

```
5700A> diag D5700
  ! refusing to start DIAG D5700: the output is in OPERATE at 100.000000V dc, which is at or above 22 V.
    A long-term command cannot be aborted remotely and STBY is rejected with fault 2002 while it runs (guide §19.2), so it must not be started with a dangerous output live.
    Type 'stby' to de-energize, then repeat DIAG D5700.
```

### The SI-prefix rule

**Multiplier prefixes in this console are strict SI and case-sensitive:
`m` is milli, `M` is mega.** Unit letters are case-*in*sensitive (`10v`, `10V`
and `10 V` are the same).

```
out 100mV      100 millivolts       out 1.5mA    1.5 milliamps
out 100V 1kHz  100 V at 1 kHz       out 1V 1MHz  1 V at 1 MHz
ohms 10k       10 kΩ                ohms 1.9M    1.9 MΩ
```

The calibrator's own syntax is different — guide §3.6 makes a bare `M` mean
*milli* on amplitudes but *mega* on ohms and hertz, which is a rule about the
unit that follows, and this console has to allow a bare prefix (`ohms 1.9M`)
where no unit follows at all. So it does not copy the instrument's rule.

Because **no 5700A range reaches a megavolt or a megaamp**, a capital `M` on a
volt or an amp cannot be a legitimate request under either reading. It is
always a typo, and it is **refused rather than guessed at**:

```
5700A> out 100MV
  ! '100MV' is ambiguous and is refused: in this console prefixes are case-sensitive SI: m=milli, M=mega (u/µ=micro, k=kilo, G=giga), so '100MV' would mean 100 megavolts — outside every 5700A range. The calibrator's own syntax reads a bare M as milli (guide §3.6). Write 100mV for milli or 100V for the base unit.
```

`M` on ohms or hertz means what every engineer means and is accepted:

```
5700A> ohms 1.9M
  staged 1.900000MΩ  (STANDBY — 'oper' to energize)
5700A> outq
  OUT?    1.900000MΩ
  (in ohms this is the CHARACTERIZED value actually sourced, not the nominal request — use it as the error reference, guide §11.4)
```

That last note matters for any measurement you make: **5700A resistance is
characterized, not synthesized.** `OUT?` in ohms reports what the internal
resistor actually is (up to a few hundred ppm from nominal). Use the readback
as your reference, never the number you asked for.

Everything the console sends is a plain float in base units — the instrument
never sees a prefix.

A frequency in the ohms function is fault 812, and `out` refuses it with the
citation rather than letting the calibrator complain:

```
5700A> out 10kohm 1kHz
  ! a frequency is never sent in the ohms function — that is fault 812 (guide §14.2)
5700A> ohms 10k 1kHz
  ! usage: ohms <value> — a frequency is never sent in the ohms function, that is fault 812 (guide §14.2)
```

Both spellings refuse it, and neither stages anything.

### Nudging the output: `incr`, `newref`, `mult`

`incr` is the remote twin of the front-panel adjustment knob — it enters
error mode and steps the output, which is how the `calrng` and `calwbflat`
nulling workflows are driven:

```
5700A> out 10V
5700A> oper
5700A> incr -10uV
  ADJOUT? 9.999990V dc  (error mode — 'newref' locks the edit in)
5700A> incr 1Hz
```

`OUT?` keeps reporting the *reference* while you edit; `ADJOUT?` (the `adjq`
command) reports the adjusted value. `newref` takes the adjusted value as the
new reference; `mult 1.9` multiplies it (10 V → 19 V).

All three are edits of a **live** output, so all three go through the same
typed-`yes` interlock as `oper` when the result would be at or above 22 V or
1 A. The console predicts the result first (factor × `OUT?`, step + `ADJOUT?`)
and asks *before* sending, so a declined confirmation leaves the terminals
exactly as they were:

```
5700A> out 30V
5700A> oper
5700A> mult 10
── HIGH CURRENT/HIGH VOLTAGE ──
  mult would leave 300.000000V dc on LIVE terminals, which is at or above 22 V.
  → Override and accept mult to 300.000000V dc? [type "yes" ...]
```

In standby nothing is gated — there is nothing on the terminals to be
dangerous, and `oper` still asks on the way out.

### Bounding a session: `limit`

`limit` drives the calibrator's own entry limits — the one interlock the
*instrument* enforces, whatever software asks:

```
5700A> limit 25V -25V
  LIMIT 25.000000V, -25.000000V — the calibrator itself now refuses anything beyond (faults 815/856).
  (power-up or *RST resets the limits to the full envelope)
```

Set it at the start of a probing session on a repaired unit and a slipped
`out 220V` is refused by the firmware, not just by this console. Voltage and
current limits are separate pairs (`limit 100mA -100mA`).

### Wideband (Option 5700A-03): `wband`

`wband on` switches the 50 Ω wideband output in; `out` then validates
against the wideband envelope (300 µV – 3.5 V rms, 10 Hz – 30 MHz) instead
of the standard one:

```
5700A> wband on
  WBAND ON — 50 Ω wideband output: 300 µV to 3.5 V rms, 10 Hz to 30 MHz; 'out' now validates against the wideband envelope.
  ! specifications apply at the END OF THE CABLE into its 50 Ω termination.
5700A> out 1V 1MHz
  staged 1.000000V 1.000000MHz  (STANDBY — 'oper' to energize)
```

Remember the termination: the specification is written at the end of the
cable into 50 Ω, and dBm on this output is referenced to 50 Ω (600 Ω on the
standard output — the same volts are a different dBm, guide §11.4). `wband
off` may re-select the previous standard-function value or drop the output
to 0 mV dc; check `outq`.

### The escape hatch

`raw <command...>` sends verbatim and **bypasses every check** — the envelope
validation, the ≥22 V confirmation and the live-output interlock. A trailing
`?` is read back. Non-query writes to calibration memory, `OPER`, `*PUD`,
`ETIME` and `FORMAT*` are gated behind `--service` and point you at the
dedicated command that *does* have the confirmation. The gate keys on the
first token only, so a compound line like `raw OUT 1000V;OPER` slips it — that
is the documented cost of an escape hatch that does not parse the instrument's
syntax.

## 8. After the repair: zeros, check, report, diff

This is how you answer *"did the board swap fix it?"*, and the order matters.

**Gate the whole thing on `ONTIME?`, not on the clock.** The calibrator counts
its own power-on minutes, so ask it instead of guessing when the operator
flipped the switch:

```python
from caldmm.drivers.fluke5700a import WARMUP_MAX_MINUTES   # 30

on = cal.ontime()                 # minutes since power-up, this session
while on < WARMUP_MAX_MINUTES:
    time.sleep(30)
    on = cal.ontime()
```

Specifications want "twice the time since last warmed up, to a maximum of 30
minutes" (guide §11.6). The off-duration is **not** queryable, so a script that
cannot know it should simply require the full 30. `ETIME?` is the companion —
lifetime minutes, useful for the unit log. A wall-clock gate computed on the
controller looks equivalent and is not: it silently passes if the operator
power-cycled the unit while the script was waiting.

**Run the dc zeros first.** On a unit with old zeros the dc numbers in the
report are about the zeros, not about the instrument. `caldmm-5700a-selfcal`
is the app for this — it writes a session record, captures the report before
and after, and diffs them:

```console
$ caldmm-5700a-selfcal --zeros -o /var/cal/post-repair-zeros

--- Fluke 5700A/5720A dc zeros calibration (CAL_ZERO) ------------------------
  *IDN?              FLUKE,5720A,5248000,1.0+B+*
  model / serial     5720A / 5248000
  *OPT?              5725A Attached
  procedure          dc-zeros
  mode               COMMITTING RUN -- CAL_ZERO writes the new dc zeros
  session log        /var/cal/post-repair-zeros.log

── step 3/4: zeros ──
  Running CAL_ZERO. About 2½ minutes, no remote abort.
  The rear CALIBRATION switch is NOT needed for this (guide §9 CAL_ZERO),
  but the zeros themselves are written, so a dry run skips it.
  sends: CAL_ZERO;*OPC
  THIS STEP WRITES NON-VOLATILE CALIBRATION MEMORY.
  → press Enter when the action above is complete (Ctrl-C to abort): 
  → OK with step zeros? [Y = run it / anything else = skip it] y
  → Override and accept running CAL_ZERO, which writes new dc zero constants? [type "yes" to commit / anything else = SKIP this step permanently] yes

  … CAL_ZERO: state 0 Idle
```

`CAL_ZERO` needs nothing connected and does **not** need the rear CALIBRATION
switch. It takes about 2½ minutes (plus 30 s with a 5725A attached) and is due
every 30 days.

**Then the calibration check.** `CAL_CHK` measures the main output against the
internal standards and rewrites the CHECK report. It changes no calibration
constant, so it is safe on a suspect unit — and it is the cheapest full sweep
of the instrument's own opinion of itself:

```console
$ caldmm-5700a-selfcal --check -o /var/cal/post-repair-check

── step 4/5: check ──
  Running CAL_CHK. Minutes, no remote abort.
  sends: CAL_CHK;*OPC
  → press Enter when the action above is complete (Ctrl-C to abort): 
  → OK with step check? [Y = run it / anything else = skip it] y

  … CAL_CHK: state 0 Idle

── step 5/5: capture-after ──
  Capturing the new CHECK report.
  sends: CAL_RPT? CHECK  CAL_SLST? CHECK
```

**Then capture a report and diff it against the pre-repair one.**

```console
$ caldmm-5700a-selfcal --report CAL -o /var/cal/after-repair

--- CAL_RPT? CAL -------------------------------------------------------------
  *IDN?              FLUKE,5720A,5248000,1.0+B+*
  model / serial     5720A / 5248000
  *OPT?              5725A Attached
  captured      /var/cal/after-repair.txt
  shift table   /var/cal/after-repair.csv

5700A S/N 5248000 -- OUTPUT CALIBRATION
printed 2026-01-02 03:04:05  firmware ?
spec interval ? at ? confidence
worst 10 points, as a percentage of spec:
    -18.88 %  DC VOLTAGE     220 mV +FS
     10.44 %  DC VOLTAGE      2.2 V +FS

  --compare /var/cal/after-repair.txt <another capture> diffs two of these.
```

Keep **one capture per file** — `--compare` uses the first report in each file
and says so if there are more. `--compare` contacts nothing, but it still
imports the driver module (which needs no GPIB library until it opens a
device).

---

## Command reference

```
  Output and manual sourcing
    out <value><unit> [<freq>] source a value, staying in STANDBY
    ohms <value>               source a resistance (ohms 10k / 1.9M / 0)
    oper                       energize the output (typed 'yes' at >= 22 V)
    stby                       de-energize the output
    settle                     wait for ISR SETTLED and report how long it took
    outq / adjq                OUT? / ADJOUT? — what is really being sourced
    uncert                     UNCERT? — the calibrator's own spec for this output
    range / rangelck on|off    RANGE? — the selected range; lock it
    rcomp / extsense / extguard  two-wire ohms compensation, 4-wire sense, guard
    wband [on|off]             Option 03 wideband output (50 Ω, 10 Hz-30 MHz)
    incr <±step>[unit]         nudge the output / enter error mode
    newref / mult <factor>     lock the edit in / multiply the reference
    limit [<+lim> <-lim>]      the calibrator's own entry limits; bare = query

  Status and faults
    status                     STATE, OUT, RANGE, UNCERT, ISR/ESR/STB (CLEARS ESR)
    isr / esr / stb            one register, decoded (esr and stb are destructive)
    iscr                       latched ISR changes (READING IT CLEARS IT)
    faults                     drain the fault queue and group it by assembly
    explain <code>             the offline table AND EXPLAIN?, side by side
    state                      STATE? with its decoded name
    watch [interval]           poll ISR + ISCR + faults; transients reported

  Diagnostics
    test                       *TST? self-test (blocks; the duration is measured)
    diag [target]              run a diagnostic (default D5700), then group faults
    diagstep [target]          DIAGFLT HALT + step fault by fault
    diagflt [mode]             show or set what a diagnostic fault does (NVRAM)
    fatality / fatalclr        the fatal-fault list (--service)

  Calibration and housekeeping
    caldays / caldate          days since / dates of CAL and ZERO
    calintv [days] / calconf [95|99]   read, or SET (NVRAM), interval/confidence
    caltemp [celsius]          CAL_TEMP? per activity; set it before cal commands
    calconst <name> [group]    CAL_CONST? — one constant by name
    calshift <group> <range>   CAL_SHIFT? — shifts for one range
    calslst [group] / calrpt [group]
    calrpt summary [group]     parse the report and summarize the worst shifts
    calclst [group] / ohmsref [group]  every constant / the ohms reference
    calzero                    CAL_ZERO — dc zeros, ~2.5 min, long-term
    calchk                     CAL_CHK — calibration check, long-term
    calrng <id> <mag>|null|store   range cal vs an external standard (--service)
    calwbgain pgain|ngain|store    wideband gain cal (cable on SENSE; long-term)
    calwbflat start|null|store     wideband flatness cal (INCR-null loop)
    idn / opt / etime / ontime / clock / rptstr / lrn
    echo <text>                ECHO? — the cheapest end-to-end link test

  Escape hatch and control
    raw <command...>           verbatim, BYPASSING ALL VALIDATION
    local                      GTL — hand the front panel back to the operator
    help / quit
```

`calzero` and `calchk` here are the bare commands. Prefer
`caldmm-5700a-selfcal --zeros` / `--check`, which do the same thing with a
before/after report capture and a session record on disk.

## See also

- [`docs/calibration-howto.md`](../calibration-howto.md) — verifying instruments
  and the guided artifact calibration.
- The module docstrings under `src/caldmm/cal/` — why any of this is shaped
  the way it is, and its known limitations.
