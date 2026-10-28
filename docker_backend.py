#!/usr/bin/python3
"""Backend Docker para o gerenciador de serviços.

Operações isoladas para listar, inspecionar e controlar containers.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from typing import Any


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def docker_available() -> bool:
    """Verifica se Docker está disponível e acessível."""
    r = _run(["docker", "info"])
    return r.returncode == 0


def list_containers(with_status: bool = True) -> list[dict]:
    """Lista todos os containers (rodando e parados)."""
    r = _run(["docker", "ps", "-a", "--format", "{{json .}}"])
    if r.returncode != 0 or not r.stdout.strip():
        return []

    containers = []
    for line in r.stdout.strip().splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        name = data.get("Names", "").strip()
        if not name:
            continue

        item = {
            "name": name,
            "type": "docker",
            "conf": {
                "IMAGE": data.get("Image", ""),
                "PORTS": _parse_ports(data.get("Ports", "")),
                "RESTART_POLICY": _parse_restart_policy(data.get("Status", "")),
                "COMMAND": data.get("Command", ""),
                "CREATED": data.get("CreatedAt", ""),
            },
            "status": {},
        }

        if with_status:
            item["status"] = container_status(name)

        containers.append(item)

    return containers


def _parse_ports(ports_str: str) -> list[str]:
    """Parse de '0.0.0.0:8080->80/tcp, :::8081->8081/tcp' para ['8080:80', '8081:8081']."""
    if not ports_str:
        return []
    result = []
    for part in ports_str.split(","):
        part = part.strip()
        match = re.search(r"(\d+)->(\d+)", part)
        if match:
            host, container = match.groups()
            result.append(f"{host}:{container}")
    return result


def _parse_restart_policy(status_str: str) -> str:
    """Extrai restart policy do status se disponível."""
    if "restarting" in status_str.lower():
        return "always"
    return "no"


def container_status(name: str) -> dict:
    """Status detalhado do container via docker inspect."""
    r = _run(["docker", "inspect", name])
    if r.returncode != 0 or not r.stdout.strip():
        return {
            "name": name,
            "type": "docker",
            "active": False,
            "health": "none",
            "uptime": "",
            "uptime_seconds": 0,
            "state": "unknown",
        }

    try:
        data = json.loads(r.stdout)[0]
    except (json.JSONDecodeError, IndexError):
        return {"name": name, "type": "docker", "active": False, "health": "none"}

    state = data.get("State", {})
    running = state.get("Running", False)
    health = state.get("Health", {})
    health_status = health.get("Status", "none") if health else "none"
    started_at = state.get("StartedAt", "")

    uptime_seconds = 0.0
    uptime_str = ""
    if running and started_at:
        try:
            started_ts = _parse_docker_timestamp(started_at)
            now_ts = time.time()
            uptime_seconds = max(0.0, now_ts - started_ts)
            uptime_str = human_uptime(uptime_seconds)
        except (ValueError, OSError):
            pass

    ports_mapped = []
    net_settings = data.get("NetworkSettings", {})
    ports = net_settings.get("Ports", {})
    for container_port, bindings in ports.items():
        if bindings:
            for binding in bindings:
                host_port = binding.get("HostPort")
                if host_port:
                    ports_mapped.append({
                        "host": int(host_port),
                        "container": container_port.split("/")[0],
                    })

    restart_policy = "no"
    host_config = data.get("HostConfig", {})
    rp = host_config.get("RestartPolicy", {})
    if rp:
        restart_policy = rp.get("Name", "no")

    return {
        "name": name,
        "type": "docker",
        "active": running,
        "health": health_status,
        "uptime": uptime_str,
        "uptime_seconds": uptime_seconds,
        "state": state.get("Status", "unknown"),
        "image": data.get("Config", {}).get("Image", ""),
        "ports_mapped": ports_mapped,
        "restart_policy": restart_policy,
        "main_pid": str(state.get("Pid", "")),
    }


def _parse_docker_timestamp(ts: str) -> float:
    """Converte timestamp Docker (ex: 2024-01-15T10:30:00.123456789Z) para epoch."""
    ts = ts.rstrip("Z")
    if "." in ts:
        base, frac = ts.split(".", 1)
        frac = (frac + "000000")[:6]
    else:
        base, frac = ts, "000000"
    dt = time.strptime(base, "%Y-%m-%dT%H:%M:%S")
    return time.mktime(dt) + float(frac) / 1_000_000


def human_uptime(seconds: float) -> str:
    if seconds <= 0:
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


def container_action(name: str, op: str) -> tuple[bool, str]:
    """Executa ação no container: start, stop, restart, remove."""
    valid_ops = {"start", "stop", "restart", "remove"}
    if op not in valid_ops:
        return False, f"operação inválida: {op}"

    timeout = 60 if op == "stop" else 30
    r = _run(["docker", op, name], timeout=timeout)
    output = (r.stdout + r.stderr).strip()
    return r.returncode == 0, output


def container_logs_cmd(name: str, follow: bool = False, lines: int = 200) -> list[str]:
    """Comando para logs do container (compatível com LogsWindow)."""
    cmd = ["docker", "logs"]
    if follow:
        cmd.append("-f")
    cmd += ["--tail", str(lines), name]
    return cmd


def inspect_container(name: str) -> dict:
    """Retorna inspeção completa do container."""
    r = _run(["docker", "inspect", name])
    if r.returncode != 0 or not r.stdout.strip():
        return {"error": "container não encontrado"}
    try:
        return json.loads(r.stdout)[0]
    except (json.JSONDecodeError, IndexError):
        return {"error": "falha ao parsear inspect"}


def get_restart_policy(name: str) -> str:
    """Obtém restart policy atual do container."""
    r = _run(["docker", "inspect", "--format", "{{.HostConfig.RestartPolicy.Name}}", name])
    if r.returncode == 0:
        policy = r.stdout.strip()
        return policy if policy in ("always", "unless-stopped", "no") else "no"
    return "no"


def set_restart_policy(name: str, policy: str) -> tuple[bool, str]:
    """Altera restart policy do container (requer recriar no Docker)."""
    if policy not in ("always", "unless-stopped", "no"):
        return False, f"policy inválida: {policy}"

    r = _run(["docker", "update", "--restart", policy, name])
    output = (r.stdout + r.stderr).strip()
    return r.returncode == 0, output