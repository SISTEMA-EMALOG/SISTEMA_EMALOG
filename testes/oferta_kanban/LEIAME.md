# Oferta de frete → Kanban de contratação

Prova que a tela **Selecionar Motoristas** manda a oferta de verdade e que ela
aparece no quadro de `/contracting`.

## O que estava quebrado

A tela postava em `/freight/<id>/select-drivers`, que chamava
`send_freight_offer`. Essa função falava com um `WHATSAPP_API_URL` e um
`WHATSAPP_API_TOKEN` que nunca existiram no ambiente — o sistema envia pela
Evolution. Sem essas variáveis ela gravava a mensagem com status `enviado`,
devolvia `True`, e a rota somava `sent_count += 1` sem olhar o retorno. O
operador via "Ofertas WhatsApp enviadas para 1 motoristas" e nada saía do
servidor.

Além disso, a tela gravava só `WhatsAppMessage`. O quadro de contratação lê
apenas `DriverBid`, e por isso a oferta nunca chegava ao Kanban — nem depois
de o motorista ser aceito pelo botão "Aceitar Frete", que atribuía o frete
direto sem tocar na `DriverBid`.

E a mensagem pedia "responda ACEITO ou RECUSO", mas nenhum código lia essas
respostas: elas caíam no extrator de preço, que não acha número e devolvia o
card para "Conversando".

## Cenários

1. Envio bem-sucedido: a Evolution recebeu a mensagem, a `DriverBid` nasceu em
   "Aguardando resposta" e o card aparece no quadro.
2. Falha no envio: sem sucesso falso — a bid vai para "Encerrados", a
   `WhatsAppMessage` fica como `erro` e o frete não é marcado como ofertado.
3. Motorista responde `ACEITO`: o card vai para "Interessados" com o valor da
   oferta, sem depender do Groq.
4. Motorista responde `RECUSO`: o card vai para "Encerrados".
5. `aceito por 3200` é contraproposta, não aceite: segue para o extrator de
   preço.
6. Botão "Aceitar Frete": contrata por `contract_bid`, o mesmo caminho da
   Central, move o card para "Contratados", gera os pagamentos 70/30 e encerra
   as outras ofertas do frete.

## Como rodar

O teste troca o envio da Evolution por um falso e não fala com a internet.
Ele grava no banco, então **nunca rode contra produção** — `testes/_ambiente.py`
recusa qualquer `DATABASE_URL` que não seja local.

Em SQLite:

```bash
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 TESTE_PODE_SUJAR_SQLITE=1 \
  python testes/oferta_kanban/oferta_ate_contratacao.py
```

Em PostgreSQL local, com um banco limpo:

```bash
psql -h 127.0.0.1 -p 55432 -U postgres -c "DROP DATABASE IF EXISTS oferta_kanban" \
                                        -c "CREATE DATABASE oferta_kanban"
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
DATABASE_URL="postgresql://postgres:teste@127.0.0.1:55432/oferta_kanban?sslmode=disable" \
  python testes/oferta_kanban/oferta_ate_contratacao.py
```

O `?sslmode=disable` é obrigatório: sem ele a aplicação exige SSL, a conexão
falha e ela cai para SQLite em silêncio — o teste passaria, mas no banco errado.

Termina com `RESULTADO (<banco>): TUDO OK` e código de saída 0.

## O que este teste não cobre

Que o WhatsApp da empresa esteja de fato conectado. `send_text`, em
`infraestrutura_critica/utils/evolution_api.py`, **devolve `True` simulando o
envio** quando `EVOLUTION_API_URL` ou `EVOLUTION_API_KEY` estão vazias. Isso é
proposital para desenvolvimento, mas significa que rodar a tela numa máquina
sem essas variáveis volta a mostrar "oferta enviada" sem enviar nada. Em
produção as duas estão preenchidas; o que falta conferir lá é o QR code
escaneado — ver a pendência 0.3.
