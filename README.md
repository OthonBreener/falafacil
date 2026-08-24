# FalaFácil

Aplicativo desktop local para Ubuntu que grava fala em português do Brasil, envia o áudio ao Gemini quando o usuário solicita, exibe uma transcrição editável e copia o texto para o clipboard. Em uma sessão X11 compatível, também pode colar o texto no terminal ativo; nunca executa comandos nem envia Enter.

## Recursos e comportamento

- **Seleção inteligente de microfone**: priorização automática por ordem determinística: headset detectado por heurística local (metadados de nome e host API) → dispositivo selecionado na sessão atual → memória do último microfone utilizado persistida no banco local → dispositivo interno → padrão do sistema (`is_default`) → primeiro dispositivo disponível.
- **Gravação e revisão em memória**: captura de áudio com validação de nível e pré-visualização para reproduzir antes de enviar explicitamente ao Gemini.
- **Atalhos globais simultâneos**: um botão lateral/central do mouse e um atalho seguro de teclado podem alternar a gravação em X11 e Wayland. A engrenagem `Configurações` orienta a autorização administrativa única, instala e ativa o serviço local por UID e preserva `Gravar`/`Space` quando a integração está ausente. O serviço lê `/dev/input` somente para comparar ou capturar triggers, não usa `grab()`, não suprime eventos, não armazena teclas/cliques e não envia dados pela rede.
- **Diagnóstico permanente**: a janela principal mantém `Diagnóstico` visível à direita, com abas de áudio, payload, retorno e consumo e o gráfico de tokens abaixo. A transcrição ocupa 120–190 px; as ações ficam em duas linhas; a engrenagem concentra chave e atalhos; o controle adjacente alterna tela cheia.
- **Histórico local de tokens**: o painel permanente inclui métricas da chamada/acumulado e `TokenUsageChart`; SQLite preserva somente preferências allowlisted, modelo, timestamp, outcome e seis contagens anuláveis, sem áudio, prompts, transcrições, segredos ou preços.
- **Segurança e privacidade**: chaves de API, áudio/WAV/base64, prompts, respostas transcritas e exceções brutas não são persistidos no banco SQLite nem expostos em logs; o prompt estático de transcrição pode ser exibido localmente no bloco existente de payload de debug, enquanto chaves de API, conteúdo de áudio/WAV/base64, exceções brutas e segredos não são renderizados ali. O texto transcrito é exibido localmente de forma intencional no editor editável e na área de retorno de debug para conferência e edição pelo usuário, e nenhum cálculo de preço monetário é realizado. Falhas e mensagens de erro nunca expõem segredos ou exceções brutas no status ou no painel de debug.
- **Integração de terminal e clipboard**: cópia rápida para a área de transferência e colagem direta em terminais X11 suportados.

## Navegação

- [Regras e contratos do produto](AGENTS.md)
- [Mapa da arquitetura e invariantes](ARQUITETURA.md)
- [Índice da documentação](docs/INDEX.md)

## Começo rápido

### 1. Pré-requisitos no Ubuntu

- **Python**: versão `>=3.11,<3.15`.
- **Poetry**: para gerenciamento do ambiente virtual e das dependências.
- **PortAudio (`libportaudio2`)**: biblioteca nativa necessária para captura de áudio pelo microfone.
- **xdotool** *(opcional)*: necessário apenas para colagem automática em terminal em sessões X11.
- **Secret Service**: o ambiente desktop deve fornecer um backend de Secret Service (como GNOME Keyring) para persistência segura da chave API via `keyring`. Sem Secret Service, a chave informada na interface permanece válida apenas durante a sessão atual da janela.

Para instalar as bibliotecas do sistema no Ubuntu:

```bash
sudo apt update
sudo apt install libportaudio2
sudo apt install xdotool  # opcional: apenas para colagem em terminal X11
```

### 2. Executar pelo código-fonte

A partir da raiz do repositório, instale as dependências e inicie o FalaFácil:

```bash
poetry install
poetry run python -m falafacil
```

Você também pode iniciar usando o entry point equivalente:

```bash
poetry run falafacil
```

### 3. Gerar o executável distribuível

Para compilar o binário Linux autônomo (*one-file*) com o PyInstaller:

```bash
poetry install --extras build
./scripts/build_executable.sh
```

O script gera o executável em `dist/falafacil`. Você pode testá-lo diretamente a partir da raiz:

```bash
./dist/falafacil
```

### 4. Instalar no desktop

Após compilar o executável, instale o aplicativo e seu atalho no menu de aplicativos do usuário:

```bash
./scripts/install_desktop.sh "$PWD/dist/falafacil"
```

A instalação realiza as seguintes configurações no diretório do usuário:
- Copia o binário para `~/.local/bin/falafacil`.
- Cria o lançador em `~/.local/share/applications/falafacil.desktop`.

Você pode iniciar o FalaFácil pelo menu de aplicativos do sistema ou executando o caminho instalado:

```bash
~/.local/bin/falafacil
```

### 5. Recompilar e atualizar após alterações

Sempre que modificar o código-fonte e desejar atualizar o aplicativo instalado:

1. Feche qualquer instância em execução do FalaFácil (um processo aberto continua executando o código antigo retido na memória até ser encerrado).
2. Recompile e reinstale o binário:

```bash
poetry install --extras build
./scripts/build_executable.sh
./scripts/install_desktop.sh "$PWD/dist/falafacil"
```

3. Abra novamente o aplicativo pelo menu ou por `~/.local/bin/falafacil`.

### 6. Configuração no primeiro uso

1. **Chave API Gemini**:
   - Clique no ícone de engrenagem (`⚙` / `Configurações`) no topo da janela.
   - Na seção **Chave API**, informe sua chave no campo protegido por senha.
   - A chave é mantida em memória e, se o Secret Service estiver ativo, salva de forma segura.
   - Alternativamente, você pode definir `GEMINI_API_KEY` ou `GOOGLE_API_KEY` no ambiente antes de abrir o aplicativo.

2. **Atalhos globais de mouse e teclado**:
   - Na janela de **Configurações**, configure o atalho de mouse (botões laterais `x1`/`x2` ou botão central `middle`) e o atalho de teclado (combinação com modificadores como `Ctrl+Alt+R` ou teclas especiais/função).
   - Se a integração global não estiver instalada, estiver incompatível ou indisponível, o aplicativo exibe a opção de autorização.
   - Toda a autorização ocorre dentro da própria interface gráfica via prompt administrativo do Ubuntu: você não precisa abrir terminal, usar `sudo`, editar grupos de usuários ou reiniciar a sessão.
   - Aceite a autorização quando solicitada; o aplicativo instala o serviço local por UID e retoma a captura do atalho automaticamente.
   - Os atalhos globais e os controles manuais (`Gravar` e tecla `Espaço`) podem coexistir e funcionar simultaneamente.
   - Ao acionar um atalho global, a janela é restaurada e trazida à frente antes de a gravação alternar, mesmo se estiver minimizada. Em X11 o foco é imediato; alguns compositores Wayland apenas sinalizam a janela como pronta.
   - Se um botão não for aceito, o diálogo explica o motivo; se nenhuma entrada for reconhecida, ele orienta o remapeamento no software do fabricante.

#### Botões do mouse que o Linux não enxerga

Alguns botões de mouses gamer são resolvidos dentro do próprio dispositivo e **não emitem evento algum** em `/dev/input`. Nenhum aplicativo Linux consegue detectá-los, incluindo o FalaFácil. O caso mais comum é o botão sniper (mira) do Corsair M65, cuja função padrão é um DPI-shift em firmware.

Botões assim costumam ser reprogramáveis: ao receber uma ação de botão ou tecla no lugar da função de firmware, o dispositivo passa a reportá-la por HID e o atalho volta a funcionar. O remapeamento precisa ser feito no software do fabricante, e mouses gamer guardam o perfil em memória onboard — basta remapear uma vez e a configuração acompanha o dispositivo.

Para o Corsair M65 RGB ULTRA especificamente:

- O **iCUE** não roda no Linux, mas o perfil de hardware gravado por ele persiste no mouse. Remapear o botão uma única vez em uma máquina Windows e salvar no perfil onboard resolve de forma definitiva, sem exigir software rodando no Linux.
- O **ckb-next**, alternativa livre para dispositivos Corsair, ainda **não suporta este modelo** (`1b1c:1b9e`): o suporte está em aberto na [issue #912](https://github.com/ckb-next/ckb-next/issues/912) e não faz parte da versão empacotada pelo Ubuntu. Para outros modelos Corsair já suportados, ele cumpre o mesmo papel.

Ao remapear, escolha `Mouse 4` ou `Mouse 5` — que chegam ao sistema como `x1`/`x2` — ou uma tecla de `F13` a `F24`, aceita como atalho de teclado. Depois reabra **Configurações → atalho do mouse → Configurar** e capture o botão.

Para conferir se um botão emite evento, `sudo evtest` (pacote `evtest`) mostra os eventos brutos do dispositivo escolhido: se nada aparecer ao pressioná-lo, o botão não chega ao sistema.

### 7. Verificação e solução de problemas

#### Testes locais e compilação

Para validar o ambiente e a integridade do código sem abrir janelas gráficas:

```bash
QT_QPA_PLATFORM=offscreen poetry run pytest -q
poetry run python -m compileall -q src tests
```

#### Diagnóstico de problemas comuns

- **Microfone não inicia ou botão `Gravar` indisponível**: certifique-se de que o pacote `libportaudio2` está instalado no sistema e que um dispositivo de entrada de áudio funcional está conectado.
- **Chave API pede para ser digitada novamente após reabrir**: indica ausência de um backend ativo de Secret Service (como GNOME Keyring). A chave continuará funcionando normalmente durante a sessão aberta, mas não será persistida em disco.
- **Envio ao terminal indisponível ou em sessão Wayland**: a colagem direta em janelas de terminal é suportada apenas em sessões X11 com `xdotool` instalado e para emuladores de terminal suportados. Em Wayland ou sem `xdotool`, utilize o botão **Copiar texto** (ou o atalho `Ctrl+Shift+C`) e cole manualmente com `Ctrl+Shift+V`.
- **Alterações no código não aparecem no aplicativo**: certifique-se de que encerrou completamente os processos anteriores do FalaFácil antes de testar a nova versão compilada. Um processo aberto continua executando os arquivos antigos que já estavam carregados na memória. Se a interface solicitar a atualização da integração global após uma reconstrução, autorize-a uma única vez no diálogo do sistema.

---

Para conhecer os comandos detalhados de desenvolvimento, contratos de segurança e limitações completas, consulte [AGENTS.md](AGENTS.md).
