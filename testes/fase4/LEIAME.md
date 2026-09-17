# Testes da Fase 4: relatórios

Esta pasta não entra no zip de deploy. A trava de segurança é a de
`testes/_ambiente.py`.

## O que `relatorios.py` prova

Monta um cenário com horários fixos e confere cada número contra o valor
calculado à mão. Os casos foram escolhidos por serem onde relatório costuma
errar:

- **Fuso.** Uma mensagem às 23h30 de São Paulo está gravada como 02h30 do dia
  seguinte em UTC, e precisa contar no dia local.
- **Espera.** Duas mensagens seguidas do contato são uma espera só, medida a
  partir da primeira.
- **Bot.** A resposta do bot encerra a espera sem entrar no tempo de ninguém.
- **Automação.** Uma oferta de frete com autor não conta como resposta, e a
  espera continua aberta.
- **Borda do período.** Uma resposta que chega depois do fim do período ainda
  mede uma espera que começou dentro dele; uma espera que começou antes não
  entra.
- Período inválido, acesso só de administrador, CSV com BOM e ponto e vírgula,
  e neutralização de nome de usuário que começa com `=`.
- Nenhuma função de data de dialeto no código, conferido pela árvore sintática
  do módulo.

Teste de mutação feito durante o desenvolvimento, e não incluído: com o
agrupamento forçado para UTC, o teste reprovou em 9 verificações.

## Como rodar

```
del instance\emalog.db
python testes\fase4\relatorios.py
```

PostgreSQL local, com banco vazio:

```
set DATABASE_URL=postgresql://postgres:SENHA@localhost:5432/emalog_teste?sslmode=disable
python testes\fase4\relatorios.py
```

## Teste manual

Entre como administrador, abra Conversas e clique em Relatórios.

1. Os atalhos Hoje, 7 dias e 30 dias trocam o período e recarregam.
2. Os botões de CSV baixam arquivos que abrem no Excel com acento correto e
   colunas separadas.
3. Entre como operador: o atalho não aparece, e `/conversas/relatorios`
   responde acesso negado.
4. Confira um número conhecido: atenda uma conversa de teste, anote quanto
   tempo levou para responder e veja se o tempo mediano do atendente bate.

## Limites dos números

- Assumidas, recebidas por transferência e resolvidas vêm do histórico da
  fila, que só existe a partir do deploy da Fase 2. Períodos anteriores
  aparecem zerados nessas colunas.
- Respostas do bot anteriores ao deploy da Fase 3 não estavam vinculadas à
  conversa e não aparecem.
- Conversas agrupadas pelo script de histórico contam como abertas no dia em
  que o script rodou.
- O tempo de resposta é em horas corridas, sem descontar noite nem fim de
  semana. A mediana é o número mais confiável para comparar atendentes.
