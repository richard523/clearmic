# mic-dsp-ui

GTK4/libadwaita control panel for the native PipeWire mic filter-chain.
Replaces hand-editing `~/.config/pipewire/pipewire.conf.d/…` and restarting
services by hand.

## Chain

```
input device → gate → RNNoise/DeepFilter → de-esser → SC4 → autogain → limiter → virtual source
```

- Every stage can be toggled on/off (RNNoise and DeepFilter are mutually
  exclusive). Structural changes regenerate the conf and restart PipeWire
  (~1 s).
- Parameter tweaks are applied **live** via `pw-cli set-param` — no restart,
  no dropouts.
- Live level meters for the raw input and the processed source, plus a
  **Listen** button (routes processed mic to the default output — use
  headphones).
- Presets: save/load full chain snapshots (`~/.config/mic-dsp-ui/presets/`).
- Input device picker for the capture target.

## Files

| Path | Purpose |
|---|---|
| `main.py` | GTK4 app |
| `backend.py` | PipeWire interop (pw-dump / pw-cli / pw-record / pw-loopback) |
| `catalog.py` | Stage + param definitions (LADSPA port names, ranges) |
| `confgen.py` | Generates `~/.config/pipewire/pipewire.conf.d/30-mic-dsp-ui.conf` |

App state: `~/.config/mic-dsp-ui/state.json`.

## First run

If the legacy `20-rnnoise-source.conf` exists it is migrated (values kept,
original renamed to `.bak`) and the app asks to restart PipeWire once.
The virtual source keeps its node names (`effect_input.rnnoise` /
`effect_output.rnnoise`), so apps that pinned the device keep working.

## Run

```sh
./run.sh
# or install the launcher:
cp mic-dsp-ui.desktop ~/.local/share/applications/
```

Requires: `python-gobject`, `gtk4`, `libadwaita`, `pipewire` (pw-cli,
pw-dump, pw-record, pw-loopback), `swh-plugins`, `lsp-plugins-ladspa`,
`rnnoise-plugin` (librnnoise_ladspa), `libdeep_filter_ladspa-bin`.
