# VIAL Code Agent — Integração Completa com o VIAL

Este documento descreve como `vial-code-agent` consome **todos** os módulos do
core oficial VIAL (`vendor/vial-core/prototype/`), seguindo as especificações
`fundation/`, `sdk/`, `rfc/`, `tools/`, `runtime/` e `adr/`.

## Runtime composto

`src/vial_code_agent/vial_runtime.py` instancia e compõe **toda** a superfície
do prototype. Nada fica como código morto:

| Módulo do prototype | Nuance VIAL consumida | Referência |
|---------------------|------------------------|------------|
| `state` | Organização dona de Estado persistente (OCS minimal contract) | RFC-003 §53, SDK-002 |
| `context` | Contexto Seletivo vs Completo + ciclo de vida `CREATED→VALID→FROZEN→CONSUMED` | RFC-004, RFC-007, SDK-004 |
| `tokenizer` | Orçamento determinístico de tokens do contexto/prompt | RFC-007 |
| `decision` | `propose→approve→authorize→execute` | SDK-005, RUNTIME-006 |
| `authorization` | `AuthorizationGate` separa *Capability ≠ Authority* | SDK-005 §34, TOOLS-007 |
| `tool` | Contrato `Tool`, `ToolResult` e registro de auditoria por invocação | TOOLS-001 |
| `resource` | Recursos + capabilities por tier (cheapest capable) | SDK-003, RFC-010 §2.4 |
| `identity` | `Authenticator` + `Principal` | SDK-001 §30 |
| `persistence` | Gravação atômica JSON do Estado organizacional | RFC-003 |
| `coordinator` | `Intent` log (antes da mutação), transições atômicas, recuperação | RFC-009 §2.3 |
| `reuse` | Cache com assinatura determinística + invalidação por staleness | RFC-004, RFC-008 §2.3 |
| `cost` | Modelo de custo total + `ResourceSelector` (Deterministic First) | RFC-004 §21-23, RFC-010 |
| `executor` | `DeterministicExecutor` + `Evaluator` (baseline RFC-007) | RFC-007 §2.2 |
| `errors` | Modelo estruturado `VIALError` usado nos fluxos de rejeição | SDK-001 §30-31 |

## Nuances implementadas no fluxo de código

### 1. Contexto seletivo vs completo (RFC-007, RFC-010)
O pipeline de geração usa **contexto seletivo** (`build_selective`): o
`required` do `Task` filtra os campos do Estado pela relevância, e as
referências (`state:file:...`) são anotadas no `Context`. O total de tokens é
medido pelo `tokenizer` oficial. *(config: `max_context_chars`)*

### 2. Reason once, reuse many times (RFC-008)
Antes de invocar um modelo, o agente consulta o cache de **reuso cognitivo** a
partir da assinatura determinística da operação (`op` + `args`). O cache guarda
as referências de Estado; se o arquivo mudou externamente, o registro é
**invalidado como staleness** e recalculado.

### 3. Deterministic First (RFC-010 §2.4)
Tarefas mecanicamente solucionáveis (ex.: `trim trailing whitespace`, `add
encoding header`) são roteadas para o `ResourceSelector` no tier
`deterministic` — **nenhum modelo é invocado**. Um `VialRouter` aplica a
transformação determinística e produz um diff unificado auditável. Sem
assinatura mecânica, o seletor escolhe o tier de modelo mais barato.

### 4. Capability ≠ Authority (SDK-003 §25, SDK-005 §34, TOOLS-007)
Toda aplicação de patch passa pelo `AuthorizationGate` numa ordem estrita:
1. `Decision.propose` registra o objetivo com um `Authority` declarado;
2. `approve` aprova tecnicamente (possível);
3. `authorize` valida a autoridade (`org-root`);
4. `Tool.invoke` só executa se `decision.status == AUTHORIZED`, o ator está em
   `allowed_actors`, o `required_capability`/`required_scope`/`required_policy`
   batem e há correspondência de `organization_id`/`context_id`.

Rejeições retornam `ToolResult(status=REJECTED)` com `error_code` estruturado
(ex.: `DECISION_NOT_AUTHORIZED`, `ACTOR_NOT_AUTHORIZED`,
`CAPABILITY_NOT_AUTHORIZED`, `ORGANIZATION_MISMATCH`).

### 5. Atomicidade, idempotência e recuperação (RFC-009)
A aplicação do patch é gerenciada pelo `StateCoordinator`:
- **`begin` antes da mutação** registra um `Intent` no log (gerado a partir do
  hash do patch);
- **`commit`** aplica campo+versão atomicamente com otimismo concorrencial;
- **replay de uma operação já commitada** resolve do log e **não re-aplica**
  (idempotência);
- **operações interrompidas** pendentes são retomadas do log (recuperação);
- **rollback** após falha de teste registra um `Intent` de compensação
  (`ROLLBACK-<hash>`) auditável.

### 6. Persistent cognition (RFC-003)
`VialRuntime.persist()` grava atomicamente `org/decisions/intents/reuse/
audit/cost` em `.vial-state/`. Uma nova instância **restaura** o Estado, as
Decisões, os Intents, o cache de reuso e as auditorias — a cognição sobrevive à
substituição do executor.

### 7. Auditabilidade (SDK-001 §46-51, SDK-005)
Cada invocação de `Tool` produz um `AuditRecord` (invocation_id, tool_id, actor,
decision_id, context_id, status). `vial --status` expõe o snapshot
organizacional completo: versão de estado, recursos, tools, reuso, coordinator,
decisões, execuções, auditorias e custos.

### 8. Custo econômico total (RFC-004 §21-23, RFC-010)
`CostModel` acumula `tokens`, `inference` (escalada pelo tier), `latency`,
`retrieval`, `construction` e `validation`. A tabela de preços vem de
`price_table_json` (`.vial.json`). O fluxo registra cada fase (retrieval,
construction, inference, validation) e expõe o total em `vial status`.

### 9. Identity (SDK-001 §30)
O runtime registra credenciais de `org-root` e `vial-code-agent` no
`Authenticator`. O ator da aplicação autentica com um segredo de
desenvolvimento; em produção o provedor de identidade é substituível sem mudar
o contrato de autorização.

### 10. Catálogo de Tools governadas (TOOLS-001/007)
Além de `TOOL-PATCH-APPLY`, o runtime registra um catálogo completo de Tools,
cada uma com `ToolContract` + `security_policy` (capability/scope/policy) e
classificação de risco (`low`/`medium`/`high`/`critical`):

- `TOOL-READ-FILE`, `TOOL-SEARCH`, `TOOL-LIST-FILES`,
  `TOOL-INSPECT-DEPENDENCY` — leitura/inspeção (`policy: inspect`);
- `TOOL-RUN-TEST`, `TOOL-RUN-BUILD` — execução de desenvolvimento
  (`policy: development`);
- `TOOL-RUN-GIT` — mutação via git (`risk: high` → exige Approval);
- `TOOL-RUN-AUDIT` — roda a suíte `AUDIT-000..015` do core.

`VialRuntime.invoke_tool()` deriva a Decision do contrato da Tool (capability +
policy), passa pelo `AuthorizationGate` e registra o outcome. Tools de risco
alto/crítico são rejeitadas (`APPROVAL_REQUIRED`) até que um `ApprovalRecord`
seja registrado com `approve_decision()`.

### 11. Outcome da Decision (SDK-005 conformance #4)
`apply_patch` e `invoke_tool` chamam `decision_engine.execute()` após a
execução, anexando `status`/`invocation_id`/`error` ao outcome. O lifecycle
chega a `COMPLETED` e o outcome é persistido (`decisions.json`).

### 12. Cognition Engine (RUNTIME-006)
`src/vial_code_agent/cognition.py` implementa a fronteira de Cognição:
`CognitionRequest` → `CognitionEngine.evaluate()` → `CognitionResult`
(proposal, alternatives, evidence, rationale, confidence, risks,
required_authority). Tarefas mecânicas resolvem determinístico (RFC-010) sem
modelo; o resto usa o provider. `CodeAgent.plan_cognition()` consome o Context
oficial FROZEN. O engine **nunca autoriza** — `cognition.propose()` entrega a
proposta à Decision Engine para a cadeia downstream.

### 13. Approval + risco (SDK-005, RUNTIME-006 §8/§20)
`approve_decision()` grava um `ApprovalRecord` (distinto de Decision e
Authorization). `decision_requires_approval()` exige approval para risco
high/critical. Approvals são persistidos em `approvals.json`.

### 14. Traço de auditoria (SDK-001 §46-51, RUNTIME-006 §55)
`decision_trace(decision_id)` reconstitui o *porquê*: decision (objective,
evidence, rationale, status, outcome), approval, `AuditRecord`s correlacionados
e o Context usado. Exposição via `vial --status --trace <DEC-...>`.

### 15. Memória organizacional (RUNTIME-005)
`memory()` expõe reuse, decisões com outcome, approvals e auditorias como a
superfície de memória persistente (`.vial-state/`), incluída em `vial --status`.

### 16. Interface opencode-style (chat + app)
`src/vial_code_agent/chat.py` concentra o estado da sessão (modelo, agente,
pool, sessão) e todo slash command em um `ChatController` sem framework
(testável). `src/vial_code_agent/app.py` é a TUI Textual/Rich que renderiza o
mesmo contrato visual do opencode: viewport de mensagens, composer com
autocomplete, painel lateral (session/agent/model/status/pool) e footer de
keybindings (`Tab` alterna build/plan, `Ctrl+P` seleciona modelo).

A orquestração multi-LLM é o `RoutingGraph` (`router.py`): com `--model auto` a
tarefa é analisada e despachada em paralelo para todos os modelos do pool
(primeira resposta válida vence, prioridade determinística); `/model
provider/model` fixa um único provedor. A governança do core é exposta na TUI
por `/trace <id>` e `/approve <id>` (via `VialRuntime`).

## Configuração relevante (`.vial.json` + `VIAL_*`)

| Campoe env | Padrão | Uso |
|------------|--------|-----|
| `org_id` / `VIAL_ORG_ID` | `ORG-VIAL-CODE-AGENT` | Identidade da Organização |
| `authority` / `VIAL_AUTHORITY` | `org-root` | Autoridade que autoriza decisões |
| `actor` / `VIAL_ACTOR` | `vial-code-agent` | Ator que interpreta/promove |
| `persist_state` / `VIAL_PERSIST_STATE` | `true` | Persistência da cognição em `.vial-state/` |
| `price_table_json` / `VIAL_PRICE_TABLE` | tabela padrão | Preços do modelo de custo (RFC-010) |

## Banco de hardware de evidência (integridade)

O diretório `benchmark/` continua validando o loop local sem chamadas pagas:

```text
python benchmark/run_benchmark.py
```

As extensões VIAL (reuso, determinístico, gate de autorização, coordinator,
cost) são exercidas por suites determinísticas:

```text
python -m unittest discover -s tests -v
```

## Comandos

```text
vial                                   # TUI fullscreen opencode-style
vial --status                          # snapshot organizacional completo
vial --status --trace DEC-0001         # porquê de uma decisão (audit trail)
vial --fix "trim trailing whitespace"  # rota determinística (sem modelo)
vial --fix "implement persistence"     # rota de modelo (custo) via orquestrador
vial --model openai/gpt-5.6-luna       # seleção explícita de LLM
vial --providers / --models            # descoberta de provedores/modelos
```

## Fronteira com o VIAL

A aplicação **consome** conceitos normativos do core por meio do adaptador; ela
não copia/duplica a especificação e não torna comportamento de produto normativo
para o VIAL. As extensões determinísticas (`router.MECHANICAL_OPS`) são heurísticas
de produto, não novos estados/identificadores normativos.