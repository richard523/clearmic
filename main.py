#!/usr/bin/env python3
"""Mic DSP — GTK4/libadwaita UI for the PipeWire mic filter-chain.

Follows the GNOME HIG: Adw.ToolbarView + headerbar, ViewSwitcher tabs for
the stages, PreferencesGroup/ActionRow lists, ComboRow, ToastOverlay, and
Adw.Banner only for persistent actionable states.
"""
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
from gi.repository import Adw, Gio, GLib, Gtk

import backend
import confgen
from catalog import (EXCLUSIVE, STAGES, STAGE_INFO, default_state, from_ui,
                     to_ui, ui_bounds)

APP_ID = "org.chardlinux.MicDSP"
STATE_DIR = os.path.expanduser("~/.config/mic-dsp-ui")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
PRESETS_DIR = os.path.join(STATE_DIR, "presets")
LOG_PATH = os.path.join(GLib.get_user_cache_dir(), "mic-dsp-ui", "app.log")


def log(msg):
    """Append to the app log — the only persistent record when launched
    from the desktop (stderr goes nowhere)."""
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


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


# ---------------------------------------------------------------- level bar

class LevelBar(Gtk.DrawingArea):
    """Peak meter with dB scale, peak-hold and theme-colored track."""

    def __init__(self):
        super().__init__()
        self.set_size_request(340, 40)
        self.set_content_height(40)
        self.meter = None
        self.peak_db = -99.0
        self.peak_ts = 0.0
        self.set_draw_func(self._draw)

    @staticmethod
    def _level_color(n):
        if n < 0.6:
            t = n / 0.6
            return (0.30 + 0.65 * t, 0.85, 0.45 - 0.15 * t)
        t = (n - 0.6) / 0.4
        return (0.95, 0.85 - 0.55 * t, 0.30)

    @staticmethod
    def _rounded(cr, x, y, w, h, r):
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def _draw(self, area, cr, w, h):
        db = self.meter.get_dbfs() if self.meter else -99.0
        now = time.monotonic()
        if db > self.peak_db:
            self.peak_db, self.peak_ts = db, now
        elif now - self.peak_ts > 1.5:
            self.peak_db, self.peak_ts = db, now
        # track in theme foreground color
        fg = area.get_color()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
        self._rounded(cr, 0, 0, w, h, 8)
        cr.fill()
        # fill
        n = max(0.0, min(1.0, (db + 60.0) / 60.0))
        if n > 0.003:
            r, g, b = self._level_color(n)
            cr.set_source_rgba(r, g, b, 0.95)
            self._rounded(cr, 0, 0, w * n, h, 8)
            cr.fill()
        # peak hold
        pn = max(0.0, min(1.0, (self.peak_db + 60.0) / 60.0))
        if pn > 0.003:
            cr.set_source_rgba(1, 1, 1, 0.75)
            cr.rectangle(w * pn - 1.5, 1, 2.0, h - 2)
            cr.fill()
        # ticks
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.25)
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
        self.set_default_size(900, 700)
        self.set_title("Mic DSP")
        self.state, self.migrated, self.fresh = load_state()
        confgen.write(self.state)  # conf exists even if the UI fails to build
        self.pending = {}
        self.pending_src = None
        self.meters = {}
        self._meter_tick = None
        self.monitor = backend.Monitor()
        self.enabled_switches = {}
        self.param_rows = {}      # (stage, port) -> record
        self.restarting = False
        self._preset_map = {}
        self._dev_names = []

        self._build_ui()
        self._startup()

    # ------------------------------------------------------------ UI build

    def _build_ui(self):
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        toolbar = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar)

        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        self.wtitle = Adw.WindowTitle(title="Mic DSP", subtitle="…")
        header.set_title_widget(self.wtitle)

        # presets
        self.preset_btn = Adw.SplitButton(label="Presets")
        self.preset_btn.set_menu_model(self._build_preset_menu())
        self.preset_btn.connect("clicked", self._on_preset_save_as)
        header.pack_start(self.preset_btn)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       margin_top=10, margin_bottom=10,
                       margin_start=12, margin_end=12)
        toolbar.set_content(body)

        # --- signal group (device + meters)
        sig_group = Adw.PreferencesGroup(title="Signal")
        self.dev_combo = Adw.ComboRow(title="Input device",
                                      subtitle="capture target of the chain")
        self.dev_combo.connect("notify::selected", self._on_device_selected)
        sig_group.add(self.dev_combo)

        raw_row = Adw.ActionRow(title="Raw", subtitle="input device level")
        self.raw_bar = LevelBar()
        raw_row.add_suffix(self.raw_bar)
        sig_group.add(raw_row)

        out_row = Adw.ActionRow(title="Processed",
                                subtitle="virtual source level")
        self.out_bar = LevelBar()
        out_row.add_suffix(self.out_bar)
        sig_group.add(out_row)

        # --- live test (Listen)
        self.sc_row = Adw.ActionRow(
            title="Live test",
            subtitle="Hear the processed mic on your output in real time")
        self.listen_btn = Gtk.ToggleButton(label="Listen",
                                           valign=Gtk.Align.CENTER)
        self.listen_btn.connect("toggled", self._on_listen)
        self.sc_row.add_suffix(self.listen_btn)
        sig_group.add(self.sc_row)
        body.append(sig_group)

        # --- stage tabs
        switcher = Adw.ViewSwitcher()
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        body.append(switcher)

        self.stack = Adw.ViewStack()
        switcher.set_stack(self.stack)
        for s in STAGES:
            page = self._build_stage_page(s)
            self.stack.add_titled(page, s, STAGE_INFO[s]["title"])
        body.append(self.stack)

        # banner (persistent actionable states only)
        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", self._on_banner_button)
        toolbar.add_top_bar(self.banner)

    def _build_stage_page(self, stage):
        info = STAGE_INFO[stage]
        group = Adw.PreferencesGroup(title=info["title"],
                                     description=info["subtitle"])

        erow = Adw.ActionRow(
            title="Enabled",
            subtitle="rebuilding the chain restarts PipeWire (~3 s)")
        sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        sw.connect("notify::active", self._on_stage_enable, stage)
        self.enabled_switches[stage] = sw
        erow.add_suffix(sw)
        group.add(erow)

        for p in info["params"]:
            group.add(self._build_param_row(stage, p))
        return group

    def _build_param_row(self, stage, p):
        stored = self.state["stages"][stage]["params"].get(p["port"],
                                                          p["default"])
        if p["toggle"]:
            row = Adw.ActionRow(title=p["disp"])
            sw = Gtk.Switch(valign=Gtk.Align.CENTER)
            sw.set_active(float(stored) >= 0.5)
            sw.connect("notify::active", self._on_toggle_changed, stage, p)
            row.add_suffix(sw)
            self.param_rows[(stage, p["port"])] = {"switch": sw,
                                                   "param": p}
            return row

        row = Adw.ActionRow(title=p["disp"])
        mn, mx = ui_bounds(p)
        val = to_ui(p, stored)
        step = p["step"] or (mx - mn) / 100.0
        adj = Gtk.Adjustment(value=val, lower=mn, upper=mx,
                             step_increment=step, page_increment=step * 10)
        scale = Gtk.Scale(adjustment=adj, draw_value=False,
                          hexpand=True, valign=Gtk.Align.CENTER)
        scale.set_size_request(320, -1)
        digits = 0 if p["integer"] else (2 if step < 0.5 else 1)
        scale.set_digits(digits)
        vlabel = Gtk.Label(width_chars=10, xalign=1.0,
                           css_classes=["numeric"])
        vlabel.set_text(self._fmt_value(p, val))
        scale.connect("value-changed", self._on_param_changed, stage, p)
        box = Gtk.Box(spacing=8)
        box.append(scale)
        box.append(vlabel)
        row.add_suffix(box)
        self.param_rows[(stage, p["port"])] = {"scale": scale,
                                              "adj": adj,
                                              "vlabel": vlabel,
                                              "param": p}
        return row

    # ------------------------------------------------------------ startup

    def _startup(self):
        log(f"startup: input={self.state['input']} "
            f"stages={[s for s, v in self.state['stages'].items()
                      if v['enabled']]} test_mode={self.test_mode}")
        self._refresh_devices()
        self._refresh_all_rows()
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

    # ------------------------------------------------------------ meters

    def _start_meters(self):
        self._stop_meters()
        self.meters["raw"] = backend.Meter(self.state["input"])
        self.meters["out"] = backend.Meter(backend.FILTER_OUTPUT)
        self.raw_bar.meter = self.meters["raw"]
        self.out_bar.meter = self.meters["out"]
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
        self.raw_bar.queue_draw()
        self.out_bar.queue_draw()
        return GLib.SOURCE_CONTINUE

    # ------------------------------------------------------------ refresh

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

    def _refresh_all_rows(self):
        """Push state values into every widget (without emitting changes)."""
        for stage in STAGES:
            sw = self.enabled_switches[stage]
            sw.handler_block_by_func(self._on_stage_enable)
            sw.set_active(self.state["stages"][stage]["enabled"])
            sw.handler_unblock_by_func(self._on_stage_enable)
            self._set_page_sensitive(stage,
                                     self.state["stages"][stage]["enabled"])
        for (stage, port), rec in self.param_rows.items():
            stored = self.state["stages"][stage]["params"].get(port,
                                                               rec["param"]["default"])
            if "switch" in rec:
                s = rec["switch"]
                s.handler_block_by_func(self._on_toggle_changed)
                s.set_active(float(stored) >= 0.5)
                s.handler_unblock_by_func(self._on_toggle_changed)
            else:
                p = rec["param"]
                scale = rec["scale"]
                scale.handler_block_by_func(self._on_param_changed)
                rec["adj"].set_value(to_ui(p, stored))
                rec["vlabel"].set_text(self._fmt_value(p, to_ui(p, stored)))
                scale.handler_unblock_by_func(self._on_param_changed)

    def _set_page_sensitive(self, stage, enabled):
        for (s, _port), rec in self.param_rows.items():
            if s != stage:
                continue
            w = rec.get("switch") or rec.get("scale")
            w.set_sensitive(enabled)

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
        self._refresh_all_rows()
        nid = backend.filter_node_id()
        self._set_status(f"live · node {nid}")

    # ------------------------------------------------------------ param edits

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

    def _on_param_changed(self, scale, stage, p):
        rec = self.param_rows[(stage, p["port"])]
        ui_val = rec["adj"].get_value()
        rec["vlabel"].set_text(self._fmt_value(p, ui_val))
        self._queue_edit(stage, p, from_ui(p, ui_val))

    def _on_toggle_changed(self, sw, _pspec, stage, p):
        self._queue_edit(stage, p, 1.0 if sw.get_active() else 0.0)

    def _queue_edit(self, stage, p, stored):
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
                log(f"set_controls failed: {e}")
                self.toast(f"Could not apply: {e}")
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
                if self.state["stages"][other]["enabled"]:
                    self.state["stages"][other]["enabled"] = False
                    self.toast(f"Disabled {STAGE_INFO[other]['title']} "
                                f"(only one noise suppressor)")
        else:
            if not any(s["enabled"] for s in self.state["stages"].values()):
                sw.handler_block_by_func(self._on_stage_enable)
                sw.set_active(True)
                sw.handler_unblock_by_func(self._on_stage_enable)
                self.toast("At least one stage must stay enabled")
                return
            self.state["stages"][stage]["enabled"] = False
        save_state(self.state)
        confgen.write(self.state)
        self._refresh_all_rows()
        self._apply_structural()

    def _apply_structural(self):
        log("structural change: restarting PipeWire")
        self.restarting = True
        self.toast("Restarting PipeWire to rebuild the chain…")
        self._set_status("restarting…")
        self._stop_meters()
        self._stop_listen()

        def worker():
            ok = backend.restart_pipewire()
            GLib.idle_add(self._restart_done, ok)

        threading.Thread(target=worker, daemon=True).start()

    def _restart_done(self, ok):
        self.restarting = False
        log(f"PipeWire restart {'ok' if ok else 'FAILED'}")
        if ok:
            self._set_banner(None)
            self._sync_from_live()
            if not self.test_mode:
                self._start_meters()
            self.toast("Chain is live")
        else:
            self._set_banner("PipeWire restart failed — check journalctl.",
                             "Retry")
            self._set_status("error")

    # ------------------------------------------------------------ banner/toast

    def _set_banner(self, text, button=None):
        if text is None:
            self.banner.set_revealed(False)
            return
        self.banner.set_title(text)
        self.banner.set_button_label(button or "")
        self.banner.set_revealed(True)

    def _on_banner_button(self, *_):
        self._apply_structural()

    def toast(self, msg):
        self.toast_overlay.add_toast(Adw.Toast(title=msg))

    def _set_status(self, text):
        self.wtitle.set_subtitle(text)

    # ------------------------------------------------------------ devices

    def _on_device_selected(self, *_):
        if not self._dev_names or self.restarting:
            return
        idx = self.dev_combo.get_selected()
        if idx >= len(self._dev_names):
            return
        name = self._dev_names[idx]
        if name == self.state.get("input"):
            return
        self.state["input"] = name
        save_state(self.state)
        confgen.write(self.state)
        self._apply_structural()

    # ------------------------------------------------------------ listen

    def _on_listen(self, btn):
        if btn.get_active():
            if backend.filter_node_id() is None:
                self.toast("Filter chain is not running")
                self._stop_listen()
                return
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
                    log("listen: live test started")
                    self.listen_btn.set_label("Stop")
                    self.sc_row.set_subtitle(
                        "Listening — tap Stop to end")
                else:
                    self._stop_listen()

            dlg.connect("response", on_resp)
            dlg.present()
        else:
            self._stop_listen()

    def _stop_listen(self):
        was_active = self.monitor.active
        self.monitor.stop()
        self.listen_btn.handler_block_by_func(self._on_listen)
        self.listen_btn.set_active(False)
        self.listen_btn.handler_unblock_by_func(self._on_listen)
        self.listen_btn.set_label("Listen")
        self.sc_row.set_subtitle(
            "Hear the processed mic on your output in real time")
        if was_active:
            log("listen: live test stopped")

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
                self.toast(f"Preset “{name}” saved")

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
        self._refresh_all_rows()
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
        try:
            self.win = Window(self, test_mode=self.test_mode)
        except Exception:
            import traceback
            traceback.print_exc()
            self.quit()
            return
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
