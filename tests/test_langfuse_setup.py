from mirante.config import Settings
from mirante.langfuse_setup import MODELS, PER_M, definition


def test_definicao_cobra_raciocinio_como_saida():
    """Sem output_reasoning o Langfuse subestima o custo do Gemini 2.5 em ~45% (medido)."""
    (tier,) = definition("gemini-2.5-flash", 0.30, 2.50)["pricingTiers"]
    assert tier["prices"]["output_reasoning"] == tier["prices"]["output"] == 2.50 * PER_M
    assert tier["prices"]["input"] == 0.30 * PER_M and tier["isDefault"]


def test_padrao_do_nome_casa_variantes_e_nao_prefixos():
    import re

    pattern = definition("gemini-2.5-flash", 1, 1)["matchPattern"]
    assert re.match(pattern, "gemini-2.5-flash") and re.match(pattern, "models/Gemini-2.5-Flash")
    assert not re.match(pattern, "gemini-2.5-flash-lite")  # o "." e escapado e o fim e ancorado
    assert not re.match(pattern, "gemini-2x5-flash")


def test_todo_modelo_do_fallback_tem_preco_cadastrado():
    s = Settings(_env_file=None)
    assert {s.resolved_model, *s.fallback_models} <= set(MODELS)
