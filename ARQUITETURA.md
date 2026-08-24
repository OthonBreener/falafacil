# Arquitetura

O FalaFácil é um aplicativo desktop local para Ubuntu. A composição abaixo descreve os módulos da aplicação; não há servidor próprio, ORM ou camada de API web do produto. O armazenamento local SQLite é restrito a preferências simples e histórico de consumo de tokens. A fronteira de rede é o cliente `google-genai` usado pela transcrição.

## Composição da aplicação

`falafacil.app:main` cria a única `QApplication`, define os nomes da aplicação (`FalaFácil`), inicializa de forma fail-soft o `LocalStore`, lê a credencial persistida e compõe `Settings`, `GeminiTranscriber` e `MainWindow`. `src/falafacil/__main__.py` despacha antes da GUI apenas os modos internos exatos `--shortcut-daemon` e `--install-shortcut-service`; qualquer outro argumento segue o aplicativo normal.

`MainWindow` e `AppState` vivem em `src/falafacil/ui.py`. A janela usa cabeçalho e `QSplitter` horizontal: transcrição e ações em duas linhas à esquerda, `Diagnóstico` permanente com abas e gráfico à direita. A engrenagem abre um diálogo único para chave API e os dois atalhos; o controle adjacente alterna tela cheia. A UI mantém bindings ativos/pendentes separados e só persiste após o ACK assíncrono do serviço.

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

## Atalhos globais e serviço local

`src/falafacil/shortcuts.py` define os normalizadores restritos e `InputShortcutBridge`, cliente assíncrono de `QLocalSocket`. O protocolo ASCII limita cada linha a 128 bytes, inicia com `HELLO 1`/`READY 1` e separa comandos, gerações e sinais de mouse e teclado. Respostas antigas são descartadas por tipo; framing, versão, trigger ou geração inválidos fecham a conexão sem transportar coordenadas, eventos individuais, texto digitado, exceções ou segredos.

Mouse aceita somente `middle`, `x1`, `x2`, `forward`, `back` e `task`; `left`/`right` são proibidos. Durante a captura, um botão rejeitado devolve `primary_button` ou `unsupported_button`, os únicos códigos de rejeição de mouse do protocolo; fora da captura nenhum botão não correspondente é transmitido. Teclado aceita modificadores na ordem canônica `ctrl+alt+shift+meta` e uma tecla terminal; letras/dígitos exigem `ctrl`, `alt` ou `meta`, enquanto `F1`–`F24` e teclas de mídia permitidas podem atuar sozinhas. Soltura e repetição não ativam bindings; modificadores extras impedem correspondência.

`src/falafacil/shortcut_service.py` é um daemon Qt socket-activated pelo systemd. Ele adota exclusivamente o descritor 3 validado por `LISTEN_PID`/`LISTEN_FDS`, monitora `/dev/input` com `evdev`/`QSocketNotifier`, classifica interfaces de movimento relativo, de teclado ou de botões apenas para decidir quais dispositivos admite, despacha cada evento pelo código `BTN_*`/`KEY_*` e não pela interface de origem, e mantém bindings/capturas independentes por conexão. O serviço não chama `grab()`, não suprime eventos, não escreve arquivos, não abre rede e, fora de captura, nunca transmite teclas ou botões não correspondentes.

`src/falafacil/shortcut_install.py` separa o `QProcess` não bloqueante da UI do instalador privilegiado. A UI executa sem shell apenas `pkexec <bundle> --install-shortcut-service` com ambiente mínimo e sem variáveis Gemini. O instalador root deriva o usuário exclusivamente de `PKEXEC_UID`, copia atomicamente `/proc/self/exe`, grava units fixas e ativa `falafacil-shortcutd@<uid>.socket`. A socket é `0600` do UID; o serviço usa `DynamicUser`, grupo suplementar `input`, `DevicePolicy=closed`, `DeviceAllow=char-input r` e hardening systemd. O daemon não roda como root.

A integração independe de X11, Wayland, `DISPLAY` ou compositor. Mouse e teclado podem ficar ativos simultaneamente e ambos chamam estritamente `_raise_to_front` seguido de `_toggle_recording`; a janela é restaurada e trazida à frente sem perder o estado de tela cheia, e a ativação efetiva depende da política do compositor. Falha, cancelamento ou serviço ausente preserva `Gravar`, `Space`, reprodução, transcrição, editor e clipboard. No fechamento, a UI cancela autorização pendente, invalida gerações e fecha o socket antes do `LocalStore`.
## Transcrição Gemini

`src/falafacil/transcription.py` contém `GeminiTranscriber`, `TranscriptionWorker`, `TranscriptionDebug`, `TokenUsage`, `TranscriptionError` e o prompt em português do Brasil. O envio usa `google-genai` (`from google import genai`) com áudio WAV inline em base64 e rejeita payloads acima de `INLINE_LIMIT_BYTES` (20 MiB); não existe fluxo alternativo de upload. Os clientes construídos internamente aplicam `REQUEST_TIMEOUT_MS` (120 s) via `http_options`, de modo que a falha chega ao worker em vez de prender o estado `TRANSCRIBING`.

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
- Tabela `preferences(key TEXT PRIMARY KEY, value TEXT NOT NULL)`: armazena `last_microphone_identity`, `recording_mouse_button` e `recording_keyboard_shortcut`; o schema permanece v1.
- Tabela `token_usage(id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, model TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER, thought_tokens INTEGER, cached_tokens INTEGER, tool_use_tokens INTEGER, total_tokens INTEGER, outcome TEXT NOT NULL CHECK(outcome IN ('success', 'error')))`: armazena o histórico de consumo de tokens por chamada.
- Índice `idx_token_usage_recorded_at ON token_usage(recorded_at)` para ordenação cronológica.

A conexão opera exclusivamente no thread principal com `check_same_thread=True` e `PRAGMA busy_timeout = 2000`. A retenção é local e cumulativa, sem rotinas de expiração automática. A consulta `get_token_totals()` realiza agregação com tratamento de nulos (retorna contagem zero apenas quando a tabela está vazia e preserva valores ausentes quando aplicável). A consulta `get_token_usage_history(limit=30)` retorna tuplas de `TokenUsageRecord` em ordem cronológica ascendente (mais antigas primeiro entre as últimas chamadas registradas), preservando campos nulos/indisponíveis e o desfecho (`success` ou `error`).

Esse histórico alimenta diretamente `TokenUsageChart` no painel `Diagnóstico` permanente. As barras representam `total_tokens`, distinguem sucesso e erro e preservam totais indisponíveis sem fabricar valores ou calcular preços.

O armazenamento opera no thread principal com tratamento fail-soft. As preferências de mouse e teclado são independentes e só são persistidas após `WATCHING_*`; falha de escrita mantém o binding apenas na sessão. `MainWindow.closeEvent` cancela o instalador, fecha `InputShortcutBridge`, encerra áudio/worker e então fecha o `LocalStore`.

O banco armazena somente as três preferências locais, timestamp, modelo, seis contagens de tokens anuláveis e outcome. Nunca armazena chaves, áudio, base64, prompt, transcrição, resposta textual, exceção bruta ou preço.

## Configuração e credenciais

`src/falafacil/config.py` define `Settings`, `DEFAULT_MODEL`, `has_api_key` e a mensagem de configuração. A precedência da chave é `GEMINI_API_KEY`, depois `GOOGLE_API_KEY`, depois o fallback persistido no Secret Service. `GEMINI_MODEL` escolhe o modelo, cujo padrão efetivo é `gemini-3.7-flash`. O valor da chave não aparece em `repr` nem em comparações de `Settings`.

`src/falafacil/credentials.py` define `ApiKeyStore`, `KeyringApiKeyStore` e os nomes fixos do serviço/conta. O armazenamento usa `keyring` sobre o Secret Service; não usa arquivo plaintext nem `QSettings`. Falhas do chaveiro são encapsuladas sem incluir a chave em mensagens. Sem chave ativa, a aplicação abre, mas mantém a gravação/transcrição desabilitada e não faz chamada de rede. Se a persistência não estiver disponível, uma chave aceita pela UI vale somente para a sessão atual.

## Terminal

`src/falafacil/terminal.py` define `TerminalBridge`, `TerminalTarget` e a allowlist de processos. A integração só atua quando `XDG_SESSION_TYPE=x11`, `xdotool` está disponível, a janela ativa fornece um PID e o nome lido de `/proc/<pid>/comm` pertence à allowlist. No envio, o texto vai para o clipboard e `Ctrl+Shift+V` é enviado à janela detectada; o FalaFácil não muda o foco, não envia Enter e não executa comandos.

Em Wayland, sem `xdotool`, sem PID ou sem terminal reconhecido, a ponte permanece indisponível e `Copiar texto` é o fallback. A janela/terminal é detectada no momento do envio, sem reutilizar um alvo antigo.

## Distribuição e validação

- `packaging/falafacil.spec` gera o bundle one-file a partir de `__main__.py`, incluindo os modos internos do serviço.
- `scripts/build_executable.sh` gera `dist/falafacil` sem propagar variáveis de chave.
- `scripts/install_desktop.sh` instala o app de usuário e desktop entry sem shell ou segredo; a autorização do serviço acontece depois, dentro da UI.
- `tests/` valida bridge/protocolo, daemon evdev injetado, instalador privilegiado em raiz temporária, armazenamento, UI, áudio, transcrição, terminal e empacotamento sem depender de rede, polkit, systemd, `/dev/input`, hardware ou display reais.

```text
app ──> ui ──> InputShortcutBridge ──AF_UNIX──> shortcut_service ──> evdev
 │      ├─> credentials / Secret Service               ▲
 │      ├─> storage / SQLite                            │ socket systemd por UID
 │      ├─> audio / PortAudio                           │
 │      ├─> transcription / google-genai                │
 │      └─> terminal / xdotool X11          shortcut_install / pkexec
 └─> __main__ despacha GUI, daemon ou instalação privilegiada
```

## Invariantes
1. Nenhuma chave Gemini é gravada em código, testes, `pyproject.toml`, desktop entry, logs, arquivos gerados, argumentos do launcher ou banco SQLite local. A fonte ativa segue a precedência definida em `config.py` e a credencial persistida fica exclusivamente no Secret Service.
2. O SQLite armazena somente `last_microphone_identity`, `recording_mouse_button`, `recording_keyboard_shortcut` e os metadados de consumo allowlisted. Nunca armazena chave, áudio, base64, prompt, transcrição, resposta, exceção bruta ou preço.
3. Campos de tokens não fornecidos pela API permanecem indisponíveis (`None`), sem conversão em zero artificial; o total agregado e o gráfico tratam valores ausentes com segurança e exibem zero somente quando não há registros no histórico. As barras do gráfico diferenciam visualmente sucesso e erro para valores conhecidos de `total_tokens`. A aplicação mede exclusivamente consumo de tokens, não precificação monetária da API, e não calcula nem estima preços.
4. A conexão com o banco local pertence e é acessada exclusivamente no thread principal; a validação de schema via `PRAGMA user_version` aceita estritamente versão `0` para criação do schema v1 e `1` para reabertura, recusando qualquer outra versão com erro sanitizado e fail-soft sem mutar o arquivo SQLite; falhas no armazenamento local não impedem captura de áudio, transcrição ou uso do clipboard.
5. Widgets, player, clipboard e banco pertencem ao thread principal. O worker de transcrição usa sinais; o daemon de atalhos é outro processo e transmite somente ACKs, capturas canônicas e ativações correspondentes.
6. A captura fecha o stream em sucesso e em falha, conserva o formato mono/`int16`/16 kHz e não envia áudio vazio ou abaixo do nível mínimo. A prioridade de seleção automática segue headset -> sessão atual -> memória persistida -> interno -> padrão do sistema.
7. O envio ao Gemini é explícito, acontece somente após a revisão da captura e respeita o limite inline de 20 MiB. Chamada bloqueante não ocupa o event loop.
8. Estados de erro informam a condição no status e permitem nova tentativa, correção ou uso do clipboard; resposta Gemini vazia nunca é aceita como sucesso.
9. A integração de terminal só cola em uma janela X11 ativa cujo processo foi reconhecido pela allowlist. Ela nunca executa comandos nem envia Enter; Wayland usa clipboard como fallback.
10. A integração global usa socket systemd `0600` por UID e daemon não-root com acesso de leitura a `input`. Mouse e teclado são independentes, press-only, generation-safe e válidos em X11/Wayland; repetição, soltura, modificadores extras, trigger diferente e geração antiga não ativam. O serviço nunca usa `grab()`, suprime eventos, persiste entrada ou transmite texto digitado.
Os contratos completos de produto, comandos e limitações estão em [AGENTS.md](AGENTS.md). A navegação documental está em [docs/INDEX.md](docs/INDEX.md).
