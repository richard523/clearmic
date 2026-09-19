#!/usr/bin/env python3
"""Mic DSP — GTK4/libadwaita UI for the PipeWire mic filter-chain."""
import json
import math
import os
import re
import sys
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

import backend
import confgen
from catalog import (EXCLUSIVE, STAGES, STAGE_INFO, default_state, from_ui,
                     to_ui, ui_bounds)

APP_ID = "org.chardlinux.MicDSP"
STATE_DIR = os.path.expanduser("~/.config/mic-dsp-ui")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
PRESETS_DIR = os.path.join(STATE_DIR, "presets")

CSS = b"""
.meter-label { font-size: 9pt; opacity: 0.6; }
.stage-switchbox { padding: 4px 0 6px 0; }
"""


# ---------------------------------------------------------------- state I/O

def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def load_state():
    """Returns (state, migrated, fresh)."""
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f), False, False
    migrated = confgen.migrate()
    if migrated is not None:
        save_state(migrated)
        return migrated, True, False
    st = default_state()
    devs = backend.input_devices()
    if devs:
        st["input"] = devs[0][0]
    return st, False, True


# ---------------------------------------------------------------- meter view

def _rounded(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _level_color(n):
    if n < 0.6:
        t = n / 0.6
        return (0.30 + 0.65 * t, 0.85, 0.45 - 0.15 * t)
    t = (n - 0.6) / 0.4
    return (0.95, 0.85 - 0.55 * t, 0.30)


class MeterView(Gtk.Box):
    def __init__(self, label):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.peak_db = -99.0
        self.peak_ts = 0.0
        self.meter = None
        self.label_widget = Gtk.Label(label=label, xalign=0.0,
                                      css_classes=["meter-label"])
        self.area = Gtk.DrawingArea()
        self.area.set_content_height(44)
        self.area.set_draw_func(self._draw, None, None)
        self.append(self.label_widget)
        self.append(self.area)

    def attach(self, meter):
        self.meter = meter

    def _draw(self, area, cr, w, h, *args):
        db = self.meter.get_dbfs() if self.meter else -99.0
        now = time.monotonic()
        if db > self.peak_db:
            self.peak_db, self.peak_ts = db, now
        elif now - self.peak_ts > 1.5:
            self.peak_db, self.peak_ts = db, now
        # track
        cr.set_source_rgba(0.10, 0.11, 0.13, 1.0)
        _rounded(cr, 0, 0, w, h, 8)
        cr.fill()
        # fill
        n = max(0.0, min(1.0, (db + 60.0) / 60.0))
        if n > 0.003:
            r, g, b = _level_color(n)
            cr.set_source_rgba(r, g, b, 0.95)
            _rounded(cr, 0, 0, w * n, h, 8)
            cr.fill()
        # peak hold
        pn = max(0.0, min(1.0, (self.peak_db + 60.0) / 60.0))
        if pn > 0.003:
            cr.set_source_rgba(1, 1, 1, 0.75)
            cr.rectangle(w * pn - 1.5, 1, 2.0, h - 2)
            cr.fill()
        # ticks
        cr.set_source_rgba(1, 1, 1, 0.22)
        for t in (-50, -40, -30, -20, -10):
            x = (t + 60.0) / 60.0 * w
            cr.move_to(x, 0)
            cr.line_to(x, 5)
            cr.stroke()
        cr.set_source_rgba(0.92, 0.30, 0.30, 0.9)
        cr.move_to(w - 1, 0)
        cr.line_to(w - 1, h)
        cr.stroke()


# ---------------------------------------------------------------- main window

class Window(Adw.ApplicationWindow):
    def __init__(self, app, test_mode=False):
        super().__init__(application=app)
        self.test_mode = test_mode
        self.set_default_size(880, 660)
        self.set_title("Mic DSP")
        self.state, self.migrated, self.fresh = load_state()
        confgen.write(self.state)  # conf exists even if the UI fails to build
        self.selected = None
        self.pending = {}
        self.pending_src = None
        self.meters = {}
        self._meter_tick = None
        self.monitor = backend.Monitor()
        self.stage_buttons = {}
        self.stage_switches = {}
        self.restarting = False
        self.param_rows = {}
        self._preset_map = {}
        self._dev_names = []

        self._build_ui()
        self._startup()

    # ------------------------------------------------------------ UI build

    def _build_ui(self):
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(outer)

        # banner
        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", self._on_banner_button)
        outer.append(self.banner)

        # header
        header = Adw.HeaderBar()
        outer.append(header)

        self.wtitle = Adw.WindowTitle(title="Mic DSP", subtitle="…")
        header.set_title_widget(self.wtitle)

        # presets
        self.preset_btn = Adw.SplitButton(label="Presets")
        self.preset_btn.set_menu_model(self._build_preset_menu())
        self.preset_btn.connect("clicked", self._on_preset_save_as)
        header.pack_start(self.preset_btn)

        # listen
        self.listen_btn = Gtk.ToggleButton(icon_name="audio-headphones-symbolic")
        self.listen_btn.set_tooltip_text(
            "Listen: route processed mic to the default output")
        self.listen_btn.connect("toggled", self._on_listen)
        header.pack_end(self.listen_btn)

        # meters card
        mcard = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                        margin_top=10, margin_bottom=4,
                        margin_start=14, margin_end=14)
        outer.append(mcard)

        dev_row = Gtk.Box(spacing=10)
        mcard.append(dev_row)
        dev_row.append(Gtk.Label(label="Input device",
                                 css_classes=["heading"]))
        self.dev_combo = Gtk.DropDown()
        self.dev_combo.connect("notify::selected", self._on_device_selected)
        dev_row.append(self.dev_combo)

        self.meter_raw = MeterView("Raw (input device)")
        self.meter_out = MeterView("Processed (virtual source)")
        mcard.append(self.meter_raw)
        mcard.append(self.meter_out)

        # stage strip
        strip_label = Gtk.Label(label="Chain", xalign=0.0, margin_start=14,
                                css_classes=["heading"])
        outer.append(strip_label)
        strip = Gtk.Box(spacing=8, margin_start=14, margin_end=14,
                        margin_top=2, margin_bottom=2, homogeneous=True)
        outer.append(strip)
        for s in STAGES:
            strip.append(self._build_stage_card(s))

        # params panel
        self.params_scroll = Gtk.ScrolledWindow(vexpand=True,
                                                margin_start=14,
                                                margin_end=14,
                                                margin_top=6,
                                                margin_bottom=10)
        self.params_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.params_scroll.set_child(self.params_box)
        outer.append(self.params_scroll)

    def _build_stage_card(self, stage):
        info = STAGE_INFO[stage]
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        frame.set_child(box)
        btn = Gtk.ToggleButton()
        btn.set_child(Gtk.Label(label=info["title"],
                                css_classes=["heading"]))
        btn.set_tooltip_text(info["subtitle"])
        btn.connect("toggled", self._on_stage_selected, stage)
        box.append(btn)
        swbox = Gtk.Box(css_classes=["stage-switchbox"])
        sw = Gtk.Switch(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        sw.connect("notify::active", self._on_stage_enable, stage)
        swbox.append(sw)
        box.append(swbox)
        self.stage_buttons[stage] = btn
        self.stage_switches[stage] = sw
        return frame

    # ------------------------------------------------------------ startup

    def _startup(self):
        self._refresh_devices()
        self._refresh_stage_cards()
        self._select_stage("rnnoise")
        confgen.write(self.state)
        if self.migrated:
            self._set_banner(
                "Migrated the legacy rnnoise filter-chain config "
                "(20-rnnoise-source.conf renamed to .bak).",
                "Restart PipeWire")
            self._set_status("restart needed")
        elif backend.filter_node_id() is None:
            self._set_banner("Filter chain is not running.", "Start chain")
            self._set_status("not running")
        else:
            self._sync_from_live()
        if not self.test_mode:
            self._start_meters()
        if self.test_mode:
            GLib.timeout_add(1200, self._test_quit)

    def _test_quit(self):
        print("SELFTEST OK")
        self.get_application().quit()
        return False

    def _start_meters(self):
        self._stop_meters()
        self.meters["raw"] = backend.Meter(self.state["input"])
        self.meters["out"] = backend.Meter(backend.FILTER_OUTPUT)
        self.meter_raw.attach(self.meters["raw"])
        self.meter_out.attach(self.meters["out"])
        for m in self.meters.values():
            m.start()
        self._meter_tick = GLib.timeout_add(33, self._on_meter_tick)

    def _stop_meters(self):
        if self._meter_tick is not None:
            GLib.Source.remove(self._meter_tick)
            self._meter_tick = None
        for m in self.meters.values():
            m.stop()
        self.meters = {}

    def _on_meter_tick(self):
        self.meter_raw.area.queue_draw()
        self.meter_out.area.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _sync_from_live(self):
        """Adopt live values from the running chain into state + UI."""
        live = backend.get_controls()
        if not live:
            return
        for stage in STAGES:
            if not self.state["stages"][stage]["enabled"]:
                continue
            for p in STAGE_INFO[stage]["params"]:
                key = f"{stage}:{p['port']}"
                if key in live:
                    val = live[key]
                    if isinstance(val, bool):
                        val = 1.0 if val else 0.0
                    self.state["stages"][stage]["params"][p["port"]] = \
                        float(val)
        save_state(self.state)
        confgen.write(self.state)
        self._refresh_param_panel()
        nid = backend.filter_node_id()
        self._set_status(f"live · node {nid}")

    def _refresh_devices(self):
        devs = backend.input_devices()
        store = Gtk.StringList()
        self._dev_names = []
        sel = 0
        for i, (name, desc) in enumerate(devs):
            store.append(desc or name)
            self._dev_names.append(name)
            if name == self.state.get("input"):
                sel = i
        self.dev_combo.handler_block_by_func(self._on_device_selected)
        self.dev_combo.set_model(store)
        if self._dev_names:
            self.dev_combo.set_selected(sel)
            if not self.state.get("input"):
                self.state["input"] = self._dev_names[sel]
        self.dev_combo.handler_unblock_by_func(self._on_device_selected)

    def _refresh_stage_cards(self):
        for s in STAGES:
            st = self.state["stages"][s]
            sw = self.stage_switches[s]
            sw.handler_block_by_func(self._on_stage_enable)
            sw.set_active(st["enabled"])
            sw.handler_unblock_by_func(self._on_stage_enable)
            btn = self.stage_buttons[s]
            btn.set_css_classes([] if st["enabled"] else ["flat"])

    # ------------------------------------------------------------ params UI

    def _select_stage(self, stage):
        self.selected = stage
        for s, b in self.stage_buttons.items():
            b.handler_block_by_func(self._on_stage_selected)
            b.set_active(s == stage)
            b.handler_unblock_by_func(self._on_stage_selected)
        self._refresh_param_panel()

    def _on_stage_selected(self, btn, stage):
        if btn.get_active():
            self._select_stage(stage)

    def _refresh_param_panel(self):
        for w in list(self.param_rows.values()):
            self.params_box.remove(w)
        self.param_rows = {}
        stage = self.selected
        if stage is None:
            return
        info = STAGE_INFO[stage]
        if not self.state["stages"][stage]["enabled"]:
            note = Gtk.Label(
                label=f"({info['title']} is disabled — "
                      f"changes apply once enabled)",
                css_classes=["dim-label"])
            note.set_halign(Gtk.Align.START)
            self.params_box.append(note)
        for p in info["params"]:
            row = self._build_param_row(stage, p)
            self.params_box.append(row)
            self.param_rows[p["port"]] = row

    def _build_param_row(self, stage, p):
        if p["toggle"]:
            return self._build_toggle_row(stage, p)
        row = Gtk.Box(spacing=12, margin_top=4, margin_bottom=4)
        row.append(Gtk.Label(label=p["disp"], xalign=0.0,
                            width_chars=22, hexpand=False))
        mn, mx = ui_bounds(p)
        stored = self.state["stages"][stage]["params"].get(p["port"],
                                                           p["default"])
        val = to_ui(p, stored)
        step = p["step"] or (mx - mn) / 100.0
        adj = Gtk.Adjustment(value=val, lower=mn, upper=mx,
                             step_increment=step, page_increment=step * 10)
        scale = Gtk.Scale(adjustment=adj, hexpand=True, draw_value=False)
        digits = 0 if p["integer"] else (2 if step < 0.5 else 1)
        scale.set_digits(digits)
        vlabel = Gtk.Label(width_chars=10, xalign=1.0, css_classes=["numeric"])
        vlabel.set_text(self._fmt_value(p, val))
        scale.connect("value-changed",
                      self._on_param_changed, stage, p, vlabel)
        row.append(scale)
        row.append(vlabel)
        return row

    def _build_toggle_row(self, stage, p):
        row = Gtk.Box(spacing=12, margin_top=4, margin_bottom=4)
        row.append(Gtk.Label(label=p["disp"], xalign=0.0, hexpand=True))
        sw = Gtk.Switch()
        stored = self.state["stages"][stage]["params"].get(p["port"],
                                                          p["default"])
        sw.set_active(float(stored) >= 0.5)
        sw.connect("notify::active", self._on_toggle_changed, stage, p)
        row.append(sw)
        return row

    @staticmethod
    def _fmt_value(p, ui_val):
        if p["db"]:
            return f"{ui_val:+.1f} dB"
        txt = f"{ui_val:.2f}".rstrip("0").rstrip(".")
        if p["integer"]:
            txt = f"{ui_val:.0f}"
        if p["unit"]:
            txt += f" {p['unit']}"
        return txt

    def _on_param_changed(self, scale, stage, p, vlabel):
        ui_val = scale.get_value()
        vlabel.set_text(self._fmt_value(p, ui_val))
        stored = from_ui(p, ui_val)
        self.pending[f"{stage}:{p['port']}"] = stored
        if self.pending_src:
            GLib.Source.remove(self.pending_src)
        self.pending_src = GLib.timeout_add(300, self._flush_pending)

    def _on_toggle_changed(self, sw, _pspec, stage, p):
        stored = 1.0 if sw.get_active() else 0.0
        self.pending[f"{stage}:{p['port']}"] = stored
        if self.pending_src:
            GLib.Source.remove(self.pending_src)
        self.pending_src = GLib.timeout_add(300, self._flush_pending)

    def _flush_pending(self):
        self.pending_src = None
        if not self.pending:
            return False
        pairs = dict(self.pending)
        self.pending = {}
        toggles = set()
        applicable = {}
        for key in pairs:
            stage, port = key.split(":", 1)
            self.state["stages"][stage]["params"][port] = pairs[key]
            for p in STAGE_INFO[stage]["params"]:
                if p["port"] == port and p["toggle"]:
                    toggles.add(key)
            if self.state["stages"][stage]["enabled"]:
                applicable[key] = pairs[key]
        if applicable and backend.filter_node_id() is not None:
            try:
                backend.set_controls(applicable, toggles)
            except RuntimeError as e:
                self._set_status(f"error: {e}")
        save_state(self.state)
        confgen.write(self.state)
        return False

    # ------------------------------------------------------------ structural

    def _on_stage_enable(self, sw, _pspec, stage):
        if self.restarting:
            return
        enabled = sw.get_active()
        if enabled:
            self.state["stages"][stage]["enabled"] = True
            if stage in EXCLUSIVE:
                other = "deepfilter" if stage == "rnnoise" else "rnnoise"
                self.state["stages"][other]["enabled"] = False
        else:
            if not any(s["enabled"] for s in self.state["stages"].values()):
                # keep at least one stage in the chain
                sw.handler_block_by_func(self._on_stage_enable)
                sw.set_active(True)
                sw.handler_unblock_by_func(self._on_stage_enable)
                return
            self.state["stages"][stage]["enabled"] = False
        save_state(self.state)
        confgen.write(self.state)
        self._refresh_stage_cards()
        self._refresh_param_panel()
        self._apply_structural()

    def _apply_structural(self):
        self.restarting = True
        self._set_banner("Restarting PipeWire to rebuild the chain…")
        self._set_status("restarting…")
        self._stop_meters()
        self._stop_listen()

        def worker():
            ok = backend.restart_pipewire()
            GLib.idle_add(self._restart_done, ok)

        threading.Thread(target=worker, daemon=True).start()

    def _restart_done(self, ok):
        self.restarting = False
        if ok:
            self._set_banner(None)
            self._sync_from_live()
            if not self.test_mode:
                self._start_meters()
        else:
            self._set_banner("PipeWire restart failed — check journalctl.",
                             "Retry")
            self._set_status("error")

    # ------------------------------------------------------------ banner/status

    def _set_banner(self, text, button=None):
        if text is None:
            self.banner.set_revealed(False)
            return
        self.banner.set_title(text)
        self.banner.set_button_label(button or "")
        self.banner.set_revealed(True)

    def _on_banner_button(self, *_):
        self._apply_structural()

    def _set_status(self, text):
        self.wtitle.set_subtitle(text)

    # ------------------------------------------------------------ devices

    def _on_device_selected(self, *_):
        if not self._dev_names:
            return
        idx = self.dev_combo.get_selected()
        if idx >= len(self._dev_names):
            return
        name = self._dev_names[idx]
        if name == self.state.get("input") or self.restarting:
            return
        self.state["input"] = name
        save_state(self.state)
        confgen.write(self.state)
        self._apply_structural()

    # ------------------------------------------------------------ listen

    def _on_listen(self, btn):
        if btn.get_active():
            dlg = Adw.MessageDialog(
                transient_for=self, heading="Route mic to speakers?",
                body="The processed mic will play on your default output. "
                     "Use headphones to avoid feedback.")
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("ok", "Listen")
            dlg.set_response_appearance("ok",
                                        Adw.ResponseAppearance.SUGGESTED)

            def on_resp(d, resp):
                if resp == "ok":
                    self.monitor.start()
                else:
                    btn.set_active(False)

            dlg.connect("response", on_resp)
            dlg.present()
        else:
            self._stop_listen()

    def _stop_listen(self):
        self.monitor.stop()
        self.listen_btn.handler_block_by_func(self._on_listen)
        self.listen_btn.set_active(False)
        self.listen_btn.handler_unblock_by_func(self._on_listen)

    # ------------------------------------------------------------ presets

    def _preset_files(self):
        os.makedirs(PRESETS_DIR, exist_ok=True)
        return sorted(f for f in os.listdir(PRESETS_DIR)
                      if f.endswith(".json"))

    def _build_preset_menu(self):
        menu = Gio.Menu()
        section = Gio.Menu()
        files = self._preset_files()
        if files:
            for fn in files:
                name = fn[:-5]
                action = "preset-" + re.sub(r"[^a-z0-9-]", "-",
                                             name.lower())
                self._preset_map[action] = fn
                section.append(name, f"win.{action}")
        else:
            section.append("(none — save one first)", "win.preset-none")
        menu.append_section("Presets", section)
        return menu

    def _on_preset_save_as(self, *_):
        dlg = Adw.MessageDialog(transient_for=self,
                               heading="Save preset as…")
        entry = Gtk.Entry(placeholder_text="e.g. stream-clean")
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", "Save")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def on_resp(d, resp):
            if resp == "ok":
                name = entry.get_text().strip() or "preset"
                name = re.sub(r"[^A-Za-z0-9_-]", "-", name)
                os.makedirs(PRESETS_DIR, exist_ok=True)
                with open(os.path.join(PRESETS_DIR, name + ".json"),
                          "w") as f:
                    json.dump(self.state, f, indent=2)
                self._reload_preset_menu()

        dlg.connect("response", on_resp)
        dlg.present()

    def _reload_preset_menu(self):
        self.preset_btn.set_menu_model(self._build_preset_menu())

    def apply_preset(self, fn):
        with open(os.path.join(PRESETS_DIR, fn)) as f:
            self.state = json.load(f)
        save_state(self.state)
        confgen.write(self.state)
        self._refresh_devices()
        self._refresh_stage_cards()
        self._select_stage(next(s for s in STAGES
                                if self.state["stages"][s]["enabled"]))
        self._apply_structural()

    # ------------------------------------------------------------ shutdown

    def _shutdown(self):
        if self.pending:
            for key, val in self.pending.items():
                stage, port = key.split(":", 1)
                self.state["stages"][stage]["params"][port] = val
            save_state(self.state)
            confgen.write(self.state)
        self._stop_meters()
        self._stop_listen()


class App(Adw.Application):
    def __init__(self, test_mode):
        Adw.Application.__init__(self, application_id=APP_ID)
        self.test_mode = test_mode
        self.win = None

    def do_activate(self):
        self.win = Window(self, test_mode=self.test_mode)
        a_none = Gio.SimpleAction.new("preset-none", None)
        a_none.connect("activate", lambda *_: None)
        self.win.add_action(a_none)
        for action, fn in self.win._preset_map.items():
            a = Gio.SimpleAction.new(action, None)
            a.connect("activate",
                      (lambda fn: lambda *_: self.win.apply_preset(fn))(fn))
            self.win.add_action(a)
        self.win.present()

    def do_shutdown(self):
        if self.win:
            self.win._shutdown()
        Adw.Application.do_shutdown(self)


def main():
    test_mode = "--selftest" in sys.argv
    app = App(test_mode)
    app.run(None)


if __name__ == "__main__":
    main()
