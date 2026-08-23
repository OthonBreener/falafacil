# FalaFácil

Aplicativo desktop local para Ubuntu que grava fala em português do Brasil, envia o áudio ao Gemini, exibe uma transcrição editável, copia o texto para o clipboard e, em uma sessão X11 compatível, pode colá-lo no terminal ativo. O fluxo não executa comandos e não envia Enter.

## Orientação Rápida

- [README.md](README.md) — entrada do projeto e começo rápido.
- [ARQUITETURA.md](ARQUITETURA.md) — mapa dos módulos, dependências e invariantes.
- [Índice da documentação](docs/INDEX.md) — catálogo dos documentos do projeto.
- [Contrato de agentes](docs/architecture/agentes.md) — papéis e ciclo de desenvolvimento.
- [Gate de smoke](docs/agents/smoke-tests.md) — critérios de validação e smoke aplicáveis.

## Produto e fluxo

O ponto de entrada público é `falafacil`, definido em `pyproject.toml`; a mesma aplicação pode ser iniciada com `python -m falafacil`. A inicialização segue esta ordem:

1. `falafacil.app:main` cria uma única `QApplication`, define os nomes da organização e da aplicação antes de usar configurações, tenta ler a chave persistida no Secret Service pelo `KeyringApiKeyStore` e exibe `MainWindow`. Falhas do chaveiro são tratadas como ausência persistida, sem registrar a exceção ou qualquer segredo.
2. `Settings.from_env(fallback_api_key=...)` lê `GEMINI_MODEL` e escolhe a chave pela precedência `GEMINI_API_KEY`, depois `GOOGLE_API_KEY`, depois a chave persistida. A chave permanece fora de `repr` e comparações de `Settings`.
3. Sem uma chave ativa, a janela abre normalmente, mostra a mensagem de configuração e mantém a gravação desabilitada. O botão `Configurar chave API` abre um diálogo com campo de senha; ao aceitar uma chave não vazia, a UI cria o novo transcritor antes de trocar o estado, tenta persistir no chaveiro e habilita a gravação. Se o backend não estiver disponível, a chave funciona somente nesta sessão e a UI informa que não houve persistência. A configuração não chama a API Gemini.
4. Ao clicar em `Gravar`, `AudioRecorder` abre um `sounddevice.InputStream` no microfone selecionado, mono e `int16`. Tenta `16 kHz`; se o dispositivo só aceitar outra taxa nativa, usa uma taxa suportada e reamostra o PCM para `16 kHz` antes de gerar o WAV. O callback copia os blocos capturados sem analisar áudio. O botão passa a ser `Parar e revisar áudio`.
5. Ao parar, o stream é encerrado, os bytes PCM são serializados como WAV em memória e RMS/pico são validados. Captura vazia ou abaixo de `MIN_RMS_LEVEL` fica no diagnóstico e entra em erro sem chamada de rede. Uma captura válida entra em pré-visualização: `Reproduzir áudio` usa `QBuffer`/`QMediaPlayer` e `Enviar para Gemini` é a única ação que inicia a transcrição.
6. O envio ao Gemini ocorre em um `QThread` por meio de `TranscriptionWorker`. Os sinais de sucesso ou falha retornam ao thread da interface com o `TranscriptionDebug`, sem acessar widgets no worker.
7. O texto recebido aparece em um `QPlainTextEdit`, pode ser corrigido e apagado com `Apagar texto` antes de usar `Copiar texto`. O botão `Mostrar debug` alterna um painel lateral com métricas de áudio, payload limitado e retorno. Em X11, `Enviar ao terminal` coloca o texto no clipboard e envia `Ctrl+Shift+V` à janela de terminal ativa, sem pressionar Enter. Em Wayland ou sem `xdotool`, `Copiar texto` continua sendo o fallback.

Nenhuma chave é mostrada em label, tooltip, status, exceção, log, arquivo de configuração ou argumento do launcher. A persistência da configuração usa somente o Secret Service via `keyring`/`secretstorage`; `QSettings` e arquivos plaintext não são usados para esse segredo.

Erros de microfone, API, resposta vazia, áudio excedendo o limite ou terminal indisponível são apresentados no `QLabel` de status. A janela permanece recuperável: o usuário pode corrigir a condição, gravar novamente ou usar o clipboard quando a integração de terminal não estiver disponível.

### Estados `AppState`

`MainWindow.state` usa os estados definidos em `src/falafacil/ui.py`:

| Estado | Significado observável |
|---|---|
| `IDLE` | Janela pronta para iniciar uma gravação; sem chave ou microfone, exibe a mensagem correspondente. |
| `RECORDING` | `AudioRecorder` mantém um stream aberto e o botão permite parar a captura. |
| `AUDIO_READY` | Há um `AudioCapture` válido em memória, ainda não enviado; reprodução e envio explícitos estão disponíveis. |
| `TRANSCRIBING` | O WAV já foi produzido e `TranscriptionWorker` executa a transcrição no `QThread`; ações dependentes ficam desabilitadas. |
| `READY` | Há uma transcrição no editor, pronta para revisão, cópia ou envio ao terminal. |
| `ERROR` | Uma operação falhou; a mensagem fica no status e a janela pode ser usada novamente. |

Uma transcrição concluída seleciona o texto no editor e informa que ele está pronto para revisão. `Copiar texto` também está disponível pelo atalho `Ctrl+Shift+C`; `Apagar texto` limpa o editor; a tecla `Space` alterna a gravação quando a janela está focada.

## Stack

As restrições e dependências declaradas em `pyproject.toml` são:

- Python `>=3.11,<3.15`.
- PySide6 `>=6.7` para `QApplication`, `QMainWindow`, `QPlainTextEdit`, clipboard, sinais e `QThread`.
- `google-genai>=2.3.0`, usando `from google import genai` e a Interactions API.
- `sounddevice>=0.5` para a captura, com PortAudio disponível no sistema.
- NumPy `>=1.26` para os buffers entregues ao callback do `InputStream`.
- `keyring>=25.0` e `secretstorage>=3.3` para acessar o Secret Service do desktop; não são usados como armazenamento em arquivo.
- Pytest `>=8.0` na dependência opcional de desenvolvimento.
- PyInstaller `>=6.11` na dependência opcional `build`, somente para gerar o executável distribuível.

`libportaudio2` é um requisito de runtime do microfone no Ubuntu. `xdotool` é um requisito opcional do sistema somente para colagem em terminal X11; não é uma dependência Python do projeto. O ambiente de desktop deve fornecer um backend Secret Service para persistir a chave; sem ele, a configuração continua válida somente na sessão atual.

## Estrutura

| Caminho | Responsabilidade |
|---|---|
| `README.md` | Entrada do projeto, navegação e começo rápido. |
| `ARQUITETURA.md` | Mapa arquitetural, fluxo de dependências e invariantes técnicos. |
| `docs/` | Índice, contrato dos agentes de desenvolvimento e gate de smoke. |
| `.omp/agents/` | Definições dos papéis delegados de implementador, testador e revisor. |
| `pyproject.toml` | Metadados, restrição de Python, dependências de runtime, extras `dev`/`build`, entry point e configuração do Pytest. |
| `src/falafacil/__main__.py` | Encaminha `python -m falafacil` para `falafacil.app.main`. |
| `src/falafacil/app.py` | Define nomes Qt antes da configuração, carrega a chave persistida, compõe `Settings`, `GeminiTranscriber` e `MainWindow`. |
| `src/falafacil/config.py` | Define `Settings`, `DEFAULT_MODEL`, precedência entre ambiente e fallback persistido e mensagem de configuração. |
| `src/falafacil/credentials.py` | Define o protocolo `ApiKeyStore`, os nomes fixos do serviço/conta e o adaptador `KeyringApiKeyStore` para o Secret Service, sem fallback em arquivo. |
| `src/falafacil/audio.py` | Define `AudioDevice`, `AudioCapture`, `AudioRecorder`, `MIN_RMS_LEVEL`, `AudioRecorderError`, o callback de captura, listagem de entradas e serialização WAV. |
| `src/falafacil/transcription.py` | Define `GeminiTranscriber`, `TranscriptionDebug`, `TranscriptionWorker`, `TranscriptionError`, o prompt em pt-BR e `INLINE_LIMIT_BYTES`; aceita chave injetada na criação do cliente. |
| `src/falafacil/ui.py` | Define `MainWindow`, `AppState`, seletor de microfone, pré-visualização em memória, `QMediaPlayer`, painel lateral de debug, diálogo de chave, clipboard, atalhos e ciclo de `QThread`. |
| `src/falafacil/terminal.py` | Define `TerminalBridge`, `TerminalTarget`, `TERMINAL_PROCESSES`, a detecção X11 e a colagem. |
| `packaging/falafacil.spec` | Spec do PyInstaller para analisar `src/falafacil/__main__.py` e gerar o executável one-file `falafacil`, sem dados de configuração. |
| `scripts/build_executable.sh` | Executa o PyInstaller pela raiz sem propagar chaves do ambiente e informa `dist/falafacil`. |
| `scripts/install_desktop.sh` | Instala uma cópia executável em `~/.local/bin/falafacil` e um `.desktop` gerenciado, com `Exec`/`TryExec` absolutos, `Terminal=false` e sem shell. |
| `tests/` | Testes determinísticos de configuração, credenciais, UI offscreen, transcrição/cliente fake, WAV, captura, terminal fake e instalador. |

### Fluxo de dependências

```text
app
 └─> ui
      ├─> credentials ─────────> keyring / Secret Service
      ├─> audio ───────────────> sounddevice / PortAudio
      ├─> transcription ───────> google-genai
      └─> terminal ────────────> xdotool / X11

packaging ──> __main__ ──> app
tests ──────> módulos via dependências injetadas (store, factory, stream, cliente,
              subprocesso e ambiente)
```

A aplicação não possui camadas de servidor, banco, ORM, API própria ou repositório. A fronteira de rede fica no cliente `google-genai`; áudio, Secret Service e terminal são integrações locais.

## Regras Fundamentais

- A chave Gemini nunca deve ser gravada em código, testes, `pyproject.toml`, desktop entry, logs ou arquivos gerados. A fonte ativa segue `GEMINI_API_KEY` > `GOOGLE_API_KEY` > valor do Secret Service; a UI também pode aceitar uma chave em memória nesta sessão.
- `KeyringApiKeyStore` usa exatamente o serviço/conta definidos em `src/falafacil/credentials.py` e encapsula erros sem incluir a chave na mensagem. Não criar fallback em arquivo, `QSettings` ou argumento de processo.
- Sem chave configurada, a inicialização não cria `GeminiTranscriber`, a gravação fica desabilitada e o fluxo não faz chamada de rede. Configurar pela UI cria o transcritor antes de substituir `Settings`; falha da factory não altera o estado anterior.
- Widgets Qt, player multimídia e clipboard Qt só são acessados no thread principal. O worker comunica o resultado com os sinais `finished` e `failed`, conectados a slots da UI.
- O callback do PortAudio deve somente copiar/enfileirar os bytes e registrar o status recebido; RMS, pico, forma de onda, reamostragem, I/O e widgets ficam fora do callback.
- `AudioRecorder` deve fechar o stream tanto ao finalizar com sucesso quanto ao tratar falhas. A saída permanece mono, 16 kHz e `int16`; dispositivos sem formato de entrada compatível são omitidos da lista, e o nível abaixo de `MIN_RMS_LEVEL` não é enviado.
- O áudio pendente e o `QBuffer` existem somente em memória; fechar a janela interrompe a reprodução, limpa a fonte e remove as referências.
- WAVs inline acima de `20 * 1024 * 1024` bytes (`INLINE_LIMIT_BYTES`) são rejeitados antes da chamada ao Gemini. Esse fluxo curto não usa Files API.
- O SDK permitido é `google-genai`, importado como `from google import genai`. Não reintroduzir `google-generativeai` nem APIs fictícias de transcrição.
- Uma resposta Gemini sem `output_text` não é sucesso: `GeminiTranscriber` deve produzir `TranscriptionError` para resposta vazia.
- `TerminalBridge` só atua quando `XDG_SESSION_TYPE=x11`, `xdotool` está disponível, a janela ativa fornece um PID e o processo pertence à allowlist `TERMINAL_PROCESSES`: `gnome-terminal-server`, `konsole`, `kitty`, `alacritty`, `xfce4-terminal`, `lxterminal`, `xterm`, `wezterm-gui` ou `foot`.
- A detecção usa o PID da janela e `/proc/<pid>/comm`; nunca se deve confiar apenas no título da janela.
- O envio ao terminal apenas define o clipboard e cola com `Ctrl+Shift+V` na janela detectada. Não pressiona Enter, não executa comandos e não muda o foco.
- Em Wayland, a integração automática de terminal é indisponível por contrato; `Copiar texto` é o fallback. O aplicativo não promete colagem Wayland nem execução de comandos.
- Alterações de comportamento devem incluir teste proporcional em `tests/`. Testes de credenciais, Gemini, UI, subprocessos e áudio devem continuar independentes de rede, terminal, Secret Service e microfone reais por meio das dependências injetadas.

### Nunca

- Nunca bloquear o event loop com captura de áudio ou chamada de rede.
- Nunca tocar em `QWidget` a partir do worker de transcrição ou do callback do áudio.
- Nunca hardcodar ou imprimir uma chave Gemini.
- Nunca aceitar resposta Gemini vazia como transcrição válida.
- Nunca manter ou reutilizar janela/PID de terminal antigo; a janela ativa deve ser detectada no envio.
- Nunca transformar texto transcrito em comando automaticamente.

## Regras e ciclo de agentes

- O agente principal deve pesquisar o repositório, resolver ambiguidades e preparar um plano preciso, com arquivos, símbolos, comportamento esperado, critérios verificáveis e comandos de validação, antes de delegar.
- A escrita delegada fica exclusivamente com o `implementador`, que altera somente o escopo recebido; a validação fica com o `testador`, que executa os comandos solicitados; o gate final fica com o `revisor`, que audita pedido, plano, diff, critérios e evidências.
- O FalaFácil é um repositório único: não existe backend/frontend separado nem ordem entre essas camadas.
- Mantenha planos transitórios do harness em `local://`; só crie histórico em `.plans/` quando a tarefa pedir explicitamente esse registro.
- Antes de alterar ou remover qualquer símbolo exportado, localize todas as referências. Quando um contrato mudar, atualize os consumidores, testes e documentação aplicáveis no mesmo corte.
- Atualize `ARQUITETURA.md` somente quando a arquitetura ou seus invariantes mudarem; atualize `docs/INDEX.md` somente quando o catálogo ou os caminhos documentados mudarem; atualize `docs/architecture/agentes.md` somente quando o ciclo ou os papéis de agentes mudarem.
- Se o papel delegado estiver indisponível, registre explicitamente na resposta final qual papel faltou, o motivo e quais validações ou gates não puderam ser executados; não declare aprovação sem essa exceção.

## Configuração

Execute a partir da raiz do projeto:

```bash
poetry install
poetry run python -m falafacil
```

Na janela, use `Configurar chave API` para informar a chave em um campo de senha. Ela é enviada à factory do transcritor e, quando o Secret Service está disponível, persistida pelo `keyring`; não é colocada em `QSettings`, arquivo plaintext, log, status ou argumento de processo. Se o backend não estiver disponível, a gravação continua funcionando somente até o encerramento da sessão e a UI informa a não persistência.

Como alternativa, as variáveis de ambiente são lidas sem persistência adicional. A precedência é `GEMINI_API_KEY`, depois `GOOGLE_API_KEY`, depois o valor já salvo no Secret Service:

```bash
export GEMINI_API_KEY="<defina-localmente>"
poetry run python -m falafacil
```

`GEMINI_MODEL` é opcional e substitui o modelo padrão efetivo `gemini-3.7-flash`:

```bash
export GEMINI_MODEL="gemini-3.7-flash"
```

Não substitua o placeholder acima por uma chave neste arquivo ou em qualquer arquivo versionado. Para os recursos do sistema no Ubuntu:

```bash
sudo apt install libportaudio2
sudo apt install xdotool  # somente para colagem X11
```

Sem `libportaudio2`, o aplicativo ainda pode abrir, mas a gravação não conseguirá iniciar. Sem um backend Secret Service, a configuração feita na UI não sobrevive ao encerramento. Sem `xdotool` ou em Wayland, use `Copiar texto`.

## Comandos Essenciais

```bash
poetry install
QT_QPA_PLATFORM=offscreen poetry run pytest -q \
  tests/test_config.py tests/test_credentials.py tests/test_transcription.py \
  tests/test_ui.py tests/test_packaging.py
poetry run pytest -q
poetry run python -m compileall -q src tests
poetry run python -m falafacil
poetry run python -m sounddevice
```

O entry point equivalente ao módulo é:

```bash
poetry run falafacil
```

Para gerar e instalar o executável Linux one-file, instale o extra de build e execute os scripts pela raiz:

```bash
poetry install --extras build
./scripts/build_executable.sh
tmp_home=$(mktemp -d)
HOME="$tmp_home" ./scripts/install_desktop.sh "$PWD/dist/falafacil"
```

`scripts/build_executable.sh` produz `dist/falafacil` com PyInstaller, removendo `GEMINI_API_KEY` e `GOOGLE_API_KEY` do ambiente do processo de build. `scripts/install_desktop.sh` aceita somente o caminho de um executável existente e executável, instala a cópia em `~/.local/bin/falafacil` e gera `~/.local/share/applications/falafacil.desktop`. O desktop entry usa caminho absoluto, `TryExec` correspondente, `Terminal=false`, categorias de utilitário/áudio e não usa `$HOME`, `~`, `sh -c`, `Environment` ou qualquer chave. O bundle é um executável de janela sem console/terminal.

## Testes

A suíte cobre contratos locais e determinísticos:

- `tests/test_audio.py`: formato WAV, dispositivo selecionado, blocos copiados do callback, métricas RMS/pico, nível mínimo, captura vazia, fechamento do stream, listagem filtrada de microfones e estados inválidos.
- `tests/test_config.py`: precedência `GEMINI_API_KEY` > `GOOGLE_API_KEY` > fallback persistido, ausência de chave, modelo padrão, propriedade `has_api_key` e mensagem/repr sem expor segredo.
- `tests/test_credentials.py`: chamadas ao keyring com serviço/conta exatos, ausência de credencial, rejeição de valor vazio e encapsulamento seguro de falhas de get/set/delete sem incluir a chave nas mensagens.
- `tests/test_transcription.py`: payload WAV inline em base64, MIME `audio/wav`, prompt em português do Brasil, modelo padrão, trace limitado, limite de tamanho, resposta vazia, worker, cliente fake e criação do cliente real com chave injetada sem rede.
- `tests/test_ui.py`: diálogo offscreen, provider e seleção de microfone, áudio pendente, métricas/forma de onda, reprodução fake, envio somente explícito, limpeza no fechamento, painel debug, apagar texto, fallback de persistência e falhas recuperáveis.
- `tests/test_terminal.py`: colagem X11 em processo reconhecido, rejeição de janela que não é terminal e ausência de chamadas ao `xdotool` em Wayland.
- `tests/test_packaging.py`: instalador em `HOME` temporário, cópia executável, `.desktop` com `Exec`/`TryExec` absolutos e ausência de `$HOME`, `~`, `sh -c`, `Environment` ou chave.

Execute primeiro os testes focados com `QT_QPA_PLATFORM=offscreen`, depois `poetry run pytest -q` e `poetry run python -m compileall -q src tests`. Para a entrega empacotada, use `poetry install --extras build`, `./scripts/build_executable.sh` e o instalador em um `HOME` temporário. O smoke do bundle deve iniciar `QT_QPA_PLATFORM=offscreen dist/falafacil`, confirmar a inicialização da janela sem rede e encerrar pelo controle do processo; não se deve exigir chave, microfone ou terminal para essa verificação.

Smoke manual do produto: iniciar o app, clicar `Detectar microfones`, escolher uma entrada, configurar a chave sem deixá-la aparecer, falar uma frase curta, parar a gravação, clicar `Reproduzir áudio`, conferir a captura e só então `Enviar para Gemini`. Conferir os três blocos de debug, editar o texto e copiá-lo para outro aplicativo. Em X11 com um terminal reconhecido, verificar que `Enviar ao terminal` cola o texto sem pressionar Enter. Esse smoke depende de credenciais, microfone, PortAudio, backend QtMultimedia e, para a última etapa, `xdotool`.

## Limitações Conhecidas

- PortAudio é uma dependência nativa fora do Poetry e requer `libportaudio2` (ou pacote equivalente) no sistema.
- O Secret Service e um backend compatível do desktop são necessários para persistir a chave configurada na UI; se estiverem indisponíveis, ela funciona apenas na sessão atual. Não há fallback em plaintext, `QSettings` ou outro arquivo.
- Variáveis `GEMINI_API_KEY` e `GOOGLE_API_KEY` continuam sendo aceitas e têm precedência sobre a chave persistida; a aplicação não sincroniza nem gerencia credenciais fora dessas fontes e do diálogo da UI.
- Wayland não recebe injeção automática de teclado pelo `TerminalBridge`; a alternativa é copiar o texto. Não há promessa de colagem Wayland.
- O executável entregue é um bundle PyInstaller one-file para Linux com launcher sem console; não há AppImage, instalador de outros sistemas ou suporte implícito a execução pelo `.desktop` via shell.
- O áudio é enviado inline e aceita somente falas curtas até o limite de 20 MB; não há fluxo alternativo de upload.
- A transcrição depende da rede e da disponibilidade do modelo Gemini configurado.
- O terminal recebe texto pelo clipboard e pela combinação de colagem, mas o FalaFácil não executa comandos nem envia Enter.
- A colagem só é permitida para processos da allowlist e exige uma janela ativa com PID identificável.

## Manutenção

Use os módulos e símbolos existentes como fonte de verdade antes de atualizar este documento. Ao alterar um contrato de áudio, transcrição, UI, credenciais, ambiente, empacotamento ou terminal, atualize a seção correspondente e os testes determinísticos que cobrem o comportamento observável.
Após qualquer alteração no código, revise este `AGENTS.md` e atualize a documentação sempre que o comportamento, os contratos, os comandos, as dependências, a estrutura ou as limitações do projeto forem afetados. Não deixe a documentação desatualizada em relação à implementação entregue.

Antes de documentar um novo recurso, confirme que ele existe no código e no `pyproject.toml`; não introduza neste arquivo promessas de autenticação de usuário, banco, servidor web, TTS, Files API, Live API, hotkey global, AppImage, suporte Wayland para colagem, execução de comandos ou outras capacidades não implementadas. Preserve a separação entre UI, captura, cliente Gemini, credenciais e integração X11, mantendo chamadas bloqueantes fora do thread principal e as dependências externas injetáveis nos testes.
Confirme que todos os links relativos desta documentação resolvem e que cada promessa sobre o produto permanece sustentada pelo código, pelos testes ou pelo `pyproject.toml`; corrija links e promessas desatualizados no mesmo corte.
