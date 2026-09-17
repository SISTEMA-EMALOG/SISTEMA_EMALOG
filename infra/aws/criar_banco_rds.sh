#!/usr/bin/env bash
# Cria o PostgreSQL de produção do Emalog no Amazon RDS e aponta o Elastic
# Beanstalk para ele.
#
# Rodar no AWS CloudShell, que já está logado na conta: nenhuma chave de
# acesso sai da AWS. No CloudShell: Ações > Carregar arquivo, e depois
#
#     bash criar_banco_rds.sh
#
# Pode ser rodado de novo se parar no meio: reaproveita o que já existir.
# Pede confirmação antes de criar o banco e antes de mexer no ambiente.
set -euo pipefail

export AWS_DEFAULT_REGION=sa-east-1
export AWS_PAGER=""

CNAME_PREFIXO="sistemaemalog-env"
DB_ID="emalog-db"
DB_NOME="emalog"
DB_USUARIO="emalog_admin"
SG_NOME="emalog-rds"
SUBNET_GRUPO="emalog-db-subnets"
ARQ_SENHA="$HOME/.emalog-db-senha"

passo()    { printf '\n== %s\n' "$*"; }
pare()     { printf '\nPAROU: %s\n' "$*" >&2; exit 1; }
confirma() { local r; read -r -p "$1 [s/N] " r; [[ "$r" == [sS] ]]; }
vazio()    { [[ -z "$1" || "$1" == "None" || "$1" == "null" ]]; }


passo "Conta e ambiente"
CONTA=$(aws sts get-caller-identity --query Account --output text)
AMBIENTE=$(aws elasticbeanstalk describe-environments --no-include-deleted \
  --query "Environments[?starts_with(CNAME || '', '${CNAME_PREFIXO}.')] | [0].[ApplicationName, EnvironmentName]" \
  --output text)
read -r APP ENV_NOME <<<"$AMBIENTE"
vazio "${ENV_NOME:-}" && pare "ambiente ${CNAME_PREFIXO} não encontrado na região ${AWS_DEFAULT_REGION}"

INSTANCIA=$(aws elasticbeanstalk describe-environment-resources --environment-name "$ENV_NOME" \
  --query 'EnvironmentResources.Instances[0].Id' --output text)
[[ "$INSTANCIA" == i-* ]] || pare "o ambiente ${ENV_NOME} não tem instância rodando"
VPC=$(aws ec2 describe-instances --instance-ids "$INSTANCIA" \
  --query 'Reservations[0].Instances[0].VpcId' --output text)
vazio "$VPC" && pare "não achei a VPC da instância ${INSTANCIA}"
# Libera o banco só para o grupo de segurança que o próprio Elastic Beanstalk
# criou, e não para qualquer outro que a instância tenha, como o default.
SGS_EB=$(aws ec2 describe-instances --instance-ids "$INSTANCIA" \
  --query "Reservations[0].Instances[0].SecurityGroups[?starts_with(GroupName, 'awseb')].GroupId" \
  --output text)
vazio "$SGS_EB" && pare "a instância ${INSTANCIA} não tem o grupo de segurança do Elastic Beanstalk"

# Se já houver um DATABASE_URL de outro banco, não sobrescreve às cegas.
ATUAL=$(aws elasticbeanstalk describe-configuration-settings --application-name "$APP" \
  --environment-name "$ENV_NOME" \
  --query "ConfigurationSettings[0].OptionSettings[?OptionName=='DATABASE_URL'].Value | [0]" \
  --output text)
if ! vazio "$ATUAL" && [[ "$ATUAL" != *"@${DB_ID}."* ]]; then
  pare "o ambiente já tem um DATABASE_URL apontando para outro banco (${ATUAL#*@}). Nada foi alterado."
fi

VERSAO=$(aws rds describe-db-engine-versions --engine postgres --default-only \
  --query 'DBEngineVersions[0].EngineVersion' --output text)
CLASSE=db.t4g.micro
N=$(aws rds describe-orderable-db-instance-options --engine postgres --engine-version "$VERSAO" \
  --db-instance-class "$CLASSE" --query 'length(OrderableDBInstanceOptions)' --output text)
[[ "$N" != "0" ]] || CLASSE=db.t3.micro

cat <<EOF
  conta AWS:   $CONTA
  aplicação:   $APP
  ambiente:    $ENV_NOME
  VPC:         $VPC
  banco novo:  $DB_ID, PostgreSQL $VERSAO, $CLASSE, 20 GB, uma zona,
               sem acesso público, criptografado, protegido contra exclusão
EOF

if aws rds describe-db-instances --db-instance-identifier "$DB_ID" >/dev/null 2>&1; then
  echo "  o banco $DB_ID já existe e será reaproveitado"
  [[ -s "$ARQ_SENHA" ]] || pare "o banco já existe, mas a senha não está em ${ARQ_SENHA}. Nada foi alterado."
  SENHA=$(tr -dc '0-9a-f' < "$ARQ_SENHA")
else
  echo
  echo "  Custo: dentro da camada gratuita da AWS, zero. Fora dela, cerca de"
  echo "  US\$ 25 por mês nesta região (estimativa; confira na calculadora da AWS)."
  confirma "Criar o banco agora?" || pare "cancelado, nada foi criado"

  passo "Grupo de segurança: só o Elastic Beanstalk acessa a porta 5432"
  SG_RDS=$(aws ec2 describe-security-groups \
    --filters "Name=vpc-id,Values=$VPC" "Name=group-name,Values=$SG_NOME" \
    --query 'SecurityGroups[0].GroupId' --output text)
  if vazio "$SG_RDS"; then
    SG_RDS=$(aws ec2 create-security-group --vpc-id "$VPC" --group-name "$SG_NOME" \
      --description "PostgreSQL do Emalog - acesso so pelo Elastic Beanstalk" \
      --query GroupId --output text)
  fi
  for sg in $SGS_EB; do
    if ! saida=$(aws ec2 authorize-security-group-ingress --group-id "$SG_RDS" \
        --protocol tcp --port 5432 --source-group "$sg" 2>&1); then
      grep -q 'InvalidPermission.Duplicate' <<<"$saida" || pare "$saida"
    fi
  done
  echo "  $SG_RDS"

  passo "Sub-redes do banco, na mesma VPC do sistema"
  if ! aws rds describe-db-subnet-groups --db-subnet-group-name "$SUBNET_GRUPO" >/dev/null 2>&1; then
    SUBNETS=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" \
      --query 'Subnets[].SubnetId' --output text)
    # shellcheck disable=SC2086
    aws rds create-db-subnet-group --db-subnet-group-name "$SUBNET_GRUPO" \
      --db-subnet-group-description "Sub-redes do PostgreSQL do Emalog" \
      --subnet-ids $SUBNETS --query 'DBSubnetGroup.SubnetGroupStatus' --output text
  else
    echo "  já existe"
  fi

  passo "Senha do banco"
  if [[ ! -s "$ARQ_SENHA" ]]; then
    (umask 077; openssl rand -hex 16 > "$ARQ_SENHA")
  fi
  SENHA=$(tr -dc '0-9a-f' < "$ARQ_SENHA")
  echo "  gerada e guardada em $ARQ_SENHA, só você lê"

  passo "Criando o banco"
  criar() {
    aws rds create-db-instance --db-instance-identifier "$DB_ID" \
      --engine postgres --engine-version "$VERSAO" --db-instance-class "$CLASSE" \
      --allocated-storage 20 --storage-type gp3 \
      --db-name "$DB_NOME" --master-username "$DB_USUARIO" --master-user-password "$SENHA" \
      --vpc-security-group-ids "$SG_RDS" --db-subnet-group-name "$SUBNET_GRUPO" \
      --no-publicly-accessible --no-multi-az --storage-encrypted --deletion-protection \
      --copy-tags-to-snapshot --auto-minor-version-upgrade --tags Key=Projeto,Value=emalog \
      --backup-retention-period "$1" \
      --query 'DBInstance.DBInstanceStatus' --output text 2>&1
  }
  if ! saida=$(criar 7); then
    # Contas no plano gratuito recusam backup de mais de um dia.
    grep -qi 'free' <<<"$saida" || pare "$saida"
    echo "  a conta está no plano gratuito; backup automático fica em 1 dia"
    saida=$(criar 1) || pare "$saida"
  fi
  echo "  $saida"
fi

passo "Esperando o banco ficar disponível, de 5 a 15 minutos"
aws rds wait db-instance-available --db-instance-identifier "$DB_ID"
ENDPOINT=$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" \
  --query 'DBInstances[0].Endpoint.Address' --output text)
URL="postgresql://${DB_USUARIO}:${SENHA}@${ENDPOINT}:5432/${DB_NOME}?sslmode=require"
echo "  disponível em $ENDPOINT"

passo "Apontar o sistema para o banco novo"
if [[ "$ATUAL" == "$URL" ]]; then
  echo "  o ambiente já aponta para este banco"
else
  echo "  O sistema reinicia e começa com o banco vazio. O usuário administrador é"
  echo "  recriado sozinho a partir das variáveis INITIAL_ADMIN_*. O que foi"
  echo "  cadastrado no SQLite atual não é copiado."
  confirma "Configurar DATABASE_URL no ambiente ${ENV_NOME} agora?" || {
    echo "Não configurado. Rode o script de novo quando quiser; o banco já existe."
    exit 0
  }
  aws elasticbeanstalk wait environment-updated --application-name "$APP" --environment-names "$ENV_NOME"
  AJUSTE=$(mktemp)
  trap 'rm -f "$AJUSTE"' EXIT
  (umask 077; printf '[{"Namespace":"aws:elasticbeanstalk:application:environment","OptionName":"DATABASE_URL","Value":"%s"}]' "$URL" > "$AJUSTE")
  aws elasticbeanstalk update-environment --application-name "$APP" --environment-name "$ENV_NOME" \
    --option-settings "file://$AJUSTE" --query 'Status' --output text
  echo "  esperando o ambiente reiniciar"
  aws elasticbeanstalk wait environment-updated --application-name "$APP" --environment-names "$ENV_NOME"
fi
aws elasticbeanstalk describe-environments --environment-names "$ENV_NOME" \
  --query 'Environments[0].[Status, Health]' --output text | sed 's/^/  ambiente: /'

passo "Pronto"
cat <<EOF
Copie a linha abaixo para o DATABASE_URL do seu .env, no computador.
Ela contém a senha do banco: não mande por chat nem e-mail.

DATABASE_URL=$URL

Depois, logado como administrador, abra
http://${CNAME_PREFIXO}.eba-gyy2n3xm.sa-east-1.elasticbeanstalk.com/conversas/api/status
e confira "dialeto": "postgresql".
EOF
