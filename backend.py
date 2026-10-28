#!/usr/bin/python3
"""Backend compartilhado do gerenciador de serviços.

Fonte de verdade:
  ~/.config/services/*.conf      definição de cada serviço (AUTOSTART=1/0)
  ~/.config/systemd/user/*.service  units gerados a partir dos confs

Sem dependências externas: apenas stdlib + systemctl/journalctl.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
import paths  # noqa: E402

APP_DIR = paths.APP_ROOT
paths.ensure_conf_dir()
CONF_DIR = paths.CONF_DIR
UNIT_DIR = Path(os.environ.get("SVC_UNIT_DIR", HOME / ".config/systemd/user"))
RUN_DIR = paths.RUN_DIR
DEFAULTS_FILE = CONF_DIR / "svc.conf"
BIN_DIR = HOME / ".local" / "bin"
ICON_DIR = HOME / ".local/share/icons/hicolor/scalable/apps"
APP_DESKTOP_DIR = HOME / ".local/share/applications"
AUTOSTART_DIR = HOME / ".config/autostart"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
UNIT_SUFFIX = ".service"

DEFAULT_CONF = {
    "PATH_EXTRA": "",
    "WORKDIR": str(HOME),
    "RESTART": "always",
    "RESTART_SEC": "5",
    "TIMEOUT_STOP": "30",
}

SERVICE_KEYS = [
    "DESC",
    "CMD",
    "WORKDIR",
    "PORT",
    "HEALTH_URL",
    "AUTOSTART",
    "AFTER",
    "WANTS",
    "TYPE",
    "ENV_FILE",
]

TRUTHY = {"1", "true", "yes", "on", "sim", "y"}


# ---------------------------------------------------------------- utilidades


def is_truthy(value) -> bool:
    return str(value).strip().lower() in TRUTHY


def user_name() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "servidor-lucas"


def _run(cmd, timeout=60):
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        cp = subprocess.CompletedProcess(cmd, 1, "", str(exc))
        return cp


def _sd_quote(value: str) -> str:
    if not value:
        return '""'
    if re.search(r"[\s\"'\\]", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def ensure_dirs() -> None:
    paths.ensure_conf_dir()
    for path in (CONF_DIR, RUN_DIR, UNIT_DIR, BIN_DIR, ICON_DIR, APP_DESKTOP_DIR, AUTOSTART_DIR):
        path.mkdir(parents=True, exist_ok=True)


def conf_path(name: str) -> Path:
    return CONF_DIR / f"{name}.conf"


def unit_path(name: str) -> Path:
    return UNIT_DIR / f"{name}{UNIT_SUFFIX}"


def run_path(name: str) -> Path:
    return RUN_DIR / f"{name}.sh"


def env_path(name: str) -> Path:
    return CONF_DIR / f"{name}.env"


def _write_if_changed(path: Path, text: str, mode: int | None = None) -> bool:
    old = None
    if path.exists():
        try:
            old = path.read_text()
        except OSError:
            old = None
    if old == text:
        if mode is not None:
            try:
                path.chmod(mode)
            except OSError:
                pass
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode is not None:
        try:
            path.chmod(mode)
        except OSError:
            pass
    return True


# ------------------------------------------------------------------ configs


def parse_conf_text(text: str) -> dict:
    data = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip().upper()] = value.strip()
    return data


def load_defaults() -> dict:
    data = dict(DEFAULT_CONF)
    if DEFAULTS_FILE.exists():
        data.update(parse_conf_text(DEFAULTS_FILE.read_text()))
    if not data.get("WORKDIR"):
        data["WORKDIR"] = str(HOME)
    return data


def read_conf(name: str) -> dict:
    path = conf_path(name)
    if not path.exists():
        return {}
    return parse_conf_text(path.read_text())


def read_overlay(name: str) -> dict:
    path = paths.overlay_path(name)
    if not path.exists():
        return {}
    return parse_conf_text(path.read_text())


def merged_raw(name: str) -> dict:
    data = read_conf(name)
    data.update(read_overlay(name))
    return data


def effective(name: str) -> dict:
    data = load_defaults()
    data.update(merged_raw(name))
    data.setdefault("AUTOSTART", "1")
    data = {key: paths.expand_value(str(value)) for key, value in data.items()}
    if not data.get("WORKDIR"):
        data["WORKDIR"] = str(HOME)
    return data


def write_conf(name: str, data: dict) -> None:
    if not NAME_RE.match(name):
        raise ValueError(f"nome inválido: {name!r} (use letras minúsculas, números e hífen)")
    ordered = []
    for key in SERVICE_KEYS:
        if key in data:
            ordered.append(key)
    for key in sorted(data):
        if key not in ordered:
            ordered.append(key)
    lines = [
        f"# Editado pelo gerenciador de servicos ({APP_DIR / 'svc'})",
        f"# servico: {name}",
        f"# caminhos relativos: use ${{APP_ROOT}} (raiz do app) ou ${{HOME}}",
    ]
    for key in ordered:
        value = str(data.get(key, ""))
        if key != "AUTOSTART" and value == "":
            continue
        lines.append(f"{key}={value}")
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    conf_path(name).write_text("\n".join(lines) + "\n")


def list_names() -> list[str]:
    if not CONF_DIR.exists():
        return []
    return sorted(
        p.stem
        for p in CONF_DIR.glob("*.conf")
        if p.name != DEFAULTS_FILE.name and NAME_RE.match(p.stem)
    )


# --------------------------------------------------------------- render/unit


def render_run_script(name: str, conf: dict) -> str:
    workdir = conf.get("WORKDIR") or str(HOME)
    cmd = conf.get("CMD", "").strip()
    return (
        "#!/usr/bin/env bash\n"
        f"# gerado automaticamente para o servico {name}; edite {conf_path(name)}\n"
        "set -euo pipefail\n"
        f"cd -- {shlex.quote(workdir)}\n"
        f"exec {cmd}\n"
    )


def render_unit(name: str, conf: dict) -> str:
    desc = conf.get("DESC") or name
    after = conf.get("AFTER") or "network-online.target"
    wants = conf.get("WANTS") or "network-online.target"
    workdir = conf.get("WORKDIR") or str(HOME)
    path_extra = conf.get("PATH_EXTRA") or ""
    path = (path_extra + ":" if path_extra else "") + "/usr/local/bin:/usr/bin:/bin"
    typ = (conf.get("TYPE") or "simple").strip().lower()
    restart = conf.get("RESTART") or "always"
    restart_sec = conf.get("RESTART_SEC") or "5"
    timeout_stop = conf.get("TIMEOUT_STOP") or "30"

    lines = [
        "# GERADO pelo gerenciador de servicos - nao edite a mao",
        "[Unit]",
        f"Description={desc}",
        f"After={after}",
        f"Wants={wants}",
        "StartLimitIntervalSec=300",
        "StartLimitBurst=10",
        "",
        "[Service]",
        f"Type={typ}",
        "Environment=HOME=%h",
        f"Environment=PATH={path}",
        f"EnvironmentFile=-{_sd_quote(str(env_path(name)))}",
        f"WorkingDirectory={_sd_quote(workdir)}",
        f"ExecStart=/bin/bash {_sd_quote(str(run_path(name)))}",
    ]
    if typ == "oneshot":
        lines += ["RemainAfterExit=yes", "Restart=no"]
    else:
        lines += [f"Restart={restart}", f"RestartSec={restart_sec}"]
    lines += [
        f"TimeoutStopSec={timeout_stop}",
        "KillMode=mixed",
        "OOMPolicy=continue",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- systemctl

_state_cache = {"at": 0.0, "units": {}, "files": {}}


def invalidate_cache() -> None:
    _state_cache["at"] = 0.0


def _state_maps(force: bool = False):
    if not force and time.time() - _state_cache["at"] < 0.8:
        return _state_cache["units"], _state_cache["files"]
    units = {}
    files = {}
    r = _run(["systemctl", "--user", "list-units", "--type=service", "--all", "--output=json", "--no-pager"])
    if r.returncode == 0 and r.stdout.strip():
        try:
            for item in json.loads(r.stdout):
                units[item.get("unit")] = item
        except (ValueError, TypeError):
            units = {}
    r = _run(["systemctl", "--user", "list-unit-files", "--type=service", "--output=json", "--no-pager"])
    if r.returncode == 0 and r.stdout.strip():
        try:
            for item in json.loads(r.stdout):
                files[item.get("unit_file")] = item.get("state")
        except (ValueError, TypeError):
            files = {}
    _state_cache.update({"at": time.time(), "units": units, "files": files})
    return units, files


def _monotonic_us() -> float:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as fh:
            return float(fh.read().split()[0]) * 1_000_000
    except (OSError, ValueError, IndexError):
        return 0.0


def _details(unit_names: list[str]) -> dict:
    if not unit_names:
        return {}
    r = _run(
        ["systemctl", "--user", "show", *unit_names,
         "-p", "Id", "-p", "MainPID", "-p", "ActiveEnterTimestampMonotonic",
         "--no-pager"]
    )
    out = {}
    for block in (r.stdout or "").strip().split("\n\n"):
        data = {}
        for line in block.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                data[key] = value
        ident = data.get("Id")
        if ident:
            out[ident] = data
    return out


def human_uptime(seconds: float) -> str:
    if seconds is None or seconds <= 0:
        return ""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}min"
    if minutes:
        return f"{minutes}min"
    return f"{secs}s"


def status(name: str) -> dict:
    unit = f"{name}{UNIT_SUFFIX}"
    units, files = _state_maps()
    item = units.get(unit, {})
    detail = {}
    if item:
        detail = _details([unit]).get(unit, {})
    active_state = item.get("active") or "inactive"
    sub_state = item.get("sub") or "dead"
    active = active_state in ("active", "activating", "reloading")
    pid = (detail.get("MainPID") or "").strip()
    if pid in ("", "0"):
        pid = ""
    uptime_seconds = 0.0
    enter = detail.get("ActiveEnterTimestampMonotonic")
    if active and enter and enter.isdigit():
        uptime_seconds = max(0.0, (_monotonic_us() - int(enter)) / 1_000_000)
    return {
        "name": name,
        "unit": unit,
        "active": active,
        "active_state": active_state,
        "sub_state": sub_state,
        "state": f"{active_state}/{sub_state}",
        "failed": sub_state == "failed" or active_state == "failed",
        "unit_file_state": files.get(unit, "unknown"),
        "main_pid": pid,
        "uptime_seconds": uptime_seconds,
        "uptime": human_uptime(uptime_seconds),
    }


def is_active(name: str) -> bool:
    return status(name)["active"]


def daemon_reload() -> None:
    _run(["systemctl", "--user", "daemon-reload"])
    invalidate_cache()


def _sync_boot(name: str, conf: dict) -> tuple[bool, str]:
    unit = f"{name}{UNIT_SUFFIX}"
    want = is_truthy(conf.get("AUTOSTART", "1"))
    _, files = _state_maps(force=True)
    state = files.get(unit, "unknown")
    if want and state not in ("enabled", "enabled-runtime", "static", "alias"):
        r = _run(["systemctl", "--user", "enable", unit])
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    if not want and state in ("enabled", "enabled-runtime"):
        r = _run(["systemctl", "--user", "disable", unit])
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    return True, ""


def apply(name: str, start_if_inactive: bool = True) -> dict:
    if not NAME_RE.match(name):
        raise ValueError(f"nome inválido: {name!r}")
    if not conf_path(name).exists():
        raise FileNotFoundError(f"configuração não encontrada: {conf_path(name)}")
    conf = effective(name)
    if not (conf.get("CMD") or "").strip():
        raise ValueError(f"serviço {name!r} sem CMD definido")

    ensure_dirs()
    changed = _write_if_changed(run_path(name), render_run_script(name, conf), 0o700)
    changed = _write_if_changed(unit_path(name), render_unit(name, conf), 0o644) or changed

    if changed:
        daemon_reload()

    enabled_ok, enabled_msg = _sync_boot(name, conf)

    started = False
    start_msg = ""
    st = status(name)
    if st["active"]:
        if changed:
            ok, msg = action(name, "restart")
            started = ok
            start_msg = msg
    elif start_if_inactive:
        ok, msg = action(name, "start")
        started = ok
        start_msg = msg
    invalidate_cache()
    return {
        "name": name,
        "changed": changed,
        "enabled_ok": enabled_ok,
        "enabled_msg": enabled_msg,
        "started": started,
        "start_msg": start_msg,
        "unit": str(unit_path(name)),
    }


def set_autostart(name: str, enabled: bool) -> tuple[bool, str]:
    raw = read_conf(name)
    raw["AUTOSTART"] = "1" if enabled else "0"
    write_conf(name, raw)
    if not unit_path(name).exists():
        apply(name, start_if_inactive=False)
        return True, ""
    return _sync_boot(name, effective(name))


def action(name: str, op: str) -> tuple[bool, str]:
    if op not in ("start", "stop", "restart"):
        raise ValueError(f"operação inválida: {op}")
    r = _run(["systemctl", "--user", op, f"{name}{UNIT_SUFFIX}"], timeout=90)
    invalidate_cache()
    output = (r.stdout + r.stderr).strip()
    return r.returncode == 0, output


def action_all(op: str) -> list[tuple[str, bool, str]]:
    results = []
    for name in list_names():
        ok, msg = action(name, op)
        results.append((name, ok, msg))
    return results


def remove(name: str) -> None:
    action(name, "stop")
    _run(["systemctl", "--user", "disable", f"{name}{UNIT_SUFFIX}"])
    for path in (unit_path(name), run_path(name), conf_path(name)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    daemon_reload()


# ------------------------------------------------------------------- health


def health(url: str) -> tuple[bool, int | None]:
    """Considera saudável qualquer resposta HTTP (inclusive redirect) ou TCP aberto."""
    if not url:
        return False, None
    request = urllib.request.Request(url, headers={"User-Agent": "services-manager"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return True, getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        return True, exc.code
    except Exception:
        pass
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=2):
            return True, None
    except OSError:
        return False, None


# --------------------------------------------------------------- validação


def validate(name: str, data: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    defaults = load_defaults()

    if not name or not NAME_RE.match(name):
        errors.append("Nome inválido: use letras minúsculas, números e hífen (ex.: 9router).")

    cmd = (data.get("CMD") or "").strip()
    if not cmd:
        errors.append("Comando (CMD) é obrigatório.")
    else:
        try:
            first = shlex.split(cmd)[0]
        except ValueError as exc:
            first = ""
            errors.append(f"Comando inválido: {exc}")
        if first:
            if "/" in first:
                if not Path(first).exists():
                    errors.append(f"Executável não encontrado: {first}")
            else:
                search_path = (defaults.get("PATH_EXTRA") or "") + ":" + os.environ.get("PATH", os.defpath)
                if not shutil.which(first, path=search_path):
                    errors.append(f"Executável '{first}' não está no PATH. Use o caminho absoluto.")

    workdir = (data.get("WORKDIR") or "").strip()
    if workdir and not Path(os.path.expanduser(workdir)).is_dir():
        errors.append(f"WORKDIR não existe: {workdir}")

    port = (data.get("PORT") or "").strip()
    if port:
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            errors.append(f"Porta inválida: {port}")
        else:
            for other in list_names():
                if other == name:
                    continue
                if (merged_raw(other).get("PORT") or "").strip() == port:
                    warnings.append(f"Porta {port} também está no serviço '{other}'.")

    typ = (data.get("TYPE") or "simple").strip().lower()
    if typ not in ("simple", "exec", "forking", "oneshot", "notify", "idle"):
        errors.append(f"TYPE inválido: {typ}")

    return errors, warnings


# -------------------------------------------------------------------- setup


def _detect_node_bin() -> str:
    node = shutil.which("node")
    if node:
        return str(Path(node).parent)
    candidates = sorted((HOME / ".nvm/versions/node").glob("*/bin"), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(candidates[0]) if candidates else ""


def _detect_9router() -> str:
    found = shutil.which("9router")
    if found:
        return found
    node_bin = _detect_node_bin()
    if node_bin:
        candidate = Path(node_bin) / "9router"
        if candidate.exists():
            return str(candidate)
    return ""


def linger_status() -> str:
    r = _run(["loginctl", "show-user", user_name(), "-p", "Linger"])
    return r.stdout.strip().replace("Linger=", "") or "unknown"


def enable_linger() -> tuple[bool, str]:
    r = _run(["loginctl", "enable-linger", user_name()])
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def bootstrap() -> dict:
    ensure_dirs()
    report = {"defaults_created": False, "nine_created": False, "linger": linger_status(), "linger_msg": ""}

    if not DEFAULTS_FILE.exists():
        node_bin = _detect_node_bin()
        bun_bin = HOME / ".bun/bin"
        extras = [p for p in (node_bin, str(bun_bin) if bun_bin.is_dir() else "") if p]
        DEFAULTS_FILE.write_text(
            "# Defaults globais do gerenciador de servicos\n"
            f"PATH_EXTRA={':'.join(extras)}\n"
            f"WORKDIR={HOME}\n"
            "RESTART=always\n"
            "RESTART_SEC=5\n"
            "TIMEOUT_STOP=30\n"
        )
        report["defaults_created"] = True

    if not conf_path("9router").exists():
        cmd = _detect_9router()
        if cmd:
            write_conf("9router", {
                "DESC": "9Router AI Router",
                "CMD": f"{cmd} --host 127.0.0.1 --port 20128 --no-browser --skip-update --log",
                "WORKDIR": str(HOME),
                "PORT": "20128",
                "HEALTH_URL": "http://127.0.0.1:20128/dashboard",
                "AUTOSTART": "1",
            })
            report["nine_created"] = True

    daemon_reload()

    if report["linger"] != "yes":
        ok, msg = enable_linger()
        report["linger_msg"] = msg
        report["linger"] = linger_status()

    return report


# ---------------------------------------------------------------- desktop


ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">
  <rect x="10" y="14" width="108" height="76" rx="12" fill="#3584e4"/>
  <rect x="22" y="26" width="84" height="16" rx="5" fill="#ffffff" opacity="0.92"/>
  <rect x="22" y="48" width="84" height="16" rx="5" fill="#ffffff" opacity="0.72"/>
  <circle cx="34" cy="34" r="4" fill="#3584e4"/>
  <circle cx="34" cy="56" r="4" fill="#3584e4"/>
  <circle cx="98" cy="96" r="24" fill="#2ec27e"/>
  <g fill="#ffffff" transform="translate(98 96)">
    <circle r="7"/>
    <g>
      <rect x="-3" y="-19" width="6" height="8" rx="2"/>
      <rect x="-3" y="-19" width="6" height="8" rx="2" transform="rotate(60)"/>
      <rect x="-3" y="-19" width="6" height="8" rx="2" transform="rotate(120)"/>
      <rect x="-3" y="-19" width="6" height="8" rx="2" transform="rotate(180)"/>
      <rect x="-3" y="-19" width="6" height="8" rx="2" transform="rotate(240)"/>
      <rect x="-3" y="-19" width="6" height="8" rx="2" transform="rotate(300)"/>
    </g>
  </g>
</svg>
"""


def _exec_quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _desktop_entry(name: str, desc: str, exec_line: str, icon: str = "services-manager", actions: str = "", wm_class: str = "services-manager") -> str:
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        f"Name={name}",
        f"Comment={desc}",
        f"Exec={exec_line}",
        f"Icon={icon}",
        "Terminal=false",
        "Categories=System;",
        "StartupNotify=true",
        f"StartupWMClass={wm_class}",
    ]
    if actions:
        lines.append(f"Actions={actions}")
    return "\n".join(lines) + "\n"


def _launcher() -> str:
    candidates = [
        APP_DIR / ("run.bat" if os.name == "nt" else "run.sh"),
        BIN_DIR / "services-manager",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return str(candidates[-1])


def install_desktop(add_favorite: bool = True) -> dict:
    ensure_dirs()
    result = {"files": [], "favorite": False, "favorite_msg": ""}

    icon_file = ICON_DIR / "services-manager.svg"
    icon_file.write_text(ICON_SVG)
    result["files"].append(str(icon_file))

    main_entry = _desktop_entry(
        "Gerenciador de Serviços",
        "Abrir o painel e iniciar serviços",
        _exec_quote(_launcher()),
        actions="StartAll;Start9Router;",
    )
    main_entry += (
        "\n[Desktop Action StartAll]\n"
        "Name=Iniciar todos os serviços\n"
        f"Exec={_exec_quote(_launcher())} --start all\n"
        "\n[Desktop Action Start9Router]\n"
        "Name=Iniciar 9Router\n"
        f"Exec={_exec_quote(_launcher())} --start 9router\n"
    )
    app_file = APP_DESKTOP_DIR / "services-manager.desktop"
    app_file.write_text(main_entry)
    app_file.chmod(0o755)
    result["files"].append(str(app_file))

    autostart = AUTOSTART_DIR / "services-manager.desktop"
    autostart.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Gerenciador de Serviços\n"
        "Comment=Abre o painel de serviços ao entrar na sessão\n"
        f"Exec={_exec_quote(_launcher())}\n"
        "Icon=services-manager\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-GNOME-Autostart-Delay=4\n"
    )
    autostart.chmod(0o755)
    result["files"].append(str(autostart))

    desktop_dir = _desktop_dir()
    if desktop_dir:
        shortcut = desktop_dir / "Gerenciador de Serviços.desktop"
        shortcut.write_text(main_entry)
        shortcut.chmod(0o755)
        _run(["gio", "set", str(shortcut), "metadata::trusted", "true"])
        result["files"].append(str(shortcut))

    _run(["gtk-update-icon-cache", "-f", "-t", str(ICON_DIR.parent.parent)])

    if add_favorite:
        ok, msg = _add_favorite()
        result["favorite"] = ok
        result["favorite_msg"] = msg

    return result


EXT_UUID = "ding@rastersoft.com"
EXT_BASE = HOME / ".local/share/gnome-shell/extensions"
USER_SCHEMA_DIR = HOME / ".local/share/glib-2.0/schemas"
D2P_PACKAGE = "gnome-shell-extension-dash-to-panel"
D2P_UUID = "dash-to-panel@jderose9.github.com"
D2P_SCHEMA = "org.gnome.shell.extensions.dash-to-panel"


def icons_extension_installed() -> bool:
    return (EXT_BASE / EXT_UUID / "metadata.json").exists()


def _gs_enabled_extensions() -> list[str]:
    current = _run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"])
    if current.returncode != 0:
        return []
    text = re.sub(r"^@\w+\s+", "", current.stdout.strip())
    if not text.startswith("["):
        return []
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _gs_set_enabled_extensions(items: list[str]):
    payload = "[" + ", ".join("'" + str(v).replace("'", "") + "'" for v in items) + "]"
    return _run(["gsettings", "set", "org.gnome.shell", "enabled-extensions", payload])


def enable_extension(uuid: str) -> tuple[bool, str]:
    if shutil.which("gnome-extensions"):
        r = _run(["gnome-extensions", "enable", uuid])
        if r.returncode == 0:
            return True, "habilitada"
    # O shell em execução só enxerga a extensão após relogar; grava direto no gsettings.
    items = _gs_enabled_extensions()
    if uuid in items:
        return True, "já habilitada (efetiva após relogar)"
    items.append(uuid)
    r = _gs_set_enabled_extensions(items)
    if r.returncode == 0:
        return True, "habilitada via gsettings (efetiva após relogar)"
    return False, (r.stdout + r.stderr).strip() or "falha ao habilitar"


def disable_extension(uuid: str) -> tuple[bool, str]:
    if shutil.which("gnome-extensions"):
        _run(["gnome-extensions", "disable", uuid])
    items = [i for i in _gs_enabled_extensions() if i != uuid]
    r = _gs_set_enabled_extensions(items)
    if r.returncode == 0:
        return True, "desabilitada (efetiva após relogar)"
    return False, (r.stdout + r.stderr).strip() or "falha ao desabilitar"


def enable_icons_extension() -> tuple[bool, str]:
    if not icons_extension_installed():
        return False, "extensão não instalada"
    return enable_extension(EXT_UUID)


def install_user_extension(package: str, uuid: str | None = None) -> tuple[bool, str, str]:
    """Instala uma extensão do GNOME a partir de um pacote .deb no perfil do usuário (sem sudo)."""
    if uuid and (EXT_BASE / uuid / "metadata.json").exists():
        return True, "extensão já instalada no perfil do usuário", uuid

    import tempfile

    with tempfile.TemporaryDirectory(prefix="svc-ext-") as tmp:
        download = subprocess.run(
            ["apt-get", "download", package],
            cwd=tmp, capture_output=True, text=True,
        )
        if download.returncode != 0:
            return False, (download.stderr or "apt-get download falhou").strip(), ""
        debs = list(Path(tmp).glob("*.deb"))
        if not debs:
            return False, "pacote não foi baixado", ""
        extracted = Path(tmp) / "extract"
        extracted.mkdir()
        unpack = subprocess.run(
            ["dpkg-deb", "-x", str(debs[0]), str(extracted)],
            capture_output=True, text=True,
        )
        if unpack.returncode != 0:
            return False, (unpack.stderr or "dpkg-deb falhou").strip(), ""

        root = extracted / "usr/share/gnome-shell/extensions"
        candidates = []
        if root.is_dir():
            candidates = sorted(p for p in root.iterdir() if (p / "metadata.json").is_file())
        source = None
        if uuid:
            source = next((p for p in candidates if p.name == uuid), None)
        if source is None and candidates:
            source = candidates[0]
        if source is None:
            return False, "extensão não encontrada dentro do pacote", ""

        try:
            meta = json.loads((source / "metadata.json").read_text())
        except (OSError, ValueError):
            return False, "metadata.json inválido no pacote", ""
        real_uuid = str(meta.get("uuid") or source.name)

        EXT_BASE.mkdir(parents=True, exist_ok=True)
        target = EXT_BASE / real_uuid
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)

        USER_SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
        copied = False
        for schema_source in (extracted / "usr/share/glib-2.0/schemas", target / "schemas"):
            if not schema_source.is_dir():
                continue
            for schema in schema_source.glob("*.xml"):
                shutil.copy2(schema, USER_SCHEMA_DIR / schema.name)
                copied = True
        if copied:
            compile_schemas = subprocess.run(
                ["glib-compile-schemas", str(USER_SCHEMA_DIR)],
                capture_output=True, text=True,
            )
            if compile_schemas.returncode != 0:
                return False, (compile_schemas.stderr or "glib-compile-schemas falhou").strip(), real_uuid

    return True, f"extensão instalada no perfil do usuário ({real_uuid})", real_uuid


def install_icons_extension() -> tuple[bool, str]:
    ok, msg, _ = install_user_extension("gnome-shell-extension-desktop-icons-ng", EXT_UUID)
    return ok, msg


def taskbar_panel() -> dict:
    """Instala/habilita o Dash to Panel e posiciona a barra embaixo."""
    report = {"installed": False, "enabled": False, "position": None, "messages": []}
    ok, msg, uuid = install_user_extension(D2P_PACKAGE, D2P_UUID)
    report["installed"] = ok
    report["messages"].append(msg)
    if not ok:
        return report
    uuid = uuid or D2P_UUID

    enabled, en_msg = enable_extension(uuid)
    report["enabled"] = enabled
    report["messages"].append(en_msg)

    current = _run(["gsettings", "get", D2P_SCHEMA, "panel-position"])
    if current.returncode == 0:
        setr = _run(["gsettings", "set", D2P_SCHEMA, "panel-position", "'BOTTOM'"])
        if setr.returncode != 0:
            setr = _run(["gsettings", "set", D2P_SCHEMA, "panel-position", "BOTTOM"])
        if setr.returncode == 0:
            report["position"] = "BOTTOM"
        else:
            report["messages"].append((setr.stderr or "falha ao posicionar a barra").strip())
    else:
        report["messages"].append("schema do Dash to Panel ainda não disponível")

    fav_ok, fav_msg = _add_favorite()
    report["messages"].append(fav_msg)
    return report


def taskbar_off() -> tuple[bool, str]:
    return disable_extension(D2P_UUID)


def _desktop_dir() -> Path | None:
    r = _run(["xdg-user-dir", "DESKTOP"])
    if r.returncode == 0 and r.stdout.strip():
        path = Path(r.stdout.strip())
        if path.is_dir():
            return path
    for candidate in (HOME / "Área de trabalho", HOME / "Desktop"):
        if candidate.is_dir():
            return candidate
    return None


def _add_favorite() -> tuple[bool, str]:
    current = _run(["gsettings", "get", "org.gnome.shell", "favorite-apps"])
    if current.returncode != 0:
        return False, "gsettings indisponível"
    text = re.sub(r"^@\w+\s+", "", current.stdout.strip())
    apps = []
    if text.startswith("["):
        try:
            value = ast.literal_eval(text)
            if isinstance(value, list):
                apps = [str(v) for v in value]
        except (ValueError, SyntaxError, TypeError):
            apps = []
    if "services-manager.desktop" in apps:
        return True, "já fixado no Dock"
    apps.append("services-manager.desktop")
    payload = "[" + ", ".join("'" + str(a).replace("'", "") + "'" for a in apps) + "]"
    r = _run(["gsettings", "set", "org.gnome.shell", "favorite-apps", payload])
    return r.returncode == 0, (r.stdout + r.stderr).strip() or "fixado no Dock"


# ---------------------------------------------------------------- serviços


def list_services(with_status: bool = True) -> list[dict]:
    services = []
    for name in list_names():
        conf = effective(name)
        st = status(name) if with_status else {}
        services.append({"name": name, "type": "systemd", "conf": conf, "status": st})
    return services


def journal_cmd(name: str, follow: bool = False, lines: int = 200) -> list[str]:
    cmd = ["journalctl", "--user", "-u", f"{name}{UNIT_SUFFIX}"]
    if follow:
        cmd.append("-f")
    cmd += ["-n", str(lines), "--no-pager", "-o", "short-iso"]
    return cmd


# ---------------------------------------------------------------- Docker integration


_DOCKER_AVAILABLE = None


def _check_docker_available() -> bool:
    global _DOCKER_AVAILABLE
    if _DOCKER_AVAILABLE is None:
        try:
            import docker_backend
            _DOCKER_AVAILABLE = docker_backend.docker_available()
        except Exception:
            _DOCKER_AVAILABLE = False
    return _DOCKER_AVAILABLE


def list_all_services(with_status: bool = True) -> list[dict]:
    """Lista serviços systemd + containers Docker."""
    services = list_services(with_status)

    if _check_docker_available():
        try:
            import docker_backend
            docker_services = docker_backend.list_containers(with_status)
            services.extend(docker_services)
        except Exception:
            pass
    return services


def docker_available() -> bool:
    return _check_docker_available()
