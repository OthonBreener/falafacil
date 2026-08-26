# Ciclo de Release e Publicação Homebrew

Este documento é o guia autoritativo para o ciclo de vida de versionamento, compilação, publicação e distribuição do FalaFácil via GitHub Releases e Homebrew Tap no Ubuntu Linux x86_64.

---

## 1. Visão Geral e Arquitetura de Distribuição

O FalaFácil é distribuído oficialmente para usuários finais no Ubuntu como um binário Linux x86_64 autônomo (*one-file*) com a biblioteca `libportaudio.so.2` embutida pelo PyInstaller, empacotado e versionado através de uma fórmula Homebrew.

### Repositórios e Papéis

- **Repositório de Código e Releases (`OthonBreener/falafacil`)**:
  - Contém o código-fonte, suíte de testes, scripts e o workflow de automação CI/CD.
  - Hospeda as GitHub Releases públicas e imutáveis associadas a tags Git no formato `vX.Y.Z`.
  - Cada release disponibiliza dois assets de distribuição com permissão executável `0755`:
    1. `falafacil-linux-x86_64` (binário raw)
    2. `falafacil-X.Y.Z-linux-x86_64.tar.gz` (tarball contendo o executável `falafacil` na raiz)
- **Repositório do Tap Homebrew (`OthonBreener/homebrew-falafacil`)**:
  - Repositório público que contém exclusivamente a fórmula oficial `Formula/falafacil.rb`.
  - Nome lógico totalmente qualificado da fórmula: `OthonBreener/falafacil/falafacil`.
  - A atualização do arquivo `Formula/falafacil.rb` é realizada exclusivamente pela automação do GitHub Actions autenticada pelo secret `HOMEBREW_TAP_TOKEN`.

### Invariantes de Publicação e Atualização

- **Commits em `main` não publicam releases**: Fazer push para a branch `main` atualiza o repositório de código, mas **não** gera release no GitHub nem altera a fórmula do Homebrew Tap.
- **Gatilho de release**: A publicação de uma nova versão é disparada exclusivamente pela criação e push de uma tag Git anotada no formato `vX.Y.Z` (ou via `workflow_dispatch` com input explícito de tag para retentativas seguras).
- **Atualização pelo aplicativo vs. `brew update`**:
  - O comando `brew update` atualiza apenas os metadados locais das fórmulas e taps do Homebrew, **não** instalando a nova versão do binário.
  - A atualização do produto é realizada pela interface em **Configurações → Atualizações → Instalar atualizações** (controlada assincronamente por `HomebrewUpdateController` via `update-if-needed` → `outdated` → `upgrade` → probe) ou diretamente por `brew upgrade OthonBreener/falafacil/falafacil`.
- **Separação do Daemon de Atalhos**:
  - O serviço local de atalhos de teclado e mouse (`shortcut_service`) é gerenciado separadamente por `PROTOCOL_VERSION` e autorização administrativa única via `pkexec`.
  - Atualizações do executável do FalaFácil via Homebrew não afetam nem reescrevem o serviço privilegiado já autorizado, a menos que haja alteração explícita de protocolo.

---

## 2. Migração de Instalação de Desenvolvimento para Homebrew

Desenvolvedores que utilizavam a instalação local via `./scripts/install_desktop.sh` possuem o executável copiado em `~/.local/bin/falafacil` e o lançador registrado em `~/.local/share/applications/falafacil.desktop`.

Para migrar para a distribuição oficial gerenciada pelo Homebrew sem perder configurações ou credenciais:

### Passo a passo de migração

1. **Remover o binário de desenvolvimento e o lançador antigo**:
   ```bash
   rm -f ~/.local/bin/falafacil ~/.local/share/applications/falafacil.desktop
   ```
   *(Não remova os arquivos de configuração, banco de dados ou unidades do systemd).*

2. **Instalar a versão oficial via Homebrew**:
   ```bash
   brew update
   brew install OthonBreener/falafacil/falafacil
   ```

3. **Verificar a resolução do caminho**:
   ```bash
   command -v falafacil
   ```
   O comando deve resolver para o executável no prefixo do Homebrew (por exemplo, `/home/linuxbrew/.linuxbrew/bin/falafacil` ou `$(brew --prefix)/bin/falafacil`).

4. **Executar o FalaFácil uma vez no terminal**:
   ```bash
   falafacil
   ```
   No primeiro startup sob Homebrew, a aplicação detecta o marker `libexec/falafacil-homebrew.json` e registra atomicamente o arquivo `~/.local/share/applications/falafacil.desktop` apontando para o caminho estável `opt_bin/falafacil` com `TryExec` seguro e permissões `0644`.

5. **Confirmação de Preservação de Dados**:
   - As preferências e o banco de dados SQLite local no diretório de dados padrão da aplicação Qt (`AppDataLocation`) contendo o modelo Gemini selecionado, prioridades de microfone e histórico de tokens permanecem intactos.
   - A chave API armazenada com segurança no Secret Service do desktop (Keyring) continua sendo lida normalmente.
   - O serviço de atalhos globais de teclado/mouse configurado no systemd do usuário (`systemd --user`) continua ativo e pareado.

---

## 3. Critérios de Versionamento Semântico (SemVer)

O FalaFácil segue rigorosamente o padrão SemVer (`MAJOR.MINOR.PATCH`):

- **PATCH (`0.2.X` → `0.2.X+1`)**:
  - Correções de bugs na interface, áudio, transcrição ou atalhos.
  - Atualizações internas de dependências ou correções de empacotamento que não alterem contratos observáveis.
  - Melhorias de documentação e ajustes de mensagens sanitizadas.
- **MINOR (`0.X.0` → `0.X+1.0`)**:
  - Adição de novos modelos Gemini à allowlist ou novos recursos na interface.
  - Novos argumentos de linha de comando retrocompatíveis ou melhorias no fluxo de atalhos.
- **MAJOR (`X.0.0` → `X+1.0.0`)**:
  - Alterações incompatíveis com preferências ou esquemas salvos no SQLite.
  - Quebras de contrato público no formato do protocolo de atalhos ou descontinuação de suporte.

### Seleção, Inferência e Validação Monotônica de Versão

1. **Validação de Formato e Monotonicidade Estrita**: A versão alvo `X.Y.Z` deve seguir o formato SemVer simples (`MAJOR.MINOR.PATCH` com inteiros não-negativos correspondendo a `^[0-9]+\.[0-9]+\.[0-9]+$`). A tupla numérica `(target_major, target_minor, target_patch)` deve ser estritamente maior que a tupla da versão atual em `src/falafacil/__init__.py` e estritamente maior que a tupla da tag de release mais recente (`git describe --tags --abbrev=0`). Rejeite e interrompa imediatamente diante de versões iguais ou inferiores (regressões/downgrades), mesmo que a tag nunca tenha sido usada.
2. **Versão especificada pelo usuário**: Se o usuário fornecer explicitamente a versão SemVer alvo (`X.Y.Z`), validar a monotonicidade estrita e honrar a definição.
3. **Inferência automática a partir do repositório**: Se o usuário não especificar uma versão, analisar os commits e o diff desde a tag mais recente (`git describe --tags --abbrev=0` ou `git log <latest_tag>..HEAD`) e inferir conservadoramente o tipo de incremento (`PATCH`, `MINOR` ou `MAJOR`), garantindo a estrita monotonicidade.
4. **Resolução de ambiguidades**: Se houver dúvida real entre incremento `MAJOR` e `MINOR` que não possa ser resolvida pela análise objetiva do diff e dos commits, solicitar esclarecimento ao usuário antes de iniciar qualquer mutação no repositório.
---

## 4. Descoberta e Classificação de Contratos Vinculados à Versão

### Fonte Única de Verdade

A versão do projeto é declarada em um único lugar no código-fonte:
```python
# src/falafacil/__init__.py
__version__ = "X.Y.Z"
```

- `pyproject.toml` lê esta definição dinamicamente através de `[tool.setuptools.dynamic] version = {attr = "falafacil.__version__"}`.
- O runtime Qt define `QApplication.setApplicationVersion(__version__)` a partir deste valor durante `falafacil.app:main`.

### Descoberta Dinâmica com `git grep`

Em vez de depender de listas estáticas pré-fixadas, utilize o `git grep` para descobrir todas as referências à versão atual antes de aplicar o bump:

```bash
# Descobrir todas as ocorrências da versão atual:
git grep -F "<versão_atual>"
```

### Classificação das Ocorrências Encontradas

Nem todas as referências encontradas pelo `git grep` devem ser alteradas. É obrigatório classificar cada ocorrência:

1. **Contratos Autoritativos da Versão Atual (DEVE atualizar para `X.Y.Z`)**:
   - `src/falafacil/__init__.py`: string `__version__ = "X.Y.Z"`.
   - `AGENTS.md`: referências explícitas à versão atual do produto (`app.setApplicationVersion(__version__)` usando `falafacil.__version__` como fonte única da versão `X.Y.Z`).
   - `ARQUITETURA.md`: referências à versão única do pacote Python (`falafacil.__version__`).
   - `tests/test_app.py`: asserções sobre `fake_app.app_version` e fakes de instalação padrão.
   - `tests/test_packaging.py`: asserções de versão estrita que validam `falafacil.__version__`, `importlib.metadata.version`, o dispatch `--update-probe X.Y.Z` e cenários de tag de release.
   - `tests/test_ui.py`: asserções de interface que validem texto ou versão da aplicação quando vinculadas à versão atual.
   - `docs/agents/smoke-tests.md`: exemplos contratuais do comando `--update-probe X.Y.Z` no gate do bundle.

2. **Fixtures Sintéticas, Históricas e Exemplos (NÃO alterar indiscriminadamente)**:
   - `tests/test_homebrew_update.py`: árvores e estruturas sintéticas de teste contendo versões fictícias independentes (ex: `0.1.0`, `1.2.3`, `1.2.4`) ou casos de teste para erros de formato (`v0.2.0`, `0.2.0-beta`, `0.2`, `0.2.0\n`).
   - Exemplos genéricos em documentação que ilustrem transições teóricas de SemVer (ex: `0.2.X → 0.2.X+1`).
   - Mensagens de commits ou changelogs históricos.

---

## 5. Pré-requisitos e Gate de Validação Pré-Release

Antes de iniciar mutações no código ou preparar um release, confirme os pré-requisitos de ambiente e segurança:

### 1. Verificações Pré-Voo Não Destrutivas (Pre-Flight Checks)

A sequência de pré-voo deve garantir primeiro o estado limpo e sincronizado da branch `main` antes de realizar qualquer análise de histórico ou seleção de versão:

1. **Inspeção Não Destrutiva da Branch e Árvore de Trabalho**:
   ```bash
   git status --porcelain  # deve retornar vazio (workspace limpo)
   git branch --show-current  # deve ser "main"
   ```
   *Se o workspace estiver sujo (dirty), estiver em outra branch ou contiver arquivos não rastreados inesperados, interrompa imediatamente para que o operador decida a ação. Não execute checkout ou stash destrutivo.*

2. **Busca Remota e Fast-Forward Seguro**:
   ```bash
   git fetch origin main
   git rev-list --left-right --count main...origin/main
   ```
   *Se houver commits locais divergentes não revisados, pare imediatamente.*
   Aplicar fast-forward exclusivamente após confirmação de árvore limpa e sem divergência:
   ```bash
   git pull --ff-only origin main
   ```

3. **Autenticação GitHub CLI e Visibilidade Pública dos Repositórios**:
   ```bash
   gh auth status
   # Consultar e exigir que a visibilidade de ambos os repositórios seja "PUBLIC":
   gh repo view OthonBreener/falafacil --json visibility -q .visibility  # deve retornar "PUBLIC"
   gh repo view OthonBreener/homebrew-falafacil --json visibility -q .visibility  # deve retornar "PUBLIC"
   ```
   *Se a visibilidade de qualquer um dos repositórios não for `PUBLIC`, interrompa o processo imediatamente.*

4. **Verificação do Secret `HOMEBREW_TAP_TOKEN`**:
   - Confirmar a existência do secret no repositório `OthonBreener/falafacil` via `gh secret list`.
   - *Distinção de escopo*: `gh secret list` confirma exclusivamente a existência e metadados do secret no GitHub; a CLI e as APIs do GitHub não são capazes de inspecionar o escopo interno de tokens. É obrigatória a confirmação explícita (e não secreta) do operador de que o Fine-grained Personal Access Token armazenado foi configurado com acesso restrito exclusivamente ao repositório `OthonBreener/homebrew-falafacil` com a permissão `Contents: Read and write`. Interrompa o processo se essa confirmação não existir.
   - **NUNCA imprimir, registrar ou expor o valor do token em logs, comandos ou arquivos**.

5. **Verificação Ativa da Política de Releases Imutáveis**:
   Consultar a API do GitHub e exigir que a política de releases imutáveis esteja explicitamente habilitada no repositório:
   ```bash
   gh api repos/OthonBreener/falafacil/immutable-releases --jq .enabled  # deve retornar true
   ```
   *Não apenas reconhecer teoricamente; se a API retornar false ou erro, interrompa o processo imediatamente.*
6. **Consulta e Resolução Obrigatória de Pendências (`docs/PENDENCIAS.md`)**:
   - Antes de consolidar o resumo de release e executar o bump de versão, o operador ou agente deve consultar obrigatoriamente `docs/PENDENCIAS.md`.
   - Se existirem pendências registradas ou tarefas planejadas para a release:
     - O `implementador` implementa o código e os testes correspondentes (preservando o registro em `docs/PENDENCIAS.md` durante a etapa).
     - O `testador` executa os testes e validação de smoke (`PASS`).
     - Após a validação com `PASS`, o `implementador` remove o item resolvido de `docs/PENDENCIAS.md` (deixando o documento limpo contendo apenas `# Pendências para próximas releases\n\nNenhuma pendência no momento.` se não restarem pendências).
     - O `revisor` audita o diff completo (incluindo código, testes e a limpeza de `docs/PENDENCIAS.md`) e concede `APROVADO`.
     - O agente principal / operador de release realiza o commit das pendências resolvidas na branch `main` antes de iniciar o resumo e bump de versão.
   - Se `docs/PENDENCIAS.md` já estiver limpo (`Nenhuma pendência no momento.`), prosseguir diretamente com a análise de histórico e seleção de versão.

7. **Análise de Histórico, Resumo de Release e Seleção SemVer Monotônica (pós-sincronização)**:
   - Identificar a tag mais recente no repositório sincronizado:
     ```bash
     git describe --tags --abbrev=0
     ```
   - Analisar o histórico de commits e diff na branch `main` sincronizada desde a última tag:
     ```bash
     git log <latest_tag>..HEAD --oneline
     git diff <latest_tag>..HEAD
     ```
   - Formular e registrar um resumo conciso das mudanças (features, fixes, chores).
   - Selecionar a versão alvo `X.Y.Z`: honrar a versão SemVer informada pelo usuário ou inferir conservadoramente PATCH, MINOR ou MAJOR. Validar obrigatoriamente que a tupla numérica `(target_major, target_minor, target_patch)` é estritamente maior que a versão atual (`falafacil.__version__`) e estritamente maior que a última tag de release. Interrompa se a versão for igual ou menor. Solicitar esclarecimento ao usuário apenas se houver uma ambiguidade real não resolvida entre versão MAJOR e MINOR.

8. **Prevenção de Colisão de Versão/Tag**:
   ```bash
   # Confirmar que a versão e a tag vX.Y.Z não existem localmente nem no remoto:
   git tag -l "vX.Y.Z"
   git ls-remote --tags origin refs/tags/vX.Y.Z
   gh release view vX.Y.Z
   ```
   Se a tag ou a release já existirem, interrompa o processo imediatamente.
### 2. Gate Local do Ciclo de Agentes

O ciclo de agentes segue rigorosamente os papéis definidos em `AGENTS.md` e `docs/architecture/agentes.md`:
- **`implementador`**: é o único papel autorizado a editar `src/falafacil/__init__.py` e atualizar os contratos documentais e testes vinculados à versão.
- **`testador`**: executa o gate local completo de compilação, testes offscreen, build, probe e o smoke do binário de desenvolvedor instalado.
- **`revisor`**: audita o diff completo (`git diff`), a lista de arquivos alterados e as evidências do `testador`, concedendo aprovação formal antes de qualquer commit ou criação de tag Git.
- **Exceção estrita de release para o agente principal / operador**: em uma invocação explícita de release pelo usuário, antes do bump de versão o operador realiza a consulta obrigatória a `docs/PENDENCIAS.md`. Havendo pendências ativas para a release, elas são implementadas pelo `implementador` com testes (preservando o registro em `docs/PENDENCIAS.md`), validadas pelo `testador` (`PASS`), o item resolvido é removido de `docs/PENDENCIAS.md` pelo `implementador` (deixando o documento limpo quando não restarem pendências), o diff é auditado pelo `revisor` (`APROVADO`), e o agente principal / operador de release fica estritamente autorizado a realizar o commit das pendências resolvidas na branch `main`. Em seguida, para o bump de versão, somente após a execução completa dos gates pelo `testador` (`PASS`) e a aprovação formal do `revisor` (`APROVADO`), o operador principal é autorizado a executar os commits de bump na branch `main`, push para `origin main` e criação/push da tag anotada `vX.Y.Z`. Os papéis delegados (`implementador`, `testador`, `revisor`) continuam estritamente proibidos de realizar commits, pushes, tags ou mutações de branch/PR.

Comandos obrigatórios do gate local na raiz do repositório:

```bash
# 1. Reinstalação completa de desenvolvimento e build
poetry install --extras dev --extras build
poetry run pip install --no-deps -e .

# 2. Suíte de testes completa em modo offscreen
QT_QPA_PLATFORM=offscreen poetry run pytest -q

# 3. Compilação de bytecode Python (incluindo src, tests e scripts)
poetry run python -m compileall -q src tests scripts

# 4. Construção do executável Linux one-file
./scripts/build_executable.sh

# 5. Verificação da sonda de versão do binário compilado
./dist/falafacil --update-probe X.Y.Z

# 6. Reinstalação de desenvolvedor e smoke offscreen controlado do binário instalado
tmp_home=$(mktemp -d)
HOME="$tmp_home" ./scripts/install_desktop.sh "$PWD/dist/falafacil"
env -u GEMINI_API_KEY -u GOOGLE_API_KEY -u LD_LIBRARY_PATH HOME="$tmp_home" QT_QPA_PLATFORM=offscreen timeout 5s "$tmp_home/.local/bin/falafacil" || [ $? -eq 124 ]
```

> **Nota sobre o Smoke GUI do Binário Instalado**: O teste de fumaça deve ser executado obrigatoriamente sobre o executável instalado (`$tmp_home/.local/bin/falafacil`) no diretório temporário após o instalador de desenvolvimento, com variáveis de chave e de busca de bibliotecas (`GEMINI_API_KEY`, `GOOGLE_API_KEY`, `LD_LIBRARY_PATH`) explicitamente desarmadas via `env -u`. Aplicativos gráficos Qt operam em um event loop contínuo e não encerram naturalmente com código de saída 0 sem intervenção do usuário ou sinal; o encerramento por tempo limite (`timeout 5s`, código de saída 124) ou controle de processo comprova a vivacidade (*liveness*) e a inicialização limpa da interface gráfica sem depender de bibliotecas `libportaudio2` externas no host ou chaves em ambiente.

## 6. Procedimento de Publicação da Release

Após a aprovação formal do `revisor` no gate pré-release, execute as etapas de publicação:

### Etapa 1: Inspeção de Estado Completo, Staging Dinâmico Seguro e Commit em `main`

1. **Enumeração Completa e Segura do Repositório (`git status --porcelain=v1 -z`)**:
   Antes de preparar alterações, utilize a saída NUL-safe do Git para inspecionar todas as entradas modificadas e não rastreadas sem risco com espaços, quebras de linha ou caracteres especiais:
   - Classifique cada caminho modificado (`M`, `A`, `D`, `R`).
   - Falhe fechado e interrompa imediatamente diante de qualquer entrada não rastreada (`??`) inesperada, artefatos locais (`dist/`, `build/`, `*.pyc`), credenciais/chaves (`.env`, `*.key`, segredos), conteúdo sensível ou caminhos contendo espaços ou iniciados por hífen (`-`), a menos que expressamente revisados e aprovados pelo `revisor`.
   - Inspecione qualquer novo arquivo de forma segura através de mecanismo com delimitador de opções (`git show :./caminho` ou `cat -- "caminho"`).
   - Inspecione a lista de arquivos modificados e o diff completo:
   ```bash
   # Inspecionar diff de arquivos rastreados:
   git diff --name-only
   git diff
   ```
2. **Staging Seguro com Separador de Opções (`git add -A --`)**:
   Após a enumeração e confirmação de que todos os arquivos modificados são estritamente vinculados à release (incluindo `src/falafacil/__init__.py`, `AGENTS.md`, `ARQUITETURA.md`, `tests/test_app.py`, `tests/test_packaging.py`, `tests/test_ui.py` quando vinculado à versão, `docs/agents/smoke-tests.md`), realize o stage seguro:
   ```bash
   git add -A --
   ```
3. **Verificação do Diff em Stage, Integridade e Commit/Push em `main`**:
   Re-inspecione o diff em stage (`git diff --cached`) e execute a checagem de integridade/espaços (`git diff --cached --check`) para confirmar que apenas os arquivos esperados foram preparados:
   ```bash
   git diff --cached
   git diff --cached --check
   git commit -m "chore(release): bump version to X.Y.Z"
   git push origin main
   ```
   *(Lembrete: este push apenas atualiza a branch `main` e não inicia a publicação da release; a criação e o envio da tag Git ocorrem exclusivamente na etapa seguinte após o snapshot prévio das execuções existentes).*

### Etapa 2: Snapshot Prévio, Disparo por Tag Git Única e Acompanhamento da Automação CI (`.github/workflows/release.yml`)

O envio da tag dispara automaticamente o workflow de release no GitHub Actions. O workflow executa as seguintes fases:

1. **Validação da Tag**: Confirma que a tag segue `^v[0-9]+\.[0-9]+\.[0-9]+$` e corresponde exatamente a `falafacil.__version__`.
2. **Testes e Compilação**: Executa `pytest` offscreen e `compileall` em `src tests` (o gate local do desenvolvedor/testador verifica adicionalmente `scripts`).
3. **Build do Binário**: Constrói o executável Linux x86_64 via PyInstaller com `libportaudio.so.2` embutido.
4. **Verificação do Probe**: Executa `./dist/falafacil --update-probe X.Y.Z`.
5. **Empacotamento**: Produz `falafacil-linux-x86_64` (raw) e `falafacil-X.Y.Z-linux-x86_64.tar.gz` (tarball) com permissões `0755`.
6. **Publicação no GitHub Releases**: Invoca `scripts/publish_release.py` com o token do repositório para criar a Release pública e extrair o SHA-256 do tarball.
7. **Renderização da Fórmula**: Renderiza `Formula/falafacil.rb` a partir de `packaging/homebrew/falafacil.rb.in` com a versão e o SHA-256 calculados.
8. **Auditoria e Teste Homebrew**: Configura o Homebrew em runner Ubuntu, cria um tap temporário, audita a fórmula (`brew audit`), compila a partir do tarball (`brew install --build-from-source`) e executa a suíte de teste da fórmula (`brew test`).
9. **Sincronização no Tap**: Realiza commit e push da fórmula atualizada para `OthonBreener/homebrew-falafacil` usando o secret `HOMEBREW_TAP_TOKEN`.

**Descoberta e Acompanhamento de Execução com Snapshot Prévio**:
Para garantir tolerância a eventuais atrasos de consistência do GitHub Actions e evitar seleção de execuções concorrentes ou pré-existentes:
1. Registre previamente um snapshot de todos os IDs de execuções existentes no workflow via API paginada **antes** de criar e enviar a tag.
2. Crie a tag anotada `vX.Y.Z` localmente no commit aprovado da `main` e envie-a exatamente uma vez ao GitHub.
3. Capture o SHA exato do commit apontado pela tag Git (`vX.Y.Z`).
4. Realize polling com timeout limitado e paginação até encontrar **exatamente uma nova execução** correspondente a evento `push`, `head_branch == "vX.Y.Z"` e `head_sha == tag_sha` que não existia no snapshot.
5. Acompanhe a execução pelo ID exato com `--exit-status` e confirme a conclusão `success`.

```bash
# 1. Snapshot dos IDs de runs existentes ANTES de criar e enviar a tag:
pre_tag_runs=$(gh api repos/OthonBreener/falafacil/actions/workflows/release.yml/runs --paginate -q '.workflow_runs[].id' | sort -u)

# 2. Criar a tag anotada vX.Y.Z no commit aprovado da main e enviar exatamente uma vez:
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z

# 3. Capturar o SHA do commit apontado pela tag:
tag_sha=$(git rev-list -n 1 "vX.Y.Z")

# 4. Polling com timeout limitado e paginação até surgir exatamente 1 novo run:
run_id=""
run_url=""
deadline=$((SECONDS + 120))
while [ $SECONDS -lt $deadline ]; do
  matching_candidates=$(gh api "repos/OthonBreener/falafacil/actions/workflows/release.yml/runs?event=push&per_page=100" --paginate \
    --jq ".workflow_runs[] | select(.head_branch == \"vX.Y.Z\" and .head_sha == \"$tag_sha\") | {id: .id, url: .html_url}")

  new_matching_runs=""
  if [ -n "$matching_candidates" ]; then
    while IFS= read -r candidate_json; do
      [ -z "$candidate_json" ] && continue
      cid=$(echo "$candidate_json" | jq -r '.id')
      if ! echo "$pre_tag_runs" | grep -qx "$cid"; then
        new_matching_runs="${new_matching_runs}${candidate_json}\n"
      fi
    done < <(echo "$matching_candidates" | jq -c '.')
  fi

  matched_count=$(echo -e "$new_matching_runs" | sed '/^$/d' | wc -l)
  if [ "$matched_count" -eq 1 ]; then
    run_id=$(echo -e "$new_matching_runs" | sed '/^$/d' | jq -r '.id')
    run_url=$(echo -e "$new_matching_runs" | sed '/^$/d' | jq -r '.url')
    break
  elif [ "$matched_count" -gt 1 ]; then
    echo "Erro crítico: múltiplos ($matched_count) runs novos encontrados para a tag vX.Y.Z ($tag_sha)."
    exit 1
  fi
  sleep 2
done

if [ -z "$run_id" ]; then
  echo "Erro crítico: timeout aguardando criação do run para a tag vX.Y.Z ($tag_sha)."
  exit 1
fi

echo "Monitorando execução CI identificada: $run_url (ID: $run_id)"

# 5. Acompanhar a execução com retorno de código de saída do workflow:
gh run watch "$run_id" --exit-status

# 6. Conferir conclusão bem-sucedida (deve ser estritamente 'success'):
conclusion=$(gh run view "$run_id" --json conclusion -q .conclusion)
if [ "$conclusion" != "success" ]; then
  echo "Workflow encerrou com conclusão '$conclusion' (não-sucesso). Interrompendo release."
  exit 1
fi
```
Interrompa o processo em qualquer conclusão diferente de `success` (`failure`, `cancelled`, `timed_out`, `action_required`).
---

## 7. Validação Pós-Publicação

Após o workflow concluir com sucesso (verde), realize a conferência dos artefatos públicos:

### 1. Verificação da Release no GitHub
```bash
gh release view vX.Y.Z --json tagName,isDraft,isImmutable,url,assets
```
Confirme explicitamente que `isDraft` é `false` e `isImmutable` é `true`, a tag é `vX.Y.Z` e a lista de assets contém `falafacil-linux-x86_64` e `falafacil-X.Y.Z-linux-x86_64.tar.gz`. Interrompa o processo imediatamente se `isImmutable` for `false`, `null` ou indisponível na resposta da release, mesmo que a política do repositório esteja habilitada.
### 2. Download do Tarball e Conferência Independente do SHA-256
Baixe o tarball público disponibilizado na Release, calcule seu hash SHA-256 de forma independente e compare-o byte a byte com o hash declarado na fórmula do tap Homebrew:
```bash
# Baixar o tarball público:
curl -fsSL -o "/tmp/falafacil-X.Y.Z-linux-x86_64.tar.gz" \
  "https://github.com/OthonBreener/falafacil/releases/download/vX.Y.Z/falafacil-X.Y.Z-linux-x86_64.tar.gz"

# Calcular o hash SHA-256 do arquivo baixado:
calc_sha=$(sha256sum "/tmp/falafacil-X.Y.Z-linux-x86_64.tar.gz" | awk '{print $1}')

# Obter o hash registrado na fórmula oficial do Tap:
formula_sha=$(gh api repos/OthonBreener/homebrew-falafacil/contents/Formula/falafacil.rb -q .content | base64 -d | grep 'sha256 "' | sed -E 's/.*sha256 "([a-f0-9]{64})".*/\1/')

# Exigir igualdade estrita:
if [ "$calc_sha" != "$formula_sha" ]; then
  echo "Erro crítico: SHA-256 calculado ($calc_sha) diverge do hash na fórmula ($formula_sha)"
  exit 1
fi
echo "SHA-256 verificado com sucesso: $calc_sha"
```

### 3. Verificação do Tap Homebrew
Acesse o repositório `OthonBreener/homebrew-falafacil` ou utilize `gh api` e confirme que `Formula/falafacil.rb` contém:
- A URL apontando para a tag `vX.Y.Z`.
- O hash `sha256` exato do tarball da release (idêntico a `$calc_sha`).

### 4. Validação de Instalação Limpa e Primeiro Startup Homebrew
Em um ambiente Ubuntu com Homebrew 6+:
```bash
brew update
brew install OthonBreener/falafacil/falafacil
brew test OthonBreener/falafacil/falafacil

# Validação do primeiro startup Homebrew com HOME temporário isolado e variáveis desarmadas:
tmp_home=$(mktemp -d)
brew_prefix=$(brew --prefix)
brew_bin="$brew_prefix/bin/falafacil"
opt_launch="$brew_prefix/opt/falafacil/bin/falafacil"

# Executar o binário do Homebrew em modo offscreen com timeout controlado (liveness):
env -u GEMINI_API_KEY -u GOOGLE_API_KEY -u LD_LIBRARY_PATH HOME="$tmp_home" \
  QT_QPA_PLATFORM=offscreen timeout 5s "$brew_bin" || [ $? -eq 124 ]

# Validar que o desktop entry foi criado automaticamente com permissão 0644 e caminhos corretos:
desktop_file="$tmp_home/.local/share/applications/falafacil.desktop"
if [ ! -f "$desktop_file" ]; then
  echo "Erro: desktop entry não foi criado no primeiro startup Homebrew ($desktop_file)"
  exit 1
fi

mode=$(stat -c "%a" "$desktop_file")
if [ "$mode" != "644" ]; then
  echo "Erro: permissão do desktop entry inválida: $mode (esperado 644)"
  exit 1
fi

exec_line=$(grep '^Exec=' "$desktop_file" | cut -d= -f2-)
tryexec_line=$(grep '^TryExec=' "$desktop_file" | cut -d= -f2-)
if [ "$exec_line" != "\"$opt_launch\"" ] || [ "$tryexec_line" != "$opt_launch" ]; then
  echo "Erro: Exec/TryExec no desktop entry diverge do caminho Homebrew opt estável ($opt_launch)"
  exit 1
fi

# Confirmar que command -v falafacil resolve para o binário do Homebrew:
if [ "$(command -v falafacil)" != "$brew_bin" ]; then
  echo "Erro: command -v falafacil ($(command -v falafacil)) diverge do executável Homebrew ($brew_bin)"
  exit 1
fi
```
### 5. Validação da Atualização pela Interface (a partir da 2ª release)
Em uma instalação existente da versão anterior:
1. Abra o FalaFácil.
2. Abra **Configurações → Atualizações**.
3. Clique em **Instalar atualizações**.
4. Observe a sequência automática e confirme a solicitação de reinício para a versão `X.Y.Z`.

### 6. Conferência de Estado Git e Sincronização
Confirme e apresente a evidência de que o repositório local está perfeitamente limpo e que o commit do HEAD local de `main` é exatamente idêntico ao SHA de `origin/main`:
```bash
git status --porcelain  # deve retornar vazio (workspace limpo)

# Exigir igualdade exata entre o HEAD local de main e origin/main:
local_head=$(git rev-parse HEAD)
remote_head=$(git rev-parse origin/main)
if [ "$local_head" != "$remote_head" ]; then
  echo "Erro: HEAD local ($local_head) diverge de origin/main ($remote_head)"
  exit 1
fi
echo "Repositório local perfeitamente sincronizado com origin/main ($local_head)"
```

### 7. Relatório Final Obrigatório de Conclusão da Release (Contrato de Saída)
Ao finalizar a publicação, a automação ou operador deve apresentar um relatório estruturado contendo todos os seguintes campos comprobatórios:
1. **Versão, Resumo de Release e Tag**: versão SemVer `X.Y.Z`, resumo conciso das mudanças e tag Git `vX.Y.Z`.
2. **Commit em `main`**: hash do commit de bump na branch `main`.
3. **CI Workflow Run**: ID da execução, URL do run no GitHub Actions e conclusão (`success`).
4. **GitHub Release**: URL da release pública, confirmação do estado imutável (`isDraft: false`, `isImmutable: true`), lista de assets publicados e hash SHA-256 calculado do tarball.
5. **Homebrew Tap**: hash do commit no tap `OthonBreener/homebrew-falafacil`, URL da fórmula `Formula/falafacil.rb` e hash `sha256` na fórmula (confirmado idêntico ao calculado).
6. **Evidências de Instalação Homebrew**: confirmação dos comandos `brew install`, `brew test`, registro do `.desktop` no primeiro startup e caminho retornado por `command -v falafacil`.
7. **Estado do Repositório Local**: confirmação de árvore de trabalho limpa (`git status --porcelain` vazio) e igualdade exata entre o SHA do HEAD local de `main` e `origin/main`.
## 8. Tratamento de Incidentes e Regras de Segurança

### Invariantes Estritos

- **NUNCA mover ou recriar tags remotas**: Tags no GitHub Releases são tratadas como imutáveis por gerenciadores de pacotes. Mover ou recriar uma tag quebra caches e SHAs baixados por usuários.
- **NUNCA fazer push manual na fórmula do Tap**: Todas as modificações em `OthonBreener/homebrew-falafacil` devem originar-se do workflow automatizado de CI.
- **NUNCA expor ou imprimir secrets**: O token `HOMEBREW_TAP_TOKEN` deve possuir permissão restrita (`Contents: Read and write` somente no repositório `homebrew-falafacil`) e nunca deve ser exibido em logs ou saídas de terminal.

### Retentativa Segura via `workflow_dispatch` com Descoberta Pagina e Polling

Se o workflow de release falhar após a publicação dos assets no GitHub Releases (por exemplo, por falha transitória de rede durante o checkout do tap ou rate limit):

1. **NÃO delete a release nem a tag**.
2. Capture o snapshot de todos os IDs de execuções existentes via API paginada antes do despacho.
3. Registre o SHA atual da branch `main`.
4. Dispare novamente o workflow pelo GitHub Actions fornecendo a tag correspondente e referenciando explicitamente a branch `main`.
5. Realize polling com timeout limitado e paginação para localizar determinística e unicamente o novo run de `workflow_dispatch` correspondente ao commit de `main` que não existia no snapshot pré-despacho (exigindo exatamente uma execução correspondente, nunca reutilizando o ID da execução da tag nem selecionando uma execução arbitrária).
6. Acompanhe a execução até o final via `gh run watch` com verificação de `--exit-status` e confirmação explícita de `conclusion == success`.

```bash
# 1. Snapshot dos IDs de runs existentes antes do dispatch:
dispatch_pre_runs=$(gh api repos/OthonBreener/falafacil/actions/workflows/release.yml/runs --paginate -q '.workflow_runs[].id' | sort -u)
main_sha=$(git rev-parse HEAD)

# 2. Disparar a retentativa com a tag e a referência da branch main:
gh workflow run release.yml --ref main -f tag=vX.Y.Z

# 3. Polling com timeout limitado e paginação até surgir exatamente 1 novo run de workflow_dispatch:
retry_run_id=""
retry_run_url=""
deadline=$((SECONDS + 120))
while [ $SECONDS -lt $deadline ]; do
  retry_candidates=$(gh api "repos/OthonBreener/falafacil/actions/workflows/release.yml/runs?event=workflow_dispatch&per_page=100" --paginate \
    --jq ".workflow_runs[] | select(.head_sha == \"$main_sha\") | {id: .id, url: .html_url}")

  new_retry_runs=""
  if [ -n "$retry_candidates" ]; then
    while IFS= read -r candidate_json; do
      [ -z "$candidate_json" ] && continue
      cid=$(echo "$candidate_json" | jq -r '.id')
      if ! echo "$dispatch_pre_runs" | grep -qx "$cid"; then
        new_retry_runs="${new_retry_runs}${candidate_json}\n"
      fi
    done < <(echo "$retry_candidates" | jq -c '.')
  fi

  retry_matched_count=$(echo -e "$new_retry_runs" | sed '/^$/d' | wc -l)
  if [ "$retry_matched_count" -eq 1 ]; then
    retry_run_id=$(echo -e "$new_retry_runs" | sed '/^$/d' | jq -r '.id')
    retry_run_url=$(echo -e "$new_retry_runs" | sed '/^$/d' | jq -r '.url')
    break
  elif [ "$retry_matched_count" -gt 1 ]; then
    echo "Erro crítico: múltiplos ($retry_matched_count) novos runs de workflow_dispatch encontrados para $main_sha."
    exit 1
  fi
  sleep 2
done

if [ -z "$retry_run_id" ]; then
  echo "Erro crítico: timeout aguardando criação do run de retentativa para $main_sha."
  exit 1
fi

echo "Monitorando retentativa CI identificada: $retry_run_url (ID: $retry_run_id)"

# 4. Acompanhar a execução com retorno de código de saída:
gh run watch "$retry_run_id" --exit-status

# 5. Conferir conclusão bem-sucedida (deve ser estritamente 'success'):
retry_conclusion=$(gh run view "$retry_run_id" --json conclusion -q .conclusion)
if [ "$retry_conclusion" != "success" ]; then
  echo "Retentativa encerrou com conclusão '$retry_conclusion' (não-sucesso). Interrompendo release."
  exit 1
fi
```

6. O script `scripts/publish_release.py` foi projetado para ser determinístico e idempotente: ele detecta os assets já publicados na Release remota, verifica sua integridade e hash SHA-256 sem recriar a release imutável, e prossegue com segurança para a renderização da fórmula e sincronização do tap.
### Correção de Defeitos de Código

Se a release falhou devido a um bug no código ou erro de validação:
1. Aplique a correção na branch `main`.
2. Incremente a versão para o próximo PATCH (`X.Y.(Z+1)`).
3. Execute o ciclo completo de validação e gere a nova tag `vX.Y.(Z+1)`.

---

## 9. Gatilhos de Invocação da Automação

A automação de release e o fluxo de release do FalaFácil podem ser invocados por agentes ou desenvolvedores através das frases de gatilho padronizadas em português e inglês:

- `/falafacil-release`
- `nova versão`
- `lançar release`
- `publicar versão`
- `fazer release`
- `bump de versão`
- `criar tag de release`
- `release`
- `bump version`
- `create tag`
- `publish release`
- `deploy new version`

---

## 10. Navegação e Documentos Relacionados

- [README.md](../README.md) — Começo rápido e instruções de instalação recomendada.
- [AGENTS.md](../AGENTS.md) — Contratos do produto, comandos e regras de desenvolvimento.
- [ARQUITETURA.md](../ARQUITETURA.md) — Mapa dos módulos, distribuição e invariantes de runtime.
- [Índice da Documentação](INDEX.md) — Catálogo central de documentos.
- [Gate de Smoke](agents/smoke-tests.md) — Critérios de validação de bundle e testes determinísticos.
- [Skill de Release](../.agents/skills/falafacil-release/SKILL.md) — Definição da automação de release e publicação para agentes.
