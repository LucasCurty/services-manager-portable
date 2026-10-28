#!/usr/bin/python3
"""Launcher do app: abre a GUI ou executa ações sem depender de display."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def _prescan_paths(argv: list[str]) -> None:
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--root", "--conf-dir") and index + 1 < len(argv):
            env = "SVC_ROOT" if arg == "--root" else "SVC_CONF_DIR"
            os.environ[env] = argv[index + 1]
            index += 2
            continue
        if arg.startswith("--root="):
            os.environ["SVC_ROOT"] = arg.split("=", 1)[1]
        elif arg.startswith("--conf-dir="):
            os.environ["SVC_CONF_DIR"] = arg.split("=", 1)[1]
        index += 1


_prescan_paths(sys.argv[1:])

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import backend  # noqa: E402


def notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", "-a", "Gerenciador de Serviços", "-i", "services-manager", title, body],
            check=False,
        )
    else:
        print(f"{title}: {body}")


def main() -> int:
    args = sys.argv[1:]

    if args and args[0] in ("--start", "--stop", "--restart"):
        op = args[0][2:]
        targets = args[1:] or ["all"]
        names = backend.list_names() if "all" in targets else targets
        if not names:
            notify("Gerenciador de Serviços", "nenhum serviço cadastrado")
            return 1
        failures = []
        for name in names:
            if not backend.conf_path(name).exists():
                failures.append(f"{name} (não existe)")
                continue
            success, message = backend.action(name, op)
            if not success:
                failures.append(f"{name} ({message or 'falhou'})")
        if failures:
            notify("Falha ao executar", f"{op}: " + "; ".join(failures))
            return 1
        notify("Gerenciador de Serviços", f"{op} ok: " + ", ".join(names))
        return 0

    if args and args[0] in ("--status", "--list"):
        services = backend.list_all_services()
        for item in services:
            st = item["status"]
            svc_type = item.get("type", "systemd")
            state = "ativo" if st.get("active") else ("falhou" if st.get("failed") else "parado")
            boot = "sim" if backend.is_truthy(item['conf'].get('AUTOSTART', '1')) else 'não'
            if svc_type == "docker":
                boot = item['conf'].get('RESTART_POLICY', 'no')
            print(f"[{svc_type}] {item['name']}: {state} (boot={boot})")
        return 0

    from app import run

    forwarded = [arg for arg in args if arg in ("-h", "--help") or not arg.startswith("--")]
    return run([sys.argv[0], *forwarded])


if __name__ == "__main__":
    sys.exit(main())
