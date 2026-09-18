# Testes de produção: trava contra SQLite

No Elastic Beanstalk, o sistema não sobe sem PostgreSQL. Um SQLite criado lá
fica dentro da pasta da aplicação, que cada deploy substitui, e tudo o que
fosse gravado nele se perderia sem aviso. Fora do Elastic Beanstalk, no
computador, continua caindo para SQLite como antes.

O Elastic Beanstalk é reconhecido pela pasta `/opt/elasticbeanstalk`, que ele
cria em toda instância. O teste simula essa pasta com uma pasta temporária.

Esta pasta não entra no zip de deploy. A trava de segurança dos testes é a de
`testes/_ambiente.py`.

## O que `trava_sqlite.py` prova

Cada boot roda num processo novo, como em produção.

- No Elastic Beanstalk, sem `DATABASE_URL` ou com uma que não é PostgreSQL, o
  sistema não sobe, diz o motivo no log e não cria o SQLite.
- No Elastic Beanstalk, com o PostgreSQL fora do ar, tenta 10 vezes, com 3
  segundos entre as tentativas, e desiste em menos de 100 segundos, antes do
  `--timeout` de 120 segundos do gunicorn. A senha não aparece no log.
- Fora do Elastic Beanstalk, sobe em SQLite sem `DATABASE_URL` e cai para
  SQLite com o PostgreSQL fora do ar, com uma tentativa só, como antes.
- `/conversas/api/status` mostra `"trava_sqlite": true` no Elastic Beanstalk e
  `false` fora dele.
- Só com PostgreSQL local: no Elastic Beanstalk, com o banco no ar, sobe em
  PostgreSQL.

Teste de mutação feito durante o desenvolvimento, e não incluído: com a
recusa desligada, o teste reprovou em 10 verificações.

## Como rodar

O teste apaga `instance\emalog.db` entre os cenários, por isso recusa rodar se
o arquivo já existir. O cenário do PostgreSQL fora do ar leva cerca de um
minuto.

```
del instance\emalog.db
python testes\producao\trava_sqlite.py
```

PostgreSQL local, com banco vazio:

```
set DATABASE_URL=postgresql://postgres:SENHA@localhost:5432/emalog_teste?sslmode=disable
python testes\producao\trava_sqlite.py
```

## Conferência em produção

Depois do deploy, logado como administrador, abra `/conversas/api/status`.
Precisa aparecer `"trava_sqlite": true` e `"dialeto": "postgresql"`.

Se aparecer `"trava_sqlite": false`, o sistema não reconheceu o Elastic
Beanstalk e a trava não está valendo.

Se o sistema não subir depois de um deploy, procure no log `web.stdout.log`
por "Elastic Beanstalk sem PostgreSQL". No console da AWS: Elastic Beanstalk,
ambiente, Logs, Solicitar logs.
