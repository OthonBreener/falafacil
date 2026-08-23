# Gate de fumaça do FalaFácil

Use este gate antes de entregar uma alteração. Ele complementa [`AGENTS.md`](../../AGENTS.md), que permanece a fonte de verdade dos contratos do produto, e segue o contrato dos papéis em [`docs/architecture/agentes.md`](../architecture/agentes.md).

## Regra geral

O agente principal deve registrar o escopo solicitado e o escopo realmente alterado. Antes da validação, o `revisor` confere `git status`, `git diff`, arquivos afetados e critérios de aceitação; não basta revisar o resumo do implementador. Nenhum segredo pode aparecer no diff, na saída, no relatório, no desktop entry ou em artefato gerado.

O `testador` executa, na raiz do repositório, os comandos abaixo e retorna somente o bloco `PASS` ou `FAIL` definido em `docs/architecture/agentes.md`:

1. Compilação dos fontes e testes:

   ```bash
   poetry run python -m compileall -q src tests
   ```

2. Testes focados em ambiente Qt sem janela real, conforme o conjunto declarado em `AGENTS.md`:

   ```bash
   QT_QPA_PLATFORM=offscreen poetry run pytest -q \
     tests/test_config.py tests/test_credentials.py tests/test_transcription.py \
     tests/test_ui.py tests/test_packaging.py
   ```

3. Suíte determinística completa:

   ```bash
   poetry run pytest -q
   ```

A ordem é compilação, testes focados e suíte completa. Se o escopo mudar áudio ou terminal, o testador inclui os testes determinísticos correspondentes (`tests/test_audio.py` e/ou `tests/test_terminal.py`) no comando focado ou registra a validação específica solicitada pelo principal. Os testes devem continuar independentes de rede, chave real, microfone, Secret Service, X11 e `xdotool`, usando as dependências falsas/injetadas já previstas pelo projeto.

## Critérios por escopo

### Documentação e mudanças sem hardware

Para uma alteração somente documental, verificar os links relativos novos e confirmar que cada promessa aponta para código, teste ou contrato existente; ainda assim, executar nesta ordem `compileall`, os testes focados em ambiente Qt sem janela real e a suíte determinística completa. Não é necessário executar smoke de microfone, rede, Secret Service ou X11.

### UI, áudio, transcrição ou terminal

Além dos comandos gerais, validar o fluxo crítico afetado com os testes determinísticos e fakes disponíveis. A validação deve respeitar estes limites de `AGENTS.md`:

- gravação mono `int16`, fechamento do stream e saída WAV em 16 kHz;
- envio ao Gemini somente depois da ação explícita de envio, sem bloquear a interface;
- worker Qt sem acesso a widgets fora do thread principal;
- chave ausente, erro de captura, resposta vazia e falha de integração deixam a janela recuperável;
- limite de payload inline e ausência de segredo em métricas, logs ou mensagens;
- terminal somente em X11, com janela ativa e processo permitido.

Smoke manual com recursos reais só é necessário quando o escopo ou a evidência pedirem essa confirmação e o ambiente fornecer microfone, backend QtMultimedia, credencial e, para terminal X11, `xdotool`. Não inventar validação de rede ou hardware ausente.

A regra de terminal é obrigatória: em Wayland ou sem `xdotool`, `Copiar texto` continua sendo o fallback. `TerminalBridge` não executa comando, não envia Enter e não promete colagem automática Wayland. Em X11, qualquer teste de `Enviar ao terminal` deve confirmar apenas clipboard e `Ctrl+Shift+V` na janela reconhecida, sem pressionar Enter.

### `packaging/` ou scripts de instalação

Quando a alteração tocar `packaging/` ou `scripts/`, executar também o gate do bundle:

```bash
poetry install --extras build
./scripts/build_executable.sh
tmp_home=$(mktemp -d)
HOME="$tmp_home" ./scripts/install_desktop.sh "$PWD/dist/falafacil"
QT_QPA_PLATFORM=offscreen HOME="$tmp_home" dist/falafacil
```

O último comando deve iniciar o bundle sem exigir rede, chave, microfone ou terminal; confirmar a inicialização da janela e encerrá-lo pelo controle do processo. O teste deve usar um `HOME` temporário e verificar que o executável e o desktop entry não carregam segredo, shell, caminho relativo ou dependência de uma configuração do usuário. Não executar esse bloco para alterações que não afetam empacotamento ou instalação.

## Critério de aprovação

O gate passa somente quando todos os critérios aplicáveis forem observáveis:

- o diff corresponde ao plano e não contém alterações fora do escopo;
- `compileall`, testes focados e `pytest` completo passam;
- o fluxo crítico afetado foi coberto por teste/fake ou smoke manual autorizado;
- o bundle foi construído e iniciado quando `packaging/` ou scripts foram afetados;
- fallback Wayland/ausência de `xdotool` preserva cópia para clipboard, sem Enter e sem execução de comandos;
- nenhum segredo aparece em código, testes, saída, relatório, desktop entry ou artefato;
- o resultado do `testador` contém somente `PASS` ou `FAIL` no formato obrigatório;
- o `revisor` recebe evidência suficiente e responde `APROVADO` somente após eliminar achados e validações ausentes.

Uma falha ou bloqueio de ambiente deve ser reportado como `FAIL` com comando e traceback mínimo; não deve ser mascarado por stub, mock falso, no-op ou `TODO`. Alterações não aprovadas retornam ao ciclo `implementador → testador → revisor`.
