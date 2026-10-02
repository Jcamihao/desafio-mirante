"""Integracao com o Langfuse self-hosted (docker compose). Pulado se nao estiver no ar."""

import asyncio
import base64
import json
import time
import urllib.request

import httpx
import pytest
from langfuse import Langfuse

from mirante.api.schemas import ModernizeRequest
from mirante.graph.builder import build_graph
from mirante.langfuse_setup import apply
from mirante.observability import LangfuseTracer
from mirante.service import ModernizationService
from tests.fakes import BAD_IMPORT_B, GOOD_B, InMemoryRepository, StubGenerator

pytestmark = pytest.mark.integration
HOST, PK, SK = "http://localhost:3000", "pk-lf-mirante-local", "sk-lf-mirante-local"


@pytest.fixture(scope="module")
def client():
    try:
        urllib.request.urlopen(f"{HOST}/api/public/health", timeout=3)
    except OSError:
        pytest.skip("Langfuse indisponivel; suba com `docker compose up -d`")
    # environment="test": os traces dos testes ficam separados dos de uso real no Langfuse
    return Langfuse(public_key=PK, secret_key=SK, host=HOST, environment="test")


def fetch_trace(trace_id: str, ready) -> dict:
    """O Langfuse ingere de forma assincrona (worker + ClickHouse): espera ate ficar completo."""
    auth = "Basic " + base64.b64encode(f"{PK}:{SK}".encode()).decode()
    deadline = time.time() + 60
    trace: dict = {}
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                f"{HOST}/api/public/traces/{trace_id}", headers={"Authorization": auth}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                trace = json.loads(response.read())
            if ready(trace):
                return trace
        except OSError:
            pass
        time.sleep(2)
    return trace


def run_pipeline(client, sample, stub):
    repo = InMemoryRepository()
    graph = build_graph(lambda: stub, lambda: repo)
    service = ModernizationService(graph, repo, LangfuseTracer(client, PK))
    response = asyncio.run(service.run(ModernizeRequest(source_code=sample("B"))))
    client.flush()
    return response, response.report["meta"]["langfuse_trace_id"]


def test_um_trace_por_execucao_com_span_por_no_e_scores(client, sample):
    response, trace_id = run_pipeline(client, sample, StubGenerator(GOOD_B))
    trace = fetch_trace(trace_id, lambda t: len(t.get("scores", [])) >= 4 and t.get("observations"))
    spans = {o["name"] for o in trace["observations"]}
    assert {"parsing", "analysis", "generation", "validation", "persist"} <= spans
    assert trace["name"] == "modernize" and "dialect:postgres" in trace["tags"]
    scores = {s["name"]: s for s in trace["scores"]}
    assert (
        scores["validation_passed"]["value"] == 1
        and scores["pipeline_status"]["stringValue"] == "SUCCESS"
    )
    assert scores["generation_attempts"]["value"] == 1


def test_laco_de_reparo_aparece_no_trace(client, sample):
    _, trace_id = run_pipeline(client, sample, StubGenerator(BAD_IMPORT_B, GOOD_B))
    trace = fetch_trace(trace_id, lambda t: len(t.get("scores", [])) >= 4)
    names = [o["name"] for o in trace["observations"]]
    assert names.count("generation") == 2 and names.count("validation") == 2
    scores = {s["name"]: s["value"] for s in trace["scores"]}
    assert scores["generation_attempts"] == 2  # a metrica expoe quantos reparos foram necessarios


def test_custo_inclui_os_tokens_de_raciocinio(client):
    """Mesmos tokens de uma chamada real do Gemini 2.5 Flash (medida em 2026-10-01)."""
    with httpx.Client(base_url=HOST, auth=(PK, SK), timeout=15) as http:
        apply(http)
    trace_id = Langfuse.create_trace_id()
    with client.start_as_current_observation(
        name="custo",
        as_type="generation",
        model="gemini-2.5-flash",
        usage_details={"input": 1914, "output": 1485, "output_reasoning": 1422},
        trace_context={"trace_id": trace_id},
    ):
        pass
    client.flush()
    trace = fetch_trace(
        trace_id,
        lambda t: t.get("observations") and t["observations"][0].get("calculatedTotalCost"),
    )
    expected = 1914 * 0.30e-6 + (1485 + 1422) * 2.50e-6
    assert trace["observations"][0]["calculatedTotalCost"] == pytest.approx(expected, rel=1e-6)
