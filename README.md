# Mirante SQL Modernizer

Pipeline **híbrido (LLM + regras)** que moderniza stored procedures **PL/pgSQL → Python 3.14**, orquestrado com
**LangGraph**, exposto por `POST /modernize` e `GET /health`, com histórico em **PostgreSQL**, observabilidade em
**Langfuse** (self-hosted) e uma **métrica de avaliação** — estática e comportamental — sobre os Anexos B a F.

> **Resultado em uma linha:** os 5 anexos geram código que passa nas 4 camadas de validação, e o código gerado é
> **comportamentalmente equivalente à procedure original em `22/22` cenários** rodados num PostgreSQL real
> (o que a validação estática sozinha não consegue garantir — ver [§7](#7-resultados-dos-anexos-b-a-f)).

**Sumário:** [1. Fluxo](#1-fluxo-de-modernização) · [2. Como rodar](#2-como-rodar-e-testar) ·
[3. API](#3-api) · [4. Decisões técnicas](#4-decisões-técnicas-e-trade-offs) ·
[5. Armadilhas do enunciado](#5-armadilhas-do-enunciado-e-como-o-pipeline-lida-com-elas) ·
[6. Observabilidade](#6-observabilidade-langfuse) · [7. Resultados](#7-resultados-dos-anexos-b-a-f) ·
[8. Métrica de avaliação](#8-métrica-de-avaliação) · [9. Escalabilidade](#9-escalabilidade-e-crescimento) ·
[10. Limitações](#10-limitações-conhecidas-e-próximos-passos) · [11. Uso de IA](#11-uso-de-ia-e-transparência)

---

## 1. Fluxo de modernização

Cada etapa é um **nó do grafo LangGraph com estado tipado** (`PipelineState`, em `domain/state.py`). Os nós *lêem* o
estado e devolvem só o que mudam; `errors` e `report` acumulam entre nós por *reducers*.

```mermaid
graph TD;
    __start__([início]) --> parsing(parsing);
    parsing -->|ok| analysis(analysis);
    parsing -.->|falha| persist(persist);
    analysis -->|ok| generation(generation);
    analysis -.->|falha| persist;
    generation -->|ok| validation(validation);
    generation -.->|falha do LLM| persist;
    validation -->|"repair (erros + código anterior)"| generation;
    validation -->|"done (passou ou esgotou tentativas)"| persist;
    persist --> __end__([fim]);
```

> Diagrama equivalente ao gerado pelo próprio código: `build_graph().get_graph().draw_mermaid()`.

| Nó | O que faz | Determinístico? |
|---|---|---|
| **parsing** | `pglast` (parser do próprio PostgreSQL) → IR: assinatura (IN/OUT/TABLE), variáveis **com precisão** (`numeric(18,2)`), árvore de comandos, SQL embutido (tabelas, CTEs, locks, funções) e schema opcional (Anexo A). | sim |
| **analysis** | Registro de **12 regras** (20 tipos de risco) sobre a IR → *construções* + **riscos de tradução** (com severidade e **recomendação**). Ex.: cursor N+1, arredondamento, `FOR UPDATE`, handler que engole o próprio `RAISE`. | sim |
| **generation** | LLM (Gemini; trocável) com prompt montado **só** da IR + riscos + tabelas referenciadas + assinatura das dependências. **A procedure bruta nunca é enviada.** | não |
| **validation** | 4 camadas **sem executar** o código: sintaxe 3.14 → lint (ruff) + *autofix* → imports reais (allowlist) → política da arquitetura (por AST). Se falhar, **volta ao gerador** com o erro e o código anterior (até 3 tentativas). | sim |
| **persist** | Grava a execução em `modernization_history` — **todo caminho do grafo termina aqui**: sucesso, parcial ou falha. | sim |

**Escolha arquitetural central (o enunciado pede que seja justificada — ver §4): o SQL continua SQL.** As queries são executadas pelo banco via
SQLAlchemy `text()` com *bind params*; só o **controle de fluxo** (IF/CASE/LOOP/RAISE/EXCEPTION/atribuições) é reescrito
em Python. Isso preserva a equivalência de CTE recursiva, agregações e `FOR UPDATE`, ao custo de o código gerado precisar
de um banco para rodar. A exceção consciente é o cursor com N+1 (Anexo E), traduzido para carga em lote.

---

## 2. Como rodar e testar

**Requisitos:** Docker (com Compose) e uma chave de LLM. Para desenvolvimento/testes: [`uv`](https://docs.astral.sh/uv/)
(ele instala o Python 3.14 sozinho).

### 2.1 Tudo no Docker Compose (recomendado)

```bash
cp .env.example .env                    # preencha GOOGLE_API_KEY (ou ANTHROPIC_API_KEY + LLM_PROVIDER=anthropic)
docker compose up -d --build            # api + postgres + langfuse (web, worker, postgres, clickhouse, redis, minio)
docker compose exec api python -m mirante.langfuse_setup   # 1x: cadastra o preço dos modelos no Langfuse (custo)
curl http://localhost:2024/health
```

Sobe: **API** em `:2024` · **PostgreSQL** da aplicação em `:5432` · **Langfuse** em `http://localhost:3000`
(login de desenvolvimento `dev@mirante.local` / `mirante-dev-1234`; projeto e chaves são criados no primeiro start).
A tabela `modernization_history` é criada pelo `docker-entrypoint-initdb.d` **e** pelo migrador da API no *startup*
(idempotente — necessário porque o initdb só roda com volume vazio).

### 2.2 Desenvolvimento local (servidor LangGraph fora do Docker)

```bash
uv sync                                  # Python 3.14 + dependências
docker compose up -d db langfuse-web langfuse-worker   # só a infraestrutura
uv run python -m mirante.persistence.migrate           # aplica db/init/*.sql
uv run langgraph dev --no-browser --port 2024          # servidor LangGraph + endpoints /modernize e /health
```

Sem Docker, há um Postgres embutido para desenvolvimento: `uv run --no-project --python 3.12 --with pgserver python scripts/dev_postgres.py start`
(imprime a `DATABASE_URL`).

### 2.3 Variáveis de ambiente

Todas em [`.env.example`](.env.example) (comentado). As principais:

| Variável | Para quê | Padrão |
|---|---|---|
| `LLM_PROVIDER` / `GOOGLE_API_KEY` / `ANTHROPIC_API_KEY` | provedor e chave do LLM | `gemini` |
| `LLM_MODEL` / `LLM_FALLBACK_MODELS` | modelo principal e cadeia de *fallback* | `gemini-3.6-flash` / `gemini-3.5-flash,gemini-3-flash-preview` |
| `DATABASE_URL` | PostgreSQL da aplicação | `postgresql+psycopg://mirante:mirante@localhost:5432/mirante` |
| `LANGFUSE_HOST` / `_PUBLIC_KEY` / `_SECRET_KEY` | tracing (opcional: sem chaves, desliga sozinho) | `http://localhost:3000` / chaves locais do compose |
| `MAX_GENERATION_ATTEMPTS` | 1 geração + reparos | `3` |
| `MAX_CONCURRENT_RUNS` | execuções simultâneas | `4` |

### 2.4 Testes e qualidade

```bash
uv run pytest                      # unitários: offline, sem LLM, sem banco (~20 s)
uv run pytest -m integration       # + PostgreSQL e Langfuse reais (pulados se ausentes)
uv run ruff check . && uv run mypy # lint e tipos (ambos limpos)
uv run python -m mirante.evals     # avaliação estática B–F (chama o LLM)
uv run python -m mirante.behavior --results results   # equivalência comportamental (PostgreSQL real, sem LLM)
```

**232** testes, ~91% de cobertura. Os testes unitários usam um LLM falso (`StubGenerator`) e um repositório em
memória: nenhum depende de rede, chave ou custo.

### 2.5 Banco de dados — tabela `modernization_history`

Criada por [`db/init/001_modernization_history.sql`](db/init/001_modernization_history.sql) (idempotente; migrações em
`db/init/`, aplicadas e rastreadas em `schema_migrations`). **Toda execução é gravada, qualquer que seja o desfecho.**

| Campo | Tipo | Conteúdo |
|---|---|---|
| `id` | `UUID` (PK) | identificador único |
| `source_code` | `TEXT NOT NULL` | a stored procedure enviada (sempre preservada, mesmo se o SQL for inválido) |
| `generated_code` | `TEXT` | o Python 3.14 produzido (`NULL` se não chegou a gerar) |
| `report` | `JSONB NOT NULL` | relatório estruturado das etapas: `parsing`, `analysis`, `generation`, `validation`, `errors`, `meta` |
| `status` | enum | `SUCCESS` (sucesso) · `PARTIAL` (parcial: gerou, mas a validação não convergiu) · `FAILURE` (falha) |
| `created_at` | `TIMESTAMPTZ` | momento da execução |
| `procedure_name`, `dialect`, `duration_ms` | — | colunas derivadas do relatório, para consultar/agregar sem varrer o JSONB |

Índices: `created_at`, `(status, created_at)`, `procedure_name` e **GIN** em `report` (consultas como
`report @> '{"validation": {"status": "passed"}}'`). A tabela `evaluation_runs` (migração `002`) guarda as avaliações.

---

## 3. API

Montada no servidor LangGraph via `langgraph.json` (`http.app`): **as rotas do LangGraph (`/runs`, `/threads`, `/ok`…)
continuam disponíveis** e o grafo aparece em `/assistants` como `modernizer`. OpenAPI em `/docs`.

| Endpoint | |
|---|---|
| `POST /modernize` | Recebe `{source_code, dialect?, schema_sql?, dependencies?, max_attempts?}` e devolve `{id, status, generated_code, report, errors}`. |
| `GET /health` | `{"status": "ok"\|"degraded", "version", "components": {database, llm_provider, llm_model, llm_configured}}`. `degraded` = o banco caiu (o pipeline ainda atende, mas não grava). Nunca expõe o valor da chave. |
| `GET /history`, `GET /history/{id}` | Execuções gravadas. |
| `POST /evaluate`, `GET /evaluate[/{id}]` | Roda e consulta a avaliação sobre B–F (lento: 1 chamada de LLM por caso). |

```bash
# Anexo B, com o schema (Anexo A) como contexto opcional
jq -n --rawfile sql samples/B_fn_saldo_cliente.sql --rawfile schema samples/schema.sql \
   '{source_code: $sql, schema_sql: $schema}' \
 | curl -s -X POST localhost:2024/modernize -H 'content-type: application/json' -d @- | jq '.status, .report.validation.status'

# Anexo F: chama fn_saldo_cliente (B) — passe a definição em `dependencies`
jq -n --rawfile f samples/F_sp_relatorio_mensal_cliente.sql --rawfile b samples/B_fn_saldo_cliente.sql \
   --rawfile schema samples/schema.sql '{source_code: $f, schema_sql: $schema, dependencies: [$b]}' \
 | curl -s -X POST localhost:2024/modernize -H 'content-type: application/json' -d @- | jq -r .generated_code
```

Sem `jq`, o mesmo pedido em Python (`httpx` já é dependência do projeto):

```python
# uv run python
import httpx, pathlib
sql = lambda name: next(pathlib.Path("samples").glob(f"{name}_*.sql")).read_text()
r = httpx.post("http://localhost:2024/modernize", timeout=600, json={
    "source_code": sql("F"),
    "schema_sql": pathlib.Path("samples/schema.sql").read_text(),
    "dependencies": [sql("B")],            # F chama fn_saldo_cliente (Anexo B)
})
print(r.status_code, r.json()["status"]); print(r.json()["generated_code"])
```

**O código HTTP diz *de quem* foi a falha; o corpo é sempre o mesmo** (com o relatório): `200` sucesso **ou parcial**
(há código utilizável; o parcial vem sinalizado em `status`) · `422` SQL de entrada inválido · `502` falha do provedor
de LLM · `500` falha interna (a execução fica registrada mesmo assim).

---

## 4. Decisões técnicas e trade-offs

O log completo e datado (com **o que foi medido vs. presumido**) está em **[`docs/decisions.md`](docs/decisions.md)**
(34 decisões). As que mais pesam:

| Decisão | Por quê | Trade-off |
|---|---|---|
| **`pglast`**, não `sqlglot` | *Medido:* o `sqlglot` devolve o corpo `$$…$$` como um `Heredoc` **opaco** (sem AST de IF/LOOP/FETCH/RAISE). O `pglast` usa o parser do próprio PostgreSQL. | Só fala PostgreSQL → novos dialetos entram como novo `SqlParser` (interface + registro). |
| **SQL como texto, controle de fluxo em Python** | Equivalência de queries (CTE recursiva, `FOR UPDATE`) e 1 *round-trip*. | O Python gerado exige um banco para rodar. |
| **IR + riscos no prompt; nunca a procedure crua** | Contexto enxuto e verificável; **comentários SQL nunca chegam ao modelo** (vetor de *prompt injection*). O system prompt declara tudo como *dado*. | Depende da qualidade da IR (já houve bug: ver §10). |
| **Análise = regras pequenas num registro** | Cada risco é uma função; novo risco/dialeto não muda o motor. Regras que dependem do schema são **puladas explicitamente** sem ele. | Heurística: tem falsos negativos (não vê semântica de negócio). |
| **Validação em camadas, sem executar** | `ast.parse` sozinho é **enganoso** (medido: aprovou 5/5 quando só 4/5 importavam). Imports checados por `importlib` com allowlist; política por AST. | Não prova comportamento (por isso existe a camada comportamental). |
| **Laço de reparo** `generation ⇄ validation` | Corrige erros determinísticos (ex.: `from dataclasses import frozen`) sem intervenção. | Cada tentativa custa uma chamada de LLM. |
| **Autofix determinístico** | Imports não usados saem por `ruff --fix`, sem gastar uma geração de ~40 s. | — |
| **Todo caminho → `persist`** | Requisito "persistir independente do desfecho". Crash de nó é capturado pelo `service.py`, que grava um `FAILURE`. | Se o **banco** cair, a execução não é registrada (sem *outbox*). |
| **LLM atrás de `CodeGenerator`** | Provedor trocável (Gemini/Claude), testes offline, *fallback* de modelos. | — |
| **Resiliência do LLM medida, não suposta** | *Medido:* free tier = 20 req/dia/modelo; a família 2.5 dá 404 em projetos novos; modelos 3.x oscilam em 503; o 429 diário traz um `retry in 1.6s` **enganoso**. → disjuntor por modelo, espera só no 429 por minuto, 2ª rodada com *backoff*. | Mais código de política; modelos *preview* podem sumir. |
| **Langfuse self-hosted** | O SQL de bancos legados é sensível: não sai da rede; sem custo por *trace*. | 6 serviços só para observabilidade. |

### 4.1 Bibliotecas externas e por que cada uma

O enunciado permite bibliotecas desde que justificadas. As de **execução** (em `pyproject.toml`):

| Biblioteca | Para quê | Por que ela |
|---|---|---|
| `langgraph` · `langgraph-cli[inmem]` | grafo de nós com estado tipado · servidor local (`langgraph dev`) | **exigidas pelo enunciado**; o CLI monta o servidor onde a API é plugada (`http.app`). |
| `pglast` | parsing do PL/pgSQL → AST | usa o parser do próprio PostgreSQL (`parse_plpgsql`); o `sqlglot` foi **medido e descartado** (corpo da procedure opaco). `sqlparse` não foi testado: só tokeniza, sem AST. |
| `langchain-google-genai` · `langchain-anthropic` | cliente do LLM (Gemini = padrão; Claude = alternativo) | interface única `with_structured_output`, que dá saída tipada e uso de tokens; troca de provedor por variável de ambiente. |
| `langchain` | — | **dependência obrigatória do handler do Langfuse** (o handler não importa sem ela). |
| `langfuse` | traces, spans, custo e scores | bônus do enunciado; self-hosted. |
| `sqlalchemy` · `psycopg[binary]` | acesso ao PostgreSQL | o código **gerado** usa SQLAlchemy Core (`text()` + binds), e a persistência também; `psycopg` 3 é o driver atual. |
| `fastapi` · `pydantic` · `pydantic-settings` | API · IR, estado e contratos tipados · configuração por ambiente | validação e OpenAPI de graça; Pydantic serializa direto para o JSONB e para o prompt. |
| `ruff` | lint + *autofix* **dentro da camada de validação** | por isso é dependência de execução, não só de desenvolvimento. |
| `httpx` | cliente HTTP | cadastro de preços no Langfuse (`langfuse_setup`) e exemplos da API. |

**Só desenvolvimento:** `pytest`, `pytest-cov`, `mypy`. **Fora do projeto** (usados por script, não instalados): `pgserver`
(Postgres embutido para dev sem Docker, via `uv run --with`) e `puppeteer-core` (apenas para os *screenshots* do Langfuse).

---

## 5. Armadilhas do enunciado (e como o pipeline lida com elas)

Lendo o SQL dos anexos linha a linha apareceram comportamentos que **uma tradução "óbvia" erra**. Cada um virou uma
regra de análise, um cenário do harness comportamental e um teste. **Os itens 1 e 2 foram verificados num PostgreSQL 16**
(não deduzidos do manual — ver a correção em §10).

| # | Anexo | Comportamento real | Como o pipeline trata |
|---|---|---|---|
| 1 | F | O `RAISE EXCEPTION 'Periodo invalido'` está **dentro** do bloco com `EXCEPTION WHEN OTHERS`: é **capturado pelo próprio handler** e a função devolve a linha de *fallback* — **não levanta erro**. | `RAISE_CAUGHT_BY_OWN_HANDLER` (alta) + `DEGRADED_FALLBACK`. |
| 2 | D | O handler grava `TRANSFERENCIA_ERRO` e re-lança. **Verificado:** o log **nunca persiste** (0 linhas), mesmo se o chamador capturar o erro por `EXCEPTION` ou `SAVEPOINT`. | `HANDLER_WRITES_ROLLED_BACK`: função inteira num `begin_nested()` externo. |
| 3 | D | Destino inexistente ⇒ `v_status_destino` é NULL ⇒ `NULL <> 'ATIVA'` é NULL ⇒ o `IF` **não dispara**; o erro vem depois, da FK. Em Python `None != 'ATIVA'` é `True`. | `NULL_COMPARISON` (lógica de 3 valores). |
| 4 | E | Atribuir a `NUMERIC(18,2)` **arredonda a cada atribuição**, *half away from zero*; `Decimal.quantize` usa *half-even*. (`40,20 × 2,5% = 1,005` → PG: `1,01`; Python padrão: `1,00`.) | `NUMERIC_ROUNDING` → `ROUND_HALF_UP` a cada atribuição. |
| 5 | E | Taxa calculada `0` viola `CHECK (valor > 0)` e **aborta o lote inteiro** (sem handler). | `CHECK_CONSTRAINT` (alta, lê o schema do Anexo A). |
| 6 | E | Cursor + 1 `SELECT` + 3 DML por iteração = **N+1**; o cursor lê um *snapshot* do `OPEN`. | `CURSOR_N_PLUS_1` → carga em lote preservando o snapshot. |
| 7 | F | `saldo_consolidado` = saldo **atual** ± movimento do mês (**não** é acumulado). Parece bug, mas a tradução deve **preservar**. | Fora do alcance de regra estática; coberto pelo cenário comportamental. |
| 8 | C/D | `RAISE '%'` com NULL imprime **`<NULL>`**; o Python imprimiria `None`. | `RAISE_NULL_FORMAT` (achado pelo harness). |
| 9 | C/D/E | `jsonb_build_object(:p)` com *bind* sem tipo **quebra em runtime no psycopg 3** (`could not determine data type of parameter`). | Regra no prompt + `POLICY_UNTYPED_BIND` na validação (achado pelo harness). |

---

## 6. Observabilidade (Langfuse)

Um **trace por execução** (`modernize`), um **span por nó do grafo**, chamadas de LLM como `GENERATION` com **tokens,
latência e custo**, e **scores** por execução (`validation_passed`, `generation_attempts`, `high_risks_detected`,
`pipeline_status`). O `trace_id` é gravado em `report.meta.langfuse_trace_id`, ligando a linha do histórico ao trace.
Os traces de uma avaliação compartilham uma **sessão** (`eval-…`) que recebe os scores agregados.

![Lista de traces no Langfuse](docs/images/langfuse-traces.png)

![Trace de uma execução: spans por nó, tokens, latência, custo, fallback entre modelos](docs/images/langfuse-trace-detail.png)

![Sessão de uma avaliação: 5 traces, custo total e scores agregados](docs/images/langfuse-session.png)

*A sessão `eval-a465b525` é a rodada em que E e F caíram por 503 (daí `valid_rate 0,60`, `risk_recall 1,00`): ilustra a
separação entre indisponibilidade do provedor e qualidade do código. E e F foram refeitos em outra sessão.*

Notas verificadas (com testes de integração contra o Langfuse real):

- **Custo:** a versão self-hosted só conhece Gemini 1.x → custo **0** para 2.5/3.x. `mirante.langfuse_setup` cadastra os
  preços de lista (conferidos em ai.google.dev). O cadastro precisa de `pricingTiers` com `output_reasoning`: o campo
  `prices` é descartado e, sem o raciocínio, o custo sai **45% menor**. No *free tier* o custo real é zero; o valor
  exibido é o **equivalente pago**.
- O laço de reparo aparece como 2× `generation` e 2× `validation` no mesmo trace; o *fallback* entre modelos aparece como
  uma `GENERATION` em `ERROR` seguida de outra com sucesso.
- Observabilidade **nunca derruba o pipeline**: falhas viram log.

---

## 7. Resultados dos Anexos B a F

Código gerado e relatórios em **[`results/`](results/)** — por anexo, `<anexo>_<procedure>.py` (módulo Python) e
`<anexo>_<procedure>.report.json` (o mesmo relatório gravado em `modernization_history`), mais `evaluation.json` e
`SUMMARY.md`. Gerados por `uv run python -m mirante.evals` + `scripts/export_results.py`.

| Anexo | Procedure | Modelo que gerou | Tent. | Sintaxe | Lint | Imports | Política | Riscos detectados | Tokens (in/out) | Tempo |
|---|---|---|---|---|---|---|---|---|---|---|
| **B** | `fn_saldo_cliente` | gemini-3.5-flash | 1 | ✅ | ✅ | ✅ | ✅ | 0/0 | 1.437/2.181 | 27 s |
| **C** | `sp_atualizar_status_contas_inativas` | gemini-3.5-flash | 1 | ✅ | ✅ | ✅ | ✅ | 3/3 | 2.278/5.194 | 113 s |
| **D** | `sp_transferir_entre_contas` | gemini-3.5-flash | 1 | ✅ | ✅ | ✅ | ✅ | 3/3 | 3.368/14.156 | 55 s |
| **E** | `sp_processar_lote_taxas` | gemini-3.1-flash-lite | 1 | ✅ | ✅ | ✅ | ✅ | 3/3 | 3.268/1.655 | 15 s |
| **F** | `sp_relatorio_mensal_cliente` | gemini-3.5-flash-lite | 1 | ✅ | ✅ | ✅ | ✅ | 3/3 | 2.897/2.084 | 89 s |

**Funil de validação (camada estática)** — 5 casos:

| Métrica | Valor |
|---|---|
| `ast.parse` (métrica ingênua) | 100% |
| lint · imports · política | 100% · 100% · 100% |
| **válido ponta a ponta** (todas as camadas) | **100%** |
| aprovado na 1ª tentativa (*pass@1*) | 100% |
| disponibilidade × qualidade | 100% × 100% |
| recall dos riscos esperados (análise) | 100% |
| tokens / tempo de geração | 13.248 / 25.270 · ~298 s |

> **Leia isto antes de interpretar os números.** (1) São uma **fotografia** de uma execução: o LLM não é determinístico e
> o *free tier* do Gemini teve 429 (cota diária de 20 req/dia/modelo) e 503 durante o dia. (2) O resultado é
> **composto por duas avaliações** (B–D e E–F), pois numa rodada E e F caíram por indisponibilidade do provedor
> — a origem de cada caso está em [`results/SUMMARY.md`](results/SUMMARY.md). (3) **Modelos diferentes geraram casos
> diferentes** porque a cota diária dos principais acabou (coluna "Modelo que gerou"; E e F saíram de modelos *lite*).
> (4) A rodada anterior de E/F, com 503, deu `disponibilidade 60%` e `qualidade 100%` — por isso a métrica separa as duas.


### Equivalência comportamental (PostgreSQL real)

Cada cenário roda a **procedure original** e o **Python gerado** sobre os mesmos dados e compara retorno, erro e o
estado final de 5 tabelas (`uv run python -m mirante.behavior --results results`; relatório completo em
[`results/behavior.json`](results/behavior.json)).

**22/22 cenários equivalentes (100%).**

| Anexo | Equivalentes | Cenários que miram as armadilhas |
|---|---|---|
| **B** | 3/3 | — |
| **C** | 4/4 | NULL em RAISE; OUT params, ROW_COUNT; RAISE com % |
| **D** | 8/8 | NULL em RAISE; achado 2: handler grava e re-lanca; achado 3: logica de 3 valores; FK violada depois |
| **E** | 3/3 | achado 4: half-up x half-even; N+1; achado 5 |
| **F** | 4/4 | CTE recursiva, SETOF; achado 1: RAISE capturado pelo proprio handler |

Isto é o que a validação estática **não** enxerga. Sobre os módulos de uma rodada anterior (antes das correções do
prompt e da regra `POLICY_UNTYPED_BIND`), a mesma bateria deu **9/22 (41%)** — código que passava em todas as camadas
estáticas, mas quebrava em runtime ou divergia da procedure (ver §5, itens 8–9, e §10).

> **Cuidado ao interpretar:** 22 cenários **escritos por quem escreveu o pipeline**, para atingir armadilhas conhecidas.
> 100% significa "não achei diferença nesses cenários", **não** "é equivalente". Veja os limites em §8 e §10.


---

## 8. Métrica de avaliação

Implementada em `mirante/evals/` (`POST /evaluate`, CLI `python -m mirante.evals`, tabela `evaluation_runs`, scores
agregados na sessão do Langfuse). **Duas camadas complementares**, porque medem coisas diferentes:

**Camada estática — funil por camada** (sobre o código final de cada caso):

| Métrica | O que captura |
|---|---|
| `syntax_rate` (`ast.parse`) | a métrica **ingênua**, mantida só para contraste |
| `lint_rate`, `imports_rate`, `policy_rate` | o funil: cada camada reprova coisas que a anterior aprova |
| **`valid_rate`** | passou em **todas** as camadas, ponta a ponta (inclui falha do provedor) |
| `first_attempt_rate` / `repair_gain` | pass@1 e quanto o laço de reparo entrega |
| `generation_rate` × `quality_rate` | **disponibilidade do LLM × qualidade do código** (separa indisponibilidade de erro) |
| `risk_recall` | riscos esperados (achados 1–5) que a **análise** detectou: avalia uma etapa, de graça, sem LLM |
| tokens / latência | custo operacional |

**Camada comportamental** (`mirante/behavior/`): roda a procedure **original** e o Python **gerado** sobre os mesmos
dados num PostgreSQL real e compara **retorno, erro (sqlstate + mensagem) e o estado final de 5 tabelas** em 22 cenários
que miram as armadilhas da §5. É o que separa *"o código roda"* de *"o código faz a mesma coisa"*.

- **Por que as duas:** medido — código que passou nas 4 camadas estáticas **quebrava em runtime** (bind sem tipo) e, no
  Anexo F, **omitia o filtro por cliente** (3 bugs semânticos). A estática é necessária, não suficiente.
- **O que deixa de fora:** 22 cenários escritos à mão **não são prova de equivalência**; não cobre concorrência/*locks*
  (o `ROW_LOCK` e a ordem de aquisição não são testados); mensagens de erro do próprio banco comparam só o `sqlstate`;
  amostra pequena (n = 5) e LLM não determinístico → os números são uma **fotografia**, não uma garantia.
- **Como evoluiria em produção:** corpus maior e *pass@k* com várias amostras; gerar cenários por *property-based testing*
  a partir do schema; *LLM-as-judge* para as `decisions`; *gate* de regressão em CI sobre `risk_recall` e as taxas;
  rodar o código gerado em **sandbox de verdade** (hoje: subprocesso `python -I` com ambiente sem chaves — não é sandbox).

---

## 9. Escalabilidade e crescimento

O desenho já separa as peças que escalam por motivos diferentes; o que **está implementado** e o que é **proposta**:

| Eixo | Hoje (implementado) | Proposta |
|---|---|---|
| **Volume de requisições** | Limitador de concorrência por processo (`MAX_CONCURRENT_RUNS`) protege a cota do LLM; `POST` síncrono. | `POST /modernize` → `202` + `job_id`; **fila** (Redis + workers; ou LangGraph Platform) com limite **distribuído** por provedor (*token bucket*); HPA no K8s pela profundidade da fila. |
| **Cache** | — | Chave `hash(IR normalizada + schema + dependências + modelo + versão do prompt)` em Postgres/Redis: mesma procedure ⇒ **sem chamada de LLM**. A *versão do prompt* invalida o cache quando as regras mudam. |
| **Paralelização** | Execuções independentes já rodam em paralelo (até o limite). | Em lote: resolver o **DAG de dependências** (B antes de F) e processar cada nível em paralelo. Dentro de uma procedure os nós são sequenciais por natureza. |
| **Novos dialetos SQL** | Interface `SqlParser` + registro por dialeto; regras são funções registradas. | T-SQL / PL/SQL: um parser + um conjunto de regras por dialeto; *prompt* parametrizado pelo dialeto de origem. |
| **Novos modelos de LLM** | Provedor configurável (Gemini/Claude), *fallback* em cadeia, disjuntor de cota, 2ª rodada com *backoff*. | Roteamento por complexidade (modelo barato p/ B/C, forte p/ E/F), guiado pelos scores do Langfuse. |
| **Banco** | Índices em `created_at`, `(status, created_at)`, `procedure_name` e GIN no `report`. | Particionar `modernization_history` por mês + retenção; ler réplicas para `/history`. |
| **Observabilidade** | Langfuse self-hosted completo. | Amostragem de *traces*; Langfuse escalado/gerenciado. |

---

## 10. Limitações conhecidas e próximos passos

**Limitações (e o que mediria / faria a seguir):**

- **Não é prova de equivalência.** O harness cobre 22 cenários; não testa concorrência, *locks* nem planos de execução.
  → *Property-based testing* sobre o schema; teste de concorrência para o `FOR UPDATE`.
- **LLM não determinístico e provedor instável.** Mesmo com `temperature=0` (e modelos 3.x a ignoram), execuções
  diferentes geram código diferente; no *free tier* houve 429 (cota diária de 20 req/dia/modelo) e 503 frequentes. Os
  números da §7 são uma fotografia. → *pass@k*, chaves/plano pago, modelos pinados.
- **Execução do código gerado não é *sandbox*.** O harness usa subprocesso isolado (`python -I`, ambiente sem chaves),
  mas rede e disco continuam acessíveis. → container sem rede, usuário sem privilégios, `seccomp`.
- **Sem autenticação** na API, **sem *timeout* global** por requisição (depende dos *timeouts* do cliente do LLM) e o
  limitador é **por processo**. → API *key*/JWT, *deadline* por requisição, limitador distribuído.
- **Banco indisponível ⇒ a execução não é registrada** (o usuário ainda recebe o código). → *outbox*/fila.
- **Contêiner usa `langgraph dev`** (servidor em memória, sem licença). Para estado persistente do LangGraph e escala
  horizontal: `langgraph up` / LangGraph Platform.
- **Só PL/pgSQL → Python.** A interface de dialetos existe, mas só o PostgreSQL está implementado.
- **A análise é heurística:** não detecta semântica de negócio (achado 7) nem prova ausência de riscos.
- Credenciais do `docker-compose.yml` e do Langfuse são **de desenvolvimento** (não usar em produção).

**Falhas minhas que a medição expôs (e foram corrigidas) — registradas porque importam para quem for manter:**

1. **Afirmei sem testar** que o log de erro do handler "persiste se o chamador capturar o erro" (e **escrevi isso no prompt**).
   O experimento no PostgreSQL 16 mostrou **0 linhas**. Corrigi o prompt e a regra; antes disso, uma "correção" minha
   havia piorado o código gerado do Anexo D. (`docs/decisions.md` D31)
2. **O prompt prometia "o SQL exato" mas truncava em 600 caracteres.** O Anexo F perdeu o filtro por cliente e o saldo
   atual. Achado pelo harness comportamental; corrigido, com teste de regressão (todo SQL de B–F aparece **inteiro**). (D33)
3. O **fallback de modelos** original usava modelos que dão **404** em projetos novos: escolhido sem testar.
4. O **relatório de avaliação rotulava o modelo configurado**, não o que de fato gerou (os sucessos vinham do *fallback*).

---

## 11. Uso de IA e transparência

O desafio permite assistentes de IA. Este projeto foi desenvolvido com o **Claude Code** em sessão interativa, e as
decisões foram **medidas ou testadas sempre que possível**: o `docs/decisions.md` separa explicitamente *verificado*
de *presumido*, e a §10 lista as vezes em que uma suposição estava errada. O LLM do **pipeline** é o Gemini (padrão),
trocável por Claude (`LLM_PROVIDER=anthropic`).

**Estrutura do repositório**

```
src/mirante/
  parsers/      SqlParser (interface) + PostgresParser (pglast) — ponto de extensão de dialetos
  analysis/     registro de regras de risco (rules.py) + motor
  llm/          prompts.py (contexto a partir da IR), generator.py (CodeGenerator, fallback, disjuntor)
  validation/   camadas: sintaxe, lint+autofix, imports (allowlist), política (AST)
  nodes/        os 5 nós do grafo        graph/   montagem + roteamento (arestas condicionais)
  persistence/  repository, migrate, evaluations     api/   FastAPI (rotas finas) + service.py (caso de uso)
  evals/        métrica estática           behavior/  harness de equivalência comportamental
  observability.py (Langfuse)   langfuse_setup.py (preços)   domain/ (IR, estado, modelos)
db/init/        migrações SQL idempotentes (001 histórico, 002 avaliações)
samples/        Anexos A–F (entrada)       results/   saída do pipeline sobre B–F
docs/decisions.md   log técnico das 34 decisões       tests/   unitários + integração
```
