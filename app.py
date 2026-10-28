#!/usr/bin/python3
"""Interface gráfica (GTK4 + libadwaita) do gerenciador de serviços."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import backend  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

APP_ID = "io.local.ServicesManager"
CSS_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "style.css")


def toast(window, message: str) -> None:
    if window is None:
        return
    overlay = getattr(window, "toast_overlay", None)
    if overlay is not None:
        overlay.add_toast(Adw.Toast.new(message))


class ServiceRow(Adw.ActionRow):
    def __init__(self, window: "MainWindow", name: str):
        super().__init__()
        self.window = window
        self.name = name
        self.conf = {}
        self._syncing = False
        self._hold_until = 0.0
        self.set_activatable(False)
        self.add_css_class("service-row")

        self._boot_state = None

        self.run_switch = Gtk.Switch()
        self.run_switch.set_valign(Gtk.Align.CENTER)
        self.run_switch.set_tooltip_text("Rodando")
        self.run_switch.connect("notify::active", self._on_run_toggled)
        self.add_suffix(self.run_switch)

        self.menu_button = Gtk.MenuButton(icon_name="view-more-symbolic")
        self.menu_button.add_css_class("flat")
        self.menu_button.set_tooltip_text("Mais ações")
        self.add_suffix(self.menu_button)
        self._build_menu(True)

    def _build_menu(self, boot_enabled: bool) -> None:
        self._boot_state = boot_enabled
        menu = Gio.Menu()
        menu.append(
            "Iniciar no boot: sim" if boot_enabled else "Iniciar no boot: não",
            f"app.toggle-boot::{self.name}",
        )
        menu.append("Editar", f"app.edit::{self.name}")
        menu.append("Reiniciar", f"app.restart::{self.name}")
        menu.append("Parar", f"app.stop::{self.name}")
        menu.append("Logs", f"app.logs::{self.name}")
        menu.append("Remover", f"app.remove::{self.name}")
        self.menu_button.set_menu_model(menu)

    def update(self, conf: dict, st: dict) -> None:
        self.conf = conf
        desc = conf.get("DESC") or ""
        self.set_title(self.name)
        details = []
        if desc:
            details.append(desc)
        if conf.get("PORT"):
            details.append(f"porta {conf['PORT']}")
        if st.get("main_pid"):
            details.append(f"PID {st['main_pid']}")
        if st.get("uptime"):
            details.append(st["uptime"])
        self.set_subtitle(" · ".join(details) or "—")

        boot_enabled = backend.is_truthy(conf.get("AUTOSTART", "1"))
        if boot_enabled != self._boot_state:
            self._build_menu(boot_enabled)

        if time.monotonic() >= self._hold_until:
            self._syncing = True
            self.run_switch.set_active(bool(st.get("active")))
            self._syncing = False
        self.run_switch.set_tooltip_text("Rodando" if st.get("active") else "Parado")

    def _on_run_toggled(self, switch: Gtk.Switch, _pspec) -> None:
        if self._syncing:
            return
        op = "start" if switch.get_active() else "stop"
        self._hold_until = time.monotonic() + 4
        self.window.run_action(self.name, op)


class DockerServiceRow(ServiceRow):
    def __init__(self, window: "MainWindow", name: str):
        super().__init__(window, name)
        self.add_css_class("docker-row")

        self.health_badge = Gtk.Label()
        self.health_badge.set_valign(Gtk.Align.CENTER)
        self.health_badge.add_css_class("health-badge")
        self.add_suffix(self.health_badge)

    def update(self, conf: dict, st: dict) -> None:
        self.conf = conf
        self.set_title(self.name)

        details = []
        if conf.get("IMAGE"):
            details.append(conf["IMAGE"])
        ports = conf.get("PORTS", [])
        if ports:
            details.append(" · ".join(ports[:2]) + ("…" if len(ports) > 2 else ""))
        if conf.get("RESTART_POLICY"):
            details.append(f"restart: {conf['RESTART_POLICY']}")
        self.set_subtitle(" · ".join(details) or "—")

        health = st.get("health", "none")
        health_icons = {"healthy": "✓", "unhealthy": "✗", "none": "○"}
        self.health_badge.set_text(health_icons.get(health, "?"))
        for cls in ("healthy", "unhealthy", "unknown"):
            self.health_badge.remove_css_class(cls)
        self.health_badge.add_css_class(health if health != "none" else "unknown")

        if time.monotonic() >= self._hold_until:
            self._syncing = True
            self.run_switch.set_active(bool(st.get("active")))
            self._syncing = False
        self.run_switch.set_tooltip_text("Rodando" if st.get("active") else "Parado")

        restart_policy = conf.get("RESTART_POLICY", "no")
        boot_enabled = restart_policy in ("always", "unless-stopped")
        if boot_enabled != self._boot_state:
            self._build_menu(boot_enabled)

    def _build_menu(self, boot_enabled: bool) -> None:
        self._boot_state = boot_enabled
        menu = Gio.Menu()

        policy_labels = {
            "always": "Sempre",
            "unless-stopped": "A menos que parado",
            "no": "Não",
        }
        current_policy = self.conf.get("RESTART_POLICY", "no")
        policy_label = policy_labels.get(current_policy, "Não")
        menu.append(f"Restart: {policy_label}", f"app.docker-restart-policy::{self.name}")

        menu.append("Reiniciar", f"app.restart::{self.name}")
        menu.append("Parar", f"app.stop::{self.name}")
        menu.append("Logs", f"app.logs::{self.name}")
        menu.append("Inspecionar", f"app.inspect::{self.name}")
        menu.append("Remover container", f"app.remove-docker::{self.name}")
        self.menu_button.set_menu_model(menu)

    def _on_run_toggled(self, switch: Gtk.Switch, _pspec) -> None:
        if self._syncing:
            return
        op = "start" if switch.get_active() else "stop"
        self._hold_until = time.monotonic() + 4
        self.window.run_action(self.name, op)


class ServiceDialog(Adw.Dialog):
    def __init__(self, window: "MainWindow", name: str | None = None):
        super().__init__()
        self.window = window
        self.editing = name is not None
        self.name_value = name or ""
        self.set_content_width(560)
        self.set_content_height(680)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        title = Gtk.Label(label="Editar serviço" if self.editing else "Adicionar serviço")
        title.add_css_class("title-4")
        header.set_title_widget(title)

        cancel = Gtk.Button(label="Cancelar")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)

        save = Gtk.Button(label="Salvar")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save)
        header.pack_end(save)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()

        group = Adw.PreferencesGroup(title="Serviço")
        self.name_row = Adw.EntryRow(title="Nome (ex.: 9router)")
        self.name_row.set_text(self.name_value)
        if self.editing:
            self.name_row.set_editable(False)
        group.add(self.name_row)

        self.desc_row = Adw.EntryRow(title="Descrição")
        group.add(self.desc_row)

        self.cmd_row = Adw.EntryRow(title="Comando (caminho absoluto + argumentos)")
        pick_cmd = Gtk.Button(icon_name="folder-open-symbolic")
        pick_cmd.add_css_class("flat")
        pick_cmd.set_tooltip_text("Escolher executável")
        pick_cmd.connect("clicked", self._pick_executable)
        self.cmd_row.add_suffix(pick_cmd)
        group.add(self.cmd_row)

        self.workdir_row = Adw.EntryRow(title="Diretório de trabalho")
        pick_dir = Gtk.Button(icon_name="folder-open-symbolic")
        pick_dir.add_css_class("flat")
        pick_dir.set_tooltip_text("Escolher pasta")
        pick_dir.connect("clicked", self._pick_workdir)
        self.workdir_row.add_suffix(pick_dir)
        group.add(self.workdir_row)

        self.port_row = Adw.EntryRow(title="Porta (opcional)")
        self.port_row.set_input_purpose(Gtk.InputPurpose.DIGITS)
        group.add(self.port_row)

        self.health_row = Adw.EntryRow(title="URL de health (opcional)")
        group.add(self.health_row)

        self.boot_row = Adw.SwitchRow(title="Iniciar no boot")
        self.boot_row.set_active(True)
        group.add(self.boot_row)
        page.add(group)

        advanced = Adw.PreferencesGroup(title="Avançado")
        self.after_row = Adw.EntryRow(title="AFTER")
        self.wants_row = Adw.EntryRow(title="WANTS")
        self.type_row = Adw.EntryRow(title="TYPE (simple/oneshot)")
        expander = Adw.ExpanderRow(title="Dependências e tipo")
        expander.add_row(self.after_row)
        expander.add_row(self.wants_row)
        expander.add_row(self.type_row)
        advanced.add(expander)
        page.add(advanced)

        toolbar.set_content(page)
        self.set_child(toolbar)

        if self.editing:
            self._load(self.name_value)
        else:
            self.workdir_row.set_text(str(backend.HOME))
            self.after_row.set_text("network-online.target")
            self.wants_row.set_text("network-online.target")
            self.type_row.set_text("simple")

    def _load(self, name: str) -> None:
        conf = backend.load_defaults()
        conf.update(backend.merged_raw(name))
        self.desc_row.set_text(conf.get("DESC") or "")
        self.cmd_row.set_text(conf.get("CMD") or "")
        self.workdir_row.set_text(conf.get("WORKDIR") or "")
        self.port_row.set_text(conf.get("PORT") or "")
        self.health_row.set_text(conf.get("HEALTH_URL") or "")
        self.after_row.set_text(conf.get("AFTER") or "network-online.target")
        self.wants_row.set_text(conf.get("WANTS") or "network-online.target")
        self.type_row.set_text(conf.get("TYPE") or "simple")
        self.boot_row.set_active(backend.is_truthy(conf.get("AUTOSTART", "1")))

    def _pick_executable(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Escolher executável")

        def done(source, result):
            try:
                file = source.open_finish(result)
            except GLib.Error:
                return
            if file and file.get_path():
                self.cmd_row.set_text(file.get_path())

        dialog.open(self.window, None, done)

    def _pick_workdir(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Escolher diretório")

        def done(source, result):
            try:
                folder = source.select_folder_finish(result)
            except GLib.Error:
                return
            if folder and folder.get_path():
                self.workdir_row.set_text(folder.get_path())

        dialog.select_folder(self.window, None, done)

    def _data(self) -> dict:
        name = self.name_row.get_text().strip()
        raw = backend.read_conf(name) if self.editing else {}
        raw.update({
            "DESC": self.desc_row.get_text().strip() or name,
            "CMD": self.cmd_row.get_text().strip(),
            "WORKDIR": self.workdir_row.get_text().strip() or str(backend.HOME),
            "PORT": self.port_row.get_text().strip(),
            "HEALTH_URL": self.health_row.get_text().strip(),
            "AUTOSTART": "1" if self.boot_row.get_active() else "0",
            "AFTER": self.after_row.get_text().strip() or "network-online.target",
            "WANTS": self.wants_row.get_text().strip() or "network-online.target",
            "TYPE": self.type_row.get_text().strip() or "simple",
        })
        if not raw.get("HEALTH_URL"):
            raw.pop("HEALTH_URL", None)
        if not raw.get("PORT"):
            raw.pop("PORT", None)
        return name, raw

    def _on_save(self, _button) -> None:
        name, data = self._data()
        errors, warnings = backend.validate(self.name_value or name, data)
        if errors:
            self._error("Corrija os campos:", "\n".join(errors))
            return
        try:
            backend.write_conf(name, data)
            backend.apply(name, start_if_inactive=False)
        except (ValueError, FileNotFoundError, OSError) as exc:
            self._error("Não foi possível salvar", str(exc))
            return
        self.window.refresh()
        toast(self.window, f"Serviço '{name}' salvo")
        if warnings:
            toast(self.window, warnings[0])
        self.close()

    def _error(self, heading: str, body: str) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", "OK")
        dialog.present(self.window)


class LogsWindow(Adw.Window):
    def __init__(self, parent: "MainWindow", name: str):
        super().__init__()
        self.name = name
        self.proc = None
        self.set_transient_for(parent)
        self.set_default_size(980, 560)
        self.set_title(f"Logs — {name}")

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        title = Gtk.Label(label=f"Logs — {name}")
        title.add_css_class("title-4")
        header.set_title_widget(title)
        toolbar.add_top_bar(header)

        self.view = Gtk.TextView(editable=False, monospace=True)
        self.view.set_left_margin(10)
        self.view.set_right_margin(10)
        self.view.set_top_margin(8)
        self.buffer = self.view.get_buffer()
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(self.view)
        scroller.set_vexpand(True)
        toolbar.set_content(scroller)

        self.set_content(toolbar)
        self.connect("close-request", self._on_close)
        self._start()

    def _start(self) -> None:
        is_docker = False
        try:
            services = backend.list_all_services(with_status=False)
            for svc in services:
                if svc["name"] == self.name and svc.get("type") == "docker":
                    is_docker = True
                    break
        except Exception:
            pass

        if is_docker:
            try:
                from docker_backend import container_logs_cmd
                cmd = container_logs_cmd(self.name, follow=True, lines=300)
            except Exception:
                cmd = backend.journal_cmd(self.name, follow=True, lines=300)
        else:
            cmd = backend.journal_cmd(self.name, follow=True, lines=300)

        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, errors="replace",
            )
        except OSError as exc:
            self._append(f"erro ao abrir logs: {exc}\n")
            return
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        if not self.proc or not self.proc.stdout:
            return
        for line in self.proc.stdout:
            GLib.idle_add(self._append, line)
        GLib.idle_add(self._append, "\n[journal encerrado]\n")

    def _append(self, line: str) -> bool:
        self.buffer.insert(self.buffer.get_end_iter(), line)
        mark = self.buffer.create_mark(None, self.buffer.get_end_iter(), False)
        self.view.scroll_to_mark(mark, 0.0, True, 0.0, 0.0)
        return False

    def _on_close(self, *_args) -> bool:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        return False


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: "App"):
        super().__init__(application=app, title="Gerenciador de Serviços")
        self.set_default_size(820, 640)
        self.rows: dict[str, ServiceRow] = {}

        self.toast_overlay = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()

        header = Adw.HeaderBar()
        add_button = Gtk.Button(icon_name="list-add-symbolic")
        add_button.set_tooltip_text("Adicionar serviço")
        add_button.connect("clicked", lambda *_: self.open_dialog())
        header.pack_start(add_button)

        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_button.set_tooltip_text("Atualizar")
        refresh_button.connect("clicked", lambda *_: self.refresh())
        header.pack_end(refresh_button)

        menu = Gio.Menu()
        menu.append("Abrir pasta de configuração", "app.open-config")
        menu.append("Recarregar tudo", "app.refresh-all")
        menu.append("Sobre", "app.about")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        menu_button.add_css_class("flat")
        header.pack_end(menu_button)
        toolbar.add_top_bar(header)

        self.search_entry = Gtk.SearchEntry(placeholder_text="Filtrar serviços…")
        self.search_entry.connect("search-changed", lambda *_: self._filter())
        self.search_bar = Gtk.SearchBar()
        self.search_bar.set_child(self.search_entry)
        self.search_bar.connect_entry(self.search_entry)
        toolbar.add_top_bar(self.search_bar)

        search_toggle = Gtk.ToggleButton(icon_name="system-search-symbolic")
        search_toggle.set_tooltip_text("Buscar")
        search_toggle.connect(
            "toggled", lambda btn: self.search_bar.set_search_mode(btn.get_active())
        )
        header.pack_end(search_toggle)
        self.search_bar.connect(
            "notify::search-mode-enabled",
            lambda bar, _p: search_toggle.set_active(bar.get_search_mode()),
        )

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        self.page = Adw.PreferencesPage()
        self.systemd_group = Adw.PreferencesGroup(title="Serviços do Sistema (systemd)")
        self.docker_group = Adw.PreferencesGroup(title="Containers (Docker)")
        self.page.add(self.systemd_group)
        self.page.add(self.docker_group)
        self.stack.add_named(self.page, "list")

        empty = Adw.StatusPage()
        empty.set_icon_name("utilities-system-monitor-symbolic")
        empty.set_title("Nenhum serviço cadastrado")
        empty.set_description("Adicione um serviço para gerenciar aqui.")
        empty_button = Gtk.Button(label="Adicionar serviço")
        empty_button.add_css_class("suggested-action")
        empty_button.set_halign(Gtk.Align.CENTER)
        empty_button.connect("clicked", lambda *_: self.open_dialog())
        empty.set_child(empty_button)
        self.stack.add_named(empty, "empty")

        toolbar.set_content(self.stack)

        action_bar = Gtk.ActionBar()
        self.summary = Gtk.Label(label="")
        self.summary.add_css_class("dim-label")
        action_bar.pack_start(self.summary)
        start_all = Gtk.Button(label="Iniciar todos")
        start_all.connect("clicked", lambda *_: self.run_all("start"))
        stop_all = Gtk.Button(label="Parar todos")
        stop_all.connect("clicked", lambda *_: self.run_all("stop"))
        action_bar.pack_end(stop_all)
        action_bar.pack_end(start_all)
        toolbar.add_bottom_bar(action_bar)

        self.toast_overlay.set_child(toolbar)
        self.set_content(self.toast_overlay)

        self.refresh()
        GLib.timeout_add_seconds(2, self._tick)

    def _tick(self) -> bool:
        if self.get_visible():
            self.refresh()
        return True

    def refresh(self) -> None:
        try:
            services = backend.list_all_services()
        except Exception as exc:  # noqa: BLE001
            toast(self, f"Erro ao ler serviços: {exc}")
            return

        names = {item["name"] for item in services}
        for gone in set(self.rows) - names:
            row = self.rows.pop(gone)
            if isinstance(row, DockerServiceRow):
                self.docker_group.remove(row)
            else:
                self.systemd_group.remove(row)

        systemd_count = 0
        docker_count = 0
        systemd_active = 0
        docker_active = 0

        for item in services:
            name = item["name"]
            row = self.rows.get(name)
            svc_type = item.get("type", "systemd")

            if row is None:
                if svc_type == "docker":
                    row = DockerServiceRow(self, name)
                    self.docker_group.add(row)
                else:
                    row = ServiceRow(self, name)
                    self.systemd_group.add(row)
                self.rows[name] = row
            elif svc_type == "docker" and not isinstance(row, DockerServiceRow):
                self.systemd_group.remove(row)
                row = DockerServiceRow(self, name)
                self.docker_group.add(row)
                self.rows[name] = row
            elif svc_type == "systemd" and isinstance(row, DockerServiceRow):
                self.docker_group.remove(row)
                row = ServiceRow(self, name)
                self.systemd_group.add(row)
                self.rows[name] = row

            row.update(item["conf"], item["status"])

            if svc_type == "docker":
                docker_count += 1
                if item["status"].get("active"):
                    docker_active += 1
            else:
                systemd_count += 1
                if item["status"].get("active"):
                    systemd_active += 1

        # Hide empty groups
        self.systemd_group.set_visible(systemd_count > 0)
        self.docker_group.set_visible(docker_count > 0)

        total_active = systemd_active + docker_active
        total_count = systemd_count + docker_count
        if total_count:
            self.summary.set_text(f"Sistema: {systemd_active}/{systemd_count}  |  Docker: {docker_active}/{docker_count}")
            self.stack.set_visible_child_name("list")
        else:
            self.summary.set_text("nenhum serviço")
            self.stack.set_visible_child_name("empty")
        self._filter()

    def _filter(self) -> None:
        query = self.search_entry.get_text().strip().lower()
        for name, row in self.rows.items():
            desc = (row.conf.get("DESC") or "").lower()
            row.set_visible(not query or query in name.lower() or query in desc)

    def run_action(self, name: str, op: str) -> None:
        def worker():
            # Detect if Docker service
            is_docker = False
            try:
                services = backend.list_all_services(with_status=False)
                for svc in services:
                    if svc["name"] == name and svc.get("type") == "docker":
                        is_docker = True
                        break
            except Exception:
                pass

            if is_docker:
                from docker_backend import container_action
                success, message = container_action(name, op)
            else:
                success, message = backend.action(name, op)

            GLib.idle_add(self._after_action, name, op, success, message)

        toast(self, f"{op} {name}…")
        threading.Thread(target=worker, daemon=True).start()

    def _after_action(self, name: str, op: str, success: bool, message: str) -> bool:
        if success:
            toast(self, f"{name}: {op} ok")
        else:
            toast(self, f"{name}: falha em {op} — {message}")
        GLib.timeout_add(500, lambda: (self.refresh(), False)[1])
        return False

    def run_all(self, op: str) -> None:
        def worker():
            results = backend.action_all(op)
            failures = [name for name, ok, _ in results if not ok]
            GLib.idle_add(self._after_all, op, failures)

        toast(self, f"{op} em todos os serviços…")
        threading.Thread(target=worker, daemon=True).start()

    def _after_all(self, op: str, failures: list[str]) -> bool:
        if failures:
            toast(self, f"{op}: falhou em {', '.join(failures)}")
        else:
            toast(self, f"{op}: concluído")
        self.refresh()
        return False

    def set_boot(self, name: str, enabled: bool) -> None:
        def worker():
            success, message = backend.set_autostart(name, enabled)
            GLib.idle_add(self._after_boot, name, enabled, success, message)

        threading.Thread(target=worker, daemon=True).start()

    def toggle_boot(self, name: str) -> None:
        current = backend.is_truthy(backend.effective(name).get("AUTOSTART", "1"))
        self.set_boot(name, not current)

    def _after_boot(self, name: str, enabled: bool, success: bool, message: str) -> bool:
        if success:
            toast(self, f"{name}: no boot = {'sim' if enabled else 'não'}")
        else:
            toast(self, f"{name}: falha ao alterar boot — {message}")
        self.refresh()
        return False

    def open_dialog(self, name: str | None = None) -> None:
        ServiceDialog(self, name).present(self)

    def open_logs(self, name: str) -> None:
        LogsWindow(self, name).present()

    def confirm_remove(self, name: str) -> None:
        dialog = Adw.AlertDialog(
            heading=f"Remover '{name}'?",
            body="O serviço será parado, desabilitado no boot e seus arquivos de configuração apagados.",
        )
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("remove", "Remover")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def on_response(_dialog, response):
            if response != "remove":
                return
            try:
                backend.remove(name)
            except Exception as exc:  # noqa: BLE001
                toast(self, f"Erro ao remover: {exc}")
                return
            toast(self, f"{name}: removido")
            self.refresh()

        dialog.connect("response", on_response)
        dialog.present(self)

    def cycle_docker_restart_policy(self, name: str) -> None:
        row = self.rows.get(name)
        if not row or not hasattr(row, 'conf'):
            return
        current = row.conf.get("RESTART_POLICY", "no")
        next_policy = {"no": "always", "always": "unless-stopped", "unless-stopped": "no"}[current]

        def worker():
            from docker_backend import set_restart_policy
            success, msg = set_restart_policy(name, next_policy)
            GLib.idle_add(self._after_docker_policy, name, next_policy, success, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _after_docker_policy(self, name: str, policy: str, success: bool, msg: str) -> bool:
        if success:
            toast(self, f"{name}: restart policy = {policy}")
        else:
            toast(self, f"{name}: falha ao alterar policy — {msg}")
        self.refresh()
        return False

    def confirm_remove_docker(self, name: str) -> None:
        dialog = Adw.AlertDialog(
            heading=f"Remover container '{name}'?",
            body="O container será parado e removido. Volumes não são removidos automaticamente.",
        )
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("remove", "Remover")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def on_response(_dialog, response):
            if response != "remove":
                return
            def worker():
                from docker_backend import container_action
                ok, msg = container_action(name, "remove")
                GLib.idle_add(lambda: (toast(self, msg), self.refresh())[1] if ok else toast(self, f"Falha: {msg}"))
            threading.Thread(target=worker, daemon=True).start()

        dialog.connect("response", on_response)
        dialog.present(self)

    def open_inspect(self, name: str) -> None:
        def worker():
            from docker_backend import inspect_container
            data = inspect_container(name)
            GLib.idle_add(self._show_inspect_dialog, name, data)
        threading.Thread(target=worker, daemon=True).start()

    def _show_inspect_dialog(self, name: str, data: dict) -> None:
        import json
        dialog = Adw.Dialog()
        dialog.set_content_width(700)
        dialog.set_content_height(500)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        title = Gtk.Label(label=f"Inspecionar: {name}")
        title.add_css_class("title-4")
        header.set_title_widget(title)
        close_btn = Gtk.Button(label="Fechar")
        close_btn.connect("clicked", lambda *_: dialog.close())
        header.pack_end(close_btn)
        toolbar.add_top_bar(header)

        view = Gtk.TextView(editable=False, monospace=True)
        view.set_left_margin(10)
        view.set_right_margin(10)
        buffer = view.get_buffer()
        buffer.set_text(json.dumps(data, indent=2, ensure_ascii=False))
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(view)
        scroller.set_vexpand(True)
        toolbar.set_content(scroller)

        dialog.set_child(toolbar)
        dialog.present(self)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        Gtk.Window.set_default_icon_name("services-manager")
        if os.path.exists(CSS_FILE):
            provider = Gtk.CssProvider()
            try:
                provider.load_from_path(CSS_FILE)
                display = Gdk.Display.get_default()
                if display:
                    Gtk.StyleContext.add_provider_for_display(
                        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                    )
            except GLib.Error as exc:
                print(f"aviso: CSS não carregado: {exc}", file=sys.stderr)
        self._register_actions()

    def _register_actions(self) -> None:
        for name, handler in (
            ("add", lambda *_: self._with_window(lambda w: w.open_dialog())),
            ("refresh-all", lambda *_: self._with_window(lambda w: w.refresh())),
            ("open-config", lambda *_: self._open_config()),
            ("about", lambda *_: self._about()),
            ("start-all", lambda *_: self._with_window(lambda w: w.run_all("start"))),
            ("stop-all", lambda *_: self._with_window(lambda w: w.run_all("stop"))),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

        named = {
            "start": lambda w, n: w.run_action(n, "start"),
            "stop": lambda w, n: w.run_action(n, "stop"),
            "restart": lambda w, n: w.run_action(n, "restart"),
            "logs": lambda w, n: w.open_logs(n),
            "edit": lambda w, n: w.open_dialog(n),
            "remove": lambda w, n: w.confirm_remove(n),
            "toggle-boot": lambda w, n: w.toggle_boot(n),
            "docker-restart-policy": lambda w, n: w.cycle_docker_restart_policy(n),
            "remove-docker": lambda w, n: w.confirm_remove_docker(n),
            "inspect": lambda w, n: w.open_inspect(n),
        }
        for name, handler in named.items():
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect(
                "activate",
                lambda _a, param, h=handler: self._with_window(
                    lambda w: h(w, param.get_string())
                ),
            )
            self.add_action(action)

        self.set_accels_for_action("app.add", ["<Control>n"])
        self.set_accels_for_action("app.refresh-all", ["<Control>r"])
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def _with_window(self, callback) -> None:
        self.do_activate()
        if self.window is not None:
            callback(self.window)

    def _open_config(self) -> None:
        uri = backend.CONF_DIR.as_uri()
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as exc:
            toast(self.window, f"Não foi possível abrir: {exc}")

    def _about(self) -> None:
        dialog = Adw.AboutDialog(
            application_name="Gerenciador de Serviços",
            application_icon="services-manager",
            developer_name="Servidor Lucas",
            version="1.0",
            comments="Gerencia serviços systemd de usuário a partir de ~/.config/services.",
        )
        dialog.present(self.window)

    def do_activate(self) -> None:
        if self.window is None:
            self.window = MainWindow(self)
            self.window.connect("destroy", self._on_window_destroy)
        self.window.present()

    def _on_window_destroy(self, *_args) -> None:
        self.window = None


def run(argv: list[str]) -> int:
    GLib.set_prgname("services-manager")
    app = App()
    return app.run(argv)


if __name__ == "__main__":
    sys.exit(run(sys.argv))
