# Session notes: chain ran backwards; gate/deesser "missing" but running

Date: 2026-09-27
Result: commit `5bbaec5`

## Symptom

A health check after a reboot looked clean: 0 xruns, no clipping, and
the chain attached to the Volt 1 node. But the node's Props showed
params for only 5 of 7 stages (rnnoise, deepfilter, sc4, autogain,
limiter). Gate and deesser never appeared, even though deesser was
enabled in the preset.

## Root causes (from the PipeWire 1.6.8 source and daemon debug log)

1. **The chain ran in reverse.** WirePlumber's `filter-graph.lua`
   numbers `create-filter-graph` array elements `audioconvert.filter-graph.0`,
   `.1`, ... in array order. audioconvert's `insert_graph()` keeps the
   graphs sorted by *descending* order, and processing walks that list,
   so the highest N runs first. The debug log confirms the activation
   order: limiter, autogain, sc4, deesser, deepfilter, rnnoise, gate.
   Gain and compression ran before noise suppression, and the gate ran last.

2. **Gate and deesser run but are invisible in the readback.**
   The debug log shows `instantiate .../gate_stereo gate[0]` and the same
   for deesser. They were never missing from processing. audioconvert's
   `impl_node_enum_params` builds each graph's Props pod in a 4096-byte
   stack buffer, and `spa_pod_filter` then copies the pod into the same
   buffer. Any pod over about 2 KB fails that copy and is skipped with
   `goto next`, with no log line. Approximate pod sizes: limiter 1.9 KB
   and autogain 1.8 KB fit; gate_stereo 2.6 KB and deesser_stereo 3.0 KB
   don't. Writes still work: `set-param` with `gate:<port>` keys goes
   through `spa_filter_graph_set_props` by name. The app already
   tolerates the missing readback (`_sync_from_live` skips absent keys).

## Fix

- `confgen.GRAPH_ORDER = reversed(STAGES)`: the fragment lists stages
  in reverse, so gate gets the highest N and runs first.
- The startup topology check compares order (`topology != GRAPH_ORDER`)
  instead of the sorted set, so a chain still loaded from the old
  forward-ordered fragment shows the one-time "Restart PipeWire" banner.

## Wrong turns (don't repeat)

- **"64-port limit."** `too many ports. %d > %d` in libspa-filter-graph
  refers to `MAX_HNDL` (plugin instances), not plugin ports. Switching
  to `gate_mono`/`deesser_mono` changed nothing and was reverted.
- **ctypes LADSPA descriptor decoding.** `LADSPA_Properties` is a 4-byte
  int, and `ladspa_descriptor()` returns a single pointer, not a pointer
  to a pointer. Port descriptor bits: 1=INPUT, 2=OUTPUT, 4=CONTROL,
  8=AUDIO. Reading bit 1 as "audio" made every LSP plugin look broken.
- **`strings` for port names.** It doesn't work on the LSP .so; even
  known-valid names returned 0 matches.
- **pw-cli probes of `audioconvert.filter-graph.N`.**
  `parse_prop_params` only accepts *string* values and silently skips
  object pods, so the probes did nothing. Send the graph as a quoted
  JSON string if you retry this.

## Incident: WirePlumber restart crashed the desktop

A `systemctl --user restart wireplumber` (shortly after a full
pipewire/pipewire-pulse/wireplumber restart) coincided with a
gnome-shell SIGSEGV. The session respawned; Minecraft and vesktop
(SIGTRAP) died with it. Treat audio-service restarts as able to take
down the desktop: only restart from the banner, at a moment the user
chooses.

## Debugging without restarts

- `wpctl set-log-level 0 4` enables daemon debug logs live
  (`0` = PipeWire server; no ID = WirePlumber). Set it back with
  `wpctl set-log-level 0 2`.
- To confirm a stage is live, grep the pipewire journal for
  `instantiate <label> <name>[0]`, not the Props readback.

## Pending

The reordered fragment is written and pushed, but the running chain
still uses the old order until the next PipeWire/WirePlumber restart.
