# Arquitetura

O FalaFácil é um aplicativo desktop local para Ubuntu. A composição abaixo descreve os módulos que existem hoje; não há servidor próprio, banco de dados, ORM ou camada de API do produto. A fronteira de rede é o cliente `google-genai` usado pela transcrição.

## Composição da aplicação

`falafacil.app:main` cria a única `QApplication`, define os nomes da aplicação, lê a credencial persistida e compõe `Settings`, `GeminiTranscriber` e `MainWindow` em `src/falafacil/app.py`. A janela é exibida somente depois dessa composição. O entry point `falafacil` e `python -m falafacil` chegam ao mesmo ponto; `src/falafacil/__main__.py` apenas encaminha a chamada de módulo.

`MainWindow` e `AppState` vivem em `src/falafacil/ui.py`. A UI coordena seleção de microfone, gravação, pré-visualização, reprodução em memória, envio explícito, editor de texto, painel de debug, configuração de chave, clipboard e o ciclo do `QThread`. Os estados observáveis são `IDLE`, `RECORDING`, `AUDIO_READY`, `TRANSCRIBING`, `READY` e `ERROR`; uma falha deixa a janela recuperável.

## Áudio

`src/falafacil/audio.py` encapsula `AudioDevice`, `AudioCapture`, `AudioRecorder`, listagem de entradas, callback do `sounddevice` e serialização WAV. A captura é mono, `int16` e produz áudio em 16 kHz. Quando o dispositivo só aceita outra taxa nativa, a reamostragem para 16 kHz ocorre fora do callback, antes da serialização final.

O callback do `InputStream` somente copia/enfileira os blocos recebidos e registra o status. RMS, pico, reamostragem, I/O e acesso à UI ocorrem fora dele. Captura vazia ou abaixo de `MIN_RMS_LEVEL` vira diagnóstico/erro e não é enviada ao Gemini. O áudio pendente e o buffer de reprodução permanecem em memória.

## Transcrição Gemini

`src/falafacil/transcription.py` contém `GeminiTranscriber`, `TranscriptionWorker`, `TranscriptionDebug`, `TranscriptionError` e o prompt em português do Brasil. O envio usa `google-genai` (`from google import genai`) com áudio WAV inline em base64 e rejeita payloads acima de `INLINE_LIMIT_BYTES` (20 MiB); não existe fluxo alternativo de upload.

A única ação que inicia a chamada é `Enviar para Gemini`, depois de uma captura válida e da pré-visualização. A chamada executa em `TranscriptionWorker` dentro de um `QThread`. Os sinais `finished` e `failed` retornam ao thread principal, sem acessar widgets a partir do worker. Resposta sem `output_text` é erro, não transcrição válida.

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
- `tests/` valida configuração, credenciais, áudio, transcrição, UI, terminal e empacotamento com dependências falsas/injetadas quando necessário; a suíte não depende de rede, microfone, Secret Service ou terminal reais.

Esses diretórios são fronteiras operacionais do projeto, não camadas adicionais. A direção de dependências é:

```text
app
 └─> ui
      ├─> credentials ─────────> keyring / Secret Service
      ├─> audio ───────────────> sounddevice / PortAudio
      ├─> transcription ───────> google-genai
      └─> terminal ────────────> xdotool / X11

packaging ──> __main__ ──> app
 tests ─────> módulos via dependências injetadas
```

## Invariantes

1. Nenhuma chave Gemini é gravada em código, testes, `pyproject.toml`, desktop entry, logs, arquivos gerados ou argumentos do launcher. A fonte ativa segue a precedência definida em `config.py` e a credencial persistida fica somente no Secret Service.
2. Widgets Qt, `QMediaPlayer`, clipboard e demais objetos de UI só são acessados no thread principal. O worker de transcrição comunica resultados por sinais; o callback de áudio não toca em widgets.
3. A captura fecha o stream em sucesso e em falha, conserva o formato mono/`int16`/16 kHz e não envia áudio vazio ou abaixo do nível mínimo.
4. O envio ao Gemini é explícito, acontece somente após a revisão da captura e respeita o limite inline de 20 MiB. Chamada bloqueante não ocupa o event loop.
5. Estados de erro informam a condição no status e permitem nova tentativa, correção ou uso do clipboard; resposta Gemini vazia nunca é aceita como sucesso.
6. A integração de terminal só cola em uma janela X11 ativa cujo processo foi reconhecido pela allowlist. Ela nunca executa comandos nem envia Enter; Wayland usa clipboard como fallback.

Os contratos completos de produto, comandos e limitações estão em [AGENTS.md](AGENTS.md). A navegação documental está em [docs/INDEX.md](docs/INDEX.md).
