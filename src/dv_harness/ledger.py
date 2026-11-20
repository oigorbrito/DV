"""Ledger incremental de execução (I1, D7).

Cada evento é uma linha JSON gravada com ``flush`` e ``fsync`` antes que a execução
prossiga. Se o processo for interrompido no meio de uma chamada, o ledger contém
``call_started`` sem ``call_finished``. ``reconcile`` conta essas chamadas como
``in_flight``, para que nenhum recurso consumido desapareça em silêncio.

Eventos (campo ``event``):
- ``run_started``: configuração da execução (sem variáveis de ambiente).
- ``call_started``: uma chamada ao LLM começou (antes de qualquer tentativa).
- ``call_finished``: a chamada terminou, com sucesso ou falha. Carrega o registro completo.
- ``tool_finished``: uma ferramenta executou. Carrega a saída bruta completa e seu SHA-256.
- ``run_finished``: estado final do executor.

Limitação: o ledger contém saída bruta de ferramentas. Se um arquivo lido contiver
segredo, ele estará no ledger. Essa é a contrapartida da auditoria bruta exigida pelo
protocolo, e o diretório do ledger deve ser tratado como artefato sensível.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from collections.abc import Iterator
from typing import Any

LEDGER_SCHEMA = "dv-run-ledger-v2"


class RunLedger:
    """Anexa eventos JSONL com durabilidade por linha."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def append(self, event: str, payload: dict[str, Any]) -> None:
        line = json.dumps({"schema": LEDGER_SCHEMA, "event": event, **payload},
                          ensure_ascii=False, sort_keys=True)
        self._fh.write(line + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.close()  # idempotente: file objects aceitam close() repetido

    def __enter__(self) -> RunLedger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_events(path: str | os.PathLike[str]) -> Iterator[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON line: {exc}") from exc


def reconcile(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Resume o ledger. Invariante I1(a): chamadas iniciadas = finalizadas + em voo.

    Retorna contagens e a lista de índices de chamadas em voo. Uma chamada em voo tem
    consumo desconhecido e precisa ser declarada como não medida pelo avaliador.
    """
    started: set[int] = set()
    finished: dict[int, dict[str, Any]] = {}
    tool_events = 0
    run_finished = 0
    for ev in read_events(path):
        kind = ev.get("event")
        if kind == "call_started":
            started.add(ev["call_index"])
        elif kind == "call_finished":
            finished[ev["call_index"]] = ev["record"]
        elif kind == "tool_finished":
            tool_events += 1
        elif kind == "run_finished":
            run_finished += 1
    in_flight = sorted(started - set(finished))
    return {
        "calls_started": len(started),
        "calls_finished": len(finished),
        "in_flight_call_indices": in_flight,
        "tool_events": tool_events,
        "run_finished_events": run_finished,
        "finished_records": finished,
    }
