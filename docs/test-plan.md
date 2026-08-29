# Plano de Teste — VIAL Code Agent

Guia de avaliação de todas as funcionalidades do VIAL a partir do CLI.
Cada cenário tem o **prompt/commando de entrada**, o **resultado esperado** e a
**forma de verificação**. Os cenários são independentes e podem ser executados
em qualquer ordem.

Preparação inicial:

```text
python -m pip install -e .
mkdir -p /tmp/vial-eval && cd /tmp/vial-eval
git init . && git add -A && git commit -m "base"
```

---

## 1. Deterministic First (sem chamada de modelo)

Tarefas mecânicas registradas em `MECHANICAL_OPS` devem resolver sem invocar
modelo, registrando execução determinística auditada.

**Prompt:**
```text
vial --fix "trim trailing whitespace"
```

**Resultado esperado:**
- `route: deterministic`
- Patch unificado gerado removendo espaços finais em `*.py`.
- `patch: applied` (governado por `TOOL-PATCH-APPLY`).
- Nenhuma chamada de modelo (custo de inferência = 0).

**Verificação:** com `vendor/vial-core` presente, checar em `vial --status`:
`costs.inference == 0` e `executions >= 1`; e `vial --status --trace <id>` exibe
a decisão determinística.

---

## 2. Geração com modelo + aplicação governada de patch

**Prompt:**
```text
vial --fix "add a function soma(a, b) that returns a+b and exports it" --include "*.py" --keep-on-failure
```

**Resultado esperado:**
- `route: advanced` (modelo `reasoning` ou configurado).
- Patch validado contra os arquivos selecionados (`allowed_paths`).
- `patch: applied` passa pela cadeia Decision → Authorization → Tool.
- Telemetria registra `fix`, `patch_applied` em `.vial-cache/events.jsonl`.

**Verificação:** arquivos alterados no workspace; `vial --status` mostra
`decisions`, `audit_records` e `costs.inference > 0` incrementados.

---

## 3. Verificação de testes + rollback automático

**Prompt:**
```text
vial --fix "make the function fail the test suite" --include "*.py" --test-command python -m unittest discover -s tests
```

**Resultado esperado:**
- Patch aplicado, testes executados via `TOOL-RUN-TEST`.
- `tests: failed`, patch revertido automaticamente (`patch: rolled back`).
- Compensação registrada como transação auditável (`ROLLBACK-<hash>`).
- Exit code `1`.

**Verificação:** `git diff` vazio após a execução; com runtime presente,
`vial --status --trace <decision_id>` mostra o outcome de falha.

**Contra-cenário:** repetir com `--keep-on-failure`; o patch permanece aplicado.

---

## 4. Reuso cognitivo (RFC-008)

**Prompt (executar duas vezes seguidas):**
```text
vial --fix "add encoding header" --include "*.py"
```

**Resultado esperado (segunda execução):**
- Primeira execução: `route: deterministic`, patch aplicado.
- Segunda execução (arquivos inalterados): reuso validado, `route: reuse`,
  modelo não invocado.

**Verificação:** `vial --status` mostra `reuse.hits` incrementado e
`costs.inference` inalterado na segunda rodada.

---

## 5. Roteamento (auto vs. modelo fixo + pool)

**Prompt:**
```text
vial --fix "explain how the router selects a tier" --include "*.py"
```

**Resultado esperado:**
- `route: light` (palavra "explain" → tier leve) ou `fast`, dependendo da
  configuração do pool.
- Com `--model openai/gpt-4o`, o roteador não analisa o prompt:
  `route` = modelo fixado, pool ignorado.

**Verificação:** rodar o mesmo prompt com e sem `--model <provider>/<model>` e
comparar `route` no stdout.

---

## 6. Execução governada de comandos

**Prompt:**
```text
vial --run "python --version"
```

**Resultado esperado:**
- Comando executado apenas se permitido pelo allowlist (`CommandRunner`).
- Com runtime: passa por `TOOL-RUN-BUILD` com Decision autorizada.
- `python --version` impresso no stdout, exit code `0`.

**Contra-cenário:**
```text
vial --run "rm -rf /"
```
Esperado: rejeitado (`NOT_ALLOWED` / `REJECTED`), exit code `2`, sem execução.

---

## 7. Revisão de patch sem aplicar

**Prompt:**
```text
vial --review /tmp/vial-eval/change.patch
```

**Resultado esperado:**
- Patch validado (`git apply --check`) e listado (`files: ...`).
- Patch impresso na íntegra, sem alterar o workspace.
- Evento `review` gravado na telemetria.

**Contra-cenário:** patch que toca arquivos fora do workspace → erro
`patch path escapes workspace`, exit code `1`.

---

## 8. Snapshot organizacional e audit trail

**Prompt:**
```text
vial --status
vial --status --trace DEC-0001
```

**Resultado esperado:**
- `vial --status` imprime JSON com `organization_id`, `resources`, `tools`,
  `reuse`, `coordinator`, `decisions`, `audit_records`, `costs` e `state_root`.
- `--trace` reconstitui a decisão: objetivo, autoridade, evidência, aprovação,
  registros de auditoria e contexto.

**Verificação:** depois dos cenários 1–4, `decisions`, `executions` e
`costs.inference` refletem as operações realizadas.

---

## 9. TUI — chat e comandos

```text
vial
vial -c        # continua a última sessão
vial -s <id>   # retoma uma sessão específica
```

**Prompts dentro do TUI:**
```text
/model openai/gpt-4o
/pool add openai/gpt-4o-mini
/status
/sessions
/trace <decision_id>
/approve <decision_id>
/clear
/exit
```

**Resultado esperado:**
- `/model` fixa o modelo; `/pool` monta o pool de roteamento paralelo.
- `/status` mostra session, model, routing, agent, pool e contagem de mensagens.
- `/trace` e `/approve` exigem runtime (`--vial-root`); sem ele, mensagem de
  indisponibilidade.
- `/clear` inicia nova sessão; `/exit` encerra.
- Prompt normal de chat roteia pelo `RoutingGraph` e grava a conversa em
  `.vial-sessions/<id>.jsonl`.

---

## 10. Persistência de estado (RFC-003)

**Prompt:**
```text
vial --status
# executar cenários 1–4
vial --status
```

**Resultado esperado:**
- Com `persist_state: true`, `.vial-state/` contém `organization.json`,
  `decisions.json`, `intents.json`, `reuse.json`, `audit.json`,
  `approvals.json` e `cost.json`.
- Estado restaurado na próxima execução (mesma `state_version`, histórico e
  custos acumulados).

**Verificação:** comparar a saída de `vial --status` antes e depois; reiniciar
o processo e confirmar que `costs` e `reuse` persistem.

---

## Critérios de aceite

- Deterministic First nunca invoca modelo.
- Todo patch passa por Decision + Authorization antes de mutar arquivos.
- Falha de verificação reverte o patch e registra compensação auditável.
- Reuso evita custo de inferência em tarefas idênticas com arquivos inalterados.
- Execução de comandos é restrita por allowlist; nunca irrestrita por padrão.
- Estado, decisões, custos e auditoria persistem e são recuperáveis.
