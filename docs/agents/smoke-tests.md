# Gate de fumaça do FalaFácil

Use este gate antes de entregar uma alteração. Ele complementa [`AGENTS.md`](../../AGENTS.md), que permanece a fonte de verdade dos contratos do produto, e segue o contrato dos papéis em [`docs/architecture/agentes.md`](../architecture/agentes.md).

## Regra geral

O agente principal deve registrar o escopo solicitado e o escopo realmente alterado. Antes da validação, o `revisor` confere `git status`, `git diff`, arquivos afetados e critérios de aceitação; não basta revisar o resumo do implementador. Nenhum segredo pode aparecer no diff, na saída, no relatório, no desktop entry ou em artefato gerado.

O `testador` executa, na raiz do repositório, os comandos abaixo e retorna somente o bloco `PASS` ou `FAIL` definido em `docs/architecture/agentes.md`:

1. Compilação dos fontes e testes:

   ```bash
   poetry run python -m compileall -q src tests scripts
   ```

2. Testes focados em ambiente Qt sem janela real, conforme o conjunto declarado em `AGENTS.md`:

   ```bash
   QT_QPA_PLATFORM=offscreen poetry run pytest -q \
     tests/test_shortcuts.py tests/test_storage.py tests/test_config.py \
     tests/test_credentials.py tests/test_transcription.py \
     tests/test_app.py tests/test_homebrew_update.py \
     tests/test_ui.py tests/test_packaging.py
   ```

3. Suíte determinística completa:

   ```bash
   poetry run pytest -q
   ```

A ordem é compilação, testes focados e suíte completa. Se o escopo mudar áudio ou terminal, o testador inclui os testes determinísticos correspondentes (`tests/test_audio.py` e/ou `tests/test_terminal.py`) no comando focado ou registra a validação específica solicitada pelo principal. Os testes devem continuar independentes de rede, chave real, microfone, Secret Service, X11 e `xdotool`, usando as dependências falsas/injetadas já previstas pelo projeto.

## Critérios por escopo

### Documentação e mudanças sem hardware

Para alteração documental, verificar links e confirmar cada promessa contra código/teste. Execute `compileall`, testes focados offscreen e suíte completa; hardware, polkit, systemd, `/dev/input`, rede e Secret Service só entram quando o escopo exige smoke físico.

### UI, áudio, transcrição, atalhos globais ou terminal

Além dos comandos gerais, validar o fluxo crítico afetado com os testes determinísticos e fakes disponíveis. A validação deve respeitar estes limites de `AGENTS.md`:

- gravação mono `int16`, fechamento do stream e saída WAV em 16 kHz;
- envio ao Gemini somente depois da ação explícita de envio, sem bloquear a interface;
- worker Qt sem acesso a widgets fora do thread principal;
- chave ausente, erro de captura, resposta vazia e falha de integração deixam a janela recuperável;
- mouse e teclado independentes e simultâneos em ambientes declarados X11/Wayland; pressão correspondente avança o fluxo via `_perform_primary_action` nos três estágios globais aceitos (início de gravação sem roubo de foco/elevação da janela; parada e elevação da janela para revisão do áudio capturado sem chamadas de rede; terceiro acionamento no estado `AUDIO_READY` despacha o envio ao Gemini), enquanto soltura, repetição, trigger diferente, modificador extra e geração antiga não acionam; atalhos globais compartilham debounce de 0,35 s e reconexão automática com temporizador único de 1000 ms;
- normalização segura e persistência separada de `recording_mouse_button`/`recording_keyboard_shortcut` no schema v1, somente após ACK, com fail-soft de escrita;
- framing parcial/múltiplo, limite de 128 bytes, handshake, captura one-shot, isolamento de cliente/tipo e ausência de vazamento de teclas não correspondentes;
- botão de mouse rejeitado exibe explicação no diálogo de captura, com vocabulário de rejeição fechado e nenhum envio fora do modo de captura; captura sem entrada reconhecida orienta o remapeamento em vez de permanecer em espera;
- autorização assíncrona sem shell/segredo, retomada da captura, socket `0600` por UID, daemon não-root/hardened e operação manual/`Space` preservadas em falha;
- encerramento ordenado e não bloqueante: cancelamento do instalador, desconexão de `InputShortcutBridge`, liberação de recursos de áudio e fechamento de `LocalStore` ocorrem uma única vez na UI, mantendo fechamento diferido sem `QThread.wait()` enquanto workers finalizam;
- configurações com seis grupos ordenados (`Chave API`, `Modelo Gemini`, `Atalho do mouse`, `Atalho do teclado`, `Corretor ortográfico`, `Atualizações`); grupo `Atualizações` com versão instalada, status, barra de progresso indeterminada durante execução e botão literal `Instalar atualizações`;
- atualização Homebrew protegida contra cliques duplicados e estados busy (`RECORDING`/`TRANSCRIBING`), orientando instalação via brew em ambiente sem marker e oferecendo diálogo de reinício com opções literais `Reiniciar agora` e `Mais tarde`;
- `closeEvent` bloqueia na primeira linha enquanto uma atualização Homebrew estiver em andamento, emitindo aviso sem mutar `_is_closing` e executando o encerramento ordenado somente após a finalização;
- separação explícita entre a atualização do aplicativo via Homebrew e a atualização privilegiada do serviço de atalhos globais (gerenciada por `PROTOCOL_VERSION`/pkexec);
- limite de payload inline de 20 MiB (`INLINE_LIMIT_BYTES`) aplicado exclusivamente à transcrição de áudio WAV (a revisão profunda textual envia o texto integral com `store=False` sem reivindicar limite de 20 MiB) e ausência de segredo em métricas, logs ou mensagens;
- terminal somente em X11 com processos permitidos da allowlist; diferenciação estrita entre o envio para a janela ativa detectada (colagem direta via `Ctrl+Shift+V` e clipboard, sem confirmação de ativação ou alteração de foco) e o envio para o alvo de origem salvo (capturado no início global, com revalidação imediata de PID/processo, ativação síncrona `xdotool windowactivate --sync`, confirmação de ativação da janela e colagem via `Ctrl+Shift+V`; alvos inválidos falham de forma fechada sem mutação de clipboard ou redetecção).

Smoke físico só é exigível quando o ambiente fornece Ubuntu/systemd/polkit, `/dev/input` legível pelo serviço e mouse/teclado auxiliares. Repetir em X11 e Wayland quando ambos estiverem disponíveis: configurar `x1` e `Ctrl+Alt+R`, testar fora de foco, desconectar/reconectar dispositivos e confirmar isolamento ao desativar cada binding. Sem esses recursos, registrar exatamente o não observado e usar as provas determinísticas; nunca declarar smoke físico como executado.

A regra de terminal é obrigatória: em Wayland, sem `xdotool` ou em caso de falha, os botões `Copiar novamente` e `Copiar e arquivar` atuam como fallback seguro. `TerminalBridge` não executa comando, não envia Enter e não promete colagem automática Wayland. Em X11, o envio para terminal salvo revalida PID/processo, ativa a janela sincronamente (`windowactivate --sync`), confirma a ativação e cola via `Ctrl+Shift+V` e clipboard, falhando de forma fechada sem redetecção se o alvo for inválido; o envio para janela ativa detectada cola diretamente sem confirmação de ativação ou alteração de foco; nenhum fluxo envia Enter.
### `packaging/` ou scripts de instalação

Quando a alteração tocar `packaging/` ou `scripts/`, executar também o gate do bundle:

```bash
poetry install --extras build
poetry run pip install --no-deps -e .
./scripts/build_executable.sh
./dist/falafacil --update-probe 0.3.0
tmp_home=$(mktemp -d)
HOME="$tmp_home" ./scripts/install_desktop.sh "$PWD/dist/falafacil"
env -u GEMINI_API_KEY -u GOOGLE_API_KEY -u LD_LIBRARY_PATH HOME="$tmp_home" QT_QPA_PLATFORM=offscreen timeout 5s "$tmp_home/.local/bin/falafacil" || [ $? -eq 124 ]
```

O bundle compilado deve responder `--update-probe 0.3.0` com código de saída 0, instalar o desktop entry em `$tmp_home/.local/share/applications/falafacil.desktop` modo `0644` apontando para o executável instalado via dispatch `--install-user-desktop` e abrir offscreen em smoke controlado do binário instalado (`$tmp_home/.local/bin/falafacil` com `GEMINI_API_KEY`, `GOOGLE_API_KEY` e `LD_LIBRARY_PATH` explicitamente desarmados, encerrado via timeout 124 ou controle de processo, sem esperar que uma aplicação GUI encerre naturalmente) sem exigir rede, chave, microfone, terminal ou pacote `libportaudio2` do host (PortAudio é embutido no executável one-file). O primeiro startup sob Homebrew registra o desktop entry automaticamente antes de exibir a janela, enquanto execuções a partir do código-fonte ou modo developer não realizam escritas automáticas (coberto deterministicamente em `tests/test_homebrew_update.py` e `tests/test_desktop_install.py`). Quando o serviço/instalador de atalhos globais ou `PROTOCOL_VERSION` mudarem, o smoke instalado também abre `Configurações`, solicita `Autorizar integração global`, confirma retomada automática e verifica `falafacil-shortcutd@<uid>.socket` ativo, socket `/run/falafacil-shortcutd-<uid>.sock` `0600` do usuário e serviço com usuário dinâmico/grupo `input` e fronteira de leitura restrita a dispositivos de entrada (`DevicePolicy=closed` e `char-input`). A suíte determinística nunca aciona polkit ou systemd reais.
## Critério de aprovação

O gate passa somente quando todos os critérios aplicáveis forem observáveis:

- o diff corresponde ao plano e não contém alterações fora do escopo;
- `compileall`, testes focados e `pytest` completo passam;
- o fluxo crítico afetado foi coberto por teste/fake ou smoke manual autorizado;
- o bundle foi construído e iniciado quando `packaging/` ou scripts foram afetados;
- ausência/falha do serviço global preserva `Gravar`, `Space`, editor e clipboard em X11/Wayland; ausência de `xdotool` afeta somente colagem no terminal;
- nenhum segredo aparece em código, testes, saída, relatório, desktop entry ou artefato;
- o resultado do `testador` contém somente `PASS` ou `FAIL` no formato obrigatório;
- o `revisor` recebe evidência suficiente e responde `APROVADO` somente após eliminar achados e validações ausentes.

Uma falha ou bloqueio de ambiente deve ser reportado como `FAIL` com comando e traceback mínimo; não deve ser mascarado por stub, mock falso, no-op ou `TODO`. Alterações não aprovadas retornam ao ciclo `implementador → testador → revisor`.
