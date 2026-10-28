#!/usr/bin/python3
"""Comando 'svc' - gerenciador de servicos systemd de usuario."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys


def _prescan_paths(argv: list[str]) -> list[str]:
    """Aplica --root/--conf-dir antes de importar o backend e remove os args do argparse."""
    cleaned: list[str] = []
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
        else:
            cleaned.append(arg)
        index += 1
    return cleaned


sys.argv[1:] = _prescan_paths(sys.argv[1:])

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import backend  # noqa: E402
import paths  # noqa: E402

ROOT_SETUP = """#!/usr/bin/env bash
# Execute como root: sudo bash {path}
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo "Rode com sudo: sudo bash $0" >&2
  exit 1
fi
apt-get install -y gnome-shell-extension-desktop-icons-ng
loginctl enable-linger {user}
echo
echo "Pronto. Agora, como usuario normal e depois de relogar no xrdp:"
echo "  gnome-extensions enable ding@rastersoft.com"
"""


def color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def ok(text: str) -> str:
    return color(text, "32")


def bad(text: str) -> str:
    return color(text, "31")


def warn(text: str) -> str:
    return color(text, "33")


def all_or_names(names: list[str]) -> list[str]:
    if not names or "all" in names:
        return backend.list_names()
    return names


def print_status(name: str, show_health: bool = False) -> None:
    conf = backend.effective(name)
    st = backend.status(name)
    if st["active"]:
        state = ok("ativo")
    elif st["failed"]:
        state = bad("falhou")
    else:
        state = warn("parado")
    boot = "sim" if backend.is_truthy(conf.get("AUTOSTART", "1")) else "não"
    print(f"{color(name, '1')}  [{state}]")
    print(f"  descrição : {conf.get('DESC') or '-'}")
    print(f"  comando   : {conf.get('CMD') or '-'}")
    print(f"  workdir   : {conf.get('WORKDIR') or '-'}")
    print(f"  porta     : {conf.get('PORT') or '-'}")
    print(f"  no boot   : {boot}  ({st['unit_file_state']})")
    print(f"  systemd   : {st['state']}")
    if st["main_pid"]:
        print(f"  pid       : {st['main_pid']}  (uptime {st['uptime'] or '-'})")
    if conf.get("HEALTH_URL") and show_health:
        healthy, code = backend.health(conf["HEALTH_URL"])
        label = ok(f"ok ({code})") if healthy else bad("sem resposta")
        print(f"  health    : {label}  {conf['HEALTH_URL']}")


def cmd_bootstrap(args) -> int:
    report = backend.bootstrap()
    desktop = backend.install_desktop()
    print("Diretórios e defaults prontos.")
    if report["defaults_created"]:
        print(f"  criado {backend.DEFAULTS_FILE}")
    if report["nine_created"]:
        print(f"  criado {backend.conf_path('9router')}")
    if report["linger"] == "yes":
        print("  linger: habilitado (serviços sobem no boot sem login)")
    else:
        print(warn("  linger: NÃO habilitado (serviços só sobem após login)"))
        if report["linger_msg"]:
            print(f"    {report['linger_msg']}")
        print(f"    rode: {color('sudo loginctl enable-linger ' + backend.user_name(), '1')}")
    print(f"  atalhos: {len(desktop['files'])} arquivo(s) em menu/autostart/Área de trabalho")
    if desktop["favorite"]:
        print("  Dock: fixado")
    else:
        print(warn(f"  Dock: {desktop['favorite_msg']}"))
    if backend.conf_path("9router").exists():
        res = backend.apply("9router", start_if_inactive=True)
        print(f"  apply 9router: unit {res['unit']} {'(alterado)' if res['changed'] else '(sem mudanças)'}")
        if not res["started"]:
            print(warn(f"  start 9router: {res['start_msg'] or 'não iniciado'}"))
    print()
    if backend.icons_extension_installed():
        print("Ícones no desktop: extensão instalada; se ainda não aparecer, relogue na sessão.")
    else:
        print("Ícones no desktop: rode uma vez " + color("svc icons", "1") + " e relogue na sessão.")
    return 0


def cmd_list(args) -> int:
    services = backend.list_services()
    if not services:
        print("Nenhum serviço em", backend.CONF_DIR)
        return 0
    header = f"{'SERVIÇO':<22} {'ESTADO':<10} {'BOOT':<6} {'PID':<8} {'UPTIME':<10} PORTA"
    print(color(header, "1"))
    active_count = 0
    for item in services:
        st = item["status"]
        conf = item["conf"]
        if st["active"]:
            active_count += 1
            state = ok("ativo")
        elif st["failed"]:
            state = bad("falhou")
        else:
            state = warn("parado")
        boot = "sim" if backend.is_truthy(conf.get("AUTOSTART", "1")) else "não"
        print(
            f"{item['name']:<22} {state:<18} {boot:<6} {st['main_pid'] or '-':<8} "
            f"{st['uptime'] or '-':<10} {conf.get('PORT') or '-'}"
        )
    print(f"\n{active_count} de {len(services)} ativo(s).")
    return 0


def cmd_status(args) -> int:
    names = all_or_names(args.names)
    if not names:
        print("Nenhum serviço cadastrado.")
        return 0
    for index, name in enumerate(names):
        if index:
            print()
        if not backend.conf_path(name).exists():
            print(bad(f"serviço '{name}' não encontrado"))
            continue
        print_status(name, show_health=True)
    return 0


def cmd_apply(args) -> int:
    names = all_or_names(args.names)
    if not names:
        print("Nenhum serviço cadastrado.")
        return 1
    failed = False
    for name in names:
        try:
            res = backend.apply(name, start_if_inactive=not args.no_start)
        except (ValueError, FileNotFoundError) as exc:
            print(bad(f"{name}: {exc}"))
            failed = True
            continue
        parts = ["alterado" if res["changed"] else "sem mudanças"]
        parts.append("boot=" + ("on" if res["enabled_ok"] else "off"))
        if res["started"]:
            parts.append("iniciado")
        elif args.no_start:
            parts.append("start ignorado")
        elif not res["start_msg"]:
            parts.append("já ativo")
        else:
            parts.append(bad("falha ao iniciar"))
        print(f"{name}: " + ", ".join(parts))
        if res["start_msg"] and not res["started"]:
            print(f"  {res['start_msg']}")
    return 1 if failed else 0


def cmd_action(args) -> int:
    names = all_or_names(args.names)
    if not names:
        print("Nenhum serviço cadastrado.")
        return 1
    failed = False
    for name in names:
        success, message = backend.action(name, args.command)
        label = ok(args.command) if success else bad(args.command)
        print(f"{name}: {label}")
        if message and not success:
            print(f"  {message}")
            failed = True
    return 1 if failed else 0


def cmd_boot(args) -> int:
    enabled = args.state.lower() in ("on", "1", "true", "yes", "sim")
    success, message = backend.set_autostart(args.name, enabled)
    print(f"{args.name}: iniciar no boot = {'sim' if enabled else 'não'}")
    if not success:
        print(bad(message or "falha ao alterar o boot"))
    return 0 if success else 1


def cmd_add(args) -> int:
    name = args.name
    if not backend.NAME_RE.match(name):
        print(bad("nome inválido: use letras minúsculas, números e hífen"))
        return 1
    if backend.conf_path(name).exists() and not args.force:
        print(bad(f"'{name}' já existe; use --force ou 'svc edit {name}'"))
        return 1
    cmd = args.cmd
    if not cmd:
        if not sys.stdin.isatty():
            print(bad("informe --cmd (ex.: --cmd '/caminho/bin --porta 1234')"))
            return 1
        cmd = input("Comando completo (caminho absoluto): ").strip()
    data = {
        "DESC": args.desc or name,
        "CMD": cmd,
        "WORKDIR": args.workdir or str(backend.HOME),
        "PORT": str(args.port) if args.port else "",
        "HEALTH_URL": args.health or "",
        "AUTOSTART": "1" if args.boot else "0",
        "AFTER": args.after or "network-online.target",
        "WANTS": args.wants or "network-online.target",
        "TYPE": args.type,
    }
    errors, warnings = backend.validate(name, data)
    if errors:
        for item in errors:
            print(bad("erro: " + item))
        return 1
    for item in warnings:
        print(warn("aviso: " + item))
    backend.write_conf(name, data)
    res = backend.apply(name, start_if_inactive=not args.no_start)
    print(f"criado {backend.conf_path(name)}")
    print(f"unit: {res['unit']}")
    if res["started"]:
        print(ok("serviço iniciado"))
    elif res["start_msg"]:
        print(bad(res["start_msg"]))
    return 0


def cmd_edit(args) -> int:
    path = backend.conf_path(args.name)
    if not path.exists():
        print(bad(f"configuração não encontrada: {path}"))
        return 1
    editor = os.environ.get("EDITOR") or shutil.which("nano") or shutil.which("vi") or shutil.which("sensible-editor")
    if not editor:
        print(bad("nenhum editor encontrado; defina EDITOR"))
        return 1
    result = subprocess.run([editor, str(path)])
    if result.returncode != 0:
        return result.returncode
    data = backend.read_conf(args.name)
    errors, warnings = backend.validate(args.name, backend.effective(args.name))
    for item in warnings:
        print(warn("aviso: " + item))
    if errors:
        for item in errors:
            print(bad("erro: " + item))
        print("corrija o arquivo e rode 'svc apply " + args.name + "'")
        return 1
    res = backend.apply(args.name, start_if_inactive=False)
    print(f"{args.name}: unit {'alterado' if res['changed'] else 'sem mudanças'}")
    print("use 'svc restart " + args.name + "' para aplicar em execução")
    return 0


def cmd_logs(args) -> int:
    if not backend.conf_path(args.name).exists():
        print(bad(f"serviço '{args.name}' não encontrado"))
        return 1
    cmd = backend.journal_cmd(args.name, follow=args.follow, lines=args.lines)
    os.execvp("journalctl", cmd)
    return 0


def cmd_rm(args) -> int:
    name = args.name
    if not backend.conf_path(name).exists():
        print(bad(f"serviço '{name}' não encontrado"))
        return 1
    if not args.yes:
        if not sys.stdin.isatty():
            print(bad("use --yes em modo não interativo"))
            return 1
        answer = input(f"Remover o serviço '{name}' (parar, desabilitar e apagar)? [s/N] ").strip().lower()
        if answer not in ("s", "sim", "y", "yes"):
            print("cancelado")
            return 0
    backend.remove(name)
    print(f"{name}: removido" + (" (arquivo .env mantido)" if backend.env_path(name).exists() else ""))
    return 0


def cmd_desktop(args) -> int:
    res = backend.install_desktop(add_favorite=not args.no_favorite)
    for path in res["files"]:
        print("  " + path)
    if res["favorite"]:
        print(ok("Dock: fixado"))
    else:
        print(warn("Dock: " + res["favorite_msg"]))
    print("Se o ícone não aparecer na Área de trabalho:")
    print(f"  sudo bash {backend.APP_DIR / 'root-setup.sh'}")
    return 0


def cmd_icons(args) -> int:
    success, message = backend.install_icons_extension()
    print(f"extensão: {message}" if success else bad(f"erro: {message}"))
    if not success:
        return 1
    enabled, enabled_msg = backend.enable_icons_extension()
    if enabled:
        print(ok(f"extensão habilitada ({enabled_msg})"))
    else:
        print(warn(f"não foi possível habilitar agora: {enabled_msg}"))
    print("Relogue na sessão xrdp (ou reinicie o GNOME) para o ícone aparecer na Área de trabalho.")
    print("Até lá, o atalho já funciona no Dock e no menu de aplicativos.")
    return 0


def cmd_taskbar(args) -> int:
    if args.mode == "off":
        success, message = backend.taskbar_off()
        print(ok(message) if success else bad(f"erro: {message}"))
        print("Relogue na sessão para voltar à barra padrão do GNOME.")
        return 0 if success else 1
    report = backend.taskbar_panel()
    for message in report["messages"]:
        print("  " + message)
    if not report["installed"]:
        return 1
    if report["enabled"]:
        print(ok("Dash to Panel habilitado"))
    else:
        print(warn("Dash to Panel não foi habilitado"))
    if report["position"] == "BOTTOM":
        print(ok("barra posicionada embaixo"))
    else:
        print(warn("posição da barra ficará no padrão (topo)"))
    print("Relogue na sessão xrdp para a barra aparecer.")
    print("O Gerenciador de Serviços já está fixado nas favoritas da barra.")
    return 0


def cmd_paths(args) -> int:
    info = paths.describe()
    names = backend.list_names()
    print("raiz do app:", info["app_root"])
    print("configs    :", info["conf_dir"])
    print("execuções  :", info["run_dir"])
    print("units      :", backend.UNIT_DIR)
    print("plataforma :", info["platform"])
    print("legado     :", info["legacy_conf_dir"])
    print("serviços   :", len(names), ", ".join(names) if names else "-")
    overlays = [paths.overlay_path(name).name for name in names if paths.overlay_path(name).exists()]
    if overlays:
        print("overlays   :", ", ".join(overlays))
    if info["root_override"]:
        print("override   : SVC_ROOT")
    if info["conf_override"]:
        print("override   : SVC_CONF_DIR")
    return 0


def cmd_gui(args) -> int:
    launcher = backend.BIN_DIR / "services-manager"
    if not launcher.exists():
        print(bad(f"launcher não encontrado: {launcher}"))
        return 1
    os.execv(str(launcher), [str(launcher)])
    return 0


def cmd_root_setup(args) -> int:
    path = backend.APP_DIR / "root-setup.sh"
    path.write_text(ROOT_SETUP.format(path=path, user=backend.user_name()))
    path.chmod(0o755)
    print("Script criado:", path)
    print("Rode (pede sua senha):")
    print(f"  {color('sudo bash ' + str(path), '1')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="svc", description="Gerencia serviços systemd de usuário.")
    parser.add_argument("--version", action="version", version="services-manager 1.0")
    parser.add_argument("--root", metavar="DIR", help="raiz do app (padrão: pasta do executável)")
    parser.add_argument("--conf-dir", metavar="DIR", help="pasta das confs (padrão: <raiz>/conf)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("paths", help="mostra raiz, confs e units em uso")
    p.set_defaults(func=cmd_paths)

    p = sub.add_parser("bootstrap", help="prepara diretórios, defaults, 9router e atalhos")
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("list", help="lista os serviços")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("status", help="detalha um ou todos os serviços")
    p.add_argument("names", nargs="*")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("apply", help="gera units e aplica os confs")
    p.add_argument("names", nargs="*")
    p.add_argument("--no-start", action="store_true", help="não inicia o serviço")
    p.set_defaults(func=cmd_apply)

    for op in ("start", "stop", "restart"):
        p = sub.add_parser(op, help=f"{op} serviço(s) (use 'all')")
        p.add_argument("names", nargs="*")
        p.set_defaults(func=cmd_action, command=op)

    p = sub.add_parser("boot", help="liga/desliga o início no boot")
    p.add_argument("name")
    p.add_argument("state", choices=["on", "off", "1", "0", "true", "false", "yes", "no", "sim", "não"])
    p.set_defaults(func=cmd_boot)

    p = sub.add_parser("add", help="adiciona um serviço")
    p.add_argument("name")
    p.add_argument("--cmd")
    p.add_argument("--desc")
    p.add_argument("--workdir")
    p.add_argument("--port", type=int)
    p.add_argument("--health")
    p.add_argument("--after")
    p.add_argument("--wants")
    p.add_argument("--type", default="simple")
    p.add_argument("--boot", action="store_true", default=True)
    p.add_argument("--no-boot", dest="boot", action="store_false")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("edit", help="edita o conf no $EDITOR")
    p.add_argument("name")
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("logs", help="mostra os logs do serviço")
    p.add_argument("name")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("-n", "--lines", type=int, default=200)
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("rm", help="remove o serviço")
    p.add_argument("name")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("desktop", help="cria ícones e atalho de autostart")
    p.add_argument("--no-favorite", action="store_true")
    p.set_defaults(func=cmd_desktop)

    p = sub.add_parser("icons", help="instala/habilita os ícones no desktop (sem sudo)")
    p.set_defaults(func=cmd_icons)

    p = sub.add_parser("taskbar", help="instala/configura a barra de tarefas (Dash to Panel)")
    p.add_argument("mode", choices=["panel", "off"], nargs="?", default="panel")
    p.set_defaults(func=cmd_taskbar)

    p = sub.add_parser("gui", help="abre a interface gráfica")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("root-setup", help="cria o script de passos com sudo")
    p.set_defaults(func=cmd_root_setup)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(bad(f"erro: {exc}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
