# Arquitetura

O FalaFácil é um aplicativo desktop local para Ubuntu. A composição abaixo descreve os módulos da aplicação; não há servidor próprio, ORM ou camada de API web do produto. O armazenamento local SQLite é restrito a preferências simples e histórico de consumo de tokens. A fronteira de rede é o cliente `google-genai` usado pela transcrição.

## Composição da aplicação

`falafacil.app:main` cria a única `QApplication`, define os nomes da aplicação (`FalaFácil`), inicializa de forma fail-soft o `LocalStore` no diretório de dados da aplicação (`AppDataLocation`), lê a credencial persistida e compõe `Settings`, `GeminiTranscriber` e `MainWindow` em `src/falafacil/app.py`. Se o banco local não puder ser inicializado, a aplicação prossegue com `local_store=None` sem interromper o fluxo do usuário. A janela é exibida somente depois dessa composição. O entry point `falafacil` e `python -m falafacil` chegam ao mesmo ponto; `src/falafacil/__main__.py` apenas encaminha a chamada de módulo.

`MainWindow` e `AppState` vivem em `src/falafacil/ui.py`. A UI coordena seleção e detecção de microfone, persistência do último dispositivo usado, configuração e persistência do atalho global de gravação por botão do mouse (`Configurar atalho de gravação`), indicador de status do atalho, gravação, pré-visualização, reprodução em memória, envio explícito, editor de texto, painel lateral de debug com blocos de texto (áudio, payload, retorno e métricas de consumo Gemini) e o bloco gráfico de histórico de consumo de tokens (`TokenUsageChart`), diálogo de chave, clipboard e o ciclo do `QThread`. Os estados observáveis são `IDLE`, `RECORDING`, `AUDIO_READY`, `TRANSCRIBING`, `READY` e `ERROR`; uma falha deixa a janela recuperável.

## Áudio

`src/falafacil/audio.py` encapsula `AudioDevice`, `AudioCapture`, `AudioRecorder`, listagem de entradas, classificação heurística local, seleção determinística, callback do `sounddevice` e serialização WAV.

`AudioDevice` modela os dispositivos de entrada com `index`, `name`, `max_input_channels`, `is_default`, `host_api` e `kind` (`"headset"`, `"internal"` ou `"other"`). A propriedade `identity` gera uma chave estável baseada no nome normalizado do dispositivo e da host API (ex.: `nome::host_api`), desacoplando a preferência do índice transitório do PortAudio.

A classificação heurística `_classify_input_device()` usa exclusivamente metadados fornecidos pelo `sounddevice`/PortAudio (nome e host API), sem invocar comandos de sistema (`pactl` ou consultas PipeWire). A função pura `choose_input_device()` define a prioridade de seleção automática:
1. Primeiro dispositivo classificado como `headset`;
2. Dispositivo selecionado na sessão atual, se ainda conectado e não houver headset;
3. Dispositivo correspondente à identidade lembrada no `LocalStore`;
4. Primeiro dispositivo classificado como `internal`;
5. Primeiro dispositivo marcado como padrão do sistema (`is_default`);
6. Primeiro dispositivo disponível restante;
7. `None` caso não existam dispositivos de entrada.

A identidade do microfone só é persistida no armazenamento local quando `AudioRecorder.start()` inicia a captura com sucesso. Se nenhum microfone estiver disponível, o botão de gravação é desabilitado e uma mensagem recuperável é exibida.

A captura é mono, `int16` e produz áudio em 16 kHz. Quando o dispositivo só aceita outra taxa nativa, a reamostragem para 16 kHz ocorre fora do callback, antes da serialização final. O callback do `InputStream` somente copia/enfileira os blocos recebidos e registra o status. RMS, pico, reamostragem, I/O e acesso à UI ocorrem fora dele. Captura vazia ou abaixo de `MIN_RMS_LEVEL` vira diagnóstico/erro e não é enviada ao Gemini. O áudio pendente e o buffer de reprodução permanecem em memória.

## Atalhos e eventos globais do mouse

`src/falafacil/shortcuts.py` encapsula `MouseShortcutBridge`, `MouseShortcutError`, `MouseListenerLike`, `MouseListenerFactory` e `normalize_button_name`, isolando o acesso a variáveis de ambiente e à biblioteca `pynput`.

A escuta global do mouse exige sessão X11 (`XDG_SESSION_TYPE=x11`) e a presença da variável `DISPLAY`. Em Wayland/Xwayland ou sem `DISPLAY`, a captura global é considerada indisponível; `start()` e `begin_capture()` retornam `False` e emitem mensagens sanitizadas (`Atalho global do mouse indisponível nesta sessão.`), sem instanciar listeners funcionais. A operação manual e o atalho de teclado `Space` local à janela permanecem operacionais.

A normalização canônica aceita nomes de botões alfanuméricos seguros (`left`, `right`, `middle`, `x1`, `x2`, etc.) e rejeita tokens vazios, `unknown` ou com caracteres inválidos. A importação de `pynput.mouse` é feita de forma lazy na fábrica padrão de listeners.

O listener é criado com `pynput.mouse.Listener(on_click=..., suppress=False)` e executa em thread própria do backend. O clique físico nunca é suprimido do sistema operacional (`suppress=False`). O callback processa exclusivamente o evento de pressão (`pressed=True`), ignorando solturas (`pressed=False`), e despacha eventos para a UI de forma não-bloqueante e lock-free via sinais Qt atômicos internos privados (`_activated_event(gen, x, y)` e `_button_captured_event(gen, button_name, x, y)`), mantendo os sinais públicos contratuais (`activated`, `button_captured`, `failed`) para observabilidade externa. O callback não acessa widgets Qt, gravador, `LocalStore` ou operações bloqueantes.

No modo de captura (`begin_capture()`), o listener temporário captura a primeira pressão física válida via one-shot por closure local (sem mutar flags globais de modo no callback), emite `_button_captured_event` com a geração correspondente (além do sinal público `button_captured`) e agenda a limpeza assíncrona do listener no thread principal (`_cleanup_capture_requested`). O receptor `_ShortcutCaptureRelay` é instanciado e conectado ao sinal `_button_captured_event` antes da inicialização do listener (`begin_capture()`), garantindo que emissões síncronas ou enfileiradas sejam recebidas e filtradas contra a geração da captura ativa, ignorando sinais residuais de diálogos cancelados ou anteriores.

No modo ativo (`start()`), a pressão do botão configurado emite o evento atômico `_activated_event` (além do sinal público `activated`), consumido diretamente por `MainWindow._on_mouse_shortcut_activated_event`. O slot valida a geração ativa (`gen == bridge.generation`), aplica supressão geométrica em coordenadas sobre controles interativos da janela (`QApplication.widgetAt` e fallback offscreen) para cliques com o botão esquerdo e invoca diretamente `_toggle_recording` uma única vez, sem filas FIFO globais.

O ciclo de vida é serializado por `stop()`, que incrementa a geração do bridge, invalida o callback ativo e finaliza o listener com `join(timeout=0.5)`. No fechamento da aplicação (`MainWindow.closeEvent`), o bridge é parado e a flag `_is_closing` descarta eventos tardios antes de fechar o `LocalStore`.
## Transcrição Gemini

`src/falafacil/transcription.py` contém `GeminiTranscriber`, `TranscriptionWorker`, `TranscriptionDebug`, `TokenUsage`, `TranscriptionError` e o prompt em português do Brasil. O envio usa `google-genai` (`from google import genai`) com áudio WAV inline em base64 e rejeita payloads acima de `INLINE_LIMIT_BYTES` (20 MiB); não existe fluxo alternativo de upload.

A resposta da API Interactions (`client.interactions.create()`) fornece metadados de consumo em `interaction.usage`. O transcritor extrai os campos de contagem (`total_input_tokens`, `total_output_tokens`, `total_thought_tokens`, `total_cached_tokens`, `total_tool_use_tokens` e `total_tokens`) em um DTO imutável `TokenUsage`, associado a `TranscriptionDebug`. Campos ausentes permanecem `None` (indisponíveis), sem fabricação artificial de zeros.

A única ação que inicia a chamada é `Enviar para Gemini`, depois de uma captura válida e da pré-visualização. A chamada executa em `TranscriptionWorker` dentro de um `QThread`. Os sinais `finished` e `failed` retornam ao thread principal com o texto/erro e o `TranscriptionDebug`, sem acessar widgets ou banco de dados a partir do worker. Resposta sem `output_text` é tratada como erro recuperável, mas preserva os tokens consumidos para registro e diagnóstico.

## Armazenamento local

`src/falafacil/storage.py` implementa `LocalStore`, `LocalStoreError`, `TokenTotals` e `TokenUsageRecord`, utilizando a biblioteca padrão `sqlite3`.

O caminho do banco é resolvido por `resolve_storage_path()` via `QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)` apontando para `falafacil.sqlite3`. O diretório pai é criado com permissões privadas (`0o700`) e o arquivo do banco com `0o600` quando suportado pelo sistema.

O schema é versionado via `PRAGMA user_version`:
- Versão `0` (banco novo ou não inicializado): cria o schema v1 e define `PRAGMA user_version = 1`.
- Versão `1` (banco existente): reabre e preserva a estrutura e os dados sem mutações adicionais.
- Qualquer outra versão (ex.: versões futuras ou incompatíveis): a inicialização é recusada levantando `LocalStoreError` sanitizado (`Versão de schema incompatível: <versão>.`), operando em modo fail-soft (`local_store=None` na UI) e preservando o arquivo SQLite sem qualquer mutação de schema ou dados.

O schema v1 é composto por:
- Tabela `preferences(key TEXT PRIMARY KEY, value TEXT NOT NULL)`: armazena preferências locais não secretas: `last_microphone_identity` e `recording_mouse_button`.
- Tabela `token_usage(id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, model TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER, thought_tokens INTEGER, cached_tokens INTEGER, tool_use_tokens INTEGER, total_tokens INTEGER, outcome TEXT NOT NULL CHECK(outcome IN ('success', 'error')))`: armazena o histórico de consumo de tokens por chamada.
- Índice `idx_token_usage_recorded_at ON token_usage(recorded_at)` para ordenação cronológica.

A conexão opera exclusivamente no thread principal com `check_same_thread=True` e `PRAGMA busy_timeout = 2000`. A retenção é local e cumulativa, sem rotinas de expiração automática. A consulta `get_token_totals()` realiza agregação com tratamento de nulos (retorna contagem zero apenas quando a tabela está vazia e preserva valores ausentes quando aplicável). A consulta `get_token_usage_history(limit=30)` retorna tuplas de `TokenUsageRecord` em ordem cronológica ascendente (mais antigas primeiro entre as últimas chamadas registradas), preservando campos nulos/indisponíveis e o desfecho (`success` ou `error`).

Esse histórico persistido alimenta diretamente o widget customizado `TokenUsageChart` (`Gráfico de consumo de tokens`) no painel de debug da UI. As barras do gráfico representam o `total_tokens` conhecido de cada chamada, distinguindo visualmente sucessos (verde) e erros (vermelho), enquanto totais indisponíveis são identificados sem fabricação de valores ou quebra de renderização. O histórico e o gráfico medem estritamente consumo de tokens, não precificação monetária da API, e nenhum cálculo ou conversão monetária é realizado.

O ciclo de vida do armazenamento é gerenciado pela UI com tratamento fail-soft: erros de escrita ou leitura nunca interrompem gravação, reprodução ou transcrição. A preferência `recording_mouse_button` é persistida e limpa exclusivamente no thread principal; falhas de escrita mantêm o atalho ativo apenas na sessão com mensagem de status informativa. No fechamento da janela (`MainWindow.closeEvent`), o gravador e o player são parados, o worker é finalizado, `MouseShortcutBridge.stop()` é executado e o `LocalStore.close()` é executado antes da aceitação do evento.

O banco de dados armazena estritamente a allowlist de metadados permitidos: preferências locais (`last_microphone_identity` e `recording_mouse_button`), timestamp de registro (`recorded_at`), identificador do modelo (`model`), contagens de tokens de entrada (`input_tokens`), saída (`output_tokens`), pensamento (`thought_tokens`), cache (`cached_tokens`), ferramentas (`tool_use_tokens`) e total (`total_tokens`) — preservando campos nulos/desconhecidos —, e desfecho da requisição (`outcome` como `'success'` ou `'error'`). A retenção é local e cumulativa, sem rotinas de limpeza ou expiração automática. O banco nunca armazena chaves de API, arquivos de áudio PCM/WAV, payloads ou previews em base64, texto do prompt, transcrições ou respostas textuais, mensagens de exceção brutas ou outros payloads sensíveis.

## Configuração e credenciais

`src/falafacil/config.py` define `Settings`, `DEFAULT_MODEL`, `has_api_key` e a mensagem de configuração. A precedência da chave é `GEMINI_API_KEY`, depois `GOOGLE_API_KEY`, depois o fallback persistido no Secret Service. `GEMINI_MODEL` escolhe o modelo, cujo padrão efetivo é `gemini-3.7-flash`. O valor da chave não aparece em `repr` nem em comparações de `Settings`.

`src/falafacil/credentials.py` define `ApiKeyStore`, `KeyringApiKeyStore` e os nomes fixos do serviço/conta. O armazenamento usa `keyring` sobre o Secret Service; não usa arquivo plaintext nem `QSettings`. Falhas do chaveiro são encapsuladas sem incluir a chave em mensagens. Sem chave ativa, a aplicação abre, mas mantém a gravação/transcrição desabilitada e não faz chamada de rede. Se a persistência não estiver disponível, uma chave aceita pela UI vale somente para a sessão atual.

## Terminal

`src/falafacil/terminal.py` define `TerminalBridge`, `TerminalTarget` e a allowlist de processos. A integração só atua quando `XDG_SESSION_TYPE=x11`, `xdotool` está disponível, a janela ativa fornece um PID e o nome lido de `/proc/<pid>/comm` pertence à allowlist. No envio, o texto vai para o clipboard e `Ctrl+Shift+V` é enviado à janela detectada; o FalaFácil não muda o foco, não envia Enter e não executa comandos.

Em Wayland, sem `xdotool`, sem PID ou sem terminal reconhecido, a ponte permanece indisponível e `Copiar texto` é o fallback. A janela/terminal é detectada no momento do envio, sem reutilizar um alvo antigo.

## Distribuição e validação

- `packaging/falafacil.spec` descreve o PyInstaller one-file a partir de `src/falafacil/__main__.py`, sem dados de configuração.
- `scripts/build_executable.sh` gera `dist/falafacil` pela raiz e não propaga as variáveis de chave para o processo de build.
- `scripts/install_desktop.sh` instala uma cópia executável em `~/.local/bin/falafacil` e um desktop entry gerenciado, com caminhos absolutos e sem shell ou segredo.
- `tests/` valida armazenamento local, configuração, credenciais, áudio, atalhos de mouse, transcrição, UI, terminal e empacotamento com dependências falsas/injetadas quando necessário; a suíte não depende de rede, microfone, Secret Service, X11, mouse ou terminal reais.

```text
app
 ├─> storage ─────────────> sqlite3 / AppDataLocation
 └─> ui
      ├─> credentials ────> keyring / Secret Service
      ├─> storage ────────> sqlite3 / LocalStore
      ├─> audio ──────────> sounddevice / PortAudio
      ├─> shortcuts ──────> pynput / X11
      ├─> transcription ──> google-genai
      └─> terminal ───────> xdotool / X11

packaging ──> __main__ ──> app
tests ──────> módulos via dependências injetadas
```

## Invariantes
1. Nenhuma chave Gemini é gravada em código, testes, `pyproject.toml`, desktop entry, logs, arquivos gerados, argumentos do launcher ou banco SQLite local. A fonte ativa segue a precedência definida em `config.py` e a credencial persistida fica exclusivamente no Secret Service.
2. O banco de dados local SQLite (`LocalStore`) armazena estritamente a allowlist de metadados: preferências locais não secretas (`last_microphone_identity` e `recording_mouse_button`), timestamp `recorded_at`, identificador do modelo, contagens de tokens (entrada, saída, pensamento, cache, ferramentas e total, preservando valores ausentes/nulos) e outcome (`success` ou `error`). A retenção é local e cumulativa, sem rotinas de limpeza automática. O banco nunca armazena chaves de API, áudio PCM/WAV, previews/payloads em base64, texto do prompt, transcrições/respostas textuais, mensagens de exceção brutas ou outros payloads sensíveis.
3. Campos de tokens não fornecidos pela API permanecem indisponíveis (`None`), sem conversão em zero artificial; o total agregado e o gráfico tratam valores ausentes com segurança e exibem zero somente quando não há registros no histórico. As barras do gráfico diferenciam visualmente sucesso e erro para valores conhecidos de `total_tokens`. A aplicação mede exclusivamente consumo de tokens, não precificação monetária da API, e não calcula nem estima preços.
4. A conexão com o banco local pertence e é acessada exclusivamente no thread principal; a validação de schema via `PRAGMA user_version` aceita estritamente versão `0` para criação do schema v1 e `1` para reabertura, recusando qualquer outra versão com erro sanitizado e fail-soft sem mutar o arquivo SQLite; falhas no armazenamento local não impedem captura de áudio, transcrição ou uso do clipboard.
5. Widgets Qt, `QMediaPlayer`, clipboard e demais objetos de UI só são acessados no thread principal. O worker de transcrição comunica resultados por sinais; os callbacks de áudio e de listener de mouse não tocam em widgets nem em banco de dados.
6. A captura fecha o stream em sucesso e em falha, conserva o formato mono/`int16`/16 kHz e não envia áudio vazio ou abaixo do nível mínimo. A prioridade de seleção automática segue headset -> sessão atual -> memória persistida -> interno -> padrão do sistema.
7. O envio ao Gemini é explícito, acontece somente após a revisão da captura e respeita o limite inline de 20 MiB. Chamada bloqueante não ocupa o event loop.
8. Estados de erro informam a condição no status e permitem nova tentativa, correção ou uso do clipboard; resposta Gemini vazia nunca é aceita como sucesso.
9. A integração de terminal só cola em uma janela X11 ativa cujo processo foi reconhecido pela allowlist. Ela nunca executa comandos nem envia Enter; Wayland usa clipboard como fallback.
10. A captura global de mouse para gravação atua exclusivamente sob sessão X11 com `DISPLAY` e biblioteca `pynput`. O canal interno de despacho utiliza eventos atômicos privados (`_activated_event`, `_button_captured_event`) consumidos pela UI com validação direta de geração do bridge, fences de interleavings e supressão de controles locais, mantendo os sinais públicos contratuais (`activated`, `button_captured`, `failed`) para observabilidade externa. O atalho físico é press-only, reutiliza estritamente `_toggle_recording`, não suprime o clique do sistema (`suppress=False`) e despacha sinais Qt sem executar código bloqueante, I/O, retenção de locks em threads de callback ou chamadas de banco no listener. Em Wayland/Xwayland ou sem display, o aplicativo opera em modo fail-soft, mantendo o botão manual e o atalho `Space` da janela operacionais.
Os contratos completos de produto, comandos e limitações estão em [AGENTS.md](AGENTS.md). A navegação documental está em [docs/INDEX.md](docs/INDEX.md).
