"""Equivalencia comportamental: roda a procedure ORIGINAL (PL/pgSQL) e o Python GERADO sobre os
mesmos dados e compara retorno, erro e estado final das tabelas.

Isolamento: banco temporario, uma schema por lado (`orig` / `gen`), mesmos dados iniciais. As
procedures originais so existem em `orig`: o codigo gerado nao consegue "trapacear" chamando-as.
O codigo gerado roda em subprocesso com ambiente minimo (ver `worker.py`).
"""

import datetime
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from mirante.behavior.scenarios import SCENARIOS, Scenario
from mirante.behavior.worker import canon, canonical_rows
from mirante.evals.cases import SAMPLES_DIR

TABLES = ("clientes", "contas", "transacoes", "taxas", "log_auditoria")
VOLATILE_TS = ("data_transacao", "data_cadastro", "data_abertura")  # podem vir de NOW()
IGNORED = {"criado_em"}  # sempre NOW(), sem valor para comparar
WORKER = Path(__file__).with_name("worker.py")
SQL_ORIGINALS = sorted(SAMPLES_DIR.glob("[B-F]_*.sql"))


@dataclass
class Outcome:
    annex: str
    scenario: str
    targets: str
    passed: bool
    diffs: list[str] = field(default_factory=list)
    original: dict[str, Any] = field(default_factory=dict)
    generated: dict[str, Any] = field(default_factory=dict)


def _raw(conn: Any, sql: str) -> None:
    """Executa DDL/seed direto no driver: o SQLAlchemy tentaria interpolar os `%` dos RAISE."""
    conn.connection.driver_connection.execute(sql)


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, datetime.date):
        return {"$date": value.isoformat()}
    return value


class Harness:
    def __init__(self, admin_url: URL, modules: dict[str, Path]) -> None:
        """`modules`: nome da procedure -> arquivo .py gerado."""
        self._admin_url = admin_url
        self._modules = modules

    # --- ambiente -------------------------------------------------------------------------------
    def _engine(self, db: str, schema: str | None = None) -> Engine:
        """Engine do banco temporario; com `schema`, as consultas enxergam so essa schema."""
        options = {"connect_args": {"options": f"-c search_path={schema}"}} if schema else {}
        return create_engine(self._admin_url.set(database=db), **options)

    def run(self, scenarios: list[Scenario] | None = None) -> list[Outcome]:
        scenarios = scenarios if scenarios is not None else SCENARIOS
        db = f"mirante_behavior_{uuid.uuid4().hex[:8]}"
        admin = create_engine(
            self._admin_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
        )
        with admin.connect() as conn:
            conn.execute(text(f"CREATE DATABASE {db}"))
        module_dir = Path(tempfile.mkdtemp(prefix="mirante_behavior_"))
        try:
            for procedure, path in self._modules.items():
                shutil.copy(
                    path, module_dir / f"{procedure}.py"
                )  # F importa o modulo de B pelo nome
            return [
                self._run_one(db, module_dir, s) for s in scenarios if s.procedure in self._modules
            ]
        finally:
            shutil.rmtree(module_dir, ignore_errors=True)
            with admin.connect() as conn:
                conn.execute(text(f"DROP DATABASE {db} WITH (FORCE)"))
            admin.dispose()

    def _prepare(self, db: str, scenario: Scenario) -> tuple[Engine, Engine]:
        base = self._engine(db)
        with base.begin() as conn:
            _raw(conn, "DROP SCHEMA IF EXISTS orig CASCADE; DROP SCHEMA IF EXISTS gen CASCADE;")
            for schema in ("orig", "gen"):
                _raw(conn, f"CREATE SCHEMA {schema}; SET LOCAL search_path = {schema};")
                _raw(conn, (SAMPLES_DIR / "schema.sql").read_text())
                if schema == "orig":  # so o lado original tem as procedures
                    for sql_file in SQL_ORIGINALS:
                        _raw(conn, sql_file.read_text())
                _raw(conn, scenario.seed_sql)
        base.dispose()
        return self._engine(db, "orig"), self._engine(db, "gen")

    # --- execucao -------------------------------------------------------------------------------
    def _run_one(self, db: str, module_dir: Path, scenario: Scenario) -> Outcome:
        orig_engine, gen_engine = self._prepare(db, scenario)
        original = self._call_original(orig_engine, scenario)
        generated = self._call_generated(db, module_dir, scenario)
        diffs = self._compare(original, generated)
        for table in TABLES:
            before, after = self._dump(orig_engine, table), self._dump(gen_engine, table)
            if before != after:
                diffs.append(f"estado de {table}: original={_short(before)} gerado={_short(after)}")
        orig_engine.dispose()
        gen_engine.dispose()
        return Outcome(
            scenario.annex, scenario.name, scenario.targets, not diffs, diffs, original, generated
        )

    @staticmethod
    def _call_original(engine: Engine, scenario: Scenario) -> dict[str, Any]:
        try:
            with engine.begin() as conn:  # commit se a chamada termina; rollback se levanta
                result = conn.exec_driver_sql(scenario.call_sql, scenario.args)
                rows = result.fetchall() if result.returns_rows else []
        except DBAPIError as exc:
            diag = getattr(exc.orig, "diag", None)
            return {
                "ok": False,
                "error": {
                    "sqlstate": getattr(exc.orig, "sqlstate", None),
                    "message": getattr(diag, "message_primary", str(exc)),
                },
            }
        return {"ok": True, "rows": canonical_rows([tuple(r) for r in rows])}

    def _call_generated(self, db: str, module_dir: Path, scenario: Scenario) -> dict[str, Any]:
        payload = {
            "url": self._admin_url.set(database=db).render_as_string(hide_password=False),
            "schema": "gen",
            "module_dir": str(module_dir),
            "module": scenario.procedure,
            "function": scenario.procedure,
            "args": [_encode(a) for a in scenario.args],
        }
        # `-I`: modo isolado. Ambiente minimo: nenhuma chave de API chega ao codigo gerado.
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-I", str(WORKER)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=60,
            env={"PATH": "/usr/bin:/bin"},
            check=False,
        )
        try:
            result: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
            return result
        except IndexError, json.JSONDecodeError:
            return {
                "ok": False,
                "error": {"type": "WorkerCrash", "message": proc.stderr[-300:], "sqlstate": None},
            }

    @staticmethod
    def _dump(engine: Engine, table: str) -> list[Any]:
        with engine.connect() as conn:
            now: datetime.datetime = conn.execute(text("SELECT localtimestamp")).scalar_one()
            result = conn.exec_driver_sql(f"SELECT * FROM {table} ORDER BY id")  # noqa: S608
            columns = list(result.keys())
            rows = []
            for raw in result.fetchall():
                row = {}
                for name, value in zip(columns, raw, strict=True):
                    if name in IGNORED:
                        continue
                    recent = (
                        isinstance(value, datetime.datetime)
                        and abs((now - value).total_seconds()) < 300
                    )
                    row[name] = "<agora>" if name in VOLATILE_TS and recent else canon(value)
                rows.append(row)
            return rows

    @staticmethod
    def _compare(original: dict[str, Any], generated: dict[str, Any]) -> list[str]:
        if original["ok"] != generated["ok"]:
            return [f"desfecho: original={_brief(original)} gerado={_brief(generated)}"]
        if original["ok"]:
            if original["rows"] != generated["rows"]:
                return [f"retorno: original={original['rows']} gerado={generated['rows']}"]
            return []
        o, g = original["error"], generated["error"]
        g_state = g["sqlstate"] or "P0001"  # excecao do proprio codigo = RAISE EXCEPTION
        if o["sqlstate"] != g_state:
            return [
                f"erro: original={o['sqlstate']} gerado={g['type']}({g_state}): {g['message'][:90]}"
            ]
        if o["sqlstate"] == "P0001" and o["message"] != g["message"]:
            return [f"mensagem: original={o['message']!r} gerado={g['message']!r}"]
        return []


def _brief(result: dict[str, Any]) -> str:
    if result["ok"]:
        return f"ok{result['rows']}"
    e = result["error"]
    return f"erro[{e.get('sqlstate') or e.get('type')}] {str(e['message'])[:70]}"


def _short(value: Any) -> str:
    s = json.dumps(value, ensure_ascii=False, default=str)
    return s if len(s) <= 230 else s[:230] + "..."


def discover_modules(results_dir: Path) -> dict[str, Path]:
    """`results/<anexo>_<procedure>.py` -> {procedure: caminho}."""
    modules = {}
    for path in sorted(results_dir.glob("[B-F]_*.py")):
        modules[path.stem.split("_", 1)[1]] = path
    return modules
