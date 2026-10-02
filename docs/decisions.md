# Decisoes tecnicas (log vivo)

## D1. Parser: pglast (nao sqlglot)
- **Verificado** (samples B-F): `sqlglot.parse_one(..., dialect="postgres")` aceita o `CREATE FUNCTION`,
  mas o corpo entre `$$` vira um `Heredoc` opaco (string) — sem AST de IF/LOOP/FETCH/RAISE.
- `pglast.parse_plpgsql()` (libpg_query, o parser do proprio Postgres) devolve a AST completa do
  PL/pgSQL: `if`, `loop`, `open/fetch/close`, `case`, `raise`, `getdiag`, `return_query`, `execsql`.
- Os SQLs embutidos (`execsql`) sao re-parseados com `pglast.parse_sql` para achar tabelas/CTEs/FOR UPDATE.
- Trade-off: pglast so fala PostgreSQL. Novos dialetos (T-SQL, PL/SQL) entram como novos adaptadores
  atras da interface `SqlParser` (ponto de extensao pedido no desafio).

## D2. Servidor: langgraph CLI + FastAPI customizado
- **Verificado**: `langgraph.json` aceita `http.app` (app Starlette/FastAPI montado no servidor).
  Por ali expomos `POST /modernize` e `GET /health`; o grafo continua disponivel em `/runs`.

## D3. Python 3.14
- **Verificado**: `uv sync` em Python 3.14.7 instala langgraph 1.2, langfuse 4.16, pglast 8.4, psycopg etc.
- `ast.parse` so valida a sintaxe do interpretador que o executa -> o validador roda em 3.14.

## D4. Docker
- Docker Desktop nao esta integrado ao WSL nesta maquina -> `docker-compose.yml` ainda NAO foi
  executado aqui (so validado como YAML). Testar antes da entrega.

## D5. IR em Pydantic + `Stmt` generico com `blocks` nomeados
- Uma unica classe `Stmt(kind, text, sql, detail, blocks)` cobre todos os comandos PL/pgSQL; sub-blocos
  (`then`, `else`, `body`, `exception:others`) ficam em `blocks`. Serializa direto para JSONB e para o prompt.
- Trade-off: menos tipagem por comando que uma classe por kind, em troca de um walker unico e novos
  comandos sem mudar o schema. Kinds nao mapeados caem num handler generico (nao quebram o parsing).

## D6. Precisao dos tipos recuperada do texto do DECLARE
- **Verificado**: o datum do `parse_plpgsql` so traz o tipo-base (`numeric`), sem `(18,2)`.
- A precisao e recuperada pela linha do DECLARE (via `lineno`) e canonizada com o proprio parser
  (`SELECT NULL::<tipo>`); fallback para o tipo-base. Necessario porque atribuir a NUMERIC(18,2) arredonda.
- Parametros vem de `CreateFunctionStmt` (ja trazem precisao). Modo `d` (sem modo explicito) == IN.
  Colunas `RETURNS TABLE` ficam so em `returns.columns` (nao em `params`).

## D7. Erro de parsing e estado, nao excecao
- `parsing_node` devolve `errors` + `report.parsing.status="failed"`; o grafo segue para a persistencia,
  garantindo "toda execucao e gravada, mesmo em falha".

## D8. Analise = registro de regras pequenas (`analysis/rules.py`)
- Cada risco e uma funcao `(Ctx) -> Iterable[Risk]` com `@rule`. Novo risco/dialeto = nova funcao; o motor
  (`engine.py`) nao muda. Regras que dependem do schema (`needs_schema`) sao puladas *explicitamente*
  (`skipped_rules` no relatorio) quando o Anexo A nao e enviado — nunca falham em silencio.
- Deteccao por AST (pglast Visitor), nao regex sobre SQL. Unica excecao: o tipo declarado no DECLARE (D6).
- Cada `Risk` carrega `recommendation`: e isso que alimenta o prompt da geracao (passo 4).

## D9. Falso positivo corrigido: parametro PL/pgSQL nao e coluna
- `DATE_TRUNC('month', p_data_fim)` disparava NON_SARGABLE (F). Variaveis/parametros agora sao excluidos.
  Travado por teste de regressao.

## D10. O que a analise NAO detecta (vai para o LLM / revisao humana)
- Semantica de negocio: em F, `saldo_consolidado` = saldo atual +/- movimento do mes (nao e acumulado);
  parece bug, mas a traducao deve preservar. Regra estatica nao distingue "bug" de "regra".
- Precisao do ponto de dados em runtime (ex.: taxa = 0 so ocorre com dados especificos): sinalizamos o
  *risco* (CHECK em valor calculado), nao o caso concreto.

## D11. SQL fica SQL; so o controle de fluxo vira Python (decisao arquitetural da geracao)
- Queries executam no banco via SQLAlchemy Core `text()` com bind params; IF/CASE/LOOP/RAISE/EXCEPTION/atribuicoes
  viram Python. Garante equivalencia das queries (CTE recursiva, FOR UPDATE, agregacoes) e 1 round-trip.
- Trade-off: o Python gerado nao roda sem banco (testes exigem Postgres); e menos "puro" que reescrever em ORM.
- Excecao consciente: cursor com N+1 -> carga em lote (conjunto lido uma vez = snapshot do cursor) + DML em lote.

## D12. O LLM nunca ve a procedure bruta (context engineering)
- Prompt = assinatura + variaveis (com precisao) + arvore de comandos com o SQL exato + riscos com
  recomendacao + so as tabelas referenciadas + assinatura das dependencias. Comentarios SQL nao chegam ao
  modelo (a IR vem do parser): reduz tokens e remove vetor de prompt injection. System prompt declara o
  conteudo como DADO, nao instrucao.

## D13. LLM atras de `CodeGenerator` + provedor configuravel + fallback de modelos
- `CodeGenerator` (Protocol): testes usam `StubGenerator` (offline, deterministico). Provedor via `LLM_PROVIDER`
  (gemini|anthropic); `LLM_FALLBACK_MODELS` encadeia modelos quando o principal falha (**verificado**: o Gemini
  devolveu 503 "high demand" em 2 de 5 execucoes). Temperatura 0.
- Erro do LLM vira estado de falha (persistivel), nunca excecao nao tratada.

## D14. Defeitos reais achados lendo o codigo gerado (viraram regra/teste)
- Handler de EXCEPTION dentro do savepoint (D): a *recomendacao* da regra SUBTRANSACTION_ROLLBACK era ambigua.
  Corrigida ("handler FORA do savepoint") e confirmado na regeracao. Licao: recomendacao ruim => codigo ruim.
- `from dataclasses import dataclass, frozen` (F): `ast.parse` e ruff passam, mas o import falha.
  => a validacao precisa de camadas: ast.parse -> ruff -> import real -> (comportamental). Isso tambem mostra
  que "taxa de ast.parse" sozinha e uma metrica enganosa (5/5 aprovados, 4/5 realmente importaveis).
- Pendente (nao verificado): `jsonb_build_object(:param)` com psycopg3 pode exigir cast de tipo no bind.

## D15. Validacao em camadas, sem executar o codigo gerado
- Ordem (barata -> cara): syntax (`ast.parse`, Python 3.14) -> lint (ruff F,E9) -> imports (importlib + allowlist)
  -> policy (regras do system prompt conferidas por AST: sem commit/rollback, SQL so com bind, sem float,
  assinatura `(conn, <IN>)` correta). So a sintaxe interrompe; as demais rodam juntas para o reparo receber
  todos os erros de uma vez (menos chamadas de LLM).
- Nao executa codigo nao confiavel: imports sao checados por `importlib` com allowlist (stdlib + sqlalchemy,
  psycopg, pydantic + dependencias traduzidas). Execucao real exige sandbox (limitacao documentada).
- Evidencia: `from dataclasses import frozen` passou em ast.parse e ruff; so a camada de imports barrou.

## D16. Autofix deterministico (parte "regras" do hibrido)
- Imports nao usados (F401) sao removidos por `ruff --fix` em vez de pedir nova geracao (cada chamada custa ~40s).
  F401/F841 sao *avisos*, nao disparam reparo. Roda ANTES da camada de imports: um import invalido mas morto
  tambem e removido (seguro: o modulo resultante funciona). Teste documenta essa interacao.

## D17. Laco de reparo generation <-> validation
- `after_validation`: passou -> fim; falhou e `len(generation_log) < max` -> volta ao gerador com feedback +
  codigo anterior no prompt; senao encerra com validacao "failed" (status PARTIAL na persistencia).
  Limite padrao 3 (1 geracao + 2 reparos), configuravel por requisicao (`max_attempts`).
- **Verificado ao vivo** (Gemini 2.5 Flash, modulo F real com `frozen`): o reparo corrigiu o import e passou nas
  4 camadas. Mas o modelo nao e deterministico mesmo a temperatura 0: numa 2a execucao o F saiu certo de primeira,
  entao o laco so foi exercitado ao vivo de forma forcada.

## D18. Fragilidade: codigo grande em campo string do structured output
- **Incidente real**: no reparo, o Gemini devolveu o modulo em 3 linhas com `\n` literais (JSON duplamente escapado);
  a camada de sintaxe barrou ("unexpected character after line continuation").
- Mitigacao na fronteira do LLM (`clean_code`): detecta codigo "colapsado" (<=3 quebras reais e >=10 `\n` literais)
  e decodifica por JSON; guarda de heuristica evita tocar codigo legitimo com `"\n".join`. Falha de decodificacao
  cai na validacao. Alternativa futura: pedir o codigo em bloco cercado fora do JSON.

## D19. Persistencia: todo caminho do grafo termina em `persist`
- Sucesso, parcial, falha de parsing e falha do LLM convergem no no `persist` (uma linha por execucao, **verificado**
  em teste: `len(records) == 1`). Crashes inesperados de um no (bug) escapam do grafo: a camada de API (passo 7)
  captura e grava um FAILURE tambem — o grafo sozinho nao pode garantir isso.
- Status por regra pura (`compute_status`): FAILURE (erro de estagio ou sem codigo) | PARTIAL (gerou mas nao
  validou) | SUCCESS. Codigo sem validacao NAO conta como sucesso. Em PARTIAL guarda-se o ultimo codigo (util p/ revisao).
- Banco fora do ar vira erro de estagio `persistence` e a resposta ainda leva o codigo gerado (o usuario ja pagou
  pelo LLM); sem `history_id`. **Limitacao**: nesse caso a execucao NAO fica registrada (sem fila/outbox).

## D20. Modelagem
- Colunas exigidas (id UUID, source_code, generated_code, report JSONB, status ENUM, created_at TIMESTAMPTZ) +
  derivadas para consulta (procedure_name, dialect, duration_ms): listagens/metricas sem varrer JSONB.
- `generated_code` e NULL-avel (falha antes de gerar); `source_code` NOT NULL (a entrada sempre e preservada).
- Indices: created_at DESC, (status, created_at DESC), procedure_name e GIN `jsonb_path_ops` no relatorio
  (operador `@>`). ENUM em vez de TEXT+CHECK: tipo explicito, 4 bytes, e o banco rejeita valores invalidos (testado).
- Migracoes: `db/init/*.sql` idempotentes (servem ao docker-entrypoint-initdb.d) + migrador com `schema_migrations`
  e `pg_advisory_xact_lock` (**verificado**: 4 instancias concorrentes -> so uma aplica).

## D21. Dev/teste sem Docker
- Docker nao esta integrado ao WSL e `pgserver` nao tem wheel 3.14: o Postgres (16.2) roda num ambiente 3.12
  efemero (`scripts/dev_postgres.py`) e o app 3.14 conecta por socket Unix. Testes de integracao criam um banco
  temporario por sessao (nunca tocam o de desenvolvimento) e sao pulados se nao houver Postgres.
- Licao: ao ligar o no de persistencia, os testes unitarios do grafo passaram a gravar no banco de dev sem avisar
  (o erro e engolido por projeto). Corrigido com `InMemoryRepository` injetado; checado que o dev nao foi tocado.

## D22. API: rotas finas + `service.py` (caso de uso) + dependencias injetaveis
- Camadas: `api/` (HTTP: schemas, rotas, deps) -> `service.py` (executa o grafo e garante o registro) -> `graph/`
  -> `nodes/` -> `persistence/`. Rotas so traduzem HTTP; regra (crash, mapeamento de status) fica no servico.
- `app.dependency_overrides` + `Annotated[..., Depends()]`: os 30 testes da API rodam sem rede, banco ou LLM.
- **Verificado com o `langgraph dev` real**: `http.app` monta nossa app ao lado das rotas do LangGraph; `/ok` (deles) e
  `/health` (nosso) convivem; o `lifespan` roda (migra o banco); o grafo aparece em `/assistants` como `modernizer`.

## D23. Codigo HTTP = de quem foi a falha; corpo sempre igual
- SUCCESS/PARTIAL -> 200 (PARTIAL vem sinalizado: ha codigo utilizavel); FAILURE: parsing -> 422 (entrada do
  cliente), generation -> 502 (LLM externo), demais -> 500. O corpo traz sempre `id`, `status`, `report`, `errors`.
- Payload invalido (vazio, >200k chars, `max_attempts` fora de 1..5, >10 dependencias) e rejeitado pelo Pydantic
  ANTES do pipeline: nao gasta LLM nem grava.

## D24. Robustez operacional
- **Crash de um no** (bug) escapa do grafo e nao chega ao `persist`: o servico captura, grava um FAILURE
  (estagio `pipeline`) e responde 500 — fecha a lacuna deixada no D19. Se o banco tambem estiver fora, `id=None`.
- **Limitador de concorrencia** (`MAX_CONCURRENT_RUNS`, padrao 4): cada execucao segura o LLM por 20-120 s; sem
  limite um pico estoura a quota do provedor. Excesso espera a vez (**verificado**: 6 requisicoes simultaneas, pico
  de exatamente 2 com limite 2). Limitacao: e por processo; varias replicas exigem fila/limite distribuido.
- `/health` retorna 200 `degraded` com banco fora (o pipeline ainda atende, mas nao grava); informa SE ha chave
  de LLM configurada, nunca o valor (testado). Sem autenticacao nesta versao (limitacao a documentar no README).
- Sem timeout global por requisicao: depende dos timeouts do cliente do LLM (180 s x 3 tentativas por geracao).

## D25. Observabilidade (Langfuse self-hosted): um trace por execucao, span por no, scores por execucao
- `observability.py`: `Tracer` atras de interface (como o LLM). O `trace_id` e gerado ANTES da execucao e passado ao
  handler (o SDK nao expoe `last_trace_id`); ele e gravado em `report.meta.langfuse_trace_id`, ligando a linha do
  historico ao trace. Observabilidade nunca derruba o pipeline (falhas viram log).
- **Verificado contra o Langfuse real** (testes de integracao): trace `modernize` com tags, spans `parsing`,
  `analysis`, `generation`, `validation`, `persist` (+ decisoes de roteamento `after_*`); o laco de reparo aparece
  como 2x `generation` e 2x `validation`. Scores por execucao: `validation_passed` (BOOL), `generation_attempts`,
  `high_risks_detected`, `pipeline_status` (CATEGORICAL).
- Traces de uma avaliacao compartilham uma **sessao** (`langfuse_session_id`) que recebe os scores agregados.
- Escolha do Langfuse self-hosted: dado sensivel (SQL de bancos legados) nao sai da rede; sem custo por trace.
  Trade-off: 6 servicos (web, worker, postgres, clickhouse, redis, minio) so para observabilidade.

## D26. Custo no Langfuse: precisa cadastrar preco, e o raciocinio e a maior parte da saida
- **Medido**: a versao self-hosted so conhece Gemini 1.x -> custo 0 para 2.5/3.x. `langfuse_setup.py` cadastra os
  precos (lista paga, conferidos em ai.google.dev em 2026-10-01) de forma idempotente. No free tier o custo real e 0;
  o valor e o equivalente pago (util para projetar gasto).
- **Bug evitado**: o campo `prices` e descartado pela API; so `pricingTiers` aceita `output_reasoning`. Sem ele o
  custo saia 45% menor (US$ 0,0043 vs 0,0078 para 1914 in / 1485 out / 1422 raciocinio). Travado por teste de
  integracao com os tokens de uma chamada real. `gemini-3.6-flash` tem preco promocional ate 2026-12-31.

## D27. Docker (primeiro teste real do compose) achou 2 bugs que o YAML nao mostrava
- `minio/minio` saiu do Docker Hub -> `cgr.dev/chainguard/minio`. E `depends_on` sem healthcheck fazia o
  `langfuse-web` subir antes do `langfuse-db` e morrer (P1001): agora healthchecks + `condition: service_healthy`.
- `docker-entrypoint-initdb.d` so roda com o volume VAZIO: um volume antigo nao recebe migracoes novas. Por isso o
  app migra no startup (idempotente + `schema_migrations`); **verificado** contra o Postgres do Docker por TCP.

## D28. Metrica de avaliacao: funil por camada (e por que NAO so `ast.parse`)
- **O que mede** (`evals/`): (1) funil por camada sobre o codigo final: sintaxe -> lint -> imports -> politica;
  `valid_rate` = passou em todas, ponta a ponta; (2) `first_attempt_rate` (pass@1) e `repair_gain` = quanto o laco de
  reparo entrega; (3) `risk_recall` = riscos esperados (achados 1-5 do enunciado) que a ANALISE detectou: avalia uma
  etapa de forma deterministica e gratis; (4) `generation_rate` (disponibilidade do LLM) x `quality_rate` (validos
  entre os gerados): separa falha do provedor de falha de qualidade; (5) tokens e latencia.
- **Por que nao so `ast.parse`**: medido, 5/5 modulos passaram em `ast.parse` e so 4/5 importavam (`naive_gap`).
- **O que deixa de fora**: equivalencia COMPORTAMENTAL (mesmo input -> mesmo output/estado no banco); semantica de
  negocio; amostra pequena (n=5) e LLM nao deterministico (mesmo a temperatura 0, modelos 3.x ignoram `temperature`).
  Uma metrica estatica aprova codigo que roda mas faz a coisa errada.
- **Evolucao em producao**: harness de equivalencia (Postgres de teste: procedure original x Python gerado, mesmo
  seed, comparar retorno + estado das tabelas + erros; confirmaria tambem o risco `jsonb_build_object` + psycopg3,
  ainda NAO verificado); corpus maior e pass@k com varias amostras; LLM-as-judge para as `decisions`; gate de
  regressao em CI sobre `risk_recall` e `valid_rate`; rodar a execucao do codigo gerado num sandbox.
- Persistencia: tabela `evaluation_runs` (migracao 002), `POST/GET /evaluate`, CLI `python -m mirante.evals`;
  scores agregados na sessao do Langfuse. Executor sequencial (respeita o limite de requisicoes do provedor).

## D29. Resiliencia do LLM: o que a medicao ensinou
- **Medido (free tier, 2026-10-01)**: `gemini-2.5-flash` = 20 req/DIA; `2.5-pro` e `2.5-flash-lite` dao 404 para novos
  usuarios; modelos `3.x` oscilam em 503 "high demand". O fallback inicial (flash-latest, 2.5-pro) era inutil.
- Politica: (1) 429 por cota DIARIA (`PerDay` no payload) -> sem espera, disjuntor por modelo (compartilhado entre
  requisicoes, cooldown 1h, fail-open se todos esgotados); (2) 429 por minuto -> espera o `retry in Ns` e repete no
  MESMO modelo uma vez; (3) 503/timeout -> proximo modelo. Timeout 120 s, 1 retry interno (antes: 329 s para falhar).
- **Armadilha**: o 429 diario traz um `retry in 1.6s` enganoso (vale para o limite por minuto); esperar nao resolve.
- A avaliacao registra o modelo que REALMENTE gerou (antes rotulava o configurado, mas os sucessos vinham do fallback).

## D30. QA
- `ruff` (E,F,I,UP,B,SIM) e `mypy` (56 arquivos) limpos; 202 testes, ~92% de cobertura; testes de integracao
  (`-m integration`) usam Postgres/Langfuse reais e sao pulados se ausentes. Um unico `type: ignore[call-overload]`
  (2 linhas do builder: o LangGraph nao infere `NodeInputT` para nos criados por fabrica), com justificativa no codigo.
- Flakiness real: o relogio do WSL chega a voltar 7,5 s -> testes de ordenacao por `created_at` fixam o timestamp.

## D31. CORRECAO do D14: o log do handler que re-lanca NUNCA persiste (verificado no PostgreSQL 16)
- **Eu estava errado.** O D14 e a regra 3 do prompt afirmavam que, se o chamador capturar o erro, "as escritas do
  handler persistem" (e o passo 4 "corrigiu" o codigo gerado do Anexo D para o handler ficar FORA do savepoint).
  Isso foi deduzido do manual e nunca testado.
- **Experimento** (Anexo D, PG 16): chamador PL/pgSQL com `EXCEPTION WHEN OTHERS THEN NULL` e cliente com
  `SAVEPOINT` + `ROLLBACK TO` + commit -> **0 linhas** `TRANSFERENCIA_ERRO` nos dois casos (controle valido: 1 linha OK).
  O erro que sai da procedure desfaz a chamada INTEIRA, handler incluso.
- Traducao fiel: a funcao inteira dentro de `with conn.begin_nested():` (o erro propaga e desfaz tudo). A primeira
  versao do Gemini (handler dentro do savepoint) era a fiel; a minha "correcao" a piorou. Handler que NAO re-lanca
  (F, fallback) persiste normalmente. Regra de analise e prompt corrigidos.
- Licao: todo "verificado" no texto das regras precisa de um teste que o prove (agora: `tests/test_behavior.py`).

## D32. Camada comportamental: o que a validacao estatica nao ve (22 cenarios, PG real)
- `mirante.behavior`: banco temporario, schemas `orig`/`gen` com os mesmos dados; procedures originais so em `orig`
  (o codigo gerado nao consegue "trapacear"); o Python gerado roda em SUBPROCESSO (`python -I`, ambiente sem chaves de
  API; NAO e sandbox de seguranca — em producao: container sem rede). Compara retorno (Decimal normalizado),
  erro (sqlstate + mensagem) e dump das 5 tabelas (mascarando so valores de NOW()).
- **Achados**, sobre codigo que passou em ast.parse, ruff, imports e politica:
  1. `jsonb_build_object(:p)` com bind sem tipo quebra em runtime no psycopg 3 (`could not determine data type of
     parameter $N`): o risco levantado no passo 4 se CONFIRMOU. Sobre os modulos antigos do Gemini, so 9/22 (41%)
     cenarios eram equivalentes (D: 1/8; E: 2/3). Virou: regra do prompt, recomendacao da analise e a regra estatica
     `POLICY_UNTYPED_BIND` na validacao (reprova C, D e E antigos).
  2. `RAISE '%'` com NULL: o PG imprime `<NULL>`, o Python gerado imprimia `None` (virou regra do prompt + risco).
- A metrica estatica (`valid_rate`) e o harness medem coisas diferentes: o primeiro e necessario, nao suficiente.
- Limites: 22 cenarios escritos a mao (nao e prova de equivalencia); comparacao so em estado/retorno/erro (nao compara
  planos, locks, nem concorrencia — o ROW_LOCK/deadlock nao e testado); mensagens de erros do banco comparam so o sqlstate.

## D33. Bug no proprio prompt achado pelo harness: o SQL era truncado em 600 caracteres
- O `_one_line` cortava o SQL em 600 caracteres, mas o prompt (e o D12) prometia "o SQL exato de cada comando". A query
  do Anexo F tem ~1800: o modelo nunca viu o `v_saldo_atual +` do SELECT final, o filtro por cliente nem o `+ INTERVAL
  '1 day'`, e preencheu as lacunas por suposicao. Resultado medido: F equivalente em 1/4 cenarios (3 bugs semanticos
  distintos), enquanto B/C/D/E ficaram 18/18. A validacao estatica aprovou o F nas 4 camadas.
- Correcao: sem truncamento + teste de regressao parametrizado (todo SQL do IR de B-F aparece INTEIRO no prompt).
- Licao: "o modelo recebe X" e uma afirmacao testavel; toda promessa de contexto virou teste.

## D34. Entrega final: o que foi verificado, e como o resultado foi composto
- **Resultado B-F (estatico):** 5/5 validos na 1a tentativa, `risk_recall` 100%; **comportamental:** 22/22 cenarios
  equivalentes. Composto por 2 avaliacoes (B-D: a194d9d9; E-F: 6bc0383f) porque numa rodada E e F cairam por 503; modelos
  diferentes geraram casos diferentes (a cota diaria de 20 req/dia/modelo dos principais acabou): B-D `gemini-3.5-flash`,
  E `gemini-3.1-flash-lite`, F `gemini-3.5-flash-lite`. Registrado por caso em `results/evaluation.json` (`models`).
- **Verificado de ponta a ponta no Docker:** `docker compose up -d --build` (api + db + Langfuse), `/health`, `/modernize`
  (422 e caminho feliz com LLM real), `/history`, `langfuse_setup` dentro do contêiner, migracao idempotente contra o
  Postgres do compose, avaliacao real gravando em `evaluation_runs` do compose.
- **Preco duvidoso removido:** `gemini-3.5-flash-lite` (a pagina oficial ora o listava com US$ 0,30/2,50 — igual ao 2.5-flash —
  ora como "nao listado"): sem preco cadastrado de proposito (custo aparece como 0, nao como um numero inventado).
- Higiene: testes de integracao do Langfuse usam `environment="test"`; `results/` fora do lint do projeto (codigo gerado e
  validado pela camada de validacao, nao pelo estilo); `scripts/run_samples.py` removido (substituido por `mirante.evals`
  + `scripts/export_results.py`).
- **Nao verificado / limites:** cenarios escritos por mim (nao e prova de equivalencia); concorrencia/`FOR UPDATE` nao
  testados; execucao do codigo gerado nao e sandbox; as duas chaves Gemini foram coladas no chat (devem ser rotacionadas).
