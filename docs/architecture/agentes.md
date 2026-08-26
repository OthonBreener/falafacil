# Arquitetura de agentes de desenvolvimento

Este documento descreve o ciclo de agentes que altera e valida o FalaFácil. Ele trata somente de agentes de desenvolvimento do repositório; não descreve agentes do produto, prompts Gemini ou automação de transcrição. Os contratos de produto, segurança, stack, comandos e limitações permanecem em [`AGENTS.md`](../../AGENTS.md). O gate executável está em [`docs/agents/smoke-tests.md`](../agents/smoke-tests.md).

## Escopo e autoridade

- O `AGENTS.md` é a fonte de verdade para o comportamento do aplicativo. Este documento não cria uma segunda especificação para áudio, UI, credenciais, transcrição, terminal ou empacotamento.
- O repositório é único. Não há ordem `backend` antes de `frontend`, nem coordenação entre repositórios ou contratos HTTP como no projeto de referência.
- O agente principal coordena o trabalho: pesquisa o repositório e a documentação, localiza símbolos e consumidores, resolve ambiguidades com fatos observáveis, prepara o plano, delega o escopo e reúne evidências.
- A delegação só ocorre depois de um plano preciso, com arquivos/símbolos, comportamento esperado, critérios verificáveis e comandos de validação. Informação ausente não deve ser preenchida por suposição.

## Papéis

### Agente principal

O principal:

1. lê `AGENTS.md` e a documentação arquitetural aplicável antes de planejar;
2. pesquisa o código, os testes e os consumidores afetados;
3. registra decisões, escopo, critérios de aceitação e riscos;
4. encaminha escrita ao `implementador`, validação ao `testador` e gate final ao `revisor`;
5. confere as respostas e repete o ciclo quando a evidência não cobrir o contrato;
6. na operação de release invocada explicitamente pelo usuário, realiza a consulta prévia obrigatória a `docs/PENDENCIAS.md`. Havendo pendências ativas para a release, encaminha a implementação ao `implementador` e a validação ao `testador`; após o `PASS`, o `implementador` remove o item concluído de `docs/PENDENCIAS.md` (deixando o documento limpo), o `revisor` audita o diff com `APROVADO`, e o principal fica estritamente autorizado a realizar o commit das pendências resolvidas na branch `main`. Em seguida, no fluxo de bump de versão, após o `testador` reportar `PASS` no gate e o `revisor` responder `APROVADO`, o principal / operador de release fica estritamente autorizado a realizar os commits de bump na branch `main`, push para `origin main` e criação/push da tag anotada `vX.Y.Z` conforme [`docs/RELEASE.md`](../RELEASE.md).

O principal não pode usar a delegação para ocultar uma ambiguidade, reduzir o escopo sem aprovação ou declarar sucesso sem diff e evidência correspondentes.

### `implementador`

É o único papel autorizado a escrever código ou documentação delegada. Executa exatamente o plano recebido e altera somente o escopo necessário, reutilizando padrões existentes. Antes de editar:

- lê `AGENTS.md` e `ARQUITETURA.md`;
- usa localização (incluindo LSP, quando disponível) para encontrar todas as referências de qualquer símbolo exportado que será alterado;
- define o corte completo: consumidores, testes e documentação aplicáveis também são atualizados quando o contrato muda.

Não executa testes, builds, linters ou formatadores nesta etapa. Não cria commit, push, branch ou PR. Não cria segredo, stub, mock falso, no-op ou `TODO` para preencher implementação; não mantém caminho obsoleto ou alias não solicitado. Se faltar informação indispensável, relata o bloqueio em vez de inventar comportamento.

Resposta obrigatória do `implementador`:

1. arquivos alterados e o que mudou em cada um;
2. decisões técnicas ou riscos relevantes;
3. validações que o `testador` deve executar;
4. bloqueios ou trabalho inalcançável, se houver.

### `testador`

É responsável exclusivamente por executar os comandos de validação solicitados. Segue o gate de [`smoke-tests.md`](../agents/smoke-tests.md), usa fakes nos testes quando o contrato permitir e não depende de rede, microfone, Secret Service ou X11 reais para a suíte determinística. Não edita, cria, remove, reverte ou formata arquivos; não implementa correções; não instala dependências nem configura ambiente sem instrução explícita; não cria commit, push, branch ou PR.

A resposta do `testador` contém somente um dos blocos:

```text
PASS
Comando: <comando executado>
Resumo: <quantidade de testes e tempo, quando disponíveis>
```

ou:

```text
FAIL
Comando: <comando executado ou tentado>
Traceback filtrado:
<trecho mínimo e específico da falha ou do bloqueio>
```

Em falha, preserva apenas o teste, arquivo, linha e traceback/assertion diff indispensáveis. Não acrescenta diagnóstico, proposta de correção ou contexto editorial.

### `revisor`

É o gate final, somente leitura. Compara pedido, plano, diff, arquivos afetados, testes e documentação. Confirma que consumidores de símbolos exportados foram localizados, que alterações de contrato atualizaram testes/documentação aplicáveis e que a evidência do `testador` cobre o comportamento alterado. Também verifica segurança de segredos, concorrência, compatibilidade e aderência aos contratos de `AGENTS.md`.

Não executa testes, builds, linters ou formatadores; não edita arquivos; não cria commit, push, branch ou PR. Não aprova stub, mock falso, no-op, `TODO`, implementação incompleta ou caminho obsoleto não solicitado. Cada achado deve indicar arquivo e linha, severidade, risco concreto e correção mínima.

Resposta obrigatória do `revisor`:

Sem problemas:

```text
APROVADO
Evidência revisada: <diff/arquivos e resultados de validação>
```

Com problemas:

```text
REPROVADO
<arquivo>:<linha>: <severidade>: <problema>. Risco: <impacto>. Correção: <ação mínima>.
```

Os achados são listados por severidade; depois deles, somente validações ausentes, se houver.

## Ciclo de entrega

O fluxo é sequencial e pode repetir-se:

```text
principal pesquisa/planeja/orquestra
    └─> implementador altera o escopo
          └─> testador executa o gate
                └─> revisor audita a evidência
                      ├─ APROVADO → entrega
                      └─ REPROVADO/FAIL → principal ajusta o plano
                                          └─ implementador → testador → revisor
```

Antes de exportar ou remover qualquer símbolo, o implementador deve localizar referências. Depois da edição, nenhum consumidor, teste ou documento aplicável pode permanecer no contrato antigo. A entrega só passa quando o revisor tem acesso ao plano, diff/arquivos e resultados do `testador`; sem evidência, o resultado é bloqueio, não aprovação.

Nenhum papel cria commit, push, branch ou PR, exceto pela regra estrita de release: quando a automação de release do FalaFácil for invocada explicitamente pelo usuário (via skill ou gatilhos autorizados), antes do bump de versão deve ser realizada a consulta obrigatória a `docs/PENDENCIAS.md`; havendo pendências ativas para a release, elas são implementadas pelo `implementador` com testes, validadas pelo `testador` (`PASS`), o item resolvido é removido de `docs/PENDENCIAS.md` pelo `implementador` (deixando o documento limpo quando não restarem pendências), o diff é auditado pelo `revisor` (`APROVADO`), e o agente principal / operador de release fica estritamente autorizado a realizar o commit das pendências resolvidas na branch `main`. Em seguida, para o bump de versão, após a execução completa dos gates pelo `testador` (`PASS`) e a aprovação formal do `revisor` (`APROVADO`), o agente principal / operador de release fica estritamente autorizado a realizar os commits de bump na branch `main`, push para `origin main` e criação/push da tag anotada `vX.Y.Z`. Nenhum papel delegado (`implementador`, `testador`, `revisor`) pode realizar commits, pushes, tags ou mutações de branch/PR; nenhuma outra branch ou PR pode ser criada ou mutacionada. Nenhum papel grava ou expõe segredo, adiciona stub, mock falso, no-op ou `TODO`, nem introduz capacidades não confirmadas no código e no `AGENTS.md`.
