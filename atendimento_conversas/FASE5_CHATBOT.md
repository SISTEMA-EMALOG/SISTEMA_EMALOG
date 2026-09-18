# Fase 5 — Chatbot de regras: raiz "Oferta de frete por região"

Desenho da primeira raiz de resposta do chatbot, decidido em 18/09/2026.
Esta é a Fase 5 do `PLANO_DE_FASES.md`: máquina de estados, sem IA, com
handoff automático para humano quando sai do previsto.

Ainda não há código. Este documento é o contrato do que será construído.

---

## 1. O que esta raiz faz

O operador sobe uma lista **só com números de telefone**. O bot fala com cada
número, descobre se é motorista cadastrado, completa o cadastro quando falta
algo, pergunta onde ele está, oferta os fretes que saem daquela região,
registra a escolha e entrega a conversa a um atendente humano.

É a única raiz que **nós iniciamos**. Todas as outras conversas do sistema hoje
começam pelo motorista ou por uma oferta de frete específica.

### Três origens

| Origem | Quem entra | Onde começa |
|---|---|---|
| `campanha` | número da lista subida, frio | estado [1] `entrada` |
| `reoferta` | motorista já cadastrado, **sem viagem ativa**, que demonstrou interesse, quando entra carga compatível | estado [4] `viagem_ativa` |
| `pedido` | motorista que manda *CARGAS* a qualquer momento | estado [4] `viagem_ativa` |

As duas últimas pulam apresentação e identificação — a gente já sabe quem é.
Mas **nunca pulam a geolocalização**: motorista muda de lugar, e a última UF
conhecida não serve para ofertar carga.

A reoferta é o que mantém a raiz viva depois da campanha. Regras em `[R]`,
na seção 5.

```
        LISTA DE TELEFONES (só o número)
                    |
              [1] entrada  -- sem resposta em 24h --> (encerra, contato fica "não respondeu")
                    |  respondeu
                    v
          [2] identificacao --------------+--------------+
                    | completo            | incompleto   | sem cadastro
                    |                     v              v
                    |         [3] coleta_minima  (nome + veículo + cidade/UF,
                    |                     |         NATIVO, sem agente EMA)
                    |                     v
                    |               [12] handoff  (o atendente finaliza o cadastro)
                    v
          [4] viagem_ativa -- em viagem --> [11] encerramento (volta depois)
                    | livre
                    v
          [5] geolocalizacao -- não informou 2x --> [12] handoff
                    | UF/cidade
                    v
          [6] busca_fretes -- nenhum --> [A] sem_regiao --> [B] atualizar_regioes
                    | 1 ou mais
                    v
          [7] lista_fretes  <-->  (mais opções / próxima página)
                    | escolheu        \__ recusou tudo --> [C] motivo_recusa --> [B]
                    v
          [8] escolha -- não é esse --> volta para [7]
                    |
                    v
          [9] confirmacao -- não --> volta para [7]
                    | sim
                    v
          [10] reserva  (DriverBid + posição: titular, reserva 2, reserva 3)
                    |
                    v
          [11] encerramento  (bot se cala)
                    |
                    v
          [12] handoff + checklist  -->  fila da Central de Atendimento
```

---

## 2. Onde o código vai morar

Módulo novo: **`chatbot_regras/`**.

A raiz atravessa três módulos que já existem — cadastro
(`prospeccao_captacao_motorista`), oferta (`oferta_frete_motorista`) e fila
(`atendimento_conversas`) — e não é lógica de nenhum dos três. Pela regra 2 do
`CLAUDE.md` da Central, "este módulo só contém lógica de atendimento", então a
máquina de estados não cabe lá dentro.

```
chatbot_regras/
    chatbot.py              rotas: subir lista, acompanhar campanha, painel
    utils/
        maquina.py          a máquina de estados (o coração)
        estados.py          um handler por estado
        mensagens.py        os textos enviados ao motorista
        campanha.py         subir a lista, validar, disparar com folga
        reserva.py          titular e reservas do frete
```

Modelos novos vão para `infraestrutura_critica/models.py`, como manda a regra 1.

---

## 3. Onde entra no roteamento de hoje

O `_process_single_message` de `prospeccao_captacao_motorista/ema_agent.py`
decide hoje, nesta ordem:

1. humano assumiu a conversa — o bot se cala
2. sessão EMA **e** oferta ativa ao mesmo tempo — escala para humano
3. sessão EMA — agente EMA
4. oferta ativa (`DriverBid`) — agente de negociação
5. nada

A raiz nova entra **como passo 5**, antes do "nada":

```
5. sessão de bot ativa  ->  máquina de estados     <-- construído (etapa 5.2)
6. nada
```

O bot é **100% regras, guiado por seleção de opções — nunca aciona o agente
EMA**. O EMA não consome API neste sistema; qualquer coleta que ele faria é
resolvida nativamente pela máquina (estado [3] `coleta_minima`). Entrar depois do
EMA e da oferta é só uma questão de precedência: se já existe uma `EmaSession` ou
uma `DriverBid` ativa para aquele número, quem responde é o caminho de hoje; a
máquina do bot só age quando não há nenhum dos dois. Nenhum dos quatro caminhos
antigos muda de comportamento — o passo 5 é aditivo.

---

## 4. Os estados

Regras válidas para todos:

- Cada estado tem no máximo **2 tentativas**. Na terceira resposta que a
  máquina não entende, vai para `handoff`.
- Toda mensagem enviada pelo bot vira `WhatsAppMessage` com
  `direction='outbound'` e `source='bot'`. Sem isso ela não aparece na Central
  nem nos relatórios da Fase 4 — é o defeito 3.1 do `PENDENCIAS.md`, que não
  pode ser repetido aqui.
- Silêncio do motorista: lembrete em 1h, encerra em 24h.
- O estado atual e o contexto ficam na tabela `BotSession` (seção 8), nunca em
  memória do processo.

### [1] entrada

| | |
|---|---|
| **Quem fala** | o bot, por disparo da campanha |
| **Espera** | qualquer resposta |
| **Sai para** | `identificacao` (respondeu), fim (silêncio em 24h), `optout` ("pare", "não quero", "sair") |

> Oi! Aqui é a EMA, da Emalog Transportes. 🚚
> A gente trabalha com cargas em todo o Brasil e está montando a lista de
> motoristas parceiros.
> Você trabalha com transporte de carga hoje?
> Se não quiser receber mensagens, responde SAIR que eu não te chamo mais.
>
> EMA | EMALOG

O "SAIR" na primeira mensagem não é gentileza: é lista fria, e sem saída
explícita o número da empresa vira denúncia e bloqueio no WhatsApp.

### [2] identificacao

Sem mensagem. A máquina resolve o telefone contra o cadastro.

O telefone é normalizado **só** por `normalize_contact_key`
(`atendimento_conversas/utils/phone.py`). A outra normalização do sistema,
`normalize_phone` de `evolution_api.py`, erra todo número de DDD 55 — é o
defeito 4.3 do `PENDENCIAS.md`. A raiz nova não usa essa.

| Resultado | Vai para |
|---|---|
| nenhum `Driver` com esse telefone | cria cadastro mínimo (nome provisório + telefone) e vai para `coleta_minima` |
| `Driver` existe, falta campo obrigatório | `coleta_minima` (coleta só o mínimo que falta) |
| `Driver` existe e está completo | `viagem_ativa` |

**Cadastro completo** = todos os campos `required` de `FIELD_DEFS`
(`prospeccao_captacao_motorista/utils/ema_agent.py`), respeitando o
`carreta_only`, mais CNH dentro da validade. É o critério rigoroso: na
prática, quase todo número de lista fria vai passar pelo EMA antes de ver um
frete. Foi decidido assim de propósito — frete ofertado a cadastro incompleto
volta como problema para o atendente.

### [3] coleta_minima

Coleta **nativa**, por seleção de opções, do mínimo para *iniciar* o cadastro —
**sem nenhum envolvimento do agente EMA** (decidido em 18/09/2026). O EMA não
consome API neste sistema, então tudo que ele faria a máquina resolve em código.
O atendente humano finaliza o cadastro depois do handoff.

O bot pergunta **só o que falta**, um campo por mensagem, e grava direto no
`Driver`:

1. **Nome** (texto livre) — troca o nome provisório.
2. **Veículo** — por opção numerada, no vocabulário canônico do sistema
   (`TRUCK_TYPES`: `3/4`, `vlc`, `toco`, `truck`, `bitruck`, `carreta`). É o
   mesmo valor que o cadastro, os filtros e o matching leem.
3. **Cidade/UF** de origem (texto: "Campinas SP" ou só "SP"). Só a UF é
   obrigatória; a cidade refina.

> Boa! Pra começar seu cadastro, como é seu nome completo?
>
> EMA | EMALOG

Cada pergunta tem 2 tentativas; opção inválida re-pergunta e, na 3ª sem
entender, `handoff`. Uma resposta válida zera o contador da pergunta seguinte.

Quando o mínimo está completo (ou já estava, para um motorista incompleto por
outros campos), o bot avisa e faz `handoff` com motivo `coleta_minima_ok`:

> Anotado, obrigado! ✅
> Já estou passando você para um atendente da equipe, que finaliza seu cadastro
> e te mostra as cargas.
> É só responder nesta mesma conversa.
>
> EMA | EMALOG

O cadastro nasce `validated=False`, para um operador conferir. O bot **não**
oferta frete a esses motoristas: com o cadastro ainda incompleto, quem conduz
daqui em diante é o atendente (a oferta de frete exige cadastro completo).

### [4] viagem_ativa

Sem mensagem, se não houver viagem. O motorista está ocupado quando:

- tem `Freight` com `assigned_driver_id` igual a ele e status `aceito` ou
  `em_transito`; ou
- `Driver.availability_status == 'em_frete'`; ou
- tem `DriverBid` em negociação aberta (`sent`, `responded`, `no_price`,
  `interested`).

Se ocupado, avisa e encerra sem ofertar:

> Vi aqui que você está com um frete nosso em andamento. Boa viagem! 🚚
> Quando entregar, me manda um "livre" que eu te mostro as próximas cargas.
>
> EMA | EMALOG

Ofertar frete a quem já está carregado é o jeito mais rápido de o bot perder a
confiança do motorista.

### [5] geolocalizacao

> Show, cadastro certinho! ✅
> Pra te mostrar carga que faz sentido: **onde você está agora?**
> Pode mandar a localização pelo clipe 📎 ou só escrever a cidade e o estado
> (ex.: Campinas SP).
>
> EMA | EMALOG

Aceita três formas:

1. **Pino de localização** do WhatsApp (`locationMessage`) — o webhook **não
   trata esse tipo hoje**; ver seção 10.
2. **Texto**: "Campinas SP", "estou em Ribeirão Preto", "SP".
3. **Confirmação do endereço do cadastro**: se o motorista tiver cidade/UF
   cadastrados, a segunda tentativa já pergunta pronto — "Você está em
   Campinas/SP?" — e um "sim" resolve.

Só a **UF** é obrigatória para seguir. A cidade refina a ordem da lista.
Duas tentativas sem entender — `handoff`.

### [6] busca_fretes

Sem mensagem. Consulta nova, a construir em `matching_motorista/matching.py`,
ao lado de `find_eligible_drivers`, que hoje só faz o caminho inverso
(frete para motoristas).

Um frete entra na lista quando:

- `status == 'ofertado'` e `assigned_driver_id` vazio;
- **tem `driver_cost` preenchido** — sem valor definido na criação, o frete não
  pode ser ofertado pelo bot, porque o valor é mostrado e é fechado (seção 7);
- `origin_state` igual à UF do motorista;
- tipo de veículo compatível (`freight.quote.vehicle_type` contra
  `driver.truck_type`), ou o frete não exige tipo;
- ainda tem vaga na reserva (menos de 3 posições ativas — seção 7);
- esse motorista ainda não tem `DriverBid` nesse frete.

Ordem: mesma cidade de origem primeiro, depois quem já rodou essa rota
(critério que `find_eligible_drivers` já usa), depois coleta mais próxima.

Nenhum resultado — `sem_regiao` **[A]**.

### [7] lista_fretes

Três por página, **com o valor à vista**. O valor é o `driver_cost` do frete,
definido na criação, e é fechado: não se negocia (seção 7).

Formatar com `_brl`, de `oferta_frete_motorista/utils/driver_bid_agent.py`.
O `f'{valor:,.2f}'` usado em `generate_freight_message` manda "R$ 2,500.00"
para quem lê em português — é o defeito 4.5 do `PENDENCIAS.md`, e agora que o
valor vira o centro da mensagem ele precisa estar certo.

> Achei 5 cargas saindo de SP. Você topa ir para:
>
> *1* — Campinas/SP para **Curitiba/PR**
>        Carga geral · 12 t · coleta 22/09
>        💰 *R$ 4.200,00*
> *2* — Jundiaí/SP para **Belo Horizonte/MG**
>        Bebidas · 8 t · coleta 23/09
>        💰 *R$ 2.850,00*
> *3* — Santos/SP para **Goiânia/GO**
>        Carga geral · 15 t · coleta 24/09
>        💰 *R$ 6.500,00*
>
> Esses são os valores fechados da carga.
> Responde o *número* que te interessa.
> *4* — ver mais opções   ·   *0* — nenhuma dessas
>
> EMA | EMALOG

| Resposta | Vai para |
|---|---|
| número de um frete da página | `escolha` |
| "mais", "4", "outras" | próxima página do mesmo estado |
| "0", "nenhuma", "não" | `motivo_recusa` **[C]** |
| acabaram as páginas | `motivo_recusa` **[C]** |

Os IDs mostrados ficam gravados no contexto da sessão. O motorista responde
"2" pensando na página que recebeu; se a busca rodar de novo, o "2" vira outro
frete. Este é o erro clássico de lista paginada em WhatsApp, e é por isso que a
página fica guardada, não recalculada.

### [8] escolha

> Fechou, então é essa:
>
> 📍 *Campinas/SP para Curitiba/PR*
> 📦 Carga geral · 12 t
> 🗓 Coleta prevista 22/09
> 💰 *R$ 4.200,00* — valor fechado
>
> Confirma que quer essa carga por esse valor?
> (responde *SIM* ou *NÃO*)
>
> EMA | EMALOG

"Não" volta para `lista_fretes`, na mesma página.

### [9] confirmacao

Sim — `reserva`. Não — volta para `lista_fretes`.

Os dois passos separados são de propósito: escolher e confirmar numa mensagem
só faz o motorista reservar frete errado por dedo torto no número. E com valor
fechado o segundo passo é o aceite do preço, não só da rota.

Se o motorista tentar negociar em vez de responder sim ou não, o bot avisa
**uma vez** que o valor é fechado e repete a pergunta. Se ele insistir, vai
para `handoff`: quem diz não a um motorista é gente, não robô.

### [10] reserva

Sem mensagem própria. Em uma transação, com `SELECT ... FOR UPDATE` na linha do
frete (mesmo padrão de `atendimento_conversas/utils/fila.py`):

1. cria `DriverBid` com `kanban_stage = 'interested'`,
   `driver_price = freight.driver_cost` e `preco_fixo = True`, para o card
   aparecer na Central de contratação já com o valor aceito;
2. cria `ReservaFrete` na primeira posição livre (1, 2 ou 3), com o relógio
   de 3 horas úteis já correndo (seção 7);
3. se alguém pegou a última vaga no meio do caminho, volta ao `lista_fretes`
   com a carga já retirada da lista.

**Cuidado com o agente de negociação.** O passo 4 do roteamento (seção 3) pega
qualquer `DriverBid` ativa e chama `process_bid_response`, que existe para
pechinchar preço. Numa carga de valor fechado, é exatamente o que não pode
acontecer. Duas barreiras, nesta ordem:

1. o `encerramento` marca a conversa como `manual`, e o passo 1 do roteamento
   (`humano_no_controle`) cala o bot antes de a mensagem chegar ao passo 4 — é
   a barreira que já funciona hoje;
2. o campo `preco_fixo` na `DriverBid` impede `process_bid_response` de rodar,
   para o caso de alguém devolver a conversa ao bot mais tarde.

A primeira sozinha resolve o fluxo normal. A segunda é para o dia em que
alguém clicar em "devolver ao bot" sem saber o que isso desencadeia.

### [11] encerramento

A `BotSession` vira `status='handoff'` e o bot **para de responder** essa
conversa. Daqui em diante, quem fala é gente.

Uma mensagem só, igual para todo mundo:

> Boa, anotei aqui! ✅
> Já estou passando para um da equipe, que fala com você em seguida sobre
> essa carga.
> É só responder nesta mesma conversa.
>
> EMA | EMALOG

**O bot não fala de reserva.** Decidido em 18/09/2026: posição, prazo e hora de
vencimento são assunto do atendente humano, não do robô. O bot registra a
reserva no banco e se cala.

É também o desenho mais coerente: a partir daqui a conversa é `manual`, e o
passo 1 do roteamento cala o bot nela. Qualquer mensagem do bot sobre a reserva
teria que furar essa regra — e mensagem de robô caindo no meio de uma conversa
que um atendente está conduzindo é exatamente o tipo de coisa que confunde a
Central.

A contrapartida é que **o motorista em posição de reserva não sabe que é
reserva até alguém contar**. Por isso avisar a posição é o primeiro item do
checklist do atendente, logo abaixo: motorista que segura o caminhão por uma
carga que nunca foi dele não volta a responder a EMA.

### [12] handoff e checklist

A conversa entra na fila da Central por `atendimento_conversas/utils/fila.py`,
com `handling_mode='manual'` e sem dono — qualquer atendente assume. Grava
`ConversationEvent` com `acao='bot_handoff'`.

No painel de contexto da conversa (`templates/conversas/_painel.html`, que já
existe), o atendente vê:

- **Motorista** — nome, telefone, cadastro (completo / o que falta), CNH até
- **Onde está** — cidade/UF informados e a hora
- **Carga escolhida** — número do frete, origem para destino, produto, peso,
  coleta, valor previsto ao motorista
- **Posição** — titular, ou reserva 2 / reserva 3, e **quanto falta para a
  reserva expirar**, em tempo útil, com a hora projetada (seção 7)
- **Valor** — o `driver_cost` do frete, com o aviso de que é fechado e já foi
  aceito pelo motorista
- **Falta fazer** — [ ] **avisar a posição e até quando a carga está
  reservada** (o bot não avisou) · [ ] verificação da seguradora ·
  [ ] confirmar coleta · [ ] contratar no Kanban
- **Por onde o bot passou** — a trilha de estados, com horário

---

## 5. Caminhos alternativos

### [A] sem_regiao — nenhuma carga na região

> No momento não tenho carga saindo de **SP**. 😕
> Duas coisas que posso fazer:
>
> *1* — te avisar assim que aparecer carga aí
> *2* — ver carga saindo de outro estado (me diz qual)
>
> EMA | EMALOG

Opção 1 grava `InteresseRegiao` e encerra a sessão em bom termo — quando entrar
frete com origem naquela UF, uma rotina dispara a raiz de novo, direto no
estado `lista_fretes`.

Opção 2 — `atualizar_regioes`.

### [R] reoferta — motorista conhecido, sem viagem

Decidido em 18/09/2026: **motorista sem viagem ativa pode continuar recebendo
frete, desde que tenha demonstrado interesse.** O corte não é o tempo desde a
última campanha; é estar livre e ter mostrado interesse alguma vez.

Demonstrou interesse quem, em qualquer conversa anterior: escolheu um frete,
pediu aviso de região (`InteresseRegiao`), ou respondeu informando onde estava.
Quem nunca respondeu não conta — esse é lista fria, e vale a regra de campanha
da seção 12.

A reoferta dispara quando entra frete compatível com a UF de interesse do
motorista. Antes de mandar, confere, nesta ordem:

1. não está em `OptOut`;
2. não tem viagem ativa, pelos mesmos critérios do estado [4];
3. não recebeu outra reoferta nas últimas **2 horas**;
4. não tem conversa aberta com atendente — a Central manda mais que o bot;
5. é horário comercial (seção 7). Carga não se oferta às 3h da manhã.

Depois de **3 reofertas seguidas ignoradas**, pausa esse motorista e marca para
um humano olhar. Pode ter trocado de número, parado de rodar ou só não querer
mais — e insistir é o caminho curto para virar denúncia.

**Por que 2 horas, e não 48.** Corrigido em 18/09/2026. Carga não espera: o
motorista que descarregou hoje de manhã quer ver o que tem agora, não
depois de amanhã. Com 48h, a reoferta perderia quase toda carga que aparece no
dia — e o motorista livre ficaria parado enquanto o frete ia para outro.

A contrapartida é que a trava de 3 ignoradas passa a valer em horas, não em
dias: quem não responder recebe três ofertas numa manhã e depois é pausado
sozinho. Isso é proteção, não defeito — mas quer dizer que a pausa vai
acontecer com frequência, e que a fila de "motorista pausado para revisão"
precisa de alguém olhando. Se ela encher e ninguém der conta, o número certo
não é aumentar a pausa: é voltar a espaçar a reoferta.

As 2 horas são o **piso** do intervalo, não uma promessa de mandar carga a cada
2 horas. Só sai mensagem quando entra frete compatível com a região dele.

A mensagem entra direto no assunto, porque ele já conhece a EMA:

> Oi, {nome}! Entrou carga saindo de **SP**. 🚚
> Quer ver? (responde *SIM*)
>
> EMA | EMALOG

"Sim" segue para `viagem_ativa` e daí para a geolocalização, como qualquer
outra entrada.

### [C] motivo_recusa — por que não quis nenhuma

Uma pergunta só, quando o motorista responde "0 — nenhuma dessas":

> Só pra eu te mandar coisa melhor da próxima vez: o que pesou?
>
> *1* — o valor   ·   *2* — o destino   ·   *3* — a data   ·   *4* — outro
>
> EMA | EMALOG

Qualquer resposta, inclusive nenhuma, segue para `atualizar_regioes` — a
pergunta não trava o fluxo. Grava em `BotContato.motivo_recusa` e na trilha da
sessão.

Vale a mensagem a mais porque o valor agora é fechado e não se negocia: sem
esta pergunta, ninguém fica sabendo se a tabela de preço está fora do mercado.
É o único sinal de preço que a operação vai receber do motorista.

### [B] atualizar_regioes

> Beleza. Para quais estados você topa puxar carga?
> Pode mandar vários, separados por vírgula (ex.: SP, MG, PR).
>
> EMA | EMALOG

Grava um `InteresseRegiao` por UF e roda `busca_fretes` de novo com todas elas.
Achou algo — `lista_fretes`. Não achou — volta a `sem_regiao`, agora sem a
opção 2, e encerra com o aviso registrado.

---

## 6. Saídas que valem em qualquer estado

| Gatilho | O que acontece |
|---|---|
| "atendente", "humano", "falar com alguém" | `handoff` na hora |
| insiste em negociar valor depois do aviso de que é fechado | `handoff` |
| 3ª resposta não entendida no mesmo estado | `handoff` |
| "sair", "pare", "não quero", "descadastrar" | `optout`: grava a recusa, confirma em uma linha, nunca mais dispara para esse número |
| áudio, foto ou vídeo fora de hora | `handoff` — a máquina de regras não interpreta mídia |
| atendente assume a conversa na Central | bot se cala (a checagem `humano_no_controle` do webhook já faz isso) |
| 24h de silêncio | encerra com motivo `inatividade` |
| xingamento / número errado | `handoff`, para uma pessoa decidir |

O opt-out precisa de tabela própria e ser checado **antes do disparo**, não só
durante a conversa.

---

## 7. Reserva do frete: titular e dois de reserva

Decisão de 18/09/2026. Cada frete aceita **3 posições**:

| Posição | Papel |
|---|---|
| 1 | titular — vai para a verificação da seguradora |
| 2 | primeira reserva |
| 3 | segunda reserva |

Quando o titular **não passa** na verificação manual da seguradora, ou desiste,
o atendente clica em "Promover próximo" na ficha do frete. O sistema:

1. fecha a reserva do titular com motivo (`reprovado_seguradora`, `desistiu`);
2. promove a posição 2 a titular e reinicia o relógio útil dela;
3. coloca a conversa do promovido no topo da fila da Central, para o atendente
   dar a notícia — o bot não manda essa mensagem.

A promoção é **manual**, não automática, porque a resposta da seguradora chega
fora do sistema — por telefone ou e-mail. Automatizar isso agora seria inventar
um evento que não existe.

Frete com as 3 posições ocupadas **sai da lista** de `busca_fretes` até alguma
vagar.

### Prazo: 3 horas úteis

Decidido em 18/09/2026. Passadas 3 horas úteis sem movimento, a posição expira,
a vaga libera e o próximo da fila sobe.

São **3 horas úteis**, não 3 horas de relógio de parede. O relógio só corre em
horário comercial, decidido em 18/09/2026: motorista que confirma às 22h não
pode perder a carga à 1h da manhã, com a Central vazia.

O relógio para em dois casos:

- **fora do horário comercial** — volta a correr na abertura do próximo dia útil;
- **quando o atendente move a reserva para `em_analise`** — dali em diante quem
  conduz é a seguradora, que responde por telefone ou e-mail, num tempo que não
  é nosso. Sem essa pausa, todo titular perderia a vaga no meio da verificação.

Horário comercial padrão: **segunda a sexta, 8h às 18h**, no fuso de São Paulo
— o mesmo fuso que os relatórios da Fase 4 já usam. Fica em configuração, não
no código, para mudar sem deploy.

As 3 horas úteis são uma cobrança para a equipe, não para o motorista: é o
prazo que a Central tem para assumir a conversa antes de a carga voltar à roda.
Com a fila da Fase 2 e o aviso em tempo real, é factível.

Consequência para o código: **não dá para guardar um `expira_em` fixo.** Um
carimbo de data só vale enquanto o relógio corre sem parar, e este para duas
vezes. A reserva guarda quanto tempo útil já foi consumido e desde quando está
contando; a rotina periódica soma os minutos úteis decorridos (seção 8).

Feriado não é tratado nesta primeira versão: reserva que cai na véspera de
feriado vence no feriado. A mesma limitação já existe no tempo de resposta dos
relatórios da Fase 4. Se incomodar na prática, entra depois, com tabela de
feriados.

A mensagem ao motorista não diz "3 horas": diz a hora real em que a reserva
vence, projetada pelo relógio útil — "reservada até as 11h40 de amanhã". Dizer
"3 horas" às 22h seria mentira.

Ao expirar, numa rotina periódica no mesmo molde de `_ema_inactivity_check`
(`infraestrutura_critica/app.py`):

1. a `ReservaFrete` vira `expirada` e a `DriverBid` fecha com
   `closed_reason='reserva_expirada'`;
2. a posição seguinte sobe a titular;
3. a conversa dos dois motoristas — o que perdeu e o que subiu — é marcada na
   Central, com `ConversationEvent`, e vai para o topo da fila.

**Nenhuma mensagem automática.** Quem conta ao motorista que a carga caiu, e
quem avisa o promovido, é o atendente. Vale a mesma razão de `[11]`: a conversa
já é `manual` e o bot está calado nela.

Isso põe peso na fila: reserva que expira sem ninguém olhar deixa o motorista
sem resposta. O aviso na Central precisa ser visível o bastante para não passar
batido no meio do movimento.

---

## 8. Dados novos

Tudo em `infraestrutura_critica/models.py`, com migração testada em **SQLite e
PostgreSQL** — a regra da casa, e a lição do `date_trunc`.

### `BotSession` — a sessão da máquina de estados

Tabela nova, e não reaproveitar `EmaSession`: o `driver_id` dela é
`nullable=False`, e a raiz começa justamente em números que ainda não têm
cadastro.

```
id
telefone            normalizado por normalize_contact_key, index
conversation_id     FK conversations
driver_id           FK drivers, NULL até existir cadastro
campanha_id         FK bot_campanhas, NULL se a raiz começou de outro jeito
estado              String(40), index
estado_anterior     String(40)
contexto            JSON  {uf, cidade, pagina, fretes_pagina: [ids],
                           frete_escolhido_id, ufs_interesse: [], origem}
tentativas          Integer, zerado a cada troca de estado
trilha              JSON  [{estado, em}]  — alimenta o checklist do handoff
status              ativa | handoff | encerrada | expirada | optout
motivo_fim          String(60)
ultima_msg_em       DateTime
lembrete_em         DateTime
created_at / updated_at
```

Índice único parcial: **uma** `BotSession` com `status='ativa'` por telefone.

### `BotCampanha` e `BotContato` — a lista subida

```
BotCampanha:  id, nome, arquivo, total, validos, invalidos, duplicados,
              status (rascunho|disparando|pausada|concluida),
              criado_por, created_at

BotContato:   id, campanha_id, telefone_bruto, telefone,
              status (pendente|enviado|respondeu|invalido|duplicado|
                      optout|erro),
              motivo_recusa (valor|destino|data|outro|NULL),
              bot_session_id, erro, enviado_em
```

### `ReservaFrete`

```
id, freight_id, driver_id, bid_id, posicao (1..3),
status (ativa|em_analise|aprovada|reprovada|desistiu|expirada),
motivo, criada_em,

minutos_limite      Integer, default 180 (as 3 horas úteis)
minutos_consumidos  Integer, default 0 — gravado toda vez que o relógio para
contando_desde      DateTime, NULL quando o relógio está parado
```

Sem `expira_em`: o relógio para fora do horário comercial e durante a análise
da seguradora (seção 7), e carimbo de data não sobrevive a isso. A rotina
periódica expira quando
`minutos_consumidos + minutos_úteis(contando_desde → agora) >= minutos_limite`.

A hora que o motorista vê e a contagem regressiva do painel saem da projeção
inversa: quanto tempo útil falta, jogado no calendário comercial.

Sem índice único parcial aqui: a garantia de não existirem dois titulares vem
do `SELECT ... FOR UPDATE` na linha do frete, que é o padrão já usado na fila e
funciona nos dois bancos.

### `DriverBid` — um campo novo

```
preco_fixo    Boolean, default False, server_default false
```

Marca a oferta que nasceu do bot com valor fechado. `process_bid_response` não
roda quando é `True`. É alteração de tabela existente: migração aditiva, com
`server_default`, para não quebrar as linhas que já estão lá.

### `InteresseRegiao`

```
id, driver_id, uf, cidade (opcional), ativo, created_at
```

### `Driver` — três campos novos, para a reoferta

```
bot_ultima_oferta_em    DateTime, NULL  — trava das 2h entre reofertas
bot_ofertas_ignoradas   Integer, default 0 — zera a cada resposta
bot_pausado             Boolean, default False — 3 ignoradas seguidas
```

Alteração de tabela existente: migração aditiva com `server_default`, pelo
`_ensure_column_dual_dialect` de `infraestrutura_critica/utils/migrations.py`,
que é o caminho que já funciona nos dois bancos.

### `OptOut`

```
id, telefone, origem (bot|atendente|manual), motivo, created_at
```

---

## 9. A lista de telefones

Tela nova em `chatbot_regras/`: subir `.txt`, `.csv` ou `.xlsx` com uma coluna
de telefone e nada mais.

Ao subir, antes de disparar qualquer coisa, mostrar o resumo: quantos válidos,
quantos inválidos, quantos repetidos no próprio arquivo, quantos já são
motorista cadastrado, quantos estão em opt-out. Só então o botão "Disparar".

**Disparo com folga, não em rajada.** Uma centena de mensagens frias saindo do
mesmo número em minutos é o jeito mais rápido de a Emalog perder o WhatsApp da
empresa — e hoje é uma instância só, a `emalog`, na Evolution própria. Mínimo:

- intervalo aleatório entre envios (proposta: 20 a 60 segundos);
- teto diário por campanha (proposta: 200);
- parar a campanha sozinha se a taxa de erro de envio passar de 20%;
- nunca disparar para quem está em `OptOut`.

Os números propostos precisam de confirmação (seção 12).

---

## 10. O que falta no sistema antes desta raiz funcionar

1. **Pino de localização não é lido.** O webhook trata `conversation`,
   `extendedTextMessage`, `imageMessage`, `documentMessage`, `videoMessage` e
   `audioMessage`. `locationMessage` cai no ramo "sem conteúdo reconhecível" e
   a mensagem é descartada. Sem isso, o estado `geolocalizacao` só funciona por
   texto.
2. **Consulta motorista para fretes não existe.** `find_eligible_drivers` faz
   só o caminho inverso.
3. **Valor em formato americano.** `generate_freight_message`, em
   `execucao_entrega_frete/utils/whatsapp.py`, manda "R$ 2,500.00". Com o valor
   virando o centro da mensagem do bot, corrigir deixa de ser cosmético
   (`PENDENCIAS.md` 4.5). Usar `_brl`.
4. **Frete sem `driver_cost` não pode ser ofertado pelo bot.** Vale conferir
   quantos fretes em `ofertado` estão hoje sem valor de motorista: se forem
   muitos, a lista sai vazia mesmo havendo carga na praça.
5. **Mensagem de bot precisa virar `WhatsAppMessage`.** Já é regra da seção 4,
   mas vale repetir: é exatamente onde os lembretes do EMA erraram
   (`PENDENCIAS.md` 3.1).
6. **Fase 5 pressupõe as fases 0 a 4 estáveis.** A trava contra SQLite em
   produção (`PENDENCIAS.md` 0.1) e o QR code da Evolution nova
   (`PENDENCIAS.md` 0.3) ainda não foram concluídos. Construir a raiz pode
   começar; disparar lista em produção, não.

---

## 11. Ordem sugerida de construção

Uma etapa por vez, cada uma testável sozinha, no ritmo das fases anteriores.

| Etapa | Entrega | Como testar |
|---|---|---|
| 5.1 ✅ | Modelos + migração | **feito em 18/09/2026** — `testes/fase5/esquema.py`, 29 verificações em SQLite e PostgreSQL |
| 5.2 ✅ | Máquina de estados (1, 2, 4, 11, 12) + passo 5 no roteamento (envio + `WhatsAppMessage source='bot'` + handoff para a fila) | **feito em 18/09/2026** — `maquina_5_2.py` (34) e `roteamento_5_2.py` (15), SQLite e PostgreSQL |
| 5.3 ✅ | **Coleta mínima NATIVA** (estado 3: nome + veículo + cidade/UF), **sem EMA** | **feito em 18/09/2026** — `coleta_minima_5_3.py`, SQLite e PostgreSQL. Sem cadastro / incompleto coleta o mínimo e faz handoff; o atendente finaliza |
| 5.4 | `locationMessage` no webhook + estado 5 | manda pino de um celular |
| 5.5 | Consulta de fretes + estados 6, 7 e caminhos A e B | UF com e sem carga |
| 5.6 | Estados 8, 9, 10 + `ReservaFrete` + relógio útil + promoção | dois motoristas no mesmo frete; reserva aberta às 17h precisa vencer às 10h do dia seguinte, não às 20h |
| 5.7 | Checklist no painel da Central | atendente assume e vê tudo |
| 5.8 | Campanha: subir lista, validar, disparar com folga | lista de 3 números de teste |
| 5.9 | Reoferta `[R]`: gatilho por carga nova, trava de 2h, horário e pausa | motorista livre recebe; motorista em viagem, não; duas cargas em 1h viram uma oferta só |

---

## 12. Decisões fechadas

Todas de 18/09/2026. As quatro últimas foram decididas por recomendação, com o
usuário delegando; são todas baratas de reverter, e onde a reversão custa algo
está dito.

1. **Relógio da reserva** — só corre em horário comercial, segunda a sexta das
   8h às 18h, fuso de São Paulo, em configuração e não no código. Feriado fica
   de fora nesta versão (seção 7).

2. **Fretes por página: 3.** Quatro já passa de uma tela de celular, e o
   motorista lê isso dirigindo ou no posto. Reverter é trocar uma constante.

3. **Ritmo de disparo: 30 a 90 segundos entre mensagens, teto de 150 por dia**
   — mais conservador que a proposta anterior, de propósito. A conta é simples:
   o WhatsApp da empresa é **um número só**, na instância `emalog` da Evolution
   própria, e é o mesmo por onde passa todo o atendimento da Central. Se ele
   for bloqueado por denúncia de lista fria, não cai só a campanha: cai o
   atendimento inteiro, junto com o EMA e a oferta de frete. Subir esses
   números depois é fácil; recuperar um número banido, não. Vale revisar com
   quem já disparou lista no WhatsApp antes de soltar a primeira campanha
   grande.

4. **Disparo repetido — duas regras diferentes**, corrigido em 18/09/2026:

   - **Número frio que nunca respondeu:** 30 dias entre tentativas, no máximo
     duas. Depois da segunda, vira `sem_interesse` e sai das campanhas.
   - **Motorista cadastrado, sem viagem ativa, que já demonstrou interesse:**
     continua recebendo carga, sem prazo de carência. É o caminho `[R]` da
     seção 5, com as travas de 2h entre ofertas, horário comercial e pausa
     após 3 ignoradas.

   A diferença é entre insistir com desconhecido e atender quem quer trabalhar.
   A primeira queima o número da empresa; a segunda é o produto.

5. **Motivo da recusa: sim, uma pergunta só**, no caminho **[C]** da seção 5.
   Com o valor fechado, é o único sinal que a operação recebe sobre a tabela de
   preço estar no mercado ou não.

6. **Nome provisório: `Motorista <últimos 4 dígitos>`** — "Motorista 8877".
   Não polui a ficha como o telefone inteiro e é reconhecível na fila. A própria
   coleta mínima (estado [3]) troca pelo nome real na primeira pergunta, e o
   registro fica com `validated = False` até um operador conferir.

7. **O bot não fala de reserva.** Posição, prazo e vencimento são do atendente
   (estado [11] e seção 7). O bot só registra e se cala.

8. **Coleta de cadastro: nativa, sem o agente EMA** (18/09/2026). O chatbot é
   100% regras, por seleção de opções, e **não aciona o EMA em momento nenhum** —
   o EMA não consome API neste sistema. Número sem cadastro (ou incompleto) passa
   pela coleta mínima do estado [3]: nome, veículo (opção do vocabulário
   canônico) e cidade/UF, só o que falta, e então handoff. O atendente humano
   finaliza o cadastro. Por isso a antiga etapa 5.3 ("ponte com o EMA") foi
   **substituída** por esta coleta nativa. Reverter é caro (mexe no roteamento),
   mas a decisão é firme: nada de dependência de token no caminho do bot.

### O que fica por conta do uso

- Feriado não para o relógio da reserva.
- A Central precisa assumir a conversa dentro de 3 horas úteis, senão a carga
  volta à roda. É uma promessa de operação, não só de software.
- Ninguém avisa o motorista de que ele é reserva, nem de que a reserva caiu,
  a não ser o atendente. Se a fila atrasar, ele fica sem resposta.
- A campanha respeita o teto diário, então lista de 1.000 números leva cerca de
  uma semana para rodar inteira.
- Com a reoferta a cada 2 horas, a pausa por 3 ignoradas vai acontecer bastante.
  Alguém precisa olhar a fila de motorista pausado, senão a base vai secando
  sem ninguém perceber.
