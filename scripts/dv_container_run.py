#!/usr/bin/env python3
"""Execução de um comando em container descartável com quotas fixas (ajuste 3, contrato v3).

Uso, a partir da raiz do repositório:

    python scripts/dv_container_run.py --dry-run -- python /app/scripts/dv_canary_check.py --workdir /work/canary
    python scripts/dv_container_run.py --ledger <arquivo.jsonl> -- python /app/scripts/dv_canary_check.py --workdir /work/canary
    python scripts/dv_container_run.py --pass-env ANTHROPIC_API_KEY -- python /app/scripts/dv_smoke_run.py --help

Padrões: ``--network none``, ``--cpus 1``, ``--memory 512m``, ``--pids-limit 256``, ``--timeout-s 300``.
O repositório entra em ``/app`` somente leitura (bind mount ``--mount``, válido em Docker Desktop no Windows),
com ``PYTHONPATH=/app/src``. ``--pass-env NAME`` repassa uma variável do host pelo nome (``-e NAME``): o valor
nunca aparece no argv, no dry-run nem no ledger. ``/work`` é tmpfs. O sistema de
arquivos da raiz é somente leitura. Sem capacidades extras (``--cap-drop ALL``).

Desvio deliberado de ``--rm``: o container é criado com nome único e removido com
``docker rm -f`` no ``finally``. Antes da remoção lemos ``State.OOMKilled``, que ``--rm``
impediria de consultar. A descartabilidade é a mesma; o que muda é que a remoção é explícita.

Saída: JSON com metadados do ambiente e flags de infraestrutura (``oom_killed``,
``timed_out``, ``cmd_timeouts``, ``daemon_error``). Código de saída: o do comando; 124 em
timeout; 125 se o daemon Docker não respondeu (falha de infraestrutura, não do agente).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_IMAGE = "dv-harness:local"
DEFAULT_CPUS = "1"
DEFAULT_MEMORY = "512m"
DEFAULT_PIDS_LIMIT = 256
DEFAULT_TIMEOUT_S = 300.0
EXIT_TIMEOUT = 124
EXIT_INFRA = 125
OUTPUT_MAX_CHARS = 4000
DAEMON_ERROR_MARKERS = ("Cannot connect to the Docker daemon", "failed to connect to the docker API",
                        "error during connect", "docker daemon is not running")

_MEMORY_RE = re.compile(r"^[1-9][0-9]*[bkmg]?$")
APP_TARGET = "/app"  # destino do repositório dentro do container (somente leitura)
APP_SRC = "/app/src"  # PYTHONPATH do pacote dv_harness dentro do container
PASS_ENV_RE = re.compile(r"[A-Z_][A-Z0-9_]*")  # nome de variável de ambiente, sem '='

Runner = Callable[..., subprocess.CompletedProcess]


class QuotaError(ValueError):
    """Quota ou parâmetro inválido. Levantado antes de qualquer chamada ao Docker."""


@dataclass(frozen=True)
class Quotas:
    image: str = DEFAULT_IMAGE
    cpus: str = DEFAULT_CPUS
    memory: str = DEFAULT_MEMORY
    pids_limit: int = DEFAULT_PIDS_LIMIT
    timeout_s: float = DEFAULT_TIMEOUT_S
    network: str = "none"

    def validate(self, *, allow_network: bool = False) -> None:
        try:
            cpus = float(self.cpus)
        except ValueError as exc:
            raise QuotaError(f"cpus must be a number, got {self.cpus!r}") from exc
        if cpus <= 0:
            raise QuotaError("cpus must be > 0")
        if not _MEMORY_RE.match(self.memory):
            raise QuotaError(f"memory must look like 512m or 1g, got {self.memory!r}")
        if isinstance(self.pids_limit, bool) or not isinstance(self.pids_limit, int) or self.pids_limit < 1:
            raise QuotaError("pids_limit must be an integer >= 1")
        if self.timeout_s <= 0:
            raise QuotaError("timeout_s must be > 0")
        if self.network != "none" and not allow_network:
            raise QuotaError("network must be 'none' (pass allow_network explicitly to change it)")
        if not self.image:
            raise QuotaError("image must not be empty")


def check_pass_env(names: Sequence[str]) -> None:
    """Valida ``--pass-env``. Aceita só nomes: ``NAME=valor`` é recusado, porque o valor iria para o argv e os logs.
    O argumento recusado nunca é ecoado na mensagem de erro, pois pode conter um segredo.
    """
    for name in names:
        if "=" in name:
            raise QuotaError("--pass-env takes a variable NAME only; values are never placed on the command line")
        if not PASS_ENV_RE.fullmatch(name):
            raise QuotaError("--pass-env name must match [A-Z_][A-Z0-9_]*")
        if not os.environ.get(name):
            raise QuotaError(f"--pass-env {name} is not set (or empty) in the host environment")


def _repo_mount(repo: str) -> str:
    if "," in repo:
        raise QuotaError("repo path must not contain ',' (it breaks the --mount syntax)")
    return f"type=bind,source={repo},target={APP_TARGET},readonly"


def build_argv(quotas: Quotas, name: str, command: Sequence[str], *,
               mounts: Sequence[str] = (), repo: str | None = None,
               pass_env: Sequence[str] = ()) -> list[str]:
    """Argumentos de ``docker run`` com as quotas fixas. Pura: não executa nada.

    ``repo``: caminho do host montado em ``/app`` somente leitura (``--mount type=bind,...,readonly``).
    ``pass_env``: nomes de variáveis herdadas do host (``-e NAME``, sem valor). Falha antes do docker se
    um nome for inválido, contiver ``=`` ou estiver ausente do ambiente do host.
    """
    if not command:
        raise QuotaError("command must not be empty")
    check_pass_env(pass_env)
    if repo is not None and any(re.search(rf":{APP_TARGET}(:|$)", m) for m in mounts):
        raise QuotaError("/app is the repo mount; a second mount at /app is refused")
    argv = [
        "docker", "run",
        "--name", name,
        "--cpus", quotas.cpus,
        "--memory", quotas.memory,
        "--memory-swap", quotas.memory,  # sem swap: o limite de memória é o limite real
        "--pids-limit", str(quotas.pids_limit),
        "--network", quotas.network,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:rw,size=64m,mode=1777",
        "--tmpfs", "/work:rw,size=128m,mode=1777",
        "-w", "/work",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", f"PYTHONPATH={APP_SRC}",
    ]
    if repo is not None:
        argv += ["--mount", _repo_mount(repo)]
    for mount in mounts:
        argv += ["-v", mount]
    for var in pass_env:
        argv += ["-e", var]  # só o nome: o docker herda o valor do ambiente deste processo
    return [*argv, quotas.image, *command]


@dataclass
class ContainerResult:
    name: str
    command: list[str]
    quotas: dict[str, Any]
    exit_code: int | None
    timed_out: bool
    oom_killed: bool | None  # None = o daemon não respondeu à consulta
    daemon_error: bool
    duration_ms: float
    stdout: str
    stderr: str
    cleanup_error: str | None = None  # falha ao ler OOMKilled ou remover o container (infra)
    pass_env: list[str] = field(default_factory=list)  # somente NOMES de variáveis repassadas

    @property
    def cmd_timeouts(self) -> int:
        return 1 if self.timed_out else 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"cmd_timeouts": self.cmd_timeouts}


def _clip(text: str | None) -> str:
    text = text or ""
    return text if len(text) <= OUTPUT_MAX_CHARS else text[:OUTPUT_MAX_CHARS] + "...[truncated]"


def _docker(runner: Runner, args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return runner(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


def run_container(quotas: Quotas, command: Sequence[str], *, mounts: Sequence[str] = (),
                  repo: str | None = None, pass_env: Sequence[str] = (),
                  allow_network: bool = False, ledger: Any | None = None,
                  runner: Runner = subprocess.run) -> ContainerResult:
    """Executa ``command`` num container descartável e devolve a telemetria.

    ``runner`` é injetável: os testes substituem o ``subprocess.run`` por um simulador.
    """
    quotas.validate(allow_network=allow_network)
    name = f"dv-run-{uuid.uuid4().hex[:12]}"
    argv = build_argv(quotas, name, command, mounts=mounts, repo=repo, pass_env=pass_env)
    started = time.perf_counter()
    exit_code: int | None = None
    timed_out = False
    daemon_error = False
    cleanup_error: str | None = None
    kill_error: str | None = None
    rm_error: str | None = None
    stdout = stderr = ""
    try:
        cp = runner(argv, capture_output=True, text=True, timeout=quotas.timeout_s, check=False)
        exit_code, stdout, stderr = cp.returncode, cp.stdout or "", cp.stderr or ""
        daemon_error = any(marker in stderr for marker in DAEMON_ERROR_MARKERS)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        try:
            _docker(runner, ["kill", name], timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:  # kill falhou: o container pode seguir vivo
            kill_error = f"kill: {type(exc).__name__}: {exc}"
    except (OSError, subprocess.SubprocessError) as exc:  # docker ausente do PATH ou falha do daemon
        daemon_error = True
        stderr = f"could not run docker: {type(exc).__name__}: {exc}"
    finally:
        # Limpeza protegida: nem a leitura de OOMKilled nem a remoção levantam exceção.
        oom_killed, oom_error = _inspect_oom(runner, name)
        try:
            _docker(runner, ["rm", "-f", name], timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            rm_error = f"rm: {type(exc).__name__}: {exc}"

    errors = [e for e in (kill_error, oom_error, rm_error) if e]
    if errors:
        cleanup_error = "; ".join(errors)
        daemon_error = True  # telemetria ou limpeza incompleta: falha de infraestrutura, não do comando
    if cleanup_error is not None:
        exit_code = EXIT_INFRA  # nunca 0 nem 124 quando a limpeza ou a telemetria de OOM falhou
    elif timed_out:
        exit_code = EXIT_TIMEOUT
    elif daemon_error:
        exit_code = EXIT_INFRA
    result = ContainerResult(
        name=name,
        command=list(command),
        quotas=asdict(quotas),
        exit_code=exit_code,
        timed_out=timed_out,
        oom_killed=oom_killed,
        daemon_error=daemon_error,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        stdout=_clip(stdout),
        stderr=_clip(stderr),
        cleanup_error=cleanup_error,
        pass_env=list(pass_env),
    )
    if ledger is not None:
        ledger.append("infra_finished", result.as_dict())
    return result


def _inspect_oom(runner: Runner, name: str) -> tuple[bool | None, str | None]:
    """Lê State.OOMKilled. Devolve ``(valor, erro)``. Qualquer falha de telemetria devolve ``(None, erro)``:
    nunca ``False`` por padrão, porque isso seria uma medição que não aconteceu.
    """
    try:
        cp = _docker(runner, ["inspect", "-f", "{{.State.OOMKilled}}", name], timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"inspect: {type(exc).__name__}: {exc}"
    if cp.returncode != 0:
        return None, f"inspect: exit {cp.returncode}: {(cp.stderr or '').strip()[:200]}"
    value = (cp.stdout or "").strip().lower()
    if value not in ("true", "false"):
        return None, f"inspect: unexpected output {value!r}"
    return value == "true", None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--cpus", default=DEFAULT_CPUS)
    parser.add_argument("--memory", default=DEFAULT_MEMORY)
    parser.add_argument("--pids-limit", type=int, default=DEFAULT_PIDS_LIMIT)
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--network", default="none", help="padrão 'none'; outro valor exige --allow-network")
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--mount", action="append", default=[], metavar="HOST:CONTAINER[:ro]")
    parser.add_argument("--pass-env", action="append", default=[], metavar="NAME",
                        help="repassa a variável do host pelo nome (repetível); nunca NAME=valor")
    parser.add_argument("--ledger", type=Path, default=None, help="grava evento infra_finished em JSONL")
    parser.add_argument("--dry-run", action="store_true", help="imprime o argv do docker e não executa")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command

    quotas = Quotas(image=args.image, cpus=args.cpus, memory=args.memory, pids_limit=args.pids_limit,
                    timeout_s=args.timeout_s, network=args.network)
    try:
        quotas.validate(allow_network=args.allow_network)
        if args.dry_run:
            print(" ".join(build_argv(quotas, "<name>", command, mounts=args.mount, repo=str(REPO),
                                     pass_env=args.pass_env)))
            return 0
        ledger = None
        if args.ledger is not None:
            sys.path.insert(0, str(REPO / "src"))
            from dv_harness.ledger import (
                RunLedger,  # import tardio: só quando há ledger
            )
            ledger = RunLedger(args.ledger)
        try:
            result = run_container(quotas, command, mounts=args.mount, repo=str(REPO),
                                   pass_env=args.pass_env, allow_network=args.allow_network, ledger=ledger)
        finally:
            if ledger is not None:
                ledger.close()
    except QuotaError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError) as exc:  # falha de infraestrutura fora da telemetria
        print(f"infrastructure error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INFRA

    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    return result.exit_code if result.exit_code is not None else EXIT_INFRA


if __name__ == "__main__":
    sys.exit(main())
