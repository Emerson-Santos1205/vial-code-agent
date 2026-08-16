# release-orchestrator

CLI em Python para preparar releases de projetos Git com segurança.

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
