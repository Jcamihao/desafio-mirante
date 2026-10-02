"""Camadas de validacao do Python gerado, da mais barata a mais cara.

Cada camada e uma funcao pura `(code, ctx) -> LayerResult`. Nenhuma executa o codigo gerado:
sintaxe/politica usam AST; imports usam `importlib` com allowlist (so bibliotecas conhecidas).
"""

import ast
import importlib
import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field

from mirante.domain.ir import ParsedProcedure
from mirante.domain.validation import Issue, LayerResult

PYTHON_TARGET = (3, 14)
# Bibliotecas que o codigo gerado pode importar (alem da stdlib e das dependencias traduzidas).
THIRD_PARTY_ALLOWLIST = {"sqlalchemy", "psycopg", "pydantic"}
RUFF = [sys.executable, "-m", "ruff", "check", "--isolated", "--no-cache", "--exit-zero"]
# Achados de lint que nao justificam uma nova chamada de LLM (viram avisos / autofix).
COSMETIC_LINT = {"F401", "F841"}


@dataclass
class Context:
    proc: ParsedProcedure
    dependencies: list[ParsedProcedure] = field(default_factory=list)

    @property
    def dependency_names(self) -> set[str]:
        return {d.name for d in self.dependencies}


def _fail_if_errors(name: str, issues: list[Issue], detail: str = "") -> LayerResult:
    failed = any(i.severity == "error" for i in issues)
    return LayerResult(
        name=name, status="failed" if failed else "passed", issues=issues, detail=detail
    )


# --- 1. sintaxe --------------------------------------------------------------------------------
def syntax(code: str) -> LayerResult:
    if sys.version_info[:2] < PYTHON_TARGET:
        return LayerResult(
            name="syntax",
            status="failed",
            issues=[
                Issue(
                    code="PY_VERSION",
                    message=f"Validador roda em Python {sys.version_info[:2]}, precisa 3.14+",
                )
            ],
        )
    try:
        ast.parse(code, feature_version=PYTHON_TARGET)
    except SyntaxError as exc:
        issue = Issue(code="SYNTAX", message=exc.msg, line=exc.lineno)
        return LayerResult(name="syntax", status="failed", issues=[issue])
    return LayerResult(
        name="syntax", status="passed", detail=f"ast.parse em Python {sys.version.split()[0]}"
    )


# --- 2. lint (ruff) + autofix deterministico -----------------------------------------------------
def autofix(code: str) -> tuple[str, list[str]]:
    """Remove imports/variaveis nao usados sem gastar uma chamada de LLM."""
    before = lint_issues(code)
    cosmetic = [i for i in before if i.code in COSMETIC_LINT]
    if not cosmetic:
        return code, []
    result = subprocess.run(
        [*RUFF, "--fix", "--select", "F401", "--stdin-filename", "module.py", "-"],
        input=code,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    fixed = result.stdout if result.stdout.strip() else code
    removed = [i.message for i in cosmetic if i.code == "F401"]
    return fixed, removed if fixed != code else []


def lint_issues(code: str) -> list[Issue]:
    result = subprocess.run(
        [
            *RUFF,
            "--select",
            "F,E9",
            "--output-format",
            "json",
            "--stdin-filename",
            "module.py",
            "-",
        ],
        input=code,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return [
        Issue(
            code=d["code"],
            message=d["message"],
            line=d["location"]["row"],
            severity="warning" if d["code"] in COSMETIC_LINT else "error",
        )
        for d in json.loads(result.stdout or "[]")
    ]


def lint(code: str) -> LayerResult:
    return _fail_if_errors("lint", lint_issues(code), detail="ruff --select F,E9")


# --- 3. imports resolviveis (sem executar o codigo gerado) ---------------------------------------
def imports(code: str, ctx: Context) -> LayerResult:
    issues: list[Issue] = []
    local = ctx.dependency_names  # modulos das dependencias ja traduzidas (um .py por procedure)
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                issues += _check_module(alias.name, node.lineno, local)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            module_issues = _check_module(node.module, node.lineno, local)
            issues += module_issues
            if not module_issues and node.module not in local:
                issues += _check_names(node, importlib.import_module(node.module))
            elif node.module in local:
                issues += [
                    Issue(
                        code="IMPORT_NAME",
                        message=f"'{a.name}' nao existe em '{node.module}'",
                        line=node.lineno,
                    )
                    for a in node.names
                    if a.name != node.module
                ]
    return _fail_if_errors(
        "imports", issues, detail="importlib + allowlist; codigo gerado nao e executado"
    )


def _check_module(name: str, line: int, local: set[str]) -> list[Issue]:
    top = name.split(".")[0]
    if name in local or top in local:
        return []
    if top not in sys.stdlib_module_names and top not in THIRD_PARTY_ALLOWLIST:
        return [
            Issue(code="IMPORT_FORBIDDEN", message=f"modulo '{name}' fora da allowlist", line=line)
        ]
    try:
        if importlib.util.find_spec(name) is None:
            raise ModuleNotFoundError(name)
    except ImportError, ValueError:
        return [Issue(code="IMPORT_MISSING", message=f"modulo '{name}' nao encontrado", line=line)]
    return []


def _check_names(node: ast.ImportFrom, module: object) -> list[Issue]:
    issues = []
    for alias in node.names:
        if alias.name == "*" or hasattr(module, alias.name):
            continue
        try:  # `from pacote import submodulo`
            importlib.import_module(f"{node.module}.{alias.name}")
        except ImportError:
            issues.append(
                Issue(
                    code="IMPORT_NAME",
                    message=f"cannot import name '{alias.name}' from '{node.module}'",
                    line=node.lineno,
                )
            )
    return issues


# --- 4. politica da arquitetura (regras do system prompt, conferidas por AST) --------------------
def policy(code: str, ctx: Context) -> LayerResult:
    tree = ast.parse(code)
    issues = _contract(tree, ctx.proc)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            issues += _call_rules(node)
        elif isinstance(node, ast.Constant) and isinstance(node.value, float):
            issues.append(_float_issue(node.lineno))
    return _fail_if_errors("policy", issues, detail="regras de arquitetura do prompt")


def _float_issue(line: int) -> Issue:
    return Issue(
        code="POLICY_FLOAT",
        message="float usado: valores numeric devem ser Decimal (float perde exatidao)",
        line=line,
        severity="warning",
    )


def _call_rules(node: ast.Call) -> list[Issue]:
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr in {"commit", "rollback"}
        and isinstance(func.value, ast.Name)
        and func.value.id == "conn"
    ):
        return [
            Issue(
                code="POLICY_TX_CONTROL",
                message=f"conn.{func.attr}(): a transacao pertence ao chamador",
                line=node.lineno,
            )
        ]
    if isinstance(func, ast.Name) and func.id == "float":
        return [_float_issue(node.lineno)]
    is_text_call = isinstance(func, ast.Name) and func.id == "text" and bool(node.args)
    if (
        is_text_call
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        untyped = untyped_json_binds(node.args[0].value)
        if untyped:
            return [
                Issue(
                    code="POLICY_UNTYPED_BIND",
                    message=(
                        f"bind(s) {sorted(set(untyped))} em jsonb_build_object sem tipo: psycopg 3 "
                        "falha em runtime (could not determine data type of parameter). "
                        "Use CAST(:p AS text|numeric|date|bigint)"
                    ),
                    line=node.lineno,
                )
            ]
    if is_text_call and not _is_literal_sql(node.args[0]):
        return [
            Issue(
                code="POLICY_SQL_INJECTION",
                message="text() com SQL dinamico (f-string/concat/format): use bind params",
                line=node.lineno,
            )
        ]
    return []


_TYPED_BIND = re.compile(
    r"CAST\s*\(\s*:\w+\s+AS\s+[\w ]+(?:\([\d, ]*\))?\s*\)|:\w+\s*::\s*\w+", re.IGNORECASE
)


def untyped_json_binds(sql: str) -> list[str]:
    """Binds `:nome` dentro de json[b]_build_object(...) sem CAST(... AS tipo) nem `::tipo`."""
    found: list[str] = []
    for match in re.finditer(r"json[b]?_build_object\s*\(", sql, re.IGNORECASE):
        depth, end = 1, match.end()
        while end < len(sql) and depth:
            depth += (sql[end] == "(") - (sql[end] == ")")
            end += 1
        inner = _TYPED_BIND.sub("", sql[match.end() : end - 1])
        found += re.findall(r"(?<!:):(\w+)", inner)
    return found


def _is_literal_sql(arg: ast.expr) -> bool:
    # Variavel (Name): nao da para provar a origem; so rejeitamos montagem inline.
    return isinstance(arg, ast.Constant | ast.Name)


def _contract(tree: ast.Module, proc: ParsedProcedure) -> list[Issue]:
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    fn = functions.get(proc.name)
    if fn is None:
        return [
            Issue(
                code="CONTRACT_MISSING",
                message=f"funcao '{proc.name}' nao definida no topo do modulo",
            )
        ]
    args = [a.arg for a in fn.args.posonlyargs + fn.args.args]
    expected = [p.name for p in proc.params if p.mode in {"IN", "INOUT", "VARIADIC"}]
    issues = []
    if not args or args[0] != "conn":
        issues.append(
            Issue(
                code="CONTRACT_CONN", message="primeiro parametro deve ser 'conn'", line=fn.lineno
            )
        )
    if args[1:] != expected:
        issues.append(
            Issue(
                code="CONTRACT_PARAMS",
                message=f"parametros {args[1:]} != esperados {expected} (nomes/ordem dos IN)",
                line=fn.lineno,
            )
        )
    return issues
