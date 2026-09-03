# FalaFácil

Aplicativo desktop local para Ubuntu que grava fala em português do Brasil, envia o áudio ao Gemini quando o usuário solicita, exibe uma transcrição editável e copia o texto para o clipboard. Em uma sessão X11 compatível, também pode colar o texto no terminal ativo ou de origem salvo; nunca executa comandos nem envia Enter.

## Recursos e comportamento

- **Seleção inteligente de microfone**: priorização automática por ordem determinística: headset detectado por heurística local (metadados de nome e host API) → dispositivo selecionado na sessão atual → memória do último microfone utilizado persistida no banco local → dispositivo interno → padrão do sistema (`is_default`) → primeiro dispositivo disponível.
- **Escolha de modelo Gemini**: seleção entre `gemini-3.5-flash-lite` (econômico e rápido, padrão), `gemini-3.7-flash` (qualidade) e `gemini-3.8-flash` (mais capaz) pela interface de configurações. A preferência é persistida no banco SQLite local e pode ser sobrescrita pelo ambiente via `GEMINI_MODEL`. A API de transcrição utiliza a ordem direta prompt→áudio, envia com `store=False` e registra `total_cached_tokens` observados do cache implícito da API, sem camadas explícitas de cache.
- **Gravação e revisão em memória**: fluxo de ação principal sensível ao estado (`Gravar` → `Parar e revisar áudio` → `Enviar para Gemini`), validação de nível e de integridade do áudio e pré-visualização para reproduzir antes de enviar explicitamente ao Gemini. A reprodução alterna com `Parar reprodução` e é interrompida e desanexada com segurança antes de ações incompatíveis ou encerramento (bloqueando a ação se a liberação falhar). O áudio WAV utilizável sobrevive a erros de limpeza ao parar/fechar e a falhas transitórias do Gemini para permitir nova tentativa idêntica. O botão dedicado `Descartar e gravar novamente` permite reiniciar a captura preservando o áudio pendente anterior caso o novo início, parada, nível, status ou limpeza falhe.
- **Atalhos globais simultâneos**: um botão lateral/central do mouse e um atalho seguro de teclado podem acionar a gravação em X11 e Wayland. O início global da gravação não rouba o foco da janela de trabalho (capturando opcionalmente o terminal X11 de origem em segundo plano); a parada global restaura e traz a janela à frente para revisão do áudio capturado; e uma nova ativação global no estado de áudio pronto envia para o Gemini sem nova elevação de foco. Os atalhos globais compartilham debounce de 0,35 s, e a ponte de atalhos reconecta-se automaticamente após 1000 ms em caso de desconexão ou erro do serviço. A engrenagem `Configurações` orienta a autorização administrativa única, instala e ativa o serviço local por UID e preserva os controles manuais (`Gravar` e `Space` fora de campos de texto) quando a integração está ausente. O serviço lê `/dev/input` somente para comparar ou capturar triggers, não usa `grab()`, não suprime eventos, não armazena teclas/cliques e não envia dados pela rede.
- **Corretor ortográfico e revisor com IA**: a área de trabalho organiza-se em dois blocos: **Transcrição atual** e **Última mensagem — somente nesta sessão**. O editor da última mensagem conta com feedback visual de sublinhado ondulado vermelho para palavras desconhecidas via dicionário pt-BR nativo (`libenchant-2.so.2` / `hunspell-pt-br`), balão flutuante de sugestões e menu de contexto customizado para substituição rápida ou ignorar palavra. O botão **Revisar com IA** executa revisão gramatical e ortográfica profunda (concordância, regência, crase, pontuação e homófonos contextuais) pelo Gemini assíncrono com `store=False`, substituindo e copiando o texto com suporte a Desfazer (Undo).
- **Diagnóstico permanente**: a janela principal mantém `Diagnóstico` visível à direita, com abas de áudio, payload, retorno e consumo e o gráfico de tokens abaixo. À esquerda, os blocos de **Transcrição atual** e **Última mensagem** organizam o fluxo sem limites artificiais de altura; as ações dedicadas por bloco incluem `Copiar e arquivar`, `Revisar com IA`, `Copiar novamente`, `Enviar ao terminal` e `Apagar`; a engrenagem concentra chave, modelo, atalhos, corretor e atualizações; o controle adjacente alterna tela cheia.
- **Histórico local de tokens**: o painel permanente inclui métricas da chamada/acumulado e `TokenUsageChart`; o schema SQLite v1 preserva somente preferências allowlisted (incluindo `gemini-3.8-flash`), modelo, timestamp, outcome e seis contagens anuláveis, sem áudio, prompts, transcrições, mensagens da sessão, terminais de origem salvos, segredos ou preços.
- **Segurança e privacidade**: chaves de API, áudio/WAV/base64, prompts, respostas transcritas, mensagens da sessão, terminais de origem e exceções brutas não são persistidos no banco SQLite nem expostos em logs; o prompt estático de transcrição e de revisão pode ser exibido localmente no bloco existente de payload de debug, enquanto chaves de API, conteúdo de áudio/WAV/base64, exceções brutas e segredos não são renderizados ali. O texto transcrito e a última mensagem residem exclusivamente na memória da sessão atual para conferência e edição pelo usuário, e nenhum cálculo de preço monetário é realizado. Falhas e mensagens de erro nunca expõem segredos ou exceções brutas no status ou no painel de debug.
- **Integração de terminal e clipboard**: cópia automática para a área de transferência na transcrição ou revisão, e envio direto ao terminal X11 com validação na allowlist e colagem via `Ctrl+Shift+V` sem executar comandos nem enviar Enter. Para o terminal de origem salvo na gravação global, a integração realiza revalidação imediata de PID/processo, ativação síncrona da janela (`xdotool windowactivate --sync`) e confirmação de foco antes de colar (falhando sem colar ou redetectar caso o alvo salvo seja inválido); para o terminal atualmente ativo (sem alvo salvo), detecta a janela ativa e cola diretamente sem mudar o foco.
- **Atualizações pelo Homebrew na interface**: gerenciamento de atualizações em **Configurações → Atualizações** através do botão **Instalar atualizações**, que atualiza o catálogo e aplica novas versões sem shell, terminal ou sudo. O comando `brew update` atualiza apenas os metadados do tap/fórmula; a instalação da versão ocorre pelo botão na interface ou por `brew upgrade`. A atualização do pacote do produto permanece independente da integração privilegiada de atalhos globais.

## Navegação

- [Regras e contratos do produto](AGENTS.md)
- [Mapa da arquitetura e invariantes](ARQUITETURA.md)
- [Índice da documentação](docs/INDEX.md)
- [Ciclo de release e publicação Homebrew](docs/RELEASE.md)

## Instalação recomendada (Homebrew no Ubuntu)

Para usuários finais no Ubuntu Linux x86_64, a instalação oficial é distribuída pelo Homebrew:

```bash
brew install OthonBreener/falafacil/falafacil
```

O executável distribuído via Homebrew é autônomo (*one-file*) e já inclui a biblioteca PortAudio embutida, não exigindo a instalação manual de `libportaudio2` pelo apt. Execute o `falafacil` uma vez no terminal após a instalação para registrá-lo no menu de aplicativos do desktop. Atualizações posteriores são realizadas diretamente pela interface gráfica do aplicativo em **Configurações → Atualizações**.
Caso você possua uma instalação prévia de desenvolvimento (`~/.local/bin/falafacil`), remova-a com `rm -f ~/.local/bin/falafacil ~/.local/share/applications/falafacil.desktop` antes de instalar via Homebrew. As preferências no diretório de dados, a chave API no Secret Service e o serviço de atalhos do usuário são mantidos automaticamente. Consulte o [guia de migração e release](docs/RELEASE.md) para mais detalhes.

## Começo rápido para desenvolvimento

### 1. Pré-requisitos para execução pelo código-fonte

- **Python**: versão `>=3.11,<3.15`.
- **Poetry**: para gerenciamento do ambiente virtual e das dependências.
- **PortAudio (`libportaudio2`)**: biblioteca nativa do sistema necessária no ambiente de desenvolvimento/compilação para captura de áudio pelo microfone.
- **xdotool** *(opcional)*: necessário apenas para colagem automática em terminal em sessões X11.
- **Secret Service**: o ambiente desktop deve fornecer um backend de Secret Service (como GNOME Keyring) para persistência segura da chave API via `keyring`. Sem Secret Service, a chave informada na interface permanece válida apenas durante a sessão atual da janela.

Para instalar as bibliotecas do sistema no Ubuntu para desenvolvimento:

```bash
sudo apt update
sudo apt install libportaudio2
sudo apt install xdotool  # opcional: apenas para colagem em terminal X11
sudo apt install libenchant-2-2 hunspell-pt-br  # opcional: para corretor ortográfico pt-BR
```

### 2. Executar pelo código-fonte

A partir da raiz do repositório, instale as dependências, registre o pacote local com o setuptools e inicie o FalaFácil:

```bash
poetry install --extras dev
poetry run pip install --no-deps -e .
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
poetry run pip install --no-deps -e .
./scripts/build_executable.sh
```
O script gera o executável em `dist/falafacil`. Você pode testá-lo diretamente a partir da raiz:

```bash
./dist/falafacil
```

### 4. Instalar no desktop (desenvolvimento local)

Após compilar o executável, o script de desenvolvimento instala a cópia do binário e aciona o instalador de desktop do bundle:

```bash
./scripts/install_desktop.sh "$PWD/dist/falafacil"
```

A instalação de desenvolvimento realiza as seguintes etapas no diretório do usuário:
- Copia atomicamente o binário para `~/.local/bin/falafacil`.
- Executa o modo interno `--install-user-desktop` do executável instalado para registrar atomicamente `~/.local/share/applications/falafacil.desktop` com permissões `0644`.

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
poetry run pip install --no-deps -e .
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

2. **Modelo Gemini**:
   - Na janela de **Configurações**, na seção **Modelo Gemini**, escolha o modelo desejado e clique em **Aplicar modelo**.
   - As opções disponíveis na interface são:
     - `gemini-3.5-flash-lite` (econômico e rápido, padrão)
     - `gemini-3.7-flash` (qualidade)
     - `gemini-3.8-flash` (mais capaz)
   - A preferência escolhida é salva no banco SQLite local.
   - Se a variável de ambiente `GEMINI_MODEL` estiver definida com valor não vazio, ela terá precedência sobre o modelo salvo e travará o seletor na interface.

3. **Atalhos globais de mouse e teclado**:
   - Na janela de **Configurações**, configure o atalho de mouse (botões laterais `x1`/`x2` ou botão central `middle`) e o atalho de teclado (combinação com modificadores como `Ctrl+Alt+R` ou teclas especiais/função).
   - Se a integração global não estiver instalada, estiver incompatível ou indisponível, o aplicativo exibe a opção de autorização.
   - Toda a autorização ocorre dentro da própria interface gráfica via prompt administrativo do Ubuntu: você não precisa abrir terminal, usar `sudo`, editar grupos de usuários ou reiniciar a sessão.
   - Aceite a autorização quando solicitada; o aplicativo instala o serviço local por UID e retoma a captura do atalho automaticamente.
   - Os atalhos globais e os controles manuais (`Gravar` e tecla `Espaço` fora de campos de texto) podem coexistir e funcionar simultaneamente.
   - O acionamento global para iniciar a gravação preserva o foco no aplicativo em uso (capturando opcionalmente a janela de terminal X11 de origem); ao parar a gravação, a janela do FalaFácil é restaurada e trazida à frente para revisão do áudio. Um acionamento global subsequente com o áudio pronto envia a transcrição para o Gemini sem nova alteração de foco.
   - Os atalhos globais possuem debounce compartilhado de 0,35 s para prevenir disparos acidentais; botões na interface e a tecla `Espaço` respondem imediatamente sem debounce.
   - Se a conexão com o serviço de atalhos for interrompida, o aplicativo entra em reconexão automática após 1000 ms, reemitindo os atalhos ativos após restabelecer o canal.
   - Se um botão não for aceito, o diálogo explica o motivo; se nenhuma entrada for reconhecida, ele orienta o remapeamento no software do fabricante.

4. **Atualizações pelo Homebrew**:
   - Na seção **Atualizações**, o aplicativo exibe a versão atualmente em execução (`Versão instalada: <versão>`) e o botão **Instalar atualizações**.
   - Em instalações gerenciadas pelo Homebrew, clicar no botão consulta novas versões e instala a atualização automaticamente em segundo plano. Ao concluir, um diálogo oferece **Reiniciar agora** (reiniciando a aplicação na versão atualizada) ou **Mais tarde**.
   - Em execuções a partir do código-fonte ou pelo instalador de desenvolvimento local (`scripts/install_desktop.sh`), o botão fica desabilitado e exibe a orientação oficial: `Instale o FalaFácil com: brew install OthonBreener/falafacil/falafacil`.
   - A atualização do aplicativo pelo Homebrew é independente da atualização da integração privilegiada de atalhos globais (que continua gerenciada separadamente pelo fluxo de autorização `pkexec` quando houver alteração de `PROTOCOL_VERSION`).
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

- **Microfone não inicia ou botão `Gravar` indisponível**: ao executar pelo código-fonte ou compilar localmente, certifique-se de que o pacote `libportaudio2` está instalado no sistema (`sudo apt install libportaudio2`) e que um dispositivo de entrada de áudio funcional está conectado. Na versão distribuída via Homebrew, a biblioteca já vem embutida no binário.
- **Chave API pede para ser digitada novamente após reabrir**: indica ausência de um backend ativo de Secret Service (como GNOME Keyring). A chave continuará funcionando normalmente durante a sessão aberta, mas não será persistida em disco.
- **Envio ao terminal indisponível ou em sessão Wayland**: a colagem direta em janelas de terminal é suportada apenas em sessões X11 com `xdotool` instalado e para emuladores de terminal suportados. A integração com alvo salvo revalida o processo/PID, ativa a janela de forma síncrona (`xdotool windowactivate --sync`), confirma o foco e cola com `Ctrl+Shift+V`; para o terminal atualmente ativo (sem alvo salvo), detecta a janela ativa e cola diretamente sem alterar o foco. Se um alvo salvo tornar-se inválido, o envio falha sem colar ou redetectar. Em Wayland ou sem `xdotool`, utilize o botão **Copiar novamente** (ou **Copiar e arquivar**) e cole manualmente com `Ctrl+Shift+V`.
- **Alterações no código não aparecem no aplicativo**: certifique-se de que encerrou completamente os processos anteriores do FalaFácil antes de testar a nova versão compilada. Um processo aberto continua executando os arquivos antigos que já estavam carregados na memória. Se a interface solicitar a atualização da integração global após uma reconstrução, autorize-a uma única vez no diálogo do sistema.
- **Corretor ortográfico não sublinha palavras no editor**: certifique-se de que os pacotes `libenchant-2-2` e `hunspell-pt-br` estão instalados no Ubuntu (`sudo apt install libenchant-2-2 hunspell-pt-br`) e que a opção está ativada no grupo **Corretor ortográfico** das Configurações. O botão **Revisar com IA** funciona de forma independente com o modelo Gemini configurado.
- **Atualização de versão via Homebrew**: o comando `brew update` atualiza apenas os índices e metadados das fórmulas, sem instalar a nova versão do pacote. Para instalar a atualização, utilize o botão **Instalar atualizações** na janela de **Configurações** do FalaFácil ou execute `brew upgrade OthonBreener/falafacil/falafacil`. Se a nova versão incluir alterações no protocolo de atalhos globais, a interface solicitará separadamente a atualização da integração global via diálogo do sistema.

---

Para conhecer os comandos detalhados de desenvolvimento, contratos de segurança e limitações completas, consulte [AGENTS.md](AGENTS.md).
