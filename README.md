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
python -m benchmark.run_benchmark
python -m benchmark.run_benchmark --agent --model openai/gpt-5.5
python -m benchmark.run_benchmark --adapters baseline,opencode,vial --model openai/gpt-5.5
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

Os modelos usam o formato `provider/model` aceito pelo CLI OpenCode. Exemplos
públicos usados neste projeto são `openai/gpt-5.5` e
`opencode/deepseek-v4-flash-free`; a disponibilidade depende da autenticação e
da instalação local do provider. Não use aliases internos de uma execução como
identificadores públicos de configuração.

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
Para gerar esse consenso automaticamente, informe um segundo modelo independente
com `--consensus-model`; candidatos divergentes são bloqueados.
Para Astropy, o ambiente usa a imagem pré-construída
`vial-code-agent-swebench-python39:local`, que fixa `pytest==7.4.4`, `Cython<3`,
`pytest-astropy==0.9.0` e `pytest-astropy-header==0.1.2`, compila as extensões
com `build_ext --inplace` apenas quando ainda não há artefatos, e executa pytest
com o plugin de warnings desativado,
compatível com os commits históricos do SWE-bench.

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

### Custo da Segurança Fail-Closed

O protocolo de consenso troca custo de inferência por menor risco de aplicar uma
solução inválida. Em uma execução diagnóstica de 10 tarefas, foram registradas
48 tentativas de candidato, 28 patches retornados, 23 patches estaticamente
válidos e 17 candidatos aprovados também pelos testes comportamentais. A
execução consumiu 199.713 tokens, ou aproximadamente 19.971 tokens por tarefa.

Nesse relatório, `candidate_completion_rate` é a razão entre patches retornados
e tentativas de candidato (`28/48 = 0,58`). Já
`candidate_reliability_rate` exige validade estática e aprovação comportamental,
e usa todas as tentativas como denominador (`17/48 = 0,35`). Retries e respostas
sem patch permanecem no denominador; portanto, essas métricas tornam visível o
trabalho de modelo descartado antes da governança. O custo é intencional: o
fluxo é fail-closed e não muta o workspace sem evidência independente suficiente.

Esses números são diagnósticos de uma execução específica, não uma estimativa
fixa de custo. Variam conforme modelo, prompt, workload, retries e testes.

### Primeira Evidência SWE-bench Publicada

O primeiro relatório real versionado está disponível em
[`benchmark/results/swebench-lite-10-consensus-2026-08-23.json`](benchmark/results/swebench-lite-10-consensus-2026-08-23.json).
Ele cobre 10 tarefas do SWE-bench Lite com dois candidatos independentes,
validação comportamental e adjudicação quando aplicável. O resultado foi 7/10
end-to-end, com 6/10 candidatos A válidos, 7/10 candidatos B válidos e 7/10
consensos aprovados. As três tarefas bloqueadas permanecem no relatório, com
suas evidências de candidato insuficiente, em vez de serem removidas do score.

Uma comparação sintética de 100 tarefas por adaptador está disponível em
[`benchmark/results/synthetic-adapter-cost-comparison-2026-08-23.json`](benchmark/results/synthetic-adapter-cost-comparison-2026-08-23.json).
Nesse workload, `opencode` e `vial` obtiveram 100/100. O caminho VIAL consumiu
84.701 tokens contra 66.756 do caminho `opencode` (+26,9%) e teve latência média
de 9,40 s contra 9,01 s (+4,3%). Essa é uma medida do protocolo completo neste
benchmark sintético, não do overhead isolado do VIAL Core nem uma estimativa de
qualidade em SWE-bench.

## Instalação

O projeto depende do VIAL Core em `vendor/vial-core`, configurado como um
submódulo Git. Para um clone novo, inicialize o repositório incluindo os
submódulos:

```text
git clone --recurse-submodules https://github.com/Emerson-Santos1205/vial-code-agent.git
cd vial-code-agent
```

Se o repositório já foi clonado sem `--recurse-submodules`, inicialize o
submódulo manualmente:

```text
git submodule update --init --recursive
```

Confirme que `vendor/vial-core` existe antes de executar a aplicação ou os
benchmarks. Em seguida, instale o pacote em modo editável:

```text
python -m pip install -e .
```

## Licença

O VIAL Code Agent é distribuído sob a [Apache License 2.0](LICENSE). O VIAL
Core incluído em `vendor/vial-core` é um submódulo separado, com sua própria
declaração de licença.

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
