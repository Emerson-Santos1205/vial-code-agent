# VIAL Code Agent

Agente de coding baseado no VIAL Core, com Runtime governado, persistência
auditável e uma CLI de release segura.

## Camadas

- `vial_code_agent.core`: integração com o VIAL Core.
- `vial_code_agent.vial_runtime`: estado, autorização, consenso, ferramentas e mutações.
- `vial_code_agent.agent`: geração de código e roteamento de modelos.
- `vial_code_agent.api`: fronteira pública estável para integrações.
- `vial_code_agent.cli` e `vial_code_agent.app`: interfaces, sem regras de governança próprias.

Mutação de workspace deve passar por `VialRuntime.apply_patch`; o caminho
governado valida escopo, autorização, consenso, auditoria, commit e recuperação.

## Coding agent

```text
python -m vial_code_agent --root . --vial-root vendor/vial-core --prompt "inspect the project"
python benchmark/run_benchmark.py
python benchmark/run_benchmark.py --agent --model openai/gpt-5.6-luna
python benchmark/run_benchmark.py --adapters baseline,opencode,vial --model openai/gpt-5.6-luna
```

O benchmark padrão é um **Unit / Regression Benchmark** sintético: executa 100
fixtures isoladas em oito categorias, todas derivadas de transformações pequenas
e determinísticas. Ele mede aplicação de patch, validação, rollback, retries e
execução de testes; **não é uma estimativa de qualidade de coding agent e não
substitui SWE-bench**.

O modo `--agent` (ou `--adapter opencode`) gera o patch através do coding-agent
configurado, aplica-o em uma fixture descartável, roda os testes da tarefa e
grava relatório JSON em `benchmark/results/`. Os relatórios incluem taxa de
sucesso, latência, tokens, regressões, falhas de patch, rollbacks e intervenção
humana.

Nos relatórios SWE-bench, o sucesso é decomposto em duas métricas: `agent_success_rate`
é soluções corretas dividido pelas tarefas ambientalmente válidas, enquanto
`end_to_end_success_rate` é soluções corretas dividido por todas as tarefas.
Assim, falhas classificadas como `environment` não são confundidas com falhas do
agente, mas continuam incluídas na avaliação end-to-end.

`--adapters baseline,opencode,vial` executa a mesma matriz sintética em três caminhos:
provider direto, agente convencional e agente composto pelo VIAL Runtime.
Workloads reais podem ser fornecidos com `--workload caminho/para/workload.json`
usando a mesma estrutura de `tasks` com `id`, `category`, `prompt`, `initial`,
`patch` e `tests`.

Para baixar instâncias reais do SWE-bench Lite:

```text
python benchmark/fetch_swebench.py --split test --offset 0 --length 10 --out benchmark/swebench-lite-real.json
```

O dataset contém issue, repositório, commit base, patch de referência e listas
de testes. A execução completa requer clonar cada repositório no commit base e
instalar suas dependências, por isso não é tratada como fixture local simples.
No executor SWE-bench, a imagem de testes é escolhida por instância/repositório
quando `--test-image` não é informado; esse parâmetro existe apenas como
override experimental para reproduções controladas.
O contrato de ambiente é resolvido antes do workspace e pode declarar versão
Python, dependências, comando de testes e metadados. As imagens são famílias
reutilizáveis por versão, não uma imagem obrigatória por instância.
O relatório SWE-bench é persistido em `benchmark/results/` e registra repositório,
commit base, imagem, Python, dependências, timeout, classificação e evidência por
tarefa. A aplicação do patch do agente é fail-closed: sem consenso independente
fornecido por `--consensus-file`, a tarefa é bloqueada sem mutar o workspace.

Validação de testes em sandbox Docker:

```text
python benchmark/run_sandbox.py --limit 1
```

O executor usa rede desabilitada, filesystem read-only e apenas `/tmp` gravável.

Imagem do provider OpenCode:

```text
docker build -f docker/opencode.Dockerfile -t vial-code-agent-opencode:1.18.18 .
docker run --rm vial-code-agent-opencode:1.18.18 --version
```

Credenciais devem ser montadas somente durante a execução, nunca copiadas para
a imagem:

```text
docker run --rm --network none \
  --mount type=bind,src=%USERPROFILE%\.local\share\opencode\auth.json,dst=/root/.local/share/opencode/auth.json,readonly \
  vial-code-agent-opencode:1.18.18 providers list
```

Consenso para mutações pode exigir evidência: cada candidato é aplicado em uma
cópia descartável e validado estaticamente; quando `--test-command` é usado,
os testes comportamentais também precisam passar antes do consenso ser aceito.

## Instalação

```text
python -m pip install -e .
```

O comando principal é `python -m release_orchestrator`.

## Uso

```text
python -m release_orchestrator scan
python -m release_orchestrator scan --json
python -m release_orchestrator changelog release-orchestrator-v0.1.0 --force
python -m release_orchestrator check --allow-dirty
python -m release_orchestrator release 1.2.3 --confirm
python -m release_orchestrator release 1.2.3 --confirm --dry-run
python -m release_orchestrator rollback 1.2.3 --confirm
python -m release_orchestrator rollback 1.2.3 --dry-run
```

## Subcomandos

- `scan`: mostra branch atual, último commit e arquivos modificados.
- `changelog`: gera `CHANGELOG.md` a partir de uma tag.
- `check`: valida README, testes, segredos, suíte de testes e working tree.
- `release`: valida semver, exige `--confirm`, executa checks, atualiza `VERSION` e `CHANGELOG.md`, e cria tag anotada.
- `rollback`: remove somente a tag criada pela ferramenta.

## Códigos de saída

- `0`: sucesso.
- `1`: falha de validação, repositório sujo, arquivo secreto, teste quebrado, confirmação ausente ou operação recusada.

Todos os erros são enviados para `stderr`.

## Limitações

- Usa apenas a biblioteca padrão do Python.
- Requer `git` e `python -m unittest` disponíveis no ambiente.
- `changelog` recusa sobrescrever `CHANGELOG.md` sem `--force`.
- `rollback` remove apenas tags no formato `release-orchestrator-vMAJOR.MINOR.PATCH`.
- `--dry-run` não persiste alterações.
- `release` exige `--confirm` antes de criar tag e atualizar arquivos.

## JSON

`scan`, `check` e `changelog` suportam `--json` para saída estruturada.
