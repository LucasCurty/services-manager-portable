#!/usr/bin/python3
"""Resolução de caminhos do app portátil.

Raiz do executável:
  SVC_ROOT > APPIMAGE > executável congelado > pasta deste arquivo.

Configurações:
  SVC_CONF_DIR > <raiz>/conf

Na primeira execução, copia uma única vez as confs do local antigo
(~/.config/services) para <raiz>/conf, sem sobrescrever nada.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

HOME = Path.home()


def app_root() -> Path:
    override = os.environ.get("SVC_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return Path(appimage).expanduser().resolve().parent
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_ROOT = app_root()

_conf_override = os.environ.get("SVC_CONF_DIR")
CONF_DIR = (
    Path(_conf_override).expanduser().resolve()
    if _conf_override
    else APP_ROOT / "conf"
)
RUN_DIR = CONF_DIR / "run"
LEGACY_CONF_DIR = Path(
    os.environ.get("SVC_LEGACY_CONF_DIR", HOME / ".config/services")
).expanduser()

MIGRATION_MARKER = CONF_DIR / ".migrated"


def platform_name() -> str:
    override = os.environ.get("SVC_PLATFORM")
    if override:
        return override.strip().lower()
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def overlay_path(name: str) -> Path:
    return CONF_DIR / f"{name}.{platform_name()}.conf"


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_value(value: str) -> str:
    if not value:
        return value
    mapping = {
        "APP_ROOT": str(APP_ROOT),
        "CONF_DIR": str(CONF_DIR),
        "RUN_DIR": str(RUN_DIR),
        "HOME": str(HOME),
        "USERPROFILE": str(HOME),
    }

    def replace(match: re.Match) -> str:
        return mapping.get(match.group(1), match.group(0))

    out = _VAR_RE.sub(replace, value)
    if out == "~":
        return str(HOME)
    if out.startswith("~/"):
        return str(HOME) + out[1:]
    return out


def ensure_conf_dir() -> list[str]:
    """Cria <raiz>/conf e migra as confs antigas na primeira execução."""
    copied: list[str] = []
    first_run = not MIGRATION_MARKER.exists()
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    if first_run and not os.environ.get("SVC_NO_MIGRATE"):
        try:
            same = LEGACY_CONF_DIR.resolve() == CONF_DIR.resolve()
        except OSError:
            same = False
        if LEGACY_CONF_DIR.is_dir() and not same:
            for source in sorted(LEGACY_CONF_DIR.iterdir()):
                if not source.is_file() or source.suffix not in (".conf", ".env"):
                    continue
                target = CONF_DIR / source.name
                if target.exists():
                    continue
                try:
                    shutil.copy2(source, target)
                    copied.append(source.name)
                except OSError:
                    pass
        try:
            MIGRATION_MARKER.write_text(
                "migração de %s concluída\n" % LEGACY_CONF_DIR
            )
        except OSError:
            pass
    return copied


def describe() -> dict:
    return {
        "app_root": APP_ROOT,
        "conf_dir": CONF_DIR,
        "run_dir": RUN_DIR,
        "legacy_conf_dir": LEGACY_CONF_DIR,
        "platform": platform_name(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "conf_override": bool(_conf_override),
        "root_override": bool(os.environ.get("SVC_ROOT")),
        "appimage": os.environ.get("APPIMAGE", ""),
    }
