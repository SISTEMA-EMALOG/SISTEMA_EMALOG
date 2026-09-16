"""
Normalização de telefone para a Central de Atendimento.

A chave canônica de uma Conversation é o número em E.164 SEM o '+',
só dígitos: '5511999998888'.

Por que um módulo próprio e não infraestrutura_critica/utils/phone_normalizer.py:
aquele arquivo resolve outro problema — adivinhar o DDD de um lead a partir da
cidade/UF na importação de planilha. Ele nunca devolve um número com código de
país, então não serve como chave de conversa.

E por que não reaproveitar normalize_phone de utils/evolution_api.py: lá o
código de país é decidido por `digits.startswith('55')`, o que quebra para
DDD 55 (Santa Maria/RS). '55999998888' é um celular do DDD 55 com 11 dígitos,
mas aquele teste o considera já internacionalizado e devolve o número sem o
código do país. Aqui a decisão é por COMPRIMENTO, que é o critério correto.
"""

import re

BR_COUNTRY_CODE = '55'

# Comprimentos de um número brasileiro sem código de país:
#   10 = DDD (2) + fixo/celular antigo (8)
#   11 = DDD (2) + celular com o nono dígito (9)
_BR_NATIONAL_LENGTHS = (10, 11)

# Com código de país: 55 + os acima.
_BR_E164_LENGTHS = (12, 13)


def only_digits(raw):
    """Devolve apenas os dígitos de uma string. None/vazio -> ''."""
    if not raw:
        return ''
    return re.sub(r'\D', '', str(raw))


def normalize_contact_key(raw):
    """
    Converte qualquer formato de entrada na chave canônica da conversa.

    Aceita o valor cru da Twilio ('whatsapp:+5511999998888'), o formato do
    cadastro ('(11) 99999-8888'), com ou sem código de país.

    Devolve string só de dígitos, ou None se não houver número utilizável.

        >>> normalize_contact_key('whatsapp:+5511999998888')
        '5511999998888'
        >>> normalize_contact_key('(11) 99999-8888')
        '5511999998888'
        >>> normalize_contact_key('55999998888')     # DDD 55, 11 dígitos
        '5555999998888'
    """
    digits = only_digits(raw)
    if not digits:
        return None

    # Já tem código de país brasileiro e comprimento compatível.
    if len(digits) in _BR_E164_LENGTHS and digits.startswith(BR_COUNTRY_CODE):
        return digits

    # Número nacional: acrescenta o código do país.
    if len(digits) in _BR_NATIONAL_LENGTHS:
        return BR_COUNTRY_CODE + digits

    # Número internacional ou fora do padrão: preserva como veio.
    # Melhor guardar algo consultável do que descartar a mensagem.
    return digits


def phone_variants(key):
    """
    Formas equivalentes do mesmo número, para casar com o cadastro legado.

    O nono dígito dos celulares foi adicionado em etapas no Brasil, então o
    mesmo assinante pode estar gravado com 8 ou com 9 dígitos. A Twilio sempre
    entrega a forma com 9. Sem tratar as duas, um motorista antigo abre uma
    conversa nova em vez de casar com o cadastro.

    Devolve um set de chaves canônicas candidatas, incluindo a original.
    """
    key = only_digits(key)
    if not key:
        return set()

    variants = {key}

    if key.startswith(BR_COUNTRY_CODE) and len(key) in _BR_E164_LENGTHS:
        national = key[len(BR_COUNTRY_CODE):]
        ddd, subscriber = national[:2], national[2:]

        # 9 dígitos começando com 9 -> a forma antiga de 8 dígitos.
        if len(subscriber) == 9 and subscriber.startswith('9'):
            variants.add(BR_COUNTRY_CODE + ddd + subscriber[1:])

        # 8 dígitos -> a forma nova, com o 9 na frente.
        elif len(subscriber) == 8:
            variants.add(BR_COUNTRY_CODE + ddd + '9' + subscriber)

    return variants


def find_driver_by_phone(key):
    """
    Localiza o motorista dono de um número, ou None.

    Driver.phone é texto livre com máscara ('(11) 99999-8888'), então não dá
    para comparar direto no SQL. A estratégia é portável nos dois bancos:
    filtra por LIKE nos 4 últimos dígitos, que nas máscaras brasileiras são
    sempre contíguos, e só então normaliza os candidatos em Python.
    """
    from infraestrutura_critica.models import Driver

    key = normalize_contact_key(key)
    if not key:
        return None

    last4 = key[-4:]
    if len(last4) < 4:
        return None

    wanted = phone_variants(key)

    candidates = Driver.query.filter(
        Driver.phone.like('%' + last4 + '%')
    ).all()

    for driver in candidates:
        if normalize_contact_key(driver.phone) in wanted:
            return driver

    return None
