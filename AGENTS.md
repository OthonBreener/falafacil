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

1. `falafacil.app:main` cria uma única `QApplication`, define os nomes da organização e da aplicação antes de usar configurações, inicializa de forma fail-soft o `LocalStore` no diretório de dados (`AppDataLocation`), tenta ler a chave persistida no Secret Service pelo `KeyringApiKeyStore` e exibe `MainWindow`. Falhas do chaveiro são tratadas como ausência persistida e falhas do banco local resultam em `local_store=None`, sem registrar exceções ou segredos.
2. `Settings.from_env(fallback_api_key=...)` lê `GEMINI_MODEL` e escolhe a chave pela precedência `GEMINI_API_KEY`, depois `GOOGLE_API_KEY`, depois a chave persistida. A chave permanece fora de `repr` e comparações de `Settings`.
3. Sem uma chave ativa, a janela abre normalmente, mostra a mensagem de configuração e mantém a gravação desabilitada. O botão `Configurar chave API` abre um diálogo com campo de senha; ao aceitar uma chave não vazia, a UI cria o novo transcritor antes de trocar o estado, tenta persistir no chaveiro e habilita a gravação. Se o backend não estiver disponível, a chave funciona somente nesta sessão e a UI informa que não houve persistência. A configuração não chama a API Gemini.
4. Ao detectar microfones, a UI utiliza metadados locais do PortAudio e a função de prioridade `choose_input_device()`: headset sempre tem precedência; se não houver headset, tenta o dispositivo atual ou a memória do último microfone usado salva no `LocalStore`; em seguida, tenta dispositivo interno, padrão do sistema ou o primeiro disponível. Se nenhum dispositivo existir, a gravação é desabilitada com aviso recuperável. Ao clicar em `Gravar` ou acionar a gravação pelo atalho de mouse/teclado, `AudioRecorder` abre um `sounddevice.InputStream` no microfone selecionado, mono e `int16`. Após o início bem-sucedido da captura, a identidade normalizada do dispositivo é persistida no `LocalStore`. Tenta `16 kHz`; se o dispositivo só aceitar outra taxa nativa, usa uma taxa suportada e reamostra o PCM para `16 kHz` antes de gerar o WAV. O callback copia os blocos capturados sem analisar áudio. O botão passa a ser `Parar e revisar áudio`.
5. A engrenagem abre `Configurações` com chave API, atalho do mouse e atalho do teclado. Mouse aceita somente botões laterais/central seguros — um botão rejeitado explica o motivo no diálogo em vez de ficar em espera, e uma captura sem nenhuma entrada reconhecida orienta o remapeamento no software do fabricante — e teclado aceita combinação modificada ou tecla de função/mídia; ambos podem ficar ativos simultaneamente em X11 e Wayland. Sem handshake, a UI explica que a integração local só compara/captura triggers, não armazena texto nem envia dados, executa a autorização Ubuntu por `pkexec` sem shell e retoma automaticamente a captura após ativar a socket systemd por UID. Preferências são persistidas separadamente somente após ACK; falha preserva `Gravar` e `Space`.
6. Ao parar, o stream é encerrado, os bytes PCM são serializados como WAV em memória e RMS/pico são validados. Captura vazia ou abaixo de `MIN_RMS_LEVEL` fica no diagnóstico e entra em erro sem chamada de rede. Uma captura válida entra em pré-visualização: `Reproduzir áudio` usa `QBuffer`/`QMediaPlayer` e `Enviar para Gemini` é a única ação que inicia a transcrição.
7. O envio ao Gemini ocorre em um `QThread` por meio de `TranscriptionWorker`. A resposta da Interactions API fornece `interaction.usage`, que é encapsulado em `TokenUsage` dentro de `TranscriptionDebug`. Os sinais de sucesso ou falha retornam ao thread da interface com o `TranscriptionDebug`, sem acessar widgets ou banco de dados no worker. No thread principal, a UI registra o consumo no `LocalStore` (com outcome `success` ou `error`), atualiza os dados textuais de consumo da chamada e acumulado e atualiza o gráfico de consumo de tokens por chamada (`TokenUsageChart`).
8. O texto recebido aparece no editor de 120–190 px e pode ser corrigido, apagado e copiado. `Diagnóstico` permanece visível à direita desde a primeira pintura, com abas `Áudio`, `Payload`, `Retorno` e `Consumo` e gráfico abaixo; não existe botão de toggle/dock. O cabeçalho mostra contagem de atalhos, engrenagem e tela cheia. Em X11, `Enviar ao terminal` cola via `Ctrl+Shift+V` sem Enter; em Wayland ou sem `xdotool`, `Copiar texto` é o fallback.

Nenhuma chave é mostrada em label, tooltip, status, exceção, log, arquivo, banco ou argumento. O SQLite armazena apenas `last_microphone_identity`, `recording_mouse_button`, `recording_keyboard_shortcut` e metadados allowlisted de tokens, preservando nulos; nunca armazena chave, áudio, base64, prompt, transcrição, resposta textual, exceção bruta ou preço.

Falhas de microfone, banco, API, terminal, autorização ou serviço global são fail-soft e sanitizadas. Falha da integração global não desabilita gravação manual, `Space`, reprodução, transcrição, editor ou clipboard.

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

Uma transcrição concluída seleciona o texto para revisão. `Ctrl+Shift+C` copia; `Space` alterna a gravação com foco. Os bindings globais configurados são press-only, independentes do compositor e reutilizam `_toggle_recording`. Antes de alternar a gravação, o atalho global restaura a janela minimizada e a traz à frente pelo `_raise_to_front`; `Gravar` e `Space` não alteram a ordem das janelas.

## Stack

As restrições e dependências declaradas em `pyproject.toml` são:

- Python `>=3.11,<3.15` (incluindo o módulo padrão `sqlite3` para armazenamento local).
- PySide6 `>=6.7` para `QApplication`, `QMainWindow`, `QPlainTextEdit`, clipboard, sinais, `QStandardPaths` e `QThread`.
- `google-genai>=2.3.0`, usando `from google import genai` e a Interactions API.
- `sounddevice>=0.5` para a captura, com PortAudio disponível no sistema.
- NumPy `>=1.26` para os buffers entregues ao callback do `InputStream`.
- `keyring>=25.0` e `secretstorage>=3.3` para acessar o Secret Service do desktop; não são usados como armazenamento em arquivo.
- `evdev>=1.7` para leitura restrita de dispositivos de entrada pelo serviço local socket-activated; não usa `grab()`.
- Pytest `>=8.0` na dependência opcional de desenvolvimento.
- PyInstaller `>=6.11` na dependência opcional `build`, somente para gerar o executável distribuível.

`libportaudio2` é um requisito de runtime do microfone no Ubuntu. `xdotool` é um requisito opcional do sistema somente para colagem em terminal X11; não é uma dependência Python do projeto. O ambiente de desktop deve fornecer um backend Secret Service para persistir a chave; sem ele, a configuração continua válida somente na sessão atual.

## Estrutura

| Caminho | Responsabilidade |
|---|---|
| `README.md` | Entrada do projeto, navegação e começo rápido. |
| `CLAUDE.md` | Symlink para `AGENTS.md`; mantém um único contrato de agentes sob os dois nomes. |
| `ARQUITETURA.md` | Mapa arquitetural, fluxo de dependências e invariantes técnicos. |
| `docs/` | Índice, contrato dos agentes de desenvolvimento e gate de smoke. |
| `.omp/agents/` | Definições dos papéis delegados de implementador, testador e revisor. |
| `pyproject.toml` | Metadados, restrição de Python, dependências de runtime, extras `dev`/`build`, entry point e configuração do Pytest. |
| `src/falafacil/__main__.py` | Despacha os modos internos exatos de daemon/instalação antes da GUI; demais argumentos iniciam `falafacil.app.main`. |
| `src/falafacil/app.py` | Define nomes Qt antes da configuração, inicializa o `LocalStore`, carrega a chave persistida, compõe `Settings`, `GeminiTranscriber` e `MainWindow`. |
| `src/falafacil/config.py` | Define `Settings`, `DEFAULT_MODEL`, precedência entre ambiente e fallback persistido e mensagem de configuração. |
| `src/falafacil/credentials.py` | Define o protocolo `ApiKeyStore`, os nomes fixos do serviço/conta e o adaptador `KeyringApiKeyStore` para o Secret Service, sem fallback em arquivo. |
| `src/falafacil/storage.py` | SQLite schema v1 com preferências independentes de microfone, mouse e teclado e histórico allowlisted de tokens. |
| `src/falafacil/shortcuts.py` | Normalizadores seguros e `InputShortcutBridge`, cliente generation-safe do protocolo ASCII sobre `QLocalSocket`. |
| `src/falafacil/shortcut_service.py` | Daemon Qt socket-activated, monitores `evdev`, despacho por código `BTN_*`/`KEY_*`, captura/watch independentes e hotplug sem persistir ou transmitir eventos não correspondentes. |
| `src/falafacil/shortcut_install.py` | Autorização assíncrona `pkexec`, cópia atômica fixa e units systemd endurecidas por UID. |
| `src/falafacil/audio.py` | Define `AudioDevice` (com `host_api`, `kind` e `identity` estável), `AudioCapture`, `AudioRecorder`, `MIN_RMS_LEVEL`, `AudioRecorderError`, classificação heurística local, seleção determinística por prioridade, callback de captura, listagem de entradas e serialização WAV. |
| `src/falafacil/transcription.py` | Define `GeminiTranscriber`, `TranscriptionDebug` (com `TokenUsage`), `TokenUsage`, `TranscriptionWorker`, `TranscriptionError`, o prompt em pt-BR, `INLINE_LIMIT_BYTES` e `REQUEST_TIMEOUT_MS`; aceita chave injetada na criação do cliente e extrai metadados de tokens de `interaction.usage`. |
| `src/falafacil/ui.py` | Janela em splitter com diagnóstico permanente, engrenagem/configurações, tela cheia, dois atalhos ACK-gated, áudio em memória, transcrição, clipboard e fechamento ordenado. |
| `src/falafacil/terminal.py` | Define `TerminalBridge`, `TerminalTarget`, `TERMINAL_PROCESSES`, a detecção X11 e a colagem. |
| `packaging/falafacil.spec` | Spec do PyInstaller para analisar `src/falafacil/__main__.py` e gerar o executável one-file `falafacil`, sem dados de configuração. |
| `scripts/build_executable.sh` | Executa o PyInstaller pela raiz sem propagar chaves do ambiente e informa `dist/falafacil`. |
| `scripts/install_desktop.sh` | Instala uma cópia executável em `~/.local/bin/falafacil` e um `.desktop` gerenciado, com `Exec`/`TryExec` absolutos, `Terminal=false` e sem shell. |
| `tests/` | Testes determinísticos de armazenamento local SQLite, configuração, credenciais, UI offscreen, transcrição/cliente fake, WAV, captura/classificação de áudio, terminal fake e instalador. |

### Fluxo de dependências

```text
app ──> ui ──> InputShortcutBridge ──AF_UNIX──> shortcut_service ──> evdev
 │      ├─> credentials / Secret Service               ▲
 │      ├─> storage / SQLite                            │ socket systemd por UID
 │      ├─> audio / PortAudio                           │
 │      ├─> transcription / google-genai                │
 │      └─> terminal / xdotool X11          shortcut_install / pkexec
 └─> __main__ despacha GUI, daemon ou instalação privilegiada
```

A aplicação não possui camadas de servidor próprio, ORM ou API web do produto. O armazenamento local SQLite é restrito a preferências simples e histórico de consumo de tokens. A fronteira de rede fica no cliente `google-genai`; áudio, Secret Service, banco local SQLite e terminal são integrações locais.

## Regras Fundamentais

- A chave Gemini nunca deve ser gravada em código, testes, `pyproject.toml`, desktop entry, logs, arquivos gerados ou no banco SQLite local. A fonte ativa segue `GEMINI_API_KEY` > `GOOGLE_API_KEY` > valor do Secret Service; a UI também pode aceitar uma chave em memória nesta sessão.
- `KeyringApiKeyStore` usa exatamente o serviço/conta definidos em `src/falafacil/credentials.py` e encapsula erros sem incluir a chave na mensagem. Não criar fallback em arquivo, `QSettings` ou argumento de processo.
- O SQLite armazena somente `last_microphone_identity`, `recording_mouse_button`, `recording_keyboard_shortcut` e metadados allowlisted de consumo; nunca armazena chave, áudio, base64, prompt, transcrição, resposta, exceção bruta ou preço.
- A seleção de microfone segue a ordem: headset -> dispositivo atual da sessão -> identidade lembrada no SQLite -> interno -> padrão do sistema -> primeiro disponível; se não houver dispositivos, a gravação fica desabilitada de forma recuperável.
- A classificação de microfone usa exclusivamente metadados fornecidos pelo `sounddevice`/PortAudio (nome e host API) e heurística local pura, sem invocar `pactl` ou consultas PipeWire.
- A identidade do microfone só é persistida no `LocalStore` após `recorder.start()` aceitar o dispositivo e iniciar a captura com sucesso.
- Falhas do `LocalStore` são fail-soft; preferências de mouse/teclado permanecem independentes e o histórico não afeta captura, reprodução, transcrição ou clipboard.
- O serviço global independe de X11/Wayland/`DISPLAY`, roda não-root com socket `0600` por UID, grupo suplementar `input`, `DevicePolicy=closed` e acesso somente leitura a `char-input`.
- O protocolo transporta apenas handshake, ACK, captura canônica, ativação correspondente, stop e erro sanitizado; nunca coordenadas, eventos individuais, texto digitado, exceções ou segredos.
- Mouse e teclado são press-only e generation-safe; soltura, repetição, trigger diferente, modificador extra e geração antiga não ativam. O serviço não usa `grab()`, não suprime eventos, não escreve e não abre rede.
- `recording_mouse_button` e `recording_keyboard_shortcut` só mudam no thread principal após `WATCHING_*`; cancelamento restaura o binding anterior sem regravar e stop de um tipo não altera o outro.
- A engrenagem permanece disponível em estados busy, mas ações de chave/atalho ficam desabilitadas durante `RECORDING`/`TRANSCRIBING`.
- `closeEvent` cancela `ShortcutServiceInstaller`, fecha `InputShortcutBridge` e só depois fecha `LocalStore`; callbacks tardios são descartados.
- Metadados de consumo da API Gemini são extraídos de `interaction.usage`; campos ausentes são mantidos como `indisponível` (não são convertidos em zero); o histórico acumulado exibe zero apenas para tabela vazia; o gráfico de consumo exibe barras por chamada distinguindo sucesso e erro sem inventar dados para totais desconhecidos; não há cálculo, conversão ou exibição de valor monetário ou preço da API.
- Sem chave configurada, a inicialização não cria `GeminiTranscriber`, a gravação fica desabilitada e o fluxo não faz chamada de rede. Configurar pela UI cria o transcritor antes de substituir `Settings`; falha da factory não altera o estado anterior.
- Widgets Qt, player multimídia, clipboard Qt e a conexão `LocalStore` só são acessados no thread principal. O worker comunica o resultado com os sinais `finished` e `failed`, conectados a slots da UI.
- O callback do PortAudio deve somente copiar/enfileirar os bytes e registrar o status recebido; RMS, pico, forma de onda, reamostragem, I/O, banco de dados e widgets ficam fora do callback.
- `AudioRecorder` deve fechar o stream tanto ao finalizar com sucesso quanto ao tratar falhas. A saída permanece mono, 16 kHz e `int16`; dispositivos sem formato de entrada compatível são omitidos da lista, e o nível abaixo de `MIN_RMS_LEVEL` não é enviado.
- O áudio pendente e o `QBuffer` existem somente em memória; fechar a janela interrompe a reprodução, limpa a fonte, solicita o encerramento do worker e aguarda seu término (sem cancelar a chamada de rede em andamento) e fecha o `LocalStore`.
- WAVs inline acima de `20 * 1024 * 1024` bytes (`INLINE_LIMIT_BYTES`) são rejeitados antes da chamada ao Gemini. Esse fluxo curto não usa Files API.
- Todo cliente `genai` criado por `GeminiTranscriber` recebe `http_options={"timeout": REQUEST_TIMEOUT_MS}`; nenhuma chamada ao Gemini pode ficar pendurada sem limite mantendo o estado `TRANSCRIBING` preso. Um cliente injetado nos testes não é reconfigurado.
- A classificação de erro distingue crédito esgotado de limite transitório: mensagens de depleção de crédito pré-pago (HTTP 429 com `prepayment`/`credit` + `deplet`/`exhaust`/`insufficient`) orientam a recarga do projeto e nunca sugerem "tente novamente mais tarde"; estouro de tempo limite recebe mensagem própria.
- O SDK permitido é `google-genai`, importado como `from google import genai`. Não reintroduzir `google-generativeai` nem APIs fictícias de transcrição.
- Uma resposta Gemini sem `output_text` não é sucesso: `GeminiTranscriber` deve produzir `TranscriptionError` para resposta vazia, preservando os tokens consumidos quando fornecidos pela API.
- `TerminalBridge` só atua quando `XDG_SESSION_TYPE=x11`, `xdotool` está disponível, a janela ativa fornece um PID e o processo pertence à allowlist `TERMINAL_PROCESSES`: `gnome-terminal-server`, `konsole`, `kitty`, `alacritty`, `xfce4-terminal`, `lxterminal`, `xterm`, `wezterm-gui` ou `foot`.
- A detecção usa o PID da janela e `/proc/<pid>/comm`; nunca se deve confiar apenas no título da janela.
- O envio ao terminal apenas define o clipboard e cola com `Ctrl+Shift+V` na janela detectada. Não pressiona Enter, não executa comandos e não muda o foco.
- Em Wayland, a integração automática de terminal é indisponível por contrato; `Copiar texto` é o fallback. O aplicativo não promete colagem Wayland nem execução de comandos.
- Alterações de comportamento devem incluir teste proporcional em `tests/`. Testes de armazenamento local, credenciais, Gemini, UI, subprocessos e áudio devem continuar independentes de rede, terminal, Secret Service e microfone reais por meio das dependências injetadas.

### Nunca

- Nunca bloquear o event loop com captura de áudio ou chamada de rede.
- Nunca tocar em `QWidget` ou banco de dados a partir do worker de transcrição ou do callback do áudio.
- Nunca hardcodar ou imprimir uma chave Gemini.
- Nunca gravar chaves Gemini, áudios, base64, prompts, transcrições, mensagens de exceção brutas ou outros payloads sensíveis no banco SQLite.
- Nunca calcular ou exibir estimativa de preço monetário para tokens consumidos.
- Nunca executar comandos de shell como `pactl` ou consultas PipeWire para classificação de microfone.
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
  tests/test_shortcut_service.py tests/test_shortcut_install.py \
  tests/test_shortcuts.py tests/test_storage.py tests/test_ui.py \
  tests/test_packaging.py
poetry run pytest -q
poetry run python -m compileall -q src tests
poetry run python -m falafacil
poetry run python -m sounddevice
```

O entry point equivalente ao módulo é:

```bash
poetry run falafacil
```

Para gerar e instalar o executável Linux one-file, instale o extra de build e execute os scripts pela raiz. É obrigatório recriar o executável Linux one-file `dist/falafacil` e reinstalá-lo localmente após qualquer alteração no código com `poetry install --extras build`, `./scripts/build_executable.sh` e `./scripts/install_desktop.sh "$PWD/dist/falafacil"` (o que reinstala e atualiza `~/.local/bin/falafacil` e `~/.local/share/applications/falafacil.desktop`), executando o smoke do bundle quando aplicável:

```bash
poetry install --extras build
./scripts/build_executable.sh
./scripts/install_desktop.sh "$PWD/dist/falafacil"
```

Para testar o instalador isoladamente em um diretório temporário:

```bash
tmp_home=$(mktemp -d)
HOME="$tmp_home" ./scripts/install_desktop.sh "$PWD/dist/falafacil"
```

`scripts/build_executable.sh` produz `dist/falafacil` com PyInstaller, removendo `GEMINI_API_KEY` e `GOOGLE_API_KEY` do ambiente do processo de build. `scripts/install_desktop.sh` aceita somente o caminho de um executável existente e executável, instala a cópia em `~/.local/bin/falafacil` e gera `~/.local/share/applications/falafacil.desktop`. O desktop entry usa caminho absoluto, `TryExec` correspondente, `Terminal=false`, categorias de utilitário/áudio e não usa `$HOME`, `~`, `sh -c`, `Environment` ou qualquer chave. O bundle é um executável de janela sem console/terminal.

## Testes

A suíte cobre contratos locais e determinísticos:

- `tests/test_storage.py`: schema v1, preferências independentes de microfone/mouse/teclado, rejeição de `left`/`right` e atalhos inseguros, erros sanitizados e histórico de tokens.
- `tests/test_shortcuts.py`: normalização, framing parcial/múltiplo, limite de 128 bytes, handshake, gerações independentes, captura, stop, erros e descarte de respostas antigas.
- `tests/test_shortcut_service.py`: classificação mouse/teclado, despacho por código em nós multi-interface, press/release/repeat, modificadores, captura one-shot, rejeição sanitizada de botão só durante captura, isolamento de clientes, hotplug e limpeza de estado.
- `tests/test_shortcut_install.py`: `QProcess` sem shell/segredo, cancelamento, raiz temporária, destinos fixos, modos, UID e hardening sem polkit/systemd reais.
- `tests/test_audio.py`, `tests/test_config.py`, `tests/test_credentials.py` e `tests/test_transcription.py`: contratos determinísticos existentes sem hardware/rede/segredo real.
- `tests/test_ui.py`: dois bindings ACK-gated, autorização/retomada, persistência/falha isolada, busy/close, diagnóstico permanente, engrenagem, settings, fullscreen e grabs offscreen em ambos os tamanhos, além dos fluxos de áudio/transcrição/tokens.
- `tests/test_terminal.py`: colagem X11 allowlisted e fallback Wayland.
- `tests/test_packaging.py`: desktop installer seguro e dispatch interno exato.

Execute primeiro os testes focados com `QT_QPA_PLATFORM=offscreen`, depois `poetry run pytest -q` e `poetry run python -m compileall -q src tests`. Para a entrega empacotada, use `poetry install --extras build`, `./scripts/build_executable.sh` e o instalador em um `HOME` temporário. O smoke do bundle deve iniciar `QT_QPA_PLATFORM=offscreen dist/falafacil`, confirmar a inicialização da janela sem rede e encerrar pelo controle do processo; não se deve exigir chave, microfone ou terminal para essa verificação.

Smoke manual: conferir diagnóstico permanente, editor limitado, duas linhas de ações, engrenagem com três seções e fullscreen. No app instalado, autorizar a integração uma vez, configurar `x1` e `Ctrl+Alt+R` e testar ambos fora de foco em X11 e Wayland quando disponíveis; release/repeat/texto não alternam, e desativar um tipo preserva o outro. O fluxo de áudio/Gemini e a colagem X11 continuam conforme os contratos anteriores.

## Limitações Conhecidas

- PortAudio é uma dependência nativa fora do Poetry e requer `libportaudio2` (ou pacote equivalente) no sistema.
- O Secret Service e um backend compatível do desktop são necessários para persistir a chave configurada na UI; se estiverem indisponíveis, ela funciona apenas na sessão atual. Não há fallback em plaintext, `QSettings` ou outro arquivo.
- Microfone, dois atalhos locais e histórico de tokens são persistidos apenas neste dispositivo; não há sincronização.
- Variáveis Gemini mantêm precedência sobre Secret Service.
- Wayland continua sem colagem automática do `TerminalBridge`; use clipboard.
- Atalhos globais exigem Ubuntu com systemd, polkit, `/dev/input` e grupo `input`; o app instalado cuida da autorização/ativação sem terminal ou re-login.
- Trazer a janela à frente depende do compositor: em X11 a ativação é imediata, enquanto compositores Wayland podem sinalizar a janela como pronta em vez de focá-la.
- Mouse aceita apenas lateral/central; teclado aceita combinações seguras ou funções/mídia. Ambos servem exclusivamente para iniciar/parar gravação e nunca suprimem o evento físico.
- Botões que existem apenas no firmware do mouse não emitem evento algum em `/dev/input` e são invisíveis para qualquer aplicativo Linux. É o caso do botão sniper (mira) do Corsair M65, cujo DPI-shift é resolvido no próprio dispositivo. Para usá-lo, remapeie-o antes no software do fabricante; veja o README.
- O bundle é Linux one-file; não há AppImage nem instalador de outros sistemas.
- O áudio é enviado inline e aceita somente falas curtas até o limite de 20 MB; não há fluxo alternativo de upload.
- A transcrição depende da rede e da disponibilidade do modelo Gemini configurado.
- O terminal recebe texto pelo clipboard e pela combinação de colagem, mas o FalaFácil não executa comandos nem envia Enter.
- A colagem só é permitida para processos da allowlist e exige uma janela ativa com PID identificável.

## Manutenção

Use os módulos e símbolos existentes como fonte de verdade antes de atualizar este documento. Ao alterar um contrato de áudio, transcrição, UI, credenciais, ambiente, empacotamento ou terminal, atualize a seção correspondente e os testes determinísticos que cobrem o comportamento observável.
Após qualquer alteração no código, é obrigatório recriar o executável Linux one-file `dist/falafacil` com `poetry install --extras build` e `./scripts/build_executable.sh`, reinstalá-lo localmente com `./scripts/install_desktop.sh "$PWD/dist/falafacil"` (o que reinstala e atualiza `~/.local/bin/falafacil` e `~/.local/share/applications/falafacil.desktop`) e executar o smoke do bundle quando aplicável. Revise também este `AGENTS.md` e atualize a documentação sempre que o comportamento, os contratos, os comandos, as dependências, a estrutura ou as limitações do projeto forem afetados. Não deixe a documentação desatualizada em relação à implementação entregue.

Antes de documentar recurso, confirme código, testes e `pyproject.toml`; não prometa servidor, TTS, Files/Live API, AppImage, colagem Wayland ou execução de comandos. Preserve separação entre UI, áudio, transcrição, credenciais, serviço global local e terminal X11; chamadas bloqueantes não entram no thread principal.
Confirme que todos os links relativos desta documentação resolvem e que cada promessa sobre o produto permanece sustentada pelo código, pelos testes ou pelo `pyproject.toml`; corrija links e promessas desatualizados no mesmo corte.
