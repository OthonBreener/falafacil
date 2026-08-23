# FalaFácil

Aplicativo desktop local para Ubuntu que grava fala em português do Brasil, envia o áudio ao Gemini quando o usuário solicita, exibe uma transcrição editável e copia o texto para o clipboard. Em uma sessão X11 compatível, também pode colar o texto no terminal ativo; nunca executa comandos nem envia Enter.

## Recursos e comportamento

- **Seleção inteligente de microfone**: priorização automática por ordem determinística: headset detectado por heurística local (metadados de nome e host API) → dispositivo selecionado na sessão atual → memória do último microfone utilizado persistida no banco local → dispositivo interno → padrão do sistema (`is_default`) → primeiro dispositivo disponível.
- **Gravação e revisão em memória**: captura de áudio com validação de nível e pré-visualização para reproduzir antes de enviar explicitamente ao Gemini.
- **Histórico local e gráfico de consumo de tokens**: painel lateral de debug com bloco de métricas textuais (chamada atual e acumulado) e widget gráfico dedicado (`Gráfico de consumo de tokens`) baseado em SQLite local. As barras do gráfico exibem o `total_tokens` conhecido por chamada e diferenciam visualmente chamadas bem-sucedidas (verde) de erros (vermelho), mantendo totais indisponíveis como desconhecidos com segurança. O histórico armazena estritamente a allowlist de metadados permitidos: identidade do último microfone efetivamente utilizado `last_microphone_identity`, timestamp de registro `recorded_at`, modelo `model`, desfecho `outcome` como `success` ou `error` e as seis contagens de tokens (entrada, saída, pensamento, cache, ferramentas e total), preservando valores nulos ou indisponíveis quando não fornecidos pela API; a retenção é local e cumulativa, sem rotinas de limpeza automática, sem cálculo ou exibição de preço monetário da API e sem retenção de chaves de API, arquivos de áudio PCM/WAV, payloads/previews em base64, texto do prompt, transcrições ou exceções brutas.
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

O microfone requer `libportaudio2` no Ubuntu. `xdotool` é opcional e só é usado para colagem em terminal X11; em Wayland ou sem ele, use `Copiar texto`.

Para conhecer os comandos de testes, empacotamento, limitações e contratos de segurança, consulte [AGENTS.md](AGENTS.md).
