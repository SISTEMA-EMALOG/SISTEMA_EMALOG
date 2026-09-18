# Testes da Fase 5: chatbot de regras

Esta pasta não entra no zip de deploy. A trava de segurança é a de
`testes/_ambiente.py`.

O desenho da raiz está em `atendimento_conversas/FASE5_CHATBOT.md`. Esta pasta
acompanha a construção, etapa por etapa.

## Etapa 5.1 — `esquema.py`

Prova que os modelos do chatbot existem e se comportam igual nos dois bancos.

### O que está sendo provado

- **As seis tabelas novas nascem do `db.create_all()`**: `bot_sessoes`,
  `bot_campanhas`, `bot_contatos`, `bot_reservas_frete`,
  `bot_interesses_regiao` e `bot_optouts`.

- **Uma sessão de bot em andamento por telefone.** É um índice parcial único,
  não uma checagem em Python. Dois webhooks chegando no mesmo instante abririam
  duas máquinas de estado para o mesmo número, e o motorista receberia a
  conversa em dobro. O teste tenta abrir a segunda e exige que o banco recuse.

- **Sessão encerrada não tranca o telefone.** O outro lado do mesmo índice: se
  trancasse, o motorista nunca mais poderia ser abordado.

- **`contexto` e `trilha` voltam do banco como `dict` e `list`.** É aqui que a
  página de fretes guardada sobrevive. O motorista responde "2" pensando na
  lista que recebeu; se a busca rodasse de novo, o "2" viraria outro frete.

- **Um frete aceita as três posições de reserva**, o relógio nasce com 180
  minutos úteis e nada consumido, e a oferta criada pelo bot já nasce com
  `preco_fixo = True`.

- **A migração num banco que já existia** — a seção 7, e a que mais importa.
  Nas seções anteriores as colunas vieram do `create_all` num banco vazio, que
  não é o caso da produção. A seção 7 derruba `drivers.bot_ultima_oferta_em`,
  `drivers.bot_ofertas_ignoradas`, `drivers.bot_pausado` e
  `driver_bids.preco_fixo`, chama `run_migrations` como o boot chama, e confere
  que as quatro voltaram. Depois roda a migração outra vez, porque ela roda em
  todo boot e não pode falhar na segunda. Por fim confere que a linha antiga
  ficou com a coluna nova em `NULL` — o código trata `NULL` como `False` e como
  zero.

No SQLite a seção 7 exige versão 3.35 ou maior, que é quando o `DROP COLUMN`
apareceu. Em versão anterior ela se anuncia como pulada, e a prova do `ALTER`
fica por conta do PostgreSQL.

### Como rodar

SQLite:

```
del instance\emalog.db
set PYTHONIOENCODING=utf-8
python testes\fase5\esquema.py
```

O `PYTHONIOENCODING` não é frescura: o console do Windows é cp1252 e a
aplicação imprime emoji no boot. Sem ele, o teste morre antes de começar, com
`UnicodeEncodeError`.

PostgreSQL local, com banco vazio:

```
set DATABASE_URL=postgresql://postgres:SENHA@localhost:5432/emalog_teste?sslmode=disable
set PYTHONIOENCODING=utf-8
python testes\fase5\esquema.py
```

O `?sslmode=disable` é obrigatório. Sem ele a aplicação exige SSL, a conexão
falha e ela cai para SQLite em silêncio — o teste passaria, no banco errado.
O `_ambiente.py` recusa a URL sem ele justamente por isso.

O script pode rodar várias vezes no mesmo banco: os nomes e telefones levam um
sufixo aleatório por execução.

### Resultado em 18/09/2026

29 verificações, todas passando em SQLite 3.x e PostgreSQL 17.6.

## Etapa 5.2 — `maquina_5_2.py`

Prova a máquina de estados com os estados construídos até aqui: [1] entrada,
[2] identificacao, [4] viagem_ativa, [11] encerramento e [12] handoff. Sem
frete, sem geolocalização, sem coleta EMA — o que o contrato levaria a um estado
ainda não construído cai em handoff, com motivo claro.

### O que está sendo provado

- **Número novo entra, responde e o bot inicia a coleta mínima.** Sem cadastro,
  a máquina cria o registro mínimo (nome provisório) e segue para `coleta_minima`
  (estado 3), pedindo o nome. A trilha guarda `entrada → identificacao →
  coleta_minima`. O fluxo completo da coleta é provado em `coleta_minima_5_3.py`.
- **Uma BotSession ativa por telefone.** `criar_sessao_bot` devolve a ativa
  existente em vez de abrir uma segunda; e, depois do handoff, uma nova pode
  abrir (o índice parcial único da 5.1 continua valendo).
- **`processar_mensagem_bot` devolve `None`** quando não há sessão ativa — o
  roteador segue para o ramo "nada" — e não cria sessão sozinho.
- **Saídas globais (seção 6):** "atendente" → handoff; "SAIR" → opt-out gravado
  em `OptOut` e status `optout`; foto/áudio/vídeo → handoff.
- **Estado [4]:** motorista completo e em viagem recebe a mensagem de "boa
  viagem" e a sessão encerra sem ofertar; motorista completo e livre seguiria
  para `geolocalizacao` (estado 5, ainda não existe) e vira handoff.
- **A máquina não envia WhatsApp:** devolve os textos em `replies` e persiste
  tudo (estado, tentativas, trilha, status, motivo_fim).

### Como rodar

Igual à 5.1 (mesma trava de `_ambiente.py`). SQLite:

```
del instance\emalog.db
set PYTHONIOENCODING=utf-8
python testes\fase5\maquina_5_2.py
```

PostgreSQL local, com banco vazio:

```
set DATABASE_URL=postgresql://postgres:SENHA@localhost:5432/emalog_teste?sslmode=disable
set PYTHONIOENCODING=utf-8
python testes\fase5\maquina_5_2.py
```

### Resultado em 18/09/2026

34 verificações, todas passando em SQLite 3.x e PostgreSQL 17.6.

## Etapa 5.2 (costura) — `roteamento_5_2.py`

Prova o **passo 5** ligado em `_process_single_message`: a mensagem de um número
com sessão ativa é entregue à máquina; a resposta sai por `send_text` e vira
`WhatsAppMessage(direction='outbound', source='bot')` — sem isso some da Central
(defeito 3.1); e no handoff a conversa é escalada para a fila (`handling_mode=
'manual'`). `send_text` é trocado por um falso, sem tocar a Evolution. Cobre:
número novo pede o nome sem escalar; ao completar a coleta, escala e transfere;
opt-out envia a confirmação sem escalar; motorista em viagem recebe "boa viagem";
telefone sem sessão cai no ramo "nada" (caminhos antigos intactos).

**Resultado em 18/09/2026:** 15 verificações, SQLite e PostgreSQL 17.6.

## Etapa 5.3 — `coleta_minima_5_3.py`

Prova a coleta mínima **nativa**, por seleção de opções, **sem o agente EMA**:
nome + veículo (vocabulário canônico) + cidade/UF, só o que falta, e handoff para
o atendente finalizar. Cobre: fluxo completo sem cadastro; coleta só do campo que
falta num motorista incompleto; motorista que já tem o mínimo indo direto ao
handoff; opção inválida que re-pergunta e vira handoff na 3ª; veículo aceito pelo
nome; cidade/UF só com a sigla; e "SAIR" no meio da coleta virando opt-out.

**Resultado em 18/09/2026:** SQLite e PostgreSQL 17.6, tudo passando.

## O que ainda não é testado

As etapas 5.4 em diante não existem. Não há nada aqui sobre a geolocalização
(estado 5), a lista de fretes nem o relógio útil da reserva. Em particular:

- **O relógio de horas úteis não tem teste.** Os campos existem
  (`minutos_limite`, `minutos_consumidos`, `contando_desde`), mas a conta que
  os usa é da etapa 5.6. O caso que vai quebrar é a reserva aberta às 17h de
  sexta, que precisa vencer às 10h de segunda.
- **A trava do agente de negociação não tem teste.** O campo `preco_fixo`
  existe e é gravado; quem o lê é a etapa 5.6.
