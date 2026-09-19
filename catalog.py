"""Stage and parameter catalog for the mic DSP filter-chain."""
import math

def P(port, disp, unit="", mn=0.0, mx=1.0, default=0.0, step=0.0,
      toggle=False, integer=False, db=False):
    """A control port. `db=True` params are stored linear (G) but shown/edit in dB."""
    return {"port": port, "disp": disp, "unit": unit, "min": mn, "max": mx,
            "default": default, "step": step, "toggle": toggle,
            "integer": integer, "db": db}

# chain order
STAGES = ["gate", "rnnoise", "deepfilter", "deesser", "sc4", "autogain", "limiter"]
# only one noise suppressor may be enabled at a time
EXCLUSIVE = {"rnnoise", "deepfilter"}
DEFAULT_ENABLED = {"rnnoise", "sc4", "autogain"}

STAGE_INFO = {
    "gate": {
        "title": "Gate", "subtitle": "LSP noise gate",
        "plugin": "lsp-plugins-ladspa",
        "label": "http://lsp-plug.in/plugins/ladspa/gate_stereo",
        "audio": {"in_l": "Input L", "in_r": "Input R",
                  "out_l": "Output L", "out_r": "Output R"},
        "params": [
            P("Curve threshold (G)", "Threshold", "dB",
              1.5849e-05, 1.0, 0.0316, 0.5, db=True),
            P("Curve zone size (G)", "Zone size", "dB",
              0.001, 1.0, 0.7503, 0.5, db=True),
            P("Attack (ms)", "Attack", "ms", 0.0, 2000.0, 20.0, 1.0),
            P("Release (ms)", "Release", "ms", 0.0, 5000.0, 100.0, 1.0),
            P("Hold time (ms)", "Hold", "ms", 0.0, 1000.0, 0.0, 1.0),
            P("Reduction (G)", "Reduction", "dB",
              0.00025119, 3981.07, 1990.54, 1.0, db=True),
            P("Makeup gain (G)", "Makeup", "dB",
              0.001, 1000.0, 1.0, 0.5, db=True),
            P("High-pass filter mode", "SC high-pass mode", "", 0.0, 3.0, 0.0,
              1.0, integer=True),
            P("High-pass filter frequency (Hz)", "SC high-pass", "Hz",
              10.0, 20000.0, 100.0, 10.0),
            P("Sidechain reactivity (ms)", "SC reactivity", "ms",
              0.0, 250.0, 187.5, 2.5),
            P("Hysteresis", "Hysteresis", "", 0.0, 1.0, 0.0, 1.0, toggle=True),
        ],
    },
    "rnnoise": {
        "title": "RNNoise", "subtitle": "RNN noise suppression",
        "plugin": "librnnoise_ladspa",
        "label": "noise_suppressor_stereo",
        "audio": {"in_l": "Input (L)", "in_r": "Input (R)",
                  "out_l": "Output (L)", "out_r": "Output (R)"},
        "params": [
            P("VAD Threshold (%)", "VAD threshold", "%", 0.0, 99.0, 50.0, 1.0,
              integer=True),
            P("VAD Grace Period (ms)", "VAD grace", "ms", 0.0, 1000.0, 500.0,
              10.0),
            P("Retroactive VAD Grace (ms)", "Retro grace", "ms", 0.0, 200.0,
              100.0, 5.0),
            P("Dry Mix", "Dry mix (0 = full filter)", "", 0.0, 1.0, 0.0, 0.05),
        ],
    },
    "deepfilter": {
        "title": "DeepFilter", "subtitle": "DeepFilterNet suppression",
        "plugin": "libdeep_filter_ladspa",
        "label": "deep_filter_stereo",
        "audio": {"in_l": "Audio In L", "in_r": "Audio In R",
                  "out_l": "Audio Out L", "out_r": "Audio Out R"},
        "params": [
            P("Attenuation Limit (dB)", "Attenuation limit", "dB",
              0.0, 100.0, 40.0, 1.0),
            P("Min processing threshold (dB)", "Min threshold", "dB",
              -15.0, 35.0, 4.0, 0.5),
            P("Max ERB processing threshold (dB)", "Max ERB threshold", "dB",
              -15.0, 35.0, 10.0, 0.5),
            P("Max DF processing threshold (dB)", "Max DF threshold", "dB",
              -15.0, 35.0, 8.0, 0.5),
            P("Min Processing Buffer (frames)", "Min proc buffer", "",
              0.0, 10.0, 0.0, 1.0, integer=True),
            P("Post Filter Beta", "Post filter beta", "", 0.0, 0.05, 0.02,
              0.001),
        ],
    },
    "deesser": {
        "title": "De-esser", "subtitle": "LSP sibilance control",
        "plugin": "lsp-plugins-ladspa",
        "label": "http://lsp-plug.in/plugins/ladspa/deesser_stereo",
        "audio": {"in_l": "Input L", "in_r": "Input R",
                  "out_l": "Output L", "out_r": "Output R"},
        "params": [
            P("Split frequency (Hz)", "Split frequency", "Hz",
              500.0, 4000.0, 2250.0, 10.0),
            P("Threshold (G)", "Threshold", "dB", 0.001, 1.0, 0.5, 0.5, db=True),
            P("Ratio", "Ratio", "", 1.0, 100.0, 3.0, 0.25),
            P("Attack time (ms)", "Attack", "ms", 0.0, 200.0, 10.0, 1.0),
            P("Release time (ms)", "Release", "ms", 0.0, 400.0, 60.0, 1.0),
            P("Knee (G)", "Knee", "dB", 0.0631, 1.0, 0.5, 0.5, db=True),
            P("Stereo linking (%)", "Stereo linking", "%", 0.0, 100.0, 50.0,
              1.0),
        ],
    },
    "sc4": {
        "title": "SC4", "subtitle": "SWH compressor",
        "plugin": "sc4_1882",
        "label": "sc4",
        "audio": {"in_l": "Left input", "in_r": "Right input",
                  "out_l": "Left output", "out_r": "Right output"},
        "params": [
            P("RMS/peak", "RMS/peak blend", "", 0.0, 1.0, 0.0, 0.05),
            P("Threshold level (dB)", "Threshold", "dB", -30.0, 0.0, -20.0,
              0.5),
            P("Ratio (1:n)", "Ratio", "", 1.0, 20.0, 3.0, 0.1),
            P("Knee radius (dB)", "Knee radius", "dB", 1.0, 10.0, 10.0, 0.25),
            P("Attack time (ms)", "Attack", "ms", 1.5, 400.0, 101.0, 0.5),
            P("Release time (ms)", "Release", "ms", 2.0, 800.0, 401.0, 1.0),
            P("Makeup gain (dB)", "Makeup", "dB", 0.0, 24.0, 5.0, 0.5),
        ],
    },
    "autogain": {
        "title": "Autogain", "subtitle": "LSP loudness normalizer",
        "plugin": "lsp-plugins-ladspa",
        "label": "http://lsp-plug.in/plugins/ladspa/autogain_stereo",
        "audio": {"in_l": "Input L", "in_r": "Input R",
                  "out_l": "Output L", "out_r": "Output R"},
        "params": [
            P("Desired loudness level (LUFS)", "Target loudness", "LUFS",
              -60.0, 0.0, -23.0, 0.5),
            P("The level of silence (LUFS)", "Silence level", "LUFS",
              -84.0, -36.0, -50.0, 1.0),
            P("Level drift (dB)", "Level drift", "dB", 0.0, 24.0, 12.0, 0.5),
            P("Enable maximum amplification gain limitation", "Limit boost",
              "", 0.0, 1.0, 1.0, 1.0, toggle=True),
            P("The maximum amplification gain (dB)", "Max boost", "dB",
              0.0, 108.0, 15.0, 1.0),
            P("Loudness measuring long period (ms)", "Long window", "ms",
              100.0, 2000.0, 1050.0, 10.0),
            P("Loudness measuring short period (ms)", "Short window", "ms",
              5.0, 100.0, 52.5, 0.5),
        ],
    },
    "limiter": {
        "title": "Limiter", "subtitle": "LSP brickwall limiter",
        "plugin": "lsp-plugins-ladspa",
        "label": "http://lsp-plug.in/plugins/ladspa/limiter_stereo",
        "audio": {"in_l": "Input L", "in_r": "Input R",
                  "out_l": "Output L", "out_r": "Output R"},
        "params": [
            P("Threshold (G)", "Threshold", "dB", 0.00398, 1.0, 0.5, 0.5,
              db=True),
            P("Knee level (G)", "Knee level", "dB", 0.2512, 3.981, 0.707, 0.5,
              db=True),
            P("Knee smooth (dB)", "Knee smooth", "dB", -48.0, 0.0, -12.0, 1.0),
            P("Lookahead (ms)", "Lookahead", "ms", 0.1, 20.0, 5.0, 0.1),
            P("Attack time (ms)", "Attack", "ms", 0.25, 20.0, 5.0, 0.1),
            P("Release time (ms)", "Release", "ms", 0.25, 20.0, 5.0, 0.1),
            P("Output gain (G)", "Output gain", "dB", 0.001, 1000.0, 1.0, 0.5,
              db=True),
            P("Stereo linking (%)", "Stereo linking", "%", 0.0, 100.0, 100.0,
              1.0),
        ],
    },
}

# ---- helpers ---------------------------------------------------------------

def ui_bounds(p):
    if p["db"]:
        return (20.0 * math.log10(p["min"]), 20.0 * math.log10(p["max"]))
    return (p["min"], p["max"])

def to_ui(p, v):
    if p["toggle"]:
        return 1.0 if v >= 0.5 else 0.0
    if p["db"]:
        return 20.0 * math.log10(max(v, p["min"]))
    return float(v)

def from_ui(p, x):
    if p["db"]:
        return 10.0 ** (x / 20.0)
    return float(x)

def default_state():
    return {
        "version": 1,
        "input": "",
        "source_desc": "Mic DSP",
        "stages": {
            s: {
                "enabled": s in DEFAULT_ENABLED,
                "params": {p["port"]: float(p["default"])
                           for p in STAGE_INFO[s]["params"]},
            }
            for s in STAGES
        },
    }
