"""Wrapper de container (ajuste 3): argv, quotas, telemetria de infraestrutura. Docker simulado.

Nenhum teste aqui exige daemon Docker. ``runner`` é substituído por um simulador.
Executar: python -m pytest tests/test_container_run.py -v
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from support import ROOT  # noqa: F401  (ajusta sys.path para src/)

sys.path.insert(0, str(ROOT / "scripts"))
import dv_container_run as dcr  # noqa: E402

from dv_harness.ledger import RunLedger, read_events  # noqa: E402


class FakeDocker:
    """Simulador de ``subprocess.run`` para o docker. Registra cada chamada."""

    def __init__(self, *, run=None, inspect_stdout="false\n", inspect_rc=0, rm_rc=0, kill_rc=0):
        self.calls: list[list[str]] = []
        self.run_behavior = run  # callable(argv) -> CompletedProcess ou exceção
        self.inspect_stdout = inspect_stdout
        self.inspect_rc = inspect_rc
        self.rm_rc = rm_rc
        self.kill_rc = kill_rc

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        verb = argv[1]
        if verb == "run":
            if self.run_behavior is None:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return self.run_behavior(argv)
        if verb == "inspect":
            return subprocess.CompletedProcess(argv, self.inspect_rc, stdout=self.inspect_stdout, stderr="")
        if verb == "kill":
            return subprocess.CompletedProcess(argv, self.kill_rc, stdout="", stderr="")
        if verb == "rm":
            return subprocess.CompletedProcess(argv, self.rm_rc, stdout="", stderr="")
        raise AssertionError(f"unexpected docker verb: {argv}")

    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls]


CMD = ["python", "/app/scripts/dv_canary_check.py", "--workdir", "/work/canary"]


# --- argv e quotas ----------------------------------------------------------------


def test_build_argv_fixes_quotas_and_isolation():
    argv = dcr.build_argv(dcr.Quotas(), "dv-run-x", CMD, mounts=["/repo:/app:ro"])
    pairs = dict(zip(argv, argv[1:], strict=False))
    assert pairs["--cpus"] == "1"
    assert pairs["--memory"] == "512m"
    assert pairs["--memory-swap"] == "512m"          # sem swap extra
    assert pairs["--pids-limit"] == "256"
    assert pairs["--network"] == "none"
    assert pairs["--name"] == "dv-run-x"
    assert "ALL" in argv and "--cap-drop" in argv
    assert "no-new-privileges" in argv and "--read-only" in argv
    assert "/repo:/app:ro" in argv
    assert argv[-len(CMD) - 1:] == [dcr.DEFAULT_IMAGE, *CMD]


def test_argv_does_not_use_rm_flag_because_oom_is_read_before_removal():
    argv = dcr.build_argv(dcr.Quotas(), "n", CMD)
    assert "--rm" not in argv


def test_default_network_is_none():
    assert dcr.Quotas().network == "none"


def test_network_other_than_none_is_refused_before_any_docker_call():
    fake = FakeDocker()
    with pytest.raises(dcr.QuotaError, match="network"):
        dcr.run_container(dcr.Quotas(network="host"), CMD, runner=fake)
    assert fake.calls == []


def test_network_override_requires_explicit_allow():
    fake = FakeDocker()
    result = dcr.run_container(dcr.Quotas(network="bridge"), CMD, allow_network=True, runner=fake)
    assert result.quotas["network"] == "bridge"


@pytest.mark.parametrize("bad", [
    dict(cpus="0"), dict(cpus="abc"), dict(memory="1.5g"), dict(memory=""), dict(pids_limit=0),
    dict(timeout_s=0), dict(image=""),
])
def test_invalid_quotas_are_refused(bad):
    with pytest.raises(dcr.QuotaError):
        dcr.Quotas(**bad).validate()


def test_empty_command_is_refused():
    with pytest.raises(dcr.QuotaError):
        dcr.build_argv(dcr.Quotas(), "n", [])


# --- execução e telemetria de infraestrutura --------------------------------------


def test_success_reports_exit_stdout_and_removes_container():
    fake = FakeDocker(run=lambda argv: subprocess.CompletedProcess(argv, 0, stdout="I3b holds: True\n", stderr=""))
    result = dcr.run_container(dcr.Quotas(), CMD, runner=fake)
    assert result.exit_code == 0
    assert result.oom_killed is False and result.timed_out is False and result.daemon_error is False
    assert result.stdout == "I3b holds: True\n"
    assert fake.verbs() == ["run", "inspect", "rm"]
    assert fake.calls[-1][2:] == ["-f", result.name]  # remoção explícita pelo nome


def test_oom_kill_is_an_infrastructure_flag_not_agent_behavior():
    fake = FakeDocker(run=lambda argv: subprocess.CompletedProcess(argv, 137, stdout="", stderr=""),
                      inspect_stdout="true\n")
    result = dcr.run_container(dcr.Quotas(), CMD, runner=fake)
    assert result.oom_killed is True
    assert result.exit_code == 137
    assert result.timed_out is False


def test_timeout_kills_container_and_reports_124():
    def boom(argv):
        raise subprocess.TimeoutExpired(argv, timeout=1, output=b"partial", stderr=b"")

    fake = FakeDocker(run=boom)
    result = dcr.run_container(dcr.Quotas(timeout_s=1), CMD, runner=fake)
    assert result.timed_out is True
    assert result.exit_code == dcr.EXIT_TIMEOUT
    assert result.cmd_timeouts == 1
    assert "kill" in fake.verbs() and fake.verbs()[-1] == "rm"
    assert result.stdout == "partial"


def test_daemon_error_is_infrastructure_not_the_command_result():
    fake = FakeDocker(run=lambda argv: subprocess.CompletedProcess(
        argv, 1, stdout="", stderr="failed to connect to the docker API at npipe"))
    result = dcr.run_container(dcr.Quotas(), CMD, runner=fake)
    assert result.daemon_error is True
    assert result.exit_code == dcr.EXIT_INFRA


def test_missing_docker_binary_is_infrastructure():
    # Nenhuma chamada ao docker funciona (run, inspect, rm): o finally não pode vazar exceção.
    calls = []

    def missing(argv, **kwargs):
        calls.append(argv[1])
        raise FileNotFoundError(2, "No such file", "docker")

    result = dcr.run_container(dcr.Quotas(), CMD, runner=missing)
    assert result.daemon_error is True and result.exit_code == dcr.EXIT_INFRA
    assert result.oom_killed is None           # inspect falhou: não medido
    assert result.cleanup_error is not None    # rm falhou: registrado, não ignorado em silêncio
    assert calls == ["run", "inspect", "rm"]  # a limpeza foi tentada mesmo após falha do run


def test_cleanup_failure_after_successful_run_is_infrastructure():
    # O comando terminou bem, mas o container não pôde ser removido: não é resultado limpo.
    def rm_fails(argv, **kwargs):
        if argv[1] == "rm":
            raise subprocess.SubprocessError("daemon went away")
        return subprocess.CompletedProcess(argv, 0, stdout="false\n" if argv[1] == "inspect" else "", stderr="")

    result = dcr.run_container(dcr.Quotas(), CMD, runner=rm_fails)
    assert result.exit_code == dcr.EXIT_INFRA
    assert result.daemon_error is True
    assert "SubprocessError" in result.cleanup_error


def test_cli_without_docker_on_path_returns_125_not_an_exception(monkeypatch, tmp_path, capsys):
    # Sem simulador: subprocess real, com PATH vazio. O docker não é encontrado.
    monkeypatch.setenv("PATH", str(tmp_path))
    code = dcr.main(["--", *CMD])
    assert code == dcr.EXIT_INFRA
    out = capsys.readouterr().out
    assert '"daemon_error": true' in out


def test_failed_inspect_gives_unmeasured_oom_not_false():
    fake = FakeDocker(inspect_rc=1)
    result = dcr.run_container(dcr.Quotas(), CMD, runner=fake)
    assert result.oom_killed is None  # não medido: nunca False por padrão


def test_long_output_is_truncated_with_marker():
    big = "x" * (dcr.OUTPUT_MAX_CHARS + 500)
    fake = FakeDocker(run=lambda argv: subprocess.CompletedProcess(argv, 0, stdout=big, stderr=""))
    result = dcr.run_container(dcr.Quotas(), CMD, runner=fake)
    assert result.stdout.endswith("...[truncated]")
    assert len(result.stdout) == dcr.OUTPUT_MAX_CHARS + len("...[truncated]")


def test_ledger_records_infra_event_with_environment_and_flags(tmp_path):
    fake = FakeDocker(run=lambda argv: subprocess.CompletedProcess(argv, 0, stdout="", stderr=""))
    path = tmp_path / "ledger.jsonl"
    with RunLedger(path) as ledger:
        dcr.run_container(dcr.Quotas(cpus="2", memory="1g", pids_limit=64, timeout_s=90), CMD,
                          ledger=ledger, runner=fake)
    (event,) = [e for e in read_events(path) if e["event"] == "infra_finished"]
    assert event["quotas"] == {"image": dcr.DEFAULT_IMAGE, "cpus": "2", "memory": "1g", "pids_limit": 64,
                               "timeout_s": 90, "network": "none"}
    assert event["oom_killed"] is False and event["timed_out"] is False and event["cmd_timeouts"] == 0
    assert event["daemon_error"] is False


# --- CLI --------------------------------------------------------------------------


def test_dry_run_prints_argv_without_calling_docker(capsys):
    assert dcr.main(["--dry-run", "--", *CMD]) == 0
    out = capsys.readouterr().out
    assert out.startswith("docker run ") and "--network none" in out and out.rstrip().endswith("--workdir /work/canary")


def test_cli_refuses_network_without_allow(capsys):
    assert dcr.main(["--network", "host", "--dry-run", "--", *CMD]) == 2
    assert "network" in capsys.readouterr().err


def test_dockerfile_runs_as_non_root_pins_deps_and_does_not_copy_the_repo():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER runner" in text
    assert "requirements-dv-harness.txt" in text  # dependências fixadas, não versões soltas
    assert "COPY requirements-dv-harness.txt" in text
    assert "COPY . " not in text and "ADD " not in text  # o código entra por bind mount, não por cópia
