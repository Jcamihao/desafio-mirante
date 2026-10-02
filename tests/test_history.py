from mirante.domain.history import compute_status, total_duration_ms
from mirante.domain.state import StageError
from mirante.domain.status import Status
from mirante.domain.validation import Issue, LayerResult, ValidationReport


def validation(passed: bool) -> ValidationReport:
    layer = LayerResult(
        name="syntax",
        status="passed" if passed else "failed",
        issues=[] if passed else [Issue(code="X", message="m")],
    )
    return ValidationReport(layers=[layer])


class TestComputeStatus:
    def test_sucesso(self):
        assert (
            compute_status({"generated_code": "x", "validation": validation(True)})
            == Status.SUCCESS
        )

    def test_parcial_gerou_mas_nao_validou(self):
        assert (
            compute_status({"generated_code": "x", "validation": validation(False)})
            == Status.PARTIAL
        )

    def test_falha_sem_codigo(self):
        assert compute_status({}) == Status.FAILURE

    def test_erro_de_estagio_vence_mesmo_com_codigo(self):
        state = {
            "generated_code": "x",
            "validation": validation(True),
            "errors": [StageError(stage="s", message="m")],
        }
        assert compute_status(state) == Status.FAILURE

    def test_codigo_sem_validacao_nao_conta_como_sucesso(self):
        assert compute_status({"generated_code": "x"}) == Status.PARTIAL


def test_duracao_soma_estagios_e_todas_as_tentativas():
    report = {
        "parsing": {"duration_ms": 10.4},
        "analysis": {"duration_ms": 5},
        "validation": {"duration_ms": 20},
        "generation": {"attempts": [{"duration_ms": 1000}, {"duration_ms": 2000}, {"error": "x"}]},
    }
    assert total_duration_ms(report) == 3035 and total_duration_ms({}) == 0
