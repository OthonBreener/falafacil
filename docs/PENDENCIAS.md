# Pendências para próximas releases

## 0.2.2 — corrigir modelo padrão Gemini

### `gemini-2.5-flash-lite` indisponível para novos usuários

**Status:** confirmado em 2026-08-25.

O envio pela Interactions API falha somente com `gemini-2.5-flash-lite`, embora a mesma chave funcione com `gemini-3.5-flash-lite` e `gemini-3.7-flash`. A resposta da API é:

> `models/gemini-2.5-flash-lite is no longer available to new users. Please update your code to use models/gemini-3.5-flash-lite for the latest features and improvements.`

**Correção planejada:**

- tornar `gemini-3.5-flash-lite` o modelo padrão;
- remover `gemini-2.5-flash-lite` da seleção da interface;
- migrar uma preferência persistida em 2.5 para 3.5 de forma fail-soft;
- adicionar testes para o novo padrão e para a migração;
- atualizar documentação, exemplos e o gate de release;
- publicar a versão `0.2.2`.

**Critério de aceite:** uma instalação nova e uma instalação que tenha `gemini-2.5-flash-lite` persistido devem iniciar usando `gemini-3.5-flash-lite`, e uma transcrição deve concluir sem o erro de modelo não encontrado.
