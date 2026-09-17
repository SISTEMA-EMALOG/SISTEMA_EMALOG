# Testes da Fase 2: fila compartilhada entre atendentes

A Fase 2 decide toda disputa entre atendentes no banco, por UPDATE
condicional. Estes testes provam isso com concorrência real, e não com um
atendente de cada vez.

Esta pasta não entra no zip de deploy.

## Trava de segurança

Os testes criam usuários, motoristas e conversas, e trocam o envio de
WhatsApp por um falso. Por isso eles se recusam a rodar:

- com `DATABASE_URL` apontando para qualquer host que não seja local;
- com PostgreSQL local sem `?sslmode=disable` na URL, porque a aplicação
  exigiria SSL, cairia para SQLite em silêncio e testaria o banco errado;
- sem `DATABASE_URL`, se `instance/emalog.db` já existir, a menos que você
  autorize com `TESTE_PODE_SUJAR_SQLITE=1`.

Rode sempre a partir da raiz do projeto.

## O que cada arquivo prova

**`concorrencia.py`**: cada atendente é uma thread com conexão própria, e
todas disparam juntas numa barreira. Cada cenário roda 30 vezes.

- Cinco atendentes assumindo a mesma conversa: sempre exatamente um vence.
- Transferência contra assumir, e dono contra admin transferindo juntos.
- Liberar para a fila contra outro atendente respondendo.
- Resolver no mesmo instante em que chega mensagem nova: a mensagem sempre
  termina numa conversa ativa.
- EMA escalando para humano contra atendente assumindo: zero deadlocks.
- Só no PostgreSQL, dois controles: com a ordem de lock invertida o
  deadlock acontece, e a retentativa da fila o registra. É isso que dá valor
  ao zero do cenário anterior.

**`http_tempo_real.py`**: cada atendente é um navegador simulado, com sessão
e CSRF próprios.

- Cinco cliques simultâneos em Assumir: uma resposta 200 e quatro 409 que
  dizem quem venceu.
- Dois atendentes respondendo juntos uma conversa livre: só uma mensagem
  sai para o motorista.
- Resolver com a tela desatualizada é recusado quando chegou mensagem nova.
- Só no PostgreSQL: durante a chamada HTTP ao provedor, nenhuma conexão
  fica com transação aberta e nenhuma linha fica travada.
- O socket do segundo atendente recebe o evento na hora.

**`bot_ema.py`**: pelo webhook real da Evolution.

- Controle: com a conversa livre, o bot responde.
- Com a conversa assumida, o bot não chama a IA nem envia nada, mesmo se o
  cadastro do motorista estiver desatualizado em modo automático.
- Devolvida ao EMA, o bot volta a responder.

## Como rodar

SQLite:

```
del instance\emalog.db
python testes\fase2\concorrencia.py
python testes\fase2\http_tempo_real.py
python testes\fase2\bot_ema.py
```

PostgreSQL local, um banco vazio por execução:

```
set DATABASE_URL=postgresql://postgres:SENHA@localhost:5432/emalog_teste?sslmode=disable
python testes\fase2\concorrencia.py
```

Cada script termina com `RESULTADO: TUDO OK` ou com a lista de falhas, e
sai com código diferente de zero se algo falhar.

## Teste manual com dois atendentes

O teste automatizado cobre a disputa. Este roteiro confirma a experiência
de quem usa, com dois navegadores de verdade.

**Preparo.** Crie dois usuários com papel `operador`, por exemplo
`atendente1` e `atendente2`. Abra dois navegadores diferentes, ou um normal e
outro anônimo, porque a sessão de login é por navegador. Faça login com um
usuário em cada e abra Conversas nos dois. Mande uma mensagem de WhatsApp de
um celular para o número da operação.

1. **A conversa aparece nas duas telas sem recarregar**, na aba Fila, com
   "Livre na fila" ou "EMA atendendo".
2. **Disputa.** Abra a mesma conversa nas duas telas e clique em Assumir nas
   duas o mais junto possível. Uma tela mostra "Conversa assumida". A outra
   mostra que o colega já está com a conversa.
3. **Tempo real.** Na tela de quem perdeu, sem recarregar: a faixa amarela
   "Em atendimento com atendente1" aparece, o campo de mensagem trava e a
   conversa sai da aba Fila. Na tela de quem ganhou, ela vai para Minhas.
4. **Resposta simultânea.** Com outra conversa livre, digite um texto nas
   duas telas e envie nas duas ao mesmo tempo. **O celular recebe uma
   mensagem só.** Na tela de quem perdeu, o texto continua no campo, com o
   aviso de que não foi enviado.
5. **Bot calado.** Com a conversa assumida, responda pelo celular. A mensagem
   aparece na tela, e o EMA não responde.
6. **Transferir.** Quem está com a conversa clica em Transferir e escolhe o
   colega. Na outra tela, sem recarregar, a conversa passa a ser dela e o
   campo destrava.
7. **Resolver com tela velha.** Numa conversa sua, mande uma mensagem pelo
   celular e, antes de a tela atualizar, clique em Resolver. A resolução é
   recusada com "Chegou mensagem nova nesta conversa".
8. **Devolver ao EMA.** Clique em Devolver ao EMA e responda pelo celular.
   Agora o bot responde.
9. **Queda de conexão.** Numa das telas, desligue a rede por alguns segundos,
   assuma uma conversa pela outra tela e religue. A primeira tela se atualiza
   sozinha ao reconectar.

Se em algum passo as duas telas acharem que são donas da mesma conversa, ou o
celular receber duas respostas, pare e registre a hora exata. O histórico
da conversa, no painel da direita, mostra quem fez o quê.
