from mirante.domain.validation import LayerResult, ValidationReport
from mirante.validation import layers
from mirante.validation.layers import Context

_AFTER_SYNTAX = ("lint", "imports", "policy")


def validate(code: str, ctx: Context) -> tuple[ValidationReport, str]:
    """Roda as camadas e devolve (relatorio, codigo possivelmente corrigido pelo autofix).

    So a sintaxe interrompe: as demais sao independentes, e juntar todos os erros numa unica
    rodada economiza chamadas de LLM no laco de reparo.
    """
    report = ValidationReport()
    syntax = layers.syntax(code)
    report.layers.append(syntax)
    if syntax.status == "failed":
        report.layers += [LayerResult(name=n, status="skipped") for n in _AFTER_SYNTAX]
        return report, code

    code, report.autofixed = layers.autofix(code)
    report.layers += [layers.lint(code), layers.imports(code, ctx), layers.policy(code, ctx)]
    return report, code
