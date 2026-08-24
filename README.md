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

A partir da raiz do projeto, instale as dependências e inicie a aplicação:

```bash
poetry install
poetry run python -m falafacil
```

Na janela, use `Configurar chave API` para informar a chave no campo de senha. A chave é mantida em memória e, quando o Secret Service estiver disponível, pode ser persistida pelo `keyring`; ela nunca deve ser colocada neste arquivo, em outro arquivo versionado, em logs ou em argumentos de processo. Também é possível fornecer `GEMINI_API_KEY` ou `GOOGLE_API_KEY` no ambiente local antes de iniciar o aplicativo.

Os atalhos globais de mouse e teclado são configurados pela engrenagem. Se a integração ainda não estiver ativa, o próprio aplicativo explica o acesso restrito e abre a autorização do Ubuntu; não exige terminal, `sudo`, edição de grupos ou novo login. Ambos podem ficar ativos ao mesmo tempo em X11 e Wayland, e `Space`/`Gravar` continuam disponíveis.

O microfone requer `libportaudio2` no Ubuntu. `xdotool` é opcional e só é usado para colagem em terminal X11; em Wayland ou sem ele, use `Copiar texto`.

Para conhecer os comandos de testes, empacotamento, limitações e contratos de segurança, consulte [AGENTS.md](AGENTS.md).
