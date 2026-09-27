# Session notes: live bypass toggles (no more PipeWire restarts)

Date: 2026-09-27
Result: commit `ec2015f` (plus `ecae6bc`, `cacc883` earlier in the session)

## The problem

Stage on/off changes rebuilt the DSP chain by restarting PipeWire. Every
restart destroys all open apps' capture streams, and most apps never
re-open them on their own:

- A `parec` test client survived a restart only as a process; its stream
  delivered zero bytes and it never reconnected.
- vesktop's capture nodes disappeared from the graph after a restart and
  never came back. Discord, browsers, and most games behave the same.

Related bugs fixed along the way (commit `ecae6bc`): the "restarting..."
status could stick forever because the completion handler relied on
`_sync_from_live`, which bails out early when the chain has not
instantiated yet; wedge-recovery restarts ran unguarded, so fast actions
could start two concurrent PipeWire restarts; stage toggles arriving
during a restart were silently dropped.

## Why the restart could not just be removed

The DSP is a WirePlumber filter graph embedded in each capture device
node (`node.filter-graph.rules` in `~/.config/wireplumber/
wireplumber.conf.d/50-clearmic-dsp.conf`). WirePlumber reads that
fragment only at startup, so changing which stages are in the graph
required restarting WirePlumber (which also severs app streams, because
device nodes are recreated).

Dead ends checked:

- `pw-cli load-module libpipewire-module-filter-chain` - modules are
  owned by the client connection and die when pw-cli exits. Useless for
  runtime loading.
- Runtime relinking of graph elements - the graphs are not separate pw
  nodes; they live inside the device adapter. Nothing to relink.
- snd_aloop test bench for measuring transparency - the loopback
  capture stayed silent on all 4 channels; abandoned.

## The design: static topology + live bypass

- The graph now contains ALL stages, always. On/off is a live param
  change via `pw-cli set-param` on the chain node (the same path the
  sliders already use).
- One `create-filter-graph` element per stage: audioconvert builds each
  Props pod in a fixed 4096-byte buffer, so small elements are required
  (this is also why autogain was always isolated).
- `catalog.BYPASS` holds neutral values per stage. Disabled = pushed
  bypass values; enabled = pushed user values. Suspended devices pick
  the same values up from the conf seeds.
- Preset applies push all stages live. No restarts.
- Startup compares the on-disk conf stage set with the full catalog; a
  mismatch shows a one-time "Restart PipeWire" banner.

## Caveats per stage (bypass values and their weaknesses)

| Stage      | Bypass                                | Caveat |
|------------|---------------------------------------|--------|
| gate       | threshold -90 dB, reduction -72 dB   | In true digital silence the gate can still cut up to -72 dB. Not bit-transparent, but inaudible in practice. |
| rnnoise    | Dry Mix 1.0 (100% dry)                | Relies on the plugin's dry/wet being a crossfade ("0 = full filter" per the UI label). If it sums dry+wet, output would double - ear-verify. |
| deepfilter | Attenuation Limit 0 dB                | Passthrough by semantics, but the neural model KEEPS RUNNING and burns CPU even when off. The heaviest bypassed stage. |
| deesser    | threshold -60 dB, ratio 1:1           | -60 dBFS is quiet but not impossible to exceed with hot input + high makeup; ratio 1:1 alone is transparent regardless. |
| sc4        | threshold 0 dB, ratio 1:1, makeup 0 dB| Bit-transparent with ratio 1:1. No caveat. |
| autogain   | target 0 LUFS, boost limited to 0 dB  | REASONED, NOT EAR-VERIFIED. Transparent only while input loudness stays below 0 LUFS (always true for mic capture). If LSP's "maximum amplification gain" cap does not clamp the way we think, disabled autogain could still adapt gain - verify with the listen test. |
| limiter    | threshold 0 dBFS (1.0 G)              | Signals above 0 dBFS would still limit; mic capture clamps anyway. Effectively transparent. |

General caveats:

- WirePlumber startup-only config means a topology change (catalog edit,
  app-driven or manual) still needs one restart.
- The known HFP-teardown wedge (upstream PipeWire bug) still triggers a
  PipeWire restart from the recovery path; that one is unavoidable.
- Bypassed stages' sliders are insensitive in the UI; their stored user
  values are preserved and pushed back when re-enabled.
- `_sync_from_live` adopts live values only for enabled stages, so a
  disabled stage's bypass values never pollute the stored state.

## Remaining restart scenarios (complete list after rollout)

1. The one-time topology adoption (the banner after this change).
2. WirePlumber config topology changes (manual or future catalog edits).
3. The BT HFP wedge recovery.
4. The explicit banner button ("Restart PipeWire" / "Retry").

Stage on/off, param edits, preset save/load/apply: all live, forever.

## Verification checklist

- [ ] One-time restart via the banner; apps reopen their mic once.
- [ ] Toggle each stage off/on while listening; confirm instant effect.
- [ ] Autogain off: confirm no gain drift with the listen test.
- [ ] rnnoise off: confirm dry bypass (no suppression residue).
- [ ] deepfilter off: `top` shows model CPU still spent (expected).
