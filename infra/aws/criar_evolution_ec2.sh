#!/usr/bin/env bash
# Instala a Evolution API do Emalog num servidor EC2 da própria conta, na mesma
# rede do sistema, e aponta o Elastic Beanstalk para ela.
#
# Rodar no AWS CloudShell, em sa-east-1, depois do criar_banco_rds.sh: usa o
# banco emalog-db e a senha que aquele script guardou. No CloudShell:
# Ações > Carregar arquivo, e depois
#
#     bash criar_evolution_ec2.sh
#
# O que fica pronto:
# - servidor emalog-evolution: Amazon Linux 2023, disco criptografado, protegido
#   contra exclusão, sem chave SSH (administração pelo Session Manager);
# - porta 8080 aberta só para o Elastic Beanstalk. Na ativação da conta da
#   Evolution Foundation, abre por alguns minutos só para o IP de quem ativa;
# - dados da Evolution num banco "evolution" separado, no mesmo RDS, com
#   usuário próprio;
# - chave da API e senha desse banco no Parameter Store, criptografadas;
# - instância de WhatsApp "emalog" criada;
# - EVOLUTION_API_URL, EVOLUTION_API_KEY e EVOLUTION_INSTANCE no ambiente.
#
# Pode ser rodado de novo se parar no meio: reaproveita o que já existir.
# Pede confirmação antes de criar o servidor e antes de mexer no ambiente.
set -euo pipefail

export AWS_DEFAULT_REGION=sa-east-1
export AWS_PAGER=""

CNAME_PREFIXO="sistemaemalog-env"
DB_ID="emalog-db"
DB_USUARIO="emalog_admin"
ARQ_SENHA="$HOME/.emalog-db-senha"

NOME="emalog-evolution"
PAPEL="emalog-evolution-ec2"
PARAM="/emalog/evolution"
# 2.4 é a primeira versão que exige ativação; versões antigas deixam de receber
# as correções de protocolo do WhatsApp.
IMAGEM="evoapicloud/evolution-api:2.4.0-rc2"
INSTANCIA_WA="emalog"
PORTA=8080
DIR_SERVIDOR="/opt/emalog-evolution"
NS="aws:elasticbeanstalk:application:environment"

passo()    { printf '\n== %s\n' "$*"; }
pare()     { printf '\nPAROU: %s\n' "$*" >&2; exit 1; }
confirma() { local r; read -r -p "$1 [s/N] " r; [[ "$r" == [sS] ]]; }
vazio()    { [[ -z "$1" || "$1" == "None" || "$1" == "null" ]]; }

SG_EVO=""; AJUSTE=""; ARQ_CMD=""; INSTALADOR=""
limpeza() {
  rm -f "$AJUSTE" "$ARQ_CMD" "$INSTALADOR"
  [[ -n "$SG_EVO" ]] && fecha_painel || true
}
trap limpeza EXIT

# Remove toda liberação da porta 8080 por endereço IP. As liberações para o
# Elastic Beanstalk são por grupo de segurança e ficam.
fecha_painel() {
  local cidr
  for cidr in $(aws ec2 describe-security-groups --group-ids "$SG_EVO" \
      --query "SecurityGroups[0].IpPermissions[?FromPort==\`$PORTA\`].IpRanges[].CidrIp" \
      --output text 2>/dev/null); do
    vazio "$cidr" && continue
    aws ec2 revoke-security-group-ingress --group-id "$SG_EVO" \
      --protocol tcp --port "$PORTA" --cidr "$cidr" >/dev/null 2>&1 || true
    echo "  painel fechado para $cidr"
  done
}

json_str() { local s=${1//\\/\\\\}; s=${s//\"/\\\"}; printf '"%s"' "$s"; }

# Roda um comando no servidor pelo Systems Manager e imprime a saída. Nenhum
# comando leva segredo: o que precisa da chave lê do arquivo no servidor.
no_servidor() {
  local id st
  ARQ_CMD=$(mktemp)
  printf '{"commands":[%s]}' "$(json_str "$1")" > "$ARQ_CMD"
  id=$(aws ssm send-command --instance-ids "$EC2_ID" --document-name AWS-RunShellScript \
    --comment "$NOME" --timeout-seconds 600 --parameters "file://$ARQ_CMD" \
    --query Command.CommandId --output text)
  rm -f "$ARQ_CMD"
  for _ in $(seq 1 120); do
    sleep 5
    st=$(aws ssm get-command-invocation --command-id "$id" --instance-id "$EC2_ID" \
      --query Status --output text 2>/dev/null) || continue
    case "$st" in
      Success)
        aws ssm get-command-invocation --command-id "$id" --instance-id "$EC2_ID" \
          --query StandardOutputContent --output text
        return 0 ;;
      Failed|Cancelled|TimedOut|Undeliverable|Terminated)
        aws ssm get-command-invocation --command-id "$id" --instance-id "$EC2_ID" \
          --query StandardErrorContent --output text >&2 || true
        return 1 ;;
    esac
  done
  return 1
}

CMD_CHAVE="K=\$(sed -n 's/^AUTHENTICATION_API_KEY=//p' $DIR_SERVIDOR/evolution.env)"

codigo_api() {
  no_servidor "$CMD_CHAVE; curl -s -o /dev/null -w '%{http_code}' -H \"apikey: \$K\" http://localhost:$PORTA/instance/fetchInstances" \
    | tr -dc '0-9'
}

estado_instalacao() {
  # O agente do Systems Manager sobe antes de o cloud-init rodar o instalador:
  # "running" no cloud-init conta como instalando, para não rodar dois ao mesmo tempo.
  no_servidor "if [ -f $DIR_SERVIDOR/pronto ]; then echo pronto; elif [ -f $DIR_SERVIDOR/falhou ]; then echo falhou; elif pgrep -f '[p]art-001|[i]nstalar\.sh' >/dev/null || cloud-init status 2>/dev/null | grep -q running; then echo instalando; else echo parado; fi" \
    | tr -dc 'a-z'
}

param_existe() { aws ssm get-parameter --name "$1" --query Parameter.Name --output text >/dev/null 2>&1; }
guarda()       { aws ssm put-parameter --name "$1" --type SecureString --value "$2" --overwrite \
                   --query Version --output text >/dev/null; }

espera_ambiente() {
  local st i
  for i in $(seq 1 90); do
    st=$(aws elasticbeanstalk describe-environments --environment-names "$ENV_NOME" \
      --query 'Environments[0].Status' --output text)
    [[ "$st" == "Ready" ]] && return 0
    (( i % 6 == 1 )) && echo "  ambiente: $st, esperando"
    sleep 20
  done
  return 1
}

config_atual() {
  aws elasticbeanstalk describe-configuration-settings --application-name "$APP" \
    --environment-name "$ENV_NOME" \
    --query "ConfigurationSettings[0].OptionSettings[?OptionName=='$1'].Value | [0]" --output text
}


passo "Conta e ambiente"
CONTA=$(aws sts get-caller-identity --query Account --output text)
echo "  conta AWS: $CONTA, região $AWS_DEFAULT_REGION"

# Acha o ambiente pelo endereço, sem diferenciar maiúsculas, ou pelo nome
# passado como argumento: bash criar_evolution_ec2.sh Nome-do-ambiente
LISTA=$(aws elasticbeanstalk describe-environments --no-include-deleted \
  --query 'Environments[].[ApplicationName, EnvironmentName, CNAME, Status]' --output text)
APP=""; ENV_NOME=""; CNAME=""
while IFS=$'\t' read -r a e c _; do
  [[ -z "$e" ]] && continue
  if [[ -n "${1:-}" ]]; then
    [[ "$e" == "$1" ]] && { APP=$a; ENV_NOME=$e; CNAME=$c; }
  elif [[ "${c,,}" == "${CNAME_PREFIXO}."* ]]; then
    APP=$a; ENV_NOME=$e; CNAME=$c
  fi
done <<<"$LISTA"
if vazio "$ENV_NOME"; then
  echo "  ambientes do Elastic Beanstalk nesta conta e região (aplicação, nome, endereço, situação):"
  if [[ -z "$LISTA" ]]; then echo "    nenhum"; else sed 's/^/    /' <<<"$LISTA"; fi
  pare "não achei o ambiente ${1:-com endereço ${CNAME_PREFIXO}}. Nada foi alterado."
fi
echo "  ambiente: $ENV_NOME, aplicação $APP"

INSTANCIA_EB=$(aws elasticbeanstalk describe-environment-resources --environment-name "$ENV_NOME" \
  --query 'EnvironmentResources.Instances[0].Id' --output text)
[[ "$INSTANCIA_EB" == i-* ]] || pare "o ambiente ${ENV_NOME} não tem instância rodando"
read -r VPC SUBNET AZ IP_PUB_EB <<<"$(aws ec2 describe-instances --instance-ids "$INSTANCIA_EB" \
  --query 'Reservations[0].Instances[0].[VpcId, SubnetId, Placement.AvailabilityZone, PublicIpAddress]' \
  --output text)"
vazio "${VPC:-}" && pare "não achei a VPC da instância ${INSTANCIA_EB}"
SGS_EB=$(aws ec2 describe-instances --instance-ids "$INSTANCIA_EB" \
  --query "Reservations[0].Instances[0].SecurityGroups[?starts_with(GroupName, 'awseb')].GroupId" \
  --output text)
vazio "$SGS_EB" && pare "a instância ${INSTANCIA_EB} não tem o grupo de segurança do Elastic Beanstalk"


passo "Banco"
read -r DB_STATUS RDS_HOST SG_RDS <<<"$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" \
  --query 'DBInstances[0].[DBInstanceStatus, Endpoint.Address, VpcSecurityGroups[0].VpcSecurityGroupId]' \
  --output text 2>/dev/null || true)"
vazio "${DB_STATUS:-}" && pare "o banco ${DB_ID} não existe. Rode antes o criar_banco_rds.sh. Nada foi alterado."
[[ "$DB_STATUS" == "available" ]] || pare "o banco ${DB_ID} está '${DB_STATUS}'. Espere ficar 'available' e rode de novo. Nada foi alterado."
vazio "${SG_RDS:-}" && pare "não achei o grupo de segurança do banco ${DB_ID}. Nada foi alterado."
[[ -s "$ARQ_SENHA" ]] || pare "a senha do banco não está em ${ARQ_SENHA}. Rode este script no mesmo CloudShell, na mesma região, em que rodou o criar_banco_rds.sh. Nada foi alterado."
echo "  $DB_ID disponível em $RDS_HOST"


passo "Servidor"
EC2_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=$NOME" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId' --output text)
[[ "$EC2_ID" =~ [[:space:]] ]] && pare "há mais de um servidor chamado ${NOME} (${EC2_ID}). Encerre o que sobrar e rode de novo."

if vazio "$EC2_ID"; then
  # O servidor sai para a internet (WhatsApp, imagens, ativação) pelo IP
  # público, como o do sistema. Sem ele, precisaria de um NAT, que custa mais.
  vazio "${IP_PUB_EB:-}" && pare "a instância do sistema não tem IP público; esta rede precisa de outro desenho. Nada foi alterado."

  TIPO=t4g.small; AMI_PARAM=al2023-ami-kernel-default-arm64
  N=$(aws ec2 describe-instance-type-offerings --location-type availability-zone \
    --filters "Name=location,Values=$AZ" "Name=instance-type,Values=$TIPO" \
    --query 'length(InstanceTypeOfferings)' --output text)
  [[ "$N" != "0" ]] || { TIPO=t3.small; AMI_PARAM=al2023-ami-kernel-default-x86_64; }

  cat <<EOF
  conta AWS:     $CONTA
  ambiente:      $ENV_NOME
  rede:          $VPC, sub-rede $SUBNET ($AZ), a mesma do sistema
  servidor novo: $NOME, $TIPO, Amazon Linux 2023, disco de 20 GB criptografado,
                 protegido contra exclusão, sem chave SSH
  Evolution:     $IMAGEM, porta $PORTA aberta só para o sistema
  dados:         banco "evolution" dentro do $DB_ID, com usuário próprio

  Custo: cerca de US\$ 25 a 30 por mês nesta região, somando servidor, disco e
  IP público (estimativa; confira na calculadora da AWS). O banco já existe e
  não tem custo extra.
EOF
  confirma "Criar o servidor agora?" || pare "cancelado, nada foi criado"
else
  echo "  o servidor $EC2_ID já existe e será reaproveitado"
fi

passo "Grupo de segurança: só o sistema acessa a porta $PORTA"
SG_EVO=$(aws ec2 describe-security-groups \
  --filters "Name=vpc-id,Values=$VPC" "Name=group-name,Values=$NOME" \
  --query 'SecurityGroups[0].GroupId' --output text)
if vazio "$SG_EVO"; then
  SG_EVO=$(aws ec2 create-security-group --vpc-id "$VPC" --group-name "$NOME" \
    --description "Evolution API do Emalog - acesso so pelo Elastic Beanstalk" \
    --query GroupId --output text)
fi
libera() {  # grupo porta grupo-de-origem
  local saida
  if ! saida=$(aws ec2 authorize-security-group-ingress --group-id "$1" \
      --protocol tcp --port "$2" --source-group "$3" 2>&1); then
    grep -q 'InvalidPermission.Duplicate' <<<"$saida" || pare "$saida"
  fi
}
for sg in $SGS_EB; do libera "$SG_EVO" "$PORTA" "$sg"; done
fecha_painel
echo "  $SG_EVO"
libera "$SG_RDS" 5432 "$SG_EVO"
echo "  banco $DB_ID liberado para o servidor ($SG_RDS)"

passo "Permissões do servidor"
if ! aws iam get-role --role-name "$PAPEL" >/dev/null 2>&1; then
  aws iam create-role --role-name "$PAPEL" \
    --description "Servidor da Evolution API do Emalog" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    --tags Key=Projeto,Value=emalog --query Role.RoleName --output text >/dev/null
fi
aws iam attach-role-policy --role-name "$PAPEL" \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam put-role-policy --role-name "$PAPEL" --policy-name parametros-evolution \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"ssm:GetParameter\",\"Resource\":\"arn:aws:ssm:${AWS_DEFAULT_REGION}:${CONTA}:parameter${PARAM}/*\"},{\"Effect\":\"Allow\",\"Action\":\"ssm:DeleteParameter\",\"Resource\":\"arn:aws:ssm:${AWS_DEFAULT_REGION}:${CONTA}:parameter${PARAM}/bootstrap/*\"}]}"
if ! aws iam get-instance-profile --instance-profile-name "$PAPEL" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$PAPEL" \
    --query InstanceProfile.InstanceProfileName --output text >/dev/null
fi
N=$(aws iam get-instance-profile --instance-profile-name "$PAPEL" \
  --query 'length(InstanceProfile.Roles)' --output text)
[[ "$N" != "0" ]] || aws iam add-role-to-instance-profile --instance-profile-name "$PAPEL" --role-name "$PAPEL"
echo "  $PAPEL: Session Manager e leitura de ${PARAM}/*"

passo "Segredos no Parameter Store"
param_existe "$PARAM/api-key"  || guarda "$PARAM/api-key"  "$(openssl rand -hex 32)"
param_existe "$PARAM/db-senha" || guarda "$PARAM/db-senha" "$(openssl rand -hex 24)"
echo "  ${PARAM}/api-key e ${PARAM}/db-senha, criptografados"

# A senha de administrador do RDS só fica no Parameter Store enquanto o
# servidor cria o banco "evolution"; o próprio servidor apaga logo depois.
libera_admin_do_banco() {
  guarda "$PARAM/bootstrap/rds-admin" "$(tr -dc '0-9a-f' < "$ARQ_SENHA")"
}

if vazio "$EC2_ID"; then
  passo "Criando o servidor"
  libera_admin_do_banco
  AMI=$(aws ssm get-parameter --name "/aws/service/ami-amazon-linux-latest/$AMI_PARAM" \
    --query Parameter.Value --output text)
  INSTALADOR=$(mktemp)
  cat > "$INSTALADOR" <<'MODELO'
#!/bin/bash
# Instalação da Evolution API do Emalog, gerada pelo criar_evolution_ec2.sh.
# Roda como root na primeira inicialização e pode ser rodada de novo.
set -Eeuo pipefail
DIR=__DIR__
REGIAO=__REGIAO__
PARAM=__PARAM__
IMAGEM=__IMAGEM__
RDS_HOST=__RDS_HOST__
DB_ADMIN=__DB_ADMIN__
PORTA=__PORTA__
ADMIN_ENV=""

mkdir -p "$DIR"; chmod 700 "$DIR"
[[ "$0" == "$DIR/instalar.sh" ]] || cp -f "$0" "$DIR/instalar.sh"
rm -f "$DIR/falhou" "$DIR/pronto"
exec >>/var/log/emalog-evolution.log 2>&1
trap 'echo "FALHOU na linha $LINENO"; rm -f "$ADMIN_ENV"; touch "$DIR/falhou"' ERR
echo "== $(date -Is) instalação começou"

param() {
  aws ssm get-parameter --region "$REGIAO" --name "$PARAM/$1" --with-decryption \
    --query Parameter.Value --output text
}

if ! swapon --show | grep -q /swapfile; then
  [[ -f /swapfile ]] || { fallocate -l 1G /swapfile; chmod 600 /swapfile; mkswap /swapfile; }
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile swap swap defaults 0 0' >> /etc/fstab
fi

command -v docker >/dev/null || dnf install -y docker
systemctl enable --now docker

T=$(curl -sf -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
IP=$(curl -sf -H "X-aws-ec2-metadata-token: $T" http://169.254.169.254/latest/meta-data/local-ipv4)
API_KEY=$(param api-key)
DB_SENHA=$(param db-senha)

if [[ ! -f "$DIR/banco-ok" ]]; then
  echo "== banco evolution"
  ADMIN_SENHA=$(param bootstrap/rds-admin)
  ADMIN_ENV=$(mktemp); chmod 600 "$ADMIN_ENV"
  printf 'PGPASSWORD=%s\nSENHA_EVO=%s\nPGSSLMODE=require\n' "$ADMIN_SENHA" "$DB_SENHA" > "$ADMIN_ENV"
  unset ADMIN_SENHA
  docker run --rm -i --env-file "$ADMIN_ENV" postgres:18-alpine \
    psql -h "$RDS_HOST" -U "$DB_ADMIN" -d postgres -v ON_ERROR_STOP=1 <<'SQL'
\getenv senha SENHA_EVO
SELECT 'CREATE ROLE evolution LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evolution') \gexec
ALTER ROLE evolution WITH LOGIN PASSWORD :'senha';
SELECT 'CREATE DATABASE evolution' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'evolution') \gexec
GRANT ALL PRIVILEGES ON DATABASE evolution TO evolution;
SQL
  rm -f "$ADMIN_ENV"; ADMIN_ENV=""
  touch "$DIR/banco-ok"
fi
aws ssm delete-parameter --region "$REGIAO" --name "$PARAM/bootstrap/rds-admin" 2>/dev/null || true

echo "== configuração"
(umask 077; cat > "$DIR/evolution.env" <<EOF
SERVER_NAME=emalog
SERVER_TYPE=http
SERVER_PORT=8080
SERVER_URL=http://$IP:$PORTA
AUTHENTICATION_API_KEY=$API_KEY
DATABASE_PROVIDER=postgresql
DATABASE_CONNECTION_URI=postgresql://evolution:$DB_SENHA@$RDS_HOST:5432/evolution?schema=evolution_api&sslmode=require
DATABASE_CONNECTION_CLIENT_NAME=emalog
DATABASE_SAVE_DATA_INSTANCE=true
DATABASE_SAVE_DATA_NEW_MESSAGE=true
DATABASE_SAVE_MESSAGE_UPDATE=true
DATABASE_SAVE_DATA_CONTACTS=true
DATABASE_SAVE_DATA_CHATS=true
DATABASE_SAVE_DATA_LABELS=false
DATABASE_SAVE_DATA_HISTORIC=false
CACHE_REDIS_ENABLED=true
CACHE_REDIS_URI=redis://evolution-redis:6379/6
CACHE_REDIS_PREFIX_KEY=evolution
CACHE_REDIS_SAVE_INSTANCES=false
CACHE_LOCAL_ENABLED=false
CONFIG_SESSION_PHONE_CLIENT=Emalog
CONFIG_SESSION_PHONE_NAME=Chrome
DEL_INSTANCE=false
LANGUAGE=pt-BR
LOG_LEVEL=ERROR,WARN,INFO
LOG_COLOR=false
LOG_BAILEYS=error
TELEMETRY_ENABLED=false
EOF
)

echo "== contêineres"
docker network inspect evolution >/dev/null 2>&1 || docker network create evolution
docker pull redis:7-alpine
docker pull "$IMAGEM"
docker rm -f evolution-api evolution-redis >/dev/null 2>&1 || true
docker run -d --name evolution-redis --network evolution --restart unless-stopped \
  --log-opt max-size=10m --log-opt max-file=3 \
  -v evolution_redis:/data redis:7-alpine redis-server --appendonly yes
docker run -d --name evolution-api --network evolution --restart unless-stopped \
  --log-opt max-size=10m --log-opt max-file=5 \
  -p "$PORTA:8080" --env-file "$DIR/evolution.env" \
  -v evolution_instances:/evolution/instances "$IMAGEM"

C=000
for _ in $(seq 1 60); do
  C=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$PORTA/" || true)
  [[ "$C" != "000" ]] && break
  sleep 5
done
if [[ "$C" == "000" ]]; then
  docker logs --tail 60 evolution-api
  false
fi
touch "$DIR/pronto"
echo "== $(date -Is) pronto"
MODELO
  sed -i -e "s|__DIR__|$DIR_SERVIDOR|" -e "s|__REGIAO__|$AWS_DEFAULT_REGION|" \
    -e "s|__PARAM__|$PARAM|" -e "s|__IMAGEM__|$IMAGEM|" -e "s|__RDS_HOST__|$RDS_HOST|" \
    -e "s|__DB_ADMIN__|$DB_USUARIO|" -e "s|__PORTA__|$PORTA|" "$INSTALADOR"

  # O perfil de permissões recém-criado leva alguns segundos para valer.
  for tentativa in 1 2 3 4 5 6; do
    if saida=$(aws ec2 run-instances --image-id "$AMI" --instance-type "$TIPO" \
        --subnet-id "$SUBNET" --associate-public-ip-address --security-group-ids "$SG_EVO" \
        --iam-instance-profile "Name=$PAPEL" \
        --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=20,VolumeType=gp3,Encrypted=true,DeleteOnTermination=true}' \
        --metadata-options 'HttpTokens=required,HttpEndpoint=enabled,HttpPutResponseHopLimit=1' \
        --disable-api-termination --user-data "file://$INSTALADOR" \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NOME},{Key=Projeto,Value=emalog}]" \
                             "ResourceType=volume,Tags=[{Key=Name,Value=$NOME},{Key=Projeto,Value=emalog}]" \
        --query 'Instances[0].InstanceId' --output text 2>&1); then
      EC2_ID=$saida; break
    fi
    grep -qi 'instance profile\|iamInstanceProfile' <<<"$saida" && [[ $tentativa -lt 6 ]] || pare "$saida"
    echo "  as permissões ainda estão sendo aplicadas; nova tentativa em 15 s"
    sleep 15
  done
  rm -f "$INSTALADOR"; INSTALADOR=""
  [[ "$EC2_ID" == i-* ]] || pare "a AWS não devolveu o identificador do servidor: $EC2_ID"
  echo "  $EC2_ID"
else
  ESTADO=$(aws ec2 describe-instances --instance-ids "$EC2_ID" \
    --query 'Reservations[0].Instances[0].State.Name' --output text)
  if [[ "$ESTADO" == "stopping" ]]; then
    aws ec2 wait instance-stopped --instance-ids "$EC2_ID"; ESTADO=stopped
  fi
  if [[ "$ESTADO" == "stopped" ]]; then
    echo "  estava desligado; ligando"
    aws ec2 start-instances --instance-ids "$EC2_ID" --query 'StartingInstances[0].CurrentState.Name' --output text
  fi
fi
aws ec2 wait instance-running --instance-ids "$EC2_ID"

passo "Esperando o servidor se registrar no Systems Manager, de 2 a 5 minutos"
PING=""
for _ in $(seq 1 60); do
  PING=$(aws ssm describe-instance-information --filters "Key=InstanceIds,Values=$EC2_ID" \
    --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || true)
  [[ "$PING" == "Online" ]] && break
  sleep 10
done
[[ "$PING" == "Online" ]] || pare "o servidor ${EC2_ID} não apareceu no Systems Manager em 10 minutos. Rode o script de novo daqui a pouco."

passo "Instalando a Evolution, de 5 a 10 minutos"
REINSTALOU=0
for _ in $(seq 1 60); do
  EST=$(estado_instalacao) || EST=erro
  case "$EST" in
    pronto) break ;;
    instalando|erro) sleep 20 ;;
    falhou|parado)
      if [[ $REINSTALOU == 1 ]]; then
        no_servidor "tail -n 40 /var/log/emalog-evolution.log" || true
        pare "a instalação falhou; o registro está acima. Rode o script de novo para tentar outra vez."
      fi
      echo "  a instalação anterior não terminou; recomeçando"
      no_servidor "test -f $DIR_SERVIDOR/banco-ok" >/dev/null 2>&1 || libera_admin_do_banco
      no_servidor "S=$DIR_SERVIDOR/instalar.sh; [ -f \$S ] || S=/var/lib/cloud/instance/scripts/part-001; systemctl reset-failed emalog-evolution-instalar 2>/dev/null; systemd-run --unit=emalog-evolution-instalar --collect /bin/bash \$S" >/dev/null
      REINSTALOU=1
      sleep 20 ;;
  esac
done
[[ "$EST" == "pronto" ]] || pare "a instalação ainda não terminou depois de 20 minutos. Rode o script de novo daqui a pouco."
echo "  Evolution no ar no servidor"

passo "Conta da Evolution Foundation"
COD=$(codigo_api) || pare "não consegui consultar a Evolution no servidor"
if [[ "$COD" == "200" ]]; then
  echo "  já está ativa"
elif [[ "$COD" == "503" ]]; then
  IP_EVO_PUB=$(aws ec2 describe-instances --instance-ids "$EC2_ID" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
  vazio "$IP_EVO_PUB" && pare "o servidor não tem IP público, então o painel de ativação não abre no navegador"
  cat <<EOF
  A Evolution 2.4 só funciona depois de ativada com uma conta gratuita da
  Evolution Foundation. A ativação é feita no painel da própria Evolution, que
  vai ficar aberto por alguns minutos só para o seu computador.

  Faça isso numa rede confiável, e não num Wi-Fi público: o painel não tem HTTPS.

  1. No seu navegador, abra https://checkip.amazonaws.com
  2. Digite aqui o número que aparece, no formato 200.100.50.25.
EOF
  MEU_IP=""
  until [[ "$MEU_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; do
    read -r -p "  Seu IP: " MEU_IP
    MEU_IP=$(tr -d '[:space:]' <<<"$MEU_IP")
    [[ "$MEU_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || echo "  não reconheci; copie só os números e pontos"
  done
  aws ec2 authorize-security-group-ingress --group-id "$SG_EVO" \
    --ip-permissions "IpProtocol=tcp,FromPort=$PORTA,ToPort=$PORTA,IpRanges=[{CidrIp=$MEU_IP/32,Description=ativacao-temporaria}]" \
    >/dev/null
  cat <<EOF

  3. Desligue a tradução automática do navegador e abra:

       http://$IP_EVO_PUB:$PORTA/manager

  4. O painel leva ao cadastro da Evolution Foundation. Entre com e-mail, Google
     ou GitHub e preencha o formulário com o e-mail e o telefone do responsável
     pelo sistema. No fim, o navegador volta sozinho para o painel.
  5. Se o painel pedir endereço do servidor e chave (API Key), use o endereço
     http://$IP_EVO_PUB:$PORTA e pegue a chave numa segunda aba do CloudShell:

       aws ssm get-parameter --region $AWS_DEFAULT_REGION --name $PARAM/api-key --with-decryption --query Parameter.Value --output text

     Não mande essa chave por chat nem por e-mail.

EOF
  while :; do
    read -r -p "  Quando o painel abrir já ativado, aperte Enter (ou digite sair): " R
    [[ "$R" == "sair" ]] && pare "ativação não concluída. O painel foi fechado; rode o script de novo para tentar outra vez."
    COD=$(codigo_api) || COD=erro
    [[ "$COD" == "200" ]] && break
    echo "  ainda não ativou (resposta $COD). Termine o cadastro no navegador e aperte Enter de novo."
  done
  fecha_painel
  echo "  ativada"
else
  no_servidor "docker logs --tail 30 evolution-api" || true
  pare "a Evolution respondeu ${COD:-nada} em vez de 200 ou 503. O registro está acima."
fi

passo "Instância de WhatsApp \"$INSTANCIA_WA\""
CORPO="{\"instanceName\":\"$INSTANCIA_WA\",\"integration\":\"WHATSAPP-BAILEYS\",\"qrcode\":false}"
# A resposta de criação traz o token da instância; ela não sai do servidor.
RESP=$(no_servidor "$CMD_CHAVE; R=\$(mktemp); C=\$(curl -s -o \$R -w '%{http_code}' -H \"apikey: \$K\" -H 'Content-Type: application/json' -d '$CORPO' http://localhost:$PORTA/instance/create); if [ \"\$C\" = 201 ] || [ \"\$C\" = 200 ]; then echo criada; elif grep -qi 'in use' \$R; then echo existe; else echo \"HTTP \$C\"; head -c 300 \$R; fi; rm -f \$R") \
  || pare "não consegui criar a instância no servidor"
case "$RESP" in
  criada*) echo "  criada" ;;
  existe*) echo "  já existia" ;;
  *) pare "a Evolution recusou a criação da instância: $RESP" ;;
esac

passo "Apontar o sistema para a Evolution nova"
IP_EVO=$(aws ec2 describe-instances --instance-ids "$EC2_ID" \
  --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)
URL_EVO="http://$IP_EVO:$PORTA"
CHAVE=$(aws ssm get-parameter --name "$PARAM/api-key" --with-decryption \
  --query Parameter.Value --output text)
URL_ATUAL=$(config_atual EVOLUTION_API_URL)
CHAVE_ATUAL=$(config_atual EVOLUTION_API_KEY)
INST_ATUAL=$(config_atual EVOLUTION_INSTANCE)
if [[ "$URL_ATUAL" == "$URL_EVO" && "$CHAVE_ATUAL" == "$CHAVE" && "$INST_ATUAL" == "$INSTANCIA_WA" ]]; then
  echo "  o ambiente já aponta para esta Evolution"
else
  vazio "$URL_ATUAL" && URL_ATUAL="(nenhuma)"
  vazio "$INST_ATUAL" && INST_ATUAL="(nenhuma)"
  cat <<EOF
  Evolution atual: $URL_ATUAL, instância $INST_ATUAL
  Evolution nova:  $URL_EVO, instância $INSTANCIA_WA, chave nova

  O sistema reinicia, e o WhatsApp só volta a funcionar depois de escanear o
  QR code no Emalog (instruções no fim). A chave antiga deixa de valer no
  webhook de entrada.
EOF
  if vazio "$(config_atual DATABASE_URL)"; then
    echo
    echo "  ATENÇÃO: o ambiente não tem DATABASE_URL, então o sistema ainda usa SQLite"
    echo "  e o reinício apaga o que foi cadastrado. Resolva o banco antes, se houver dados."
  fi
  confirma "Trocar a Evolution do ambiente ${ENV_NOME} agora?" || {
    echo "Não configurado. Rode o script de novo quando quiser; o servidor já está pronto."
    exit 0
  }
  espera_ambiente || pare "o ambiente ${ENV_NOME} não ficou 'Ready' em 30 minutos. Nada foi alterado nele."
  AJUSTE=$(mktemp)
  (umask 077; printf '[{"Namespace":"%s","OptionName":"EVOLUTION_API_URL","Value":"%s"},{"Namespace":"%s","OptionName":"EVOLUTION_API_KEY","Value":"%s"},{"Namespace":"%s","OptionName":"EVOLUTION_INSTANCE","Value":"%s"}]' \
    "$NS" "$URL_EVO" "$NS" "$CHAVE" "$NS" "$INSTANCIA_WA" > "$AJUSTE")
  aws elasticbeanstalk update-environment --application-name "$APP" --environment-name "$ENV_NOME" \
    --option-settings "file://$AJUSTE" --query 'Status' --output text
  rm -f "$AJUSTE"; AJUSTE=""
  echo "  esperando o ambiente reiniciar"
  espera_ambiente || echo "  ainda atualizando depois de 30 minutos; acompanhe em Elastic Beanstalk > Ambientes > Eventos"
fi
aws elasticbeanstalk describe-environments --environment-names "$ENV_NOME" \
  --query 'Environments[0].[Status, Health]' --output text | sed 's/^/  ambiente: /'
aws ssm delete-parameter --name "$PARAM/bootstrap/rds-admin" >/dev/null 2>&1 || true

passo "Pronto"
cat <<EOF
1. Atualize o cofre no computador. No .env, troque as três linhas EVOLUTION_*
   por estas. No .ebextensions/session-secret.config, use os mesmos valores no
   formato CHAVE: "valor". A chave é secreta: não mande por chat nem por e-mail.

EVOLUTION_API_URL=$URL_EVO
EVOLUTION_API_KEY=$CHAVE
EVOLUTION_INSTANCE=$INSTANCIA_WA

2. No celular da empresa, abra WhatsApp > Aparelhos conectados e desconecte
   todo aparelho que não reconhecer, inclusive a sessão do servidor antigo.

3. No Emalog, entre como administrador e abra
   http://${CNAME:-${CNAME_PREFIXO}.eba-gyy2n3xm.sa-east-1.elasticbeanstalk.com}/contracting/?tab=registrations
   Clique em "Sincronizar WhatsApp" e escaneie o QR code com o celular da
   empresa. O mesmo botão registra o webhook das mensagens recebidas.
EOF
