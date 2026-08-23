# FalaFácil

Aplicativo desktop local para Ubuntu que grava fala em português do Brasil, envia o áudio ao Gemini quando o usuário solicita, exibe uma transcrição editável e copia o texto para o clipboard. Em uma sessão X11 compatível, também pode colar o texto no terminal ativo; nunca executa comandos nem envia Enter.

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
