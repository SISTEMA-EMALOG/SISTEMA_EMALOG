"""
EMA Agent — core logic for AI-driven driver data enrichment/revalidation via WhatsApp.
Uses Gemini 2.5 Flash (via Replit AI Integrations) to conduct natural conversations
and extract structured data.

Modes:
  normal      — coleta apenas campos faltantes (comportamento original)
  revalidation — percorre TODOS os campos em ordem, confirmando/atualizando cada um
"""
import os
import re
import json
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

TRUCK_TYPES = ['3/4', 'vlc', 'toco', 'truck', 'bitruck', 'carreta']

# Ordered list of ALL fields the agent collects / revalidates.
# Documents come BEFORE their related text fields so OCR can pre-fill them
# and skip individual questions.  Text fields tagged skip_if_filled=True are
# silently skipped when they already have a value (set by OCR or previously).
FIELD_DEFS = [
    # ── CNH → extracts name, cpf, rg, birth_date, cnh_expiry ─────────────
    {'key': 'cnh_document',   'label': 'Foto da CNH (frente e verso)',
     'required': True,  'kind': 'file',
     'ocr_fills': ['name', 'cpf', 'rg', 'birth_date', 'cnh_expiry']},
    {'key': 'name',           'label': 'Nome completo',
     'required': True,  'kind': 'text',  'skip_if_filled': True},
    {'key': 'cpf',            'label': 'CPF',
     'required': True,  'kind': 'cpf',   'skip_if_filled': True},
    {'key': 'rg',             'label': 'RG',
     'required': True,  'kind': 'text',  'skip_if_filled': True},
    {'key': 'birth_date',     'label': 'Data de nascimento',
     'required': True,  'kind': 'date',  'skip_if_filled': True},
    {'key': 'cnh_expiry',     'label': 'Vencimento da CNH',
     'required': True,  'kind': 'date',  'skip_if_filled': True},
    {'key': 'email',          'label': 'E-mail',
     'required': False, 'kind': 'email'},

    # ── Comprovante de residência → extrai endereço completo ──────────────
    {'key': 'address_proof',  'label': 'Comprovante de residência',
     'required': True,  'kind': 'file',
     'ocr_fills': ['cep', 'street', 'number', 'complement',
                   'neighborhood', 'city', 'state']},
    {'key': 'cep',            'label': 'CEP',
     'required': True,  'kind': 'cep',   'skip_if_filled': True},
    {'key': 'number',         'label': 'Número do endereço',
     'required': True,  'kind': 'text',  'skip_if_filled': True},
    {'key': 'complement',     'label': 'Complemento',
     'required': False, 'kind': 'text',  'skip_if_filled': True},

    # ── Tipo de caminhão (necessário antes do CRLV) ───────────────────────
    {'key': 'truck_type',     'label': 'Tipo de caminhão',
     'required': True,  'kind': 'choice'},

    # ── ANTT → extrai número RNTRC ────────────────────────────────────────
    {'key': 'antt_document',  'label': 'Documento ANTT / RNTRC',
     'required': False, 'kind': 'file',
     'ocr_fills': ['antt_number']},

    # ── CRLV → extrai placa, modelo, ano, ANTT ────────────────────────────
    {'key': 'crlv_document',  'label': 'CRLV do veículo',
     'required': True,  'kind': 'file',
     'ocr_fills': ['vehicle_plate', 'vehicle_model', 'vehicle_year', 'antt_number']},
    {'key': 'vehicle_plate',  'label': 'Placa do veículo',
     'required': True,  'kind': 'text',  'skip_if_filled': True},
    {'key': 'vehicle_model',  'label': 'Modelo do veículo',
     'required': True,  'kind': 'text',  'skip_if_filled': True},
    {'key': 'vehicle_year',   'label': 'Ano do veículo',
     'required': True,  'kind': 'year',  'skip_if_filled': True},
    {'key': 'antt_number',    'label': 'Número ANTT / RNTRC',
     'required': False, 'kind': 'text',  'skip_if_filled': True},

    # ── Cavalinho (apenas carreta) ─────────────────────────────────────────
    {'key': 'cavalinho_crlv', 'label': 'CRLV do cavalinho',
     'required': False, 'kind': 'file',  'carreta_only': True,
     'ocr_fills': ['cavalinho_plate', 'cavalinho_model', 'cavalinho_year']},
    {'key': 'cavalinho_plate','label': 'Placa do cavalinho',
     'required': False, 'kind': 'text',  'carreta_only': True, 'skip_if_filled': True},
    {'key': 'cavalinho_model','label': 'Modelo do cavalinho',
     'required': False, 'kind': 'text',  'carreta_only': True, 'skip_if_filled': True},
    {'key': 'cavalinho_year', 'label': 'Ano do cavalinho',
     'required': False, 'kind': 'year',  'carreta_only': True, 'skip_if_filled': True},

    # ── Rastreador ─────────────────────────────────────────────────────────
    {'key': 'has_tracker',    'label': 'Possui rastreador?',
     'required': True,  'kind': 'bool'},
    {'key': 'tracker_type',   'label': 'Tipo/marca do rastreador',
     'required': False, 'kind': 'text'},

    # ── Dados bancários ────────────────────────────────────────────────────
    {'key': 'bank_name',      'label': 'Banco',
     'required': False, 'kind': 'text'},
    {'key': 'agency',         'label': 'Agência bancária',
     'required': False, 'kind': 'text'},
    {'key': 'account',        'label': 'Número da conta',
     'required': False, 'kind': 'text'},
    {'key': 'pix_key',        'label': 'Chave PIX',
     'required': False, 'kind': 'text'},

    # ── Foto avulsa do veículo ─────────────────────────────────────────────
    {'key': 'vehicle_photo',  'label': 'Foto do veículo',
     'required': False, 'kind': 'file'},
]

FILE_FIELDS = {f['key'] for f in FIELD_DEFS if f['kind'] == 'file'}
FIELD_MAP   = {f['key']: f for f in FIELD_DEFS}

import random

_SIGNATURE = "EMA | EMALOG"

def _strip_signature(text: str) -> str:
    """Remove 'EMA | EMALOG' from the end of an LLM reply so we control placement."""
    t = text.rstrip()
    if t.endswith(_SIGNATURE):
        t = t[:-len(_SIGNATURE)].rstrip().rstrip('\n').rstrip()
    return t

_CONFIRM_PHRASES = [
    "✅ Anotado!",
    "✅ Perfeito, registrado!",
    "✅ Tá bom, confirmado!",
    "✅ Show, guardei aqui!",
    "✅ Beleza, anotei!",
    "✅ Certo, tá salvo!",
    "✅ Ok, registrei!",
    "✅ Ótimo, confirmei aqui!",
]

_CONFIRM_FILE_PHRASES = [
    "✅ Recebi o documento, obrigada!",
    "✅ Chegou aqui, valeu!",
    "✅ Documento recebido, show!",
    "✅ Perfeito, tá salvo!",
]

def _rand_confirm() -> str:
    return random.choice(_CONFIRM_PHRASES)

def _rand_confirm_file() -> str:
    return random.choice(_CONFIRM_FILE_PHRASES)

def _ask_field_text(field_def: dict, current_val) -> str:
    """Return the question text for a given field in a varied, natural tone."""
    label = field_def['label']
    kind  = field_def['kind']
    has_val = current_val not in (None, '')

    if kind == 'file':
        if has_val:
            variants = [
                f"Agora: *{label}* — já tenho um arquivo salvo pra você. Quer mandar um novo ou tá bom o que tem? (Manda *OK* pra manter ou envie a nova foto/PDF)",
                f"*{label}*: já temos um salvo. Tá atualizado? Se quiser trocar, manda a nova foto ou PDF. Se tiver bom, manda *OK*.",
                f"Tenho o *{label}* no cadastro. Ainda é o mesmo? Manda *OK* ou envie a foto/PDF novo.",
            ]
        else:
            variants = [
                f"Agora preciso da *{label}*. Manda uma foto nítida ou o PDF — aceito os dois! 📸",
                f"Me manda a *{label}*, por favor. Pode ser foto pelo celular ou PDF! 📱",
                f"Preciso da *{label}*. Foto ou PDF, como preferir — eu cuido do resto! 📷",
            ]
        return random.choice(variants)

    display = _display_value(field_def, current_val) if has_val else None

    # Email tem aviso especial de que é opcional
    if kind == 'email':
        if has_val:
            variants = [
                f"*{label}*: tenho aqui *{display}*. Ainda é esse? (Se mudou, manda o novo. Se não tiver mais, manda *não tenho*.)",
                f"*{label}* salvo como *{display}*. Confirma? (Pode mandar *não tenho* se não usar mais.)",
            ]
        else:
            variants = [
                f"Qual é o seu *{label}*? 📧 (Não tem e-mail? Sem problema, manda *não tenho* que a gente pula!)",
                f"Me passa seu *{label}*, se tiver. Se não tiver, manda *não tenho* — não é obrigatório! 😉",
                f"Tem *{label}*? Se sim, me manda. Se não tiver, manda *não tenho* que tudo bem!",
            ]
        return random.choice(variants)

    if has_val:
        variants = [
            f"*{label}*: consta aqui como *{display}*. Ainda é esse?",
            f"Agora: *{label}* — tá salvo como *{display}*. Confirma pra mim?",
            f"*{label}* — tenho aqui *{display}*. Tá certinho isso?",
            f"Me confirma: *{label}* ainda é *{display}*?",
        ]
    else:
        variants = [
            f"Qual é o seu *{label}*?",
            f"Me passa o seu *{label}*.",
            f"Me informa o *{label}* por favor.",
        ]
    return random.choice(variants)

# OCR prompts indexed by field key
_OCR_PROMPTS = {
    'cnh_document': (
        "Você recebeu uma imagem da CNH (Carteira Nacional de Habilitação) brasileira. "
        "Extraia com precisão os seguintes dados:\n"
        "- NOME (campo '1 NOME / NAME / APELLIDOS' ou 'NOME COMPLETO') → chave: name\n"
        "- CPF (campo 'CIC/CPF', 'CPF' — 11 dígitos, formato xxx.xxx.xxx-xx) → chave: cpf\n"
        "- RG (campo '4ª CG/DN', 'RG', 'Nº RG', 'IDENTIDADE', 'DOC IDENTIDADE/CNH/PB') → chave: rg\n"
        "- DATA DE NASCIMENTO (campo '3 DATA LOCAL E UF DE NASCIMENTO', 'NASC' ou 'DATE OF BIRTH' — formato DD/MM/AAAA) → chave: birth_date\n"
        "- VENCIMENTO DA CNH (campo '2ª VALIDADE/VALID', 'VALIDADE', '1ª HAB' → busque a data de validade maior/mais recente — formato DD/MM/AAAA) → chave: cnh_expiry\n"
        "IMPORTANTE:\n"
        "- CPF tem exatamente 11 dígitos (excluindo pontos e traço)\n"
        "- Datas: converta SEMPRE para o formato YYYY-MM-DD no JSON\n"
        "- Se o documento estiver na frente e verso, leia os dois lados\n"
        "- Ignore campos de categoria, número de registro e observações\n"
        "Retorne APENAS um JSON válido, sem texto fora dele:\n"
        '{"name":"nome completo do motorista","cpf":"xxx.xxx.xxx-xx","rg":"número do RG",'
        '"birth_date":"YYYY-MM-DD","cnh_expiry":"YYYY-MM-DD"}'
    ),
    'crlv_document': (
        "Você recebeu uma imagem do CRLV (Certificado de Registro e Licenciamento de Veículo) brasileiro. "
        "Extraia os seguintes campos:\n"
        "- PLACA (campo 'PLACA', 'PLATE' — formato ABC-1234 ou ABC1D23 Mercosul) → chave: vehicle_plate\n"
        "- MARCA/MODELO (campos 'MARCA' + 'MODELO' combinados, ex: 'VOLVO/FH 460') → chave: vehicle_model\n"
        "- ANO DO MODELO (campo 'ANO MOD.' ou 'ANO MODELO' — ano de 4 dígitos) → chave: vehicle_year (número inteiro)\n"
        "- RNTRC/ANTT (campo 'RNTRC', 'ANTT', 'Nº RNTRC' — se existir) → chave: antt_number\n"
        "Placa no formato Mercosul (letras+número+letra+2números): converta para ABC-1D23.\n"
        "Retorne APENAS um JSON válido:\n"
        '{"vehicle_plate":"ABC-1234","vehicle_model":"MARCA/MODELO","vehicle_year":2020,"antt_number":null}'
    ),
    'cavalinho_crlv': (
        "Você recebeu um CRLV de unidade tratora (cavalo mecânico/cavalinho) brasileiro. "
        "Extraia:\n"
        "- PLACA → chave: cavalinho_plate\n"
        "- MARCA/MODELO → chave: cavalinho_model\n"
        "- ANO DO MODELO → chave: cavalinho_year (inteiro)\n"
        "Retorne APENAS um JSON válido:\n"
        '{"cavalinho_plate":"ABC-1234","cavalinho_model":"MARCA/MODELO","cavalinho_year":2020}'
    ),
    'address_proof': (
        "Você recebeu um comprovante de residência brasileiro (conta de água, luz, gás, telefone, "
        "internet, extrato bancário ou correspondência oficial). "
        "Extraia o endereço completo do TITULAR (não do remetente/empresa):\n"
        "- CEP (8 dígitos, pode estar como 00000-000) → chave: cep\n"
        "- LOGRADOURO (nome da rua/avenida/travessa, sem o número) → chave: street\n"
        "- NÚMERO da residência → chave: number\n"
        "- COMPLEMENTO (apto, bloco, casa — se houver, senão null) → chave: complement\n"
        "- BAIRRO → chave: neighborhood\n"
        "- CIDADE/MUNICÍPIO → chave: city\n"
        "- ESTADO (sigla UF com 2 letras, ex: SP, RJ, MG) → chave: state\n"
        "Retorne APENAS um JSON válido:\n"
        '{"cep":"00000000","street":"logradouro sem número","number":"123","complement":null,'
        '"neighborhood":"bairro","city":"cidade","state":"UF"}'
    ),
}

# Short confirmation phrases the driver might send
_CONFIRM_WORDS = {'sim', 'ok', 'ok.', 'certo', 'correto', 'isso', 'é isso', 'tá certo',
                  'ta certo', 'confirmado', 'confirmo', 'yes', 's', 'exato', 'pode ser',
                  'tudo certo', 'perfeito', 'ótimo', 'otimo', 'bom', 'pode'}


# ── Helpers ────────────────────────────────────────────────────────────────

def validate_cpf(cpf: str) -> bool:
    digits = re.sub(r'\D', '', cpf)
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    for i in range(2):
        total = sum(int(digits[j]) * (10 + i - j) for j in range(9 + i))
        expected = (total * 10 % 11) % 10
        if expected != int(digits[9 + i]):
            return False
    return True


def format_cpf(cpf: str) -> str:
    d = re.sub(r'\D', '', cpf)
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}" if len(d) == 11 else cpf


def lookup_cep(cep: str) -> dict | None:
    cep_clean = re.sub(r'\D', '', cep)
    if len(cep_clean) != 8:
        return None
    try:
        r = requests.get(f"https://viacep.com.br/ws/{cep_clean}/json/", timeout=5)
        data = r.json()
        if 'erro' in data:
            return None
        return {
            'cep':          cep_clean,
            'street':       data.get('logradouro', ''),
            'neighborhood': data.get('bairro', ''),
            'city':         data.get('localidade', ''),
            'state':        data.get('uf', ''),
        }
    except Exception:
        return None


def _is_field_applicable(field_def: dict, driver) -> bool:
    """Return False for fields that don't apply to this driver's configuration."""
    if field_def.get('carreta_only') and getattr(driver, 'truck_type', '') != 'carreta':
        return False
    if field_def['key'] == 'tracker_type' and not getattr(driver, 'has_tracker', False):
        return False
    return True


def _get_current_value(driver, key: str):
    return getattr(driver, key, None)


def _display_value(field_def: dict, value) -> str:
    """Human-readable representation of a field's current value."""
    if value is None or value == '':
        return '_(não preenchido)_'
    kind = field_def['kind']
    if kind == 'file':
        return '✅ _documento já enviado anteriormente_'
    if kind == 'bool':
        return '*Sim*' if value else '*Não*'
    if kind == 'date':
        try:
            if hasattr(value, 'strftime'):
                return f"*{value.strftime('%d/%m/%Y')}*"
        except Exception:
            pass
    return f"*{value}*"


def driver_missing_fields(driver) -> list[dict]:
    """Original mode: return only fields with no value."""
    missing = []
    for f in FIELD_DEFS:
        if not _is_field_applicable(f, driver):
            continue
        val = _get_current_value(driver, f['key'])
        if val is None or val == '':
            missing.append(f)
    return missing


def driver_fields_for_session(session, driver) -> list[dict]:
    """Return fields still to be processed for this session.

    - Normal mode  → only missing fields (skip_if_filled ignored)
    - Revalidation → ALL applicable fields starting from session.current_field,
                     but skipping fields tagged skip_if_filled that already have a value
                     (they were filled by OCR and don't need manual confirmation)
    """
    applicable = [f for f in FIELD_DEFS if _is_field_applicable(f, driver)]

    if not getattr(session, 'revalidation', False):
        # Normal mode: ask only about empty fields
        return [f for f in applicable if _get_current_value(driver, f['key']) in (None, '')]

    # Revalidation: start from current_field position, skip OCR-filled text fields
    current = session.current_field
    if not current:
        remaining = applicable
    else:
        keys = [f['key'] for f in applicable]
        try:
            idx = keys.index(current)
            remaining = applicable[idx:]
        except ValueError:
            remaining = applicable

    # Filter out skip_if_filled fields that already have a value
    return [
        f for f in remaining
        if not (f.get('skip_if_filled') and _get_current_value(driver, f['key']) not in (None, ''))
    ]


def driver_completion_pct(driver) -> int:
    required = [f for f in FIELD_DEFS if f['required'] and _is_field_applicable(f, driver)]
    if not required:
        return 100
    filled = sum(1 for f in required if _get_current_value(driver, f['key']) not in (None, ''))
    return round(filled / len(required) * 100)


# ── Session staged_data helpers ────────────────────────────────────────────

def _get_staged(session, key, default=None):
    """Safe getter for session.staged_data dict."""
    d = session.staged_data or {}
    return d.get(key, default)


def _set_staged(session, key, value):
    """Safe setter for session.staged_data dict (creates new dict to trigger SQLAlchemy change detection)."""
    from sqlalchemy.orm.attributes import flag_modified
    d = dict(session.staged_data or {})
    d[key] = value
    session.staged_data = d
    flag_modified(session, 'staged_data')


# ── Groq + Llama 3.3 70B helper ────────────────────────────────────────────
# Text: qwen/qwen3.8-27b | Vision/OCR uses the configured vision provider

_groq_client = None

def _get_groq():
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        _groq_client = Groq(api_key=os.environ.get('GROQ_API_KEY', ''))
    return _groq_client


def _groq_generate(messages: list, max_tokens: int = 1024, model: str = 'qwen/qwen3.8-27b') -> str:
    """Call Groq Qwen. Returns raw text, raises on error."""
    client = _get_groq()
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.3,
    )
    return resp.choices[0].message.content or ''


# ── System prompts ─────────────────────────────────────────────────────────

def _build_system_prompt(driver, missing: list[dict]) -> str:
    """Normal mode: enriched prompt with full flow knowledge and strict rules."""
    missing_desc = '\n'.join(
        f"  - {f['key']}: {f['label']} ({'⚠️ OBRIGATÓRIO' if f['required'] else 'opcional'})"
        for f in missing
    )
    # Compute what's already filled to give Gemini context on current phase
    filled_keys = [f['key'] for f in FIELD_DEFS if _get_current_value(driver, f['key']) not in (None, '')]
    phase_ctx = ''
    identity_fields_filled = any(
        _get_current_value(driver, k) not in (None, '')
        for k in ['cpf', 'rg', 'birth_date', 'cnh_expiry', 'name']
    )
    if 'cnh_document' not in filled_keys:
        phase_ctx = 'FASE ATUAL: 1a — Solicitar foto da CNH (frente e verso)'
    elif not identity_fields_filled:
        phase_ctx = ('FASE ATUAL: 1b — Coletar dados da CNH manualmente. '
                     'O documento foi salvo mas o OCR não leu todos os dados. '
                     'Colete: Nome, CPF, RG, Data de nascimento, Vencimento CNH — um por vez.')
    elif 'address_proof' not in filled_keys:
        phase_ctx = 'FASE ATUAL: 2 — Endereço (começar pelo Comprovante de Residência)'
    elif 'crlv_document' not in filled_keys:
        phase_ctx = 'FASE ATUAL: 3 — Veículo (tipo de caminhão → ANTT opcional → CRLV)'
    elif not any(k in filled_keys for k in ['bank_name', 'pix_key']):
        phase_ctx = 'FASE ATUAL: 4 — Dados bancários (todos opcionais, mas importantes)'
    else:
        phase_ctx = 'FASE ATUAL: próxima ao final — coletar campos restantes e confirmar'

    return f"""Você é a EMA, da equipe de cadastros da EMALOG.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOBRE A EMALOG E ESTE CADASTRO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A EMALOG conecta motoristas autônomos a fretes por todo o Brasil 🚛.
Cadastro completo = motorista recebe ofertas de frete. Dados incompletos = sem fretes.
Os dados coletados servem para: verificar identidade (CPF/CNH), localizar o motorista (CEP),
avaliar o veículo (placa/modelo/tipo), e efetuar pagamentos (dados bancários).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FLUXO COMPLETO — 5 FASES (siga ESTRITAMENTE nesta ordem)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FASE 1 · IDENTIDADE
  1a. Foto da CNH (frente+verso) → OCR extrai: Nome, CPF, RG, Nascimento, Vencimento CNH
  1b. E-mail (OPCIONAL — a maioria dos motoristas NÃO tem. Se disser "não tenho", "nao", "sem email" ou similar, aceite e AVANCE sem pedir de novo. Nunca insista.)

FASE 2 · ENDEREÇO
  2a. Comprovante de residência (conta água/luz/gás ou extrato) → OCR extrai CEP/endereço
  2b. Número do endereço + Complemento (se necessário)

FASE 3 · VEÍCULO
  3a. Tipo de caminhão (pergunta manual obrigatória ANTES do CRLV)
  3b. Documento ANTT/RNTRC (opcional)
  3c. CRLV do veículo → OCR extrai: Placa, Modelo, Ano
  3d. Se carreta: CRLV do cavalinho também
  3e. Rastreador? (sim/não) → se sim: qual marca/tipo?

FASE 4 · DADOS BANCÁRIOS (todos opcionais)
  Banco, Agência, Conta corrente, Chave PIX

FASE 5 · CONFIRMAÇÃO
  Sistema mostra resumo completo → motorista confirma com "OK"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REGRAS CRÍTICAS — NUNCA QUEBRE ESTAS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. FLUXO: fotos e texto se alternam naturalmente. Documentos (CNH, CRLV etc.) → OCR extrai os dados.
   Tipo de caminhão, rastreador, e-mail e dados bancários → motorista responde em texto, não por foto.
2. DOCUMENTOS (CNH, CRLV, comprovante, cavalinho):
   • OCR leu ALGO (parcial) → aceite o documento, use o que foi lido e avance para o próximo campo.
   • OCR não leu NADA → peça uma foto mais nítida UMA VEZ. Se falhar de novo, aceite e avance.
   • NUNCA peça em texto campos que devem vir de documento (Nome, CPF, RG, Endereço, Placa, etc.).
     Esses campos ficam vazios se o OCR não conseguiu — o admin preenche depois pelo sistema.
3. CAMPOS OBRIGATÓRIOS respondidos PELO MOTORISTA EM TEXTO (não vêm de foto):
   Tipo de caminhão, Possui rastreador (sim/não), E-mail (opcional), Dados bancários (opcionais).
4. CPF: O sistema já validou matematicamente. Se o motorista enviou 11 dígitos, ACEITE e coloque em updates formatado como XXX.XXX.XXX-XX. Nunca rejeite CPF recebido do motorista — a validação é feita pelo sistema, não por você.
5. DATAS: converta para YYYY-MM-DD. Data inválida → rejeite e peça novamente.
6. CEP: 8 dígitos em "updates". O sistema preenche o endereço via ViaCEP automaticamente.
7. UMA informação por mensagem. Nunca pergunte dois campos ao mesmo tempo.
8. Motorista fora de contexto → responda em 1 linha e volte ao campo atual imediatamente.
9. NÃO se apresente como robô/IA. NÃO assine as mensagens.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MOTORISTA E SITUAÇÃO ATUAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Motorista: {driver.name} | Tel: {driver.phone}
{phase_ctx}

Campos ainda pendentes (colete nesta ordem):
{missing_desc}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMPORTAMENTO E TOM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Tom: WhatsApp entre colegas. Informal, direto, sem enrolação.
- Use: tá, beleza, show, ótimo, tranquilo, me passa, me manda, valeu, pode ser
- Evite: senhor/senhora, "prezado", termos jurídicos, frases longas
- Emojis: ✅ confirmação | 📸 foto | ⚠️ erro/atenção | 🚛 frete (use com moderação)
- Máximo 4 linhas por mensagem.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FORMATO DE RESPOSTA (OBRIGATÓRIO)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Responda SEMPRE com JSON válido, sem markdown, sem texto fora do JSON:
{{
  "reply": "mensagem curta e direta ao motorista",
  "updates": {{}},
  "step_complete": false
}}
"updates" → apenas campos validados e prontos para salvar no banco.
"step_complete": true → quando o campo atual foi respondido corretamente.
"""


def _build_revalidation_prompt(driver, field_def: dict, current_value) -> str:
    """Revalidation mode: one field at a time, full context, strict completion rules."""
    key   = field_def['key']
    label = field_def['label']
    kind  = field_def['kind']
    value_display = _display_value(field_def, current_value)
    has_value = current_value not in (None, '')
    required  = field_def.get('required', False)

    # Field-specific validation rules
    rules = []
    if kind == 'cpf':
        rules.append('- CPF: o sistema já validou matematicamente. ACEITE o número e coloque em updates formatado como XXX.XXX.XXX-XX. Nunca rejeite o CPF — a validação é do sistema.')
    elif kind == 'date':
        rules.append('- Data: converta para YYYY-MM-DD. Ex: 15/03/1985 → "1985-03-15". Data inválida ou impossível → rejeite e peça novamente.')
    elif kind == 'cep':
        rules.append('- CEP: somente os 8 dígitos em updates. O sistema preenche endereço completo via ViaCEP automaticamente. CEP inválido → rejeite.')
    elif kind == 'bool':
        rules.append('- Resposta sim/não: use true ou false em updates. Se resposta ambígua, pergunte de novo.')
    elif kind == 'choice':
        rules.append(f'- Tipo de caminhão: as únicas opções válidas são {", ".join(TRUCK_TYPES)}. Liste as opções ao perguntar. Resposta fora da lista → pergunte novamente.')
    elif kind == 'year':
        rules.append('- Ano: número inteiro entre 1970 e 2030. Ano fora desse intervalo → rejeite e peça novamente.')
    elif kind == 'file':
        if has_value:
            rules.append('- Documento já salvo. Pergunte se quer atualizar ou manter o atual.')
            rules.append('- Confirmar (ok/sim/manter/tá bom) → step_complete: true, updates: {}')
            rules.append('- Enviar nova foto → trate como novo envio (o sistema cuida do upload).')
        else:
            rules.append('- Campo ainda sem documento. Solicite a foto/arquivo.')
            rules.append('- Enquanto não enviar a foto: step_complete: false.')
            if required:
                rules.append('- ⚠️ Campo OBRIGATÓRIO — não avance sem este documento.')

    # Confirmation instruction based on whether value exists
    if has_value and kind != 'file':
        importance = '⚠️ Campo obrigatório — ' if required else ''
        confirm_instruction = f"""{importance}Campo já tem valor. Mostre e peça confirmação.
- Motorista confirmar (sim, ok, isso, certo, beleza, show, correto, tá bom, pode) → MANTENHA: updates: {{"{key}": {json.dumps(str(current_value) if not isinstance(current_value, bool) else current_value)}}}, step_complete: true
- Motorista corrigir → valide o novo valor conforme regras do campo e salve, step_complete: true
- Dado novo inválido → explique o erro e peça novamente, step_complete: false"""
    else:
        req_note = '⚠️ Campo OBRIGATÓRIO — não avance sem coletar. ' if required else ''
        confirm_instruction = f'{req_note}Campo sem valor. Colete do motorista.'

    rules_text = '\n'.join(rules) if rules else '- Nenhuma validação especial. Salve o texto como está.'
    first_name = driver.name.split()[0]

    return f"""Você é a EMA, da equipe da EMALOG — plataforma que conecta motoristas autônomos a fretes no Brasil.
Está atualizando o cadastro de {first_name} pelo WhatsApp, um campo por vez.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONTEXTO DO NEGÓCIO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Cada campo coletado é essencial para que {first_name} receba ofertas de frete.
Dados incorretos causam problemas de pagamento, documentação ou segurança.
Por isso: NÃO avance sem o campo atual estar correto e completo.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CAMPO ATUAL: {label}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Valor no cadastro: {value_display}
{confirm_instruction}

Regras de validação deste campo:
{rules_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REGRAS GERAIS (sempre válidas)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- UMA informação por vez. Não mencione outros campos.
- Mensagens curtas — máximo 4 linhas.
- Dado inválido: explique o erro em 1 frase simples e peça novamente.
- Motorista fora do assunto: responda em 1 linha e volte ao campo imediatamente.
- Nunca pule campos obrigatórios mesmo que o motorista peça.
- NÃO se apresente como robô/IA. NÃO assine as mensagens.
- Tom: WhatsApp entre conhecidos. Use: tá, beleza, show, me passa, me manda.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FORMATO DE RESPOSTA (OBRIGATÓRIO)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Responda SEMPRE com JSON válido, sem markdown, sem texto fora do JSON:
{{
  "reply": "mensagem direta ao motorista",
  "updates": {{}},
  "step_complete": false
}}
"updates" → somente campos validados prontos para salvar.
"step_complete": true → APENAS quando o campo atual está correto e completo.
"""


# ── Driver summary builder ─────────────────────────────────────────────────

def _build_driver_summary(driver) -> str:
    """Build a human-readable WhatsApp summary of all driver data for final review."""
    from datetime import date as date_type

    def fmt(val):
        if val is None or val == '':
            return '_(não preenchido)_'
        if isinstance(val, date_type):
            return val.strftime('%d/%m/%Y')
        if isinstance(val, bool):
            return 'Sim' if val else 'Não'
        return str(val)

    lines = ['📋 *RESUMO DO SEU CADASTRO*\n']

    lines.append('*Dados Pessoais*')
    lines.append(f'• Nome: {fmt(driver.name)}')
    lines.append(f'• CPF: {fmt(driver.cpf)}')
    lines.append(f'• RG: {fmt(driver.rg)}')
    lines.append(f'• Nascimento: {fmt(driver.birth_date)}')
    lines.append(f'• E-mail: {fmt(driver.email)}')
    lines.append(f'• Vencimento CNH: {fmt(driver.cnh_expiry)}')
    lines.append('')

    lines.append('*Endereço*')
    addr = driver.street or ''
    if getattr(driver, 'number', None):
        addr += f', {driver.number}'
    if getattr(driver, 'complement', None):
        addr += f' ({driver.complement})'
    lines.append(f'• CEP: {fmt(driver.cep)}')
    lines.append(f'• Endereço: {addr or "_(não preenchido)_"}')
    lines.append(f'• Bairro: {fmt(driver.neighborhood)}')
    city_state = f'{driver.city} / {driver.state}' if driver.city and driver.state else fmt(driver.city or driver.state)
    lines.append(f'• Cidade: {city_state}')
    lines.append('')

    lines.append('*Veículo*')
    lines.append(f'• Tipo: {fmt(driver.truck_type)}')
    lines.append(f'• Placa: {fmt(driver.vehicle_plate)}')
    lines.append(f'• Modelo: {fmt(driver.vehicle_model)}')
    lines.append(f'• Ano: {fmt(driver.vehicle_year)}')
    has_tracker = getattr(driver, 'has_tracker', None)
    lines.append(f'• Rastreador: {"Sim" if has_tracker else "Não" if has_tracker is False else "_(não preenchido)_"}')
    if has_tracker and getattr(driver, 'tracker_type', None):
        lines.append(f'• Tipo rastreador: {driver.tracker_type}')
    lines.append(f'• ANTT/RNTRC: {fmt(getattr(driver, "antt_number", None))}')

    if getattr(driver, 'cavalinho_plate', None):
        lines.append('')
        lines.append('*Cavalo Mecânico*')
        lines.append(f'• Placa: {fmt(driver.cavalinho_plate)}')
        lines.append(f'• Modelo: {fmt(getattr(driver, "cavalinho_model", None))}')
        lines.append(f'• Ano: {fmt(getattr(driver, "cavalinho_year", None))}')

    bank_fields = [driver.bank_name, getattr(driver, 'agency', None),
                   getattr(driver, 'account', None), driver.pix_key]
    if any(bank_fields):
        lines.append('')
        lines.append('*Dados Bancários*')
        if driver.bank_name:                      lines.append(f'• Banco: {driver.bank_name}')
        if getattr(driver, 'agency', None):       lines.append(f'• Agência: {driver.agency}')
        if getattr(driver, 'account', None):      lines.append(f'• Conta: {driver.account}')
        if driver.pix_key:                        lines.append(f'• PIX: {driver.pix_key}')

    lines.append('')
    lines.append('*Documentos*')
    for fkey, label in [('cnh_document', 'CNH'), ('address_proof', 'Comprovante de residência'),
                         ('crlv_document', 'CRLV'), ('vehicle_photo', 'Foto do veículo')]:
        val = getattr(driver, fkey, None)
        lines.append(f'• {label}: {"✅ recebido" if val else "⚠️ pendente"}')

    return '\n'.join(lines)


def _request_final_confirmation(session, driver) -> str:
    """Show full data summary and ask driver to confirm before finalizing."""
    first = driver.name.split()[0]
    summary = _build_driver_summary(driver)
    msg = (f"Quase lá, {first}! 🎉 Dá uma olhada no resumo do seu cadastro:\n\n"
           f"{summary}\n\n"
           f"Tá tudo certinho? Manda *OK* pra confirmar.\n"
           f"Se precisar corrigir alguma coisa, me fala qual!\n\n"
           f"{_SIGNATURE}")
    session.status        = 'awaiting_confirmation'
    session.current_field = None
    _append_history(session, 'ema', msg)
    return msg


def _handle_final_confirmation(session, driver, text: str) -> str:
    """Handle driver response to the final summary (awaiting_confirmation state)."""
    first = driver.name.split()[0]
    if text.strip().lower() in _CONFIRM_WORDS:
        session.status       = 'completed'
        session.completed_at = datetime.utcnow()
        reply = (
            f"🙌 Fechou, {first}!\n\n"
            f"Seu cadastro tá *completo e confirmado* aqui no sistema. "
            f"Nossa equipe já foi avisada e vai dar uma revisadinha.\n\n"
            f"A partir daí você começa a receber *ofertas de frete* da EMALOG direto aqui no WhatsApp. "
            f"Fique de olho! 🚛💨\n\n"
            f"Qualquer dúvida é só chamar. Valeu, {first}, e bem-vindo à frota! 💪\n\n"
            f"{_SIGNATURE}"
        )
        _append_history(session, 'driver', text)
        _append_history(session, 'ema', reply)
        # Emit real-time notification to operators via SocketIO
        try:
            from app import socketio
            socketio.emit('ema_completed_notification', {
                'driver_id':   driver.id,
                'driver_name': driver.name,
                'session_id':  session.id,
                'completed_at': datetime.utcnow().strftime('%d/%m/%Y %H:%M'),
                'message': f'Cadastro de {driver.name} concluído — aguardando validação do operador',
            }, room='operators')
        except Exception as ex:
            logger.warning(f"[EMA] Falha ao notificar operadores: {ex}")
        return reply
    else:
        # Driver wants to change something — resume field-by-field
        applicable = [f for f in FIELD_DEFS if _is_field_applicable(f, driver)]
        session.status        = 'active'
        session.current_field = applicable[0]['key'] if applicable else None
        reply = (f"Tranquilo! Me fala o que tá errado que eu corrijo na hora. 👍\n\n{_SIGNATURE}")
        _append_history(session, 'driver', text)
        _append_history(session, 'ema', reply)
        return reply


# ── Main processing function ───────────────────────────────────────────────

def process_message(session, driver, text: str, media_path: str | None = None) -> str:
    """Process an incoming driver message. Updates session and driver in-place.
    Returns the reply string. Does NOT commit to DB — caller must do db.session.commit().
    """
    # Handle final confirmation state first, regardless of mode
    if session.status == 'awaiting_confirmation':
        return _handle_final_confirmation(session, driver, text)

    revalidation = getattr(session, 'revalidation', False)

    if revalidation:
        return _process_revalidation(session, driver, text, media_path)
    else:
        return _process_normal(session, driver, text, media_path)


# ── Normal mode (original logic) ───────────────────────────────────────────

_MEDIA_SENT_MARKERS = {'[arquivo enviado]', '[imagem enviada]', '[documento enviado]',
                       '[vídeo enviado]', '[video enviado]'}

def _process_normal(session, driver, text: str, media_path: str | None) -> str:
    missing = driver_missing_fields(driver)
    if not missing:
        session.status = 'completed'
        session.completed_at = datetime.utcnow()
        return ("✅ Seu cadastro já está completo! Obrigado, "
                f"{driver.name.split()[0]}. Qualquer dúvida, entre em contato com a equipe EMALOG. 🚛")

    if media_path:
        return _handle_media_file(session, driver, media_path, missing)

    # If the driver sent a file but download failed — ask to resend (don't process as text)
    if text.strip().lower() in _MEDIA_SENT_MARKERS:
        reply = ("⚠️ Recebi que você enviou um arquivo, mas não consegui baixar. "
                 "Pode mandar de novo a foto? 📸\n\nEMA | EMALOG")
        _append_history(session, 'driver', text)
        _append_history(session, 'ema', reply)
        return reply

    # ── Manual collection queue (after OCR failure) ─────────────────────────
    manual_queue = [k for k in list(_get_staged(session, 'manual_collect') or [])
                    if _get_current_value(driver, k) in (None, '')]
    if manual_queue:
        return _process_manual_collect(session, driver, text, manual_queue)
    # ──────────────────────────────────────────────────────────────────────

    # ── Python pre-check: validate CPF before LLM sees it ──────────────────
    first_missing_key = missing[0]['key'] if missing else None
    if first_missing_key == 'cpf':
        cpf_candidate = re.sub(r'\D', '', text)
        if len(cpf_candidate) == 11:
            if validate_cpf(cpf_candidate):
                formatted = format_cpf(cpf_candidate)
                # Check for conflict before applying
                from models import Driver as _DrCheck
                conflict = _DrCheck.query.filter(
                    _DrCheck.cpf == formatted,
                    _DrCheck.id  != driver.id
                ).first()
                if conflict:
                    logger.warning(
                        f"[EMA] CPF {formatted} conflito manual: era do motorista ID={conflict.id} "
                        f"({conflict.name}) — transferido para driver {driver.id} ({driver.name})"
                    )
                    conflict.cpf = None
                driver.cpf = formatted
                _sync_session_field(session, driver)
                reply = f"✅ CPF *{formatted}* confirmado! Agora me passa a sua data de nascimento (DD/MM/AAAA)."
                _append_history(session, 'driver', text)
                _append_history(session, 'ema', reply)
                return reply
            else:
                reply = ("⚠️ Esse CPF não passou na validação. Pode conferir os números e mandar novamente? "
                         "Precisa ter 11 dígitos certinhos.")
                _append_history(session, 'driver', text)
                _append_history(session, 'ema', reply)
                return reply
    # ──────────────────────────────────────────────────────────────────────

    system_prompt = _build_system_prompt(driver, missing)
    history_text  = _format_history(session.history or [])
    user_content  = f"Histórico:\n{history_text}\n\nMensagem do motorista agora:\n{text}" if history_text else text

    reply, updates, _ = _call_gemini(system_prompt, user_content)
    _apply_updates(driver, updates, reply_ref=[])
    _sync_session_field(session, driver)

    # If all fields just got filled, override the reply with the final summary
    if session.status == 'completed':
        _append_history(session, 'driver', text)
        _append_history(session, 'ema', reply)
        return reply + '\n\n' + _request_final_confirmation(session, driver)

    _append_history(session, 'driver', text)
    _append_history(session, 'ema', reply)
    return reply


# ── Revalidation mode ──────────────────────────────────────────────────────

def _process_revalidation(session, driver, text: str, media_path: str | None) -> str:
    """Field-by-field revalidation: confirm or update every driver field."""
    remaining = driver_fields_for_session(session, driver)

    if not remaining:
        return _request_final_confirmation(session, driver)

    field_def     = remaining[0]
    key           = field_def['key']
    kind          = field_def['kind']
    current_value = _get_current_value(driver, key)
    has_value     = current_value not in (None, '')

    # ── File fields ────────────────────────────────────────────────────────
    if kind == 'file':
        # File sent but download failed — ask driver to resend instead of confusing the LLM
        if not media_path and text.strip().lower() in _MEDIA_SENT_MARKERS:
            reply = ("⚠️ Recebi que você enviou um arquivo, mas não consegui baixar. "
                     "Pode mandar de novo a foto? 📸\n\nEMA | EMALOG")
            _append_history(session, 'driver', text)
            _append_history(session, 'ema', reply)
            return reply

        if media_path:
            # Save + OCR extraction
            reply, ocr_ok, missing_req = _save_and_ocr(session, driver, media_path, field_def)

            # Only retry (ask for clearer photo) if OCR extracted NOTHING.
            # If it extracted SOME data (ocr_ok=True), accept and advance.
            if missing_req and not ocr_ok:
                reply, should_advance = _handle_ocr_failure(
                    session, driver, field_def, missing_req, reply)
                if not should_advance:
                    # Stay on this document — ask driver to resend clearer photo
                    full_reply = f"{_strip_signature(reply)}\n\n{_SIGNATURE}"
                    _append_history(session, 'driver', f'[{field_def["label"]} enviado — OCR incompleto]')
                    _append_history(session, 'ema', full_reply)
                    return full_reply

            # OCR ok or retry exhausted — advance to next field
            _advance_revalidation(session, driver, remaining)
            next_remaining = driver_fields_for_session(session, driver)
            if next_remaining:
                nf        = next_remaining[0]
                nval_next = _get_current_value(driver, nf['key'])
                next_q    = f"\n\n{_ask_field_text(nf, nval_next)}\n\n{_SIGNATURE}"
                reply     = _strip_signature(reply) + next_q
            else:
                # All fields done — show summary for final confirmation
                _append_history(session, 'driver', f'[{field_def["label"]} enviado]')
                _append_history(session, 'ema', _strip_signature(reply))
                return _strip_signature(reply) + '\n\n' + _request_final_confirmation(session, driver)
            _append_history(session, 'driver', f'[{field_def["label"]} enviado]')
            _append_history(session, 'ema', reply)
            return reply

        # Text confirm — driver wants to keep existing document
        if has_value and text.strip().lower() in _CONFIRM_WORDS:
            return _ask_next_field(session, driver, remaining, confirmed_label=field_def['label'])

        # Build prompt and call Gemini to handle the file field conversation
        system_prompt = _build_revalidation_prompt(driver, field_def, current_value)
        history_text  = _format_history((session.history or [])[-10:])
        user_content  = f"Histórico recente:\n{history_text}\n\nMensagem do motorista:\n{text}" if history_text else text
        reply, updates, step_complete = _call_gemini(system_prompt, user_content)
        reply = _strip_signature(reply)

        if step_complete and not updates:
            # Gemini confirmed keeping existing — advance and ask next
            _advance_revalidation(session, driver, remaining)
            next_remaining = driver_fields_for_session(session, driver)
            if next_remaining:
                nf   = next_remaining[0]
                nval = _get_current_value(driver, nf['key'])
                reply += f"\n\n{_ask_field_text(nf, nval)}\n\n{_SIGNATURE}"
            else:
                reply += f"\n\n{_SIGNATURE}"

        _append_history(session, 'driver', text)
        _append_history(session, 'ema', reply)
        return reply

    # ── Non-file fields ────────────────────────────────────────────────────
    system_prompt = _build_revalidation_prompt(driver, field_def, current_value)
    history_text  = _format_history((session.history or [])[-10:])
    user_content  = f"Histórico recente:\n{history_text}\n\nMensagem do motorista:\n{text}" if history_text else text

    extra_reply_parts = []
    reply, updates, step_complete = _call_gemini(system_prompt, user_content)
    reply = _strip_signature(reply)

    if step_complete or updates:
        _apply_updates(driver, updates, reply_ref=extra_reply_parts)

        # Special: after cep update, show address confirmation in reply
        if 'cep' in updates and extra_reply_parts:
            reply = reply + '\n' + '\n'.join(extra_reply_parts)

        # Advance to next field
        _advance_revalidation(session, driver, remaining)

        # Immediately append the next field question so the conversation keeps going
        next_remaining = driver_fields_for_session(session, driver)
        if next_remaining:
            next_f   = next_remaining[0]
            next_val = _get_current_value(driver, next_f['key'])
            reply = reply + f"\n\n{_ask_field_text(next_f, next_val)}\n\n{_SIGNATURE}"
        else:
            # All fields done — show summary for final confirmation
            _append_history(session, 'driver', text)
            _append_history(session, 'ema', reply)
            return reply + '\n\n' + _request_final_confirmation(session, driver)
    else:
        reply = reply + f"\n\n{_SIGNATURE}"

    _append_history(session, 'driver', text)
    _append_history(session, 'ema', reply)
    return reply


def _advance_revalidation(session, driver, current_remaining: list[dict]):
    """Move session.current_field to the next field after the current one."""
    if len(current_remaining) <= 1:
        # All done
        session.current_field = None
        session.status        = 'completed'
        session.completed_at  = datetime.utcnow()
        return

    next_field = current_remaining[1]

    # Re-compute applicable fields for potentially changed driver (e.g. truck_type changed)
    applicable = [f for f in FIELD_DEFS if _is_field_applicable(f, driver)]
    applicable_keys = [f['key'] for f in applicable]

    # Find next_field in applicable list
    if next_field['key'] in applicable_keys:
        session.current_field = next_field['key']
    else:
        # Skip non-applicable field — find the next applicable one after current
        current_idx = applicable_keys.index(current_remaining[0]['key']) if current_remaining[0]['key'] in applicable_keys else -1
        remaining_applicable = [f for f in applicable if applicable_keys.index(f['key']) > current_idx]
        session.current_field = remaining_applicable[0]['key'] if remaining_applicable else None

    session.status = 'active' if session.current_field else 'completed'
    if not session.current_field:
        session.completed_at = datetime.utcnow()


def _ask_next_field(session, driver, current_remaining: list[dict], confirmed_label: str = '') -> str:
    """Advance to next field and return a message asking about it."""
    _advance_revalidation(session, driver, current_remaining)

    # Get updated remaining
    updated_remaining = driver_fields_for_session(session, driver)
    if not updated_remaining:
        # All revalidation fields confirmed — go to final summary
        return _request_final_confirmation(session, driver)

    next_f   = updated_remaining[0]
    next_val = _get_current_value(driver, next_f['key'])
    confirmed_part = f"{_rand_confirm()} " if confirmed_label else ''
    reply = f"{confirmed_part}{_ask_field_text(next_f, next_val)}\n\n{_SIGNATURE}"
    _append_history(session, 'ema', reply)
    return reply


def _handle_ocr_failure(session, driver, field_def: dict, missing_req_keys: list, base_msg: str) -> tuple[str, bool]:
    """Handle OCR failure for a document that should fill required fields.

    Returns (reply_message, should_advance):
    - 1st failure (nothing extracted): ask for a clearer photo → should_advance=False
    - 2nd failure: accept document and advance — NEVER ask manually for document-fillable fields
    """
    field_key = field_def['key']
    retries   = dict(_get_staged(session, 'ocr_retries') or {})
    attempt   = retries.get(field_key, 0)

    if attempt == 0 and missing_req_keys:
        # First OCR failure — ask for a better photo
        retries[field_key] = 1
        _set_staged(session, 'ocr_retries', retries)
        labels = ', '.join(FIELD_MAP[k]['label'] for k in missing_req_keys[:2])
        extra  = f' e mais {len(missing_req_keys) - 2} dado(s)' if len(missing_req_keys) > 2 else ''
        reply = (f"{base_msg}\n\n"
                 f"⚠️ Não consegui ler bem o documento — precisava de: *{labels}{extra}*.\n\n"
                 f"Pode mandar a foto *mais nítida*, de frente, com boa iluminação? 📸\n"
                 f"PDF também funciona!")
        return reply, False  # Don't advance — wait for retransmission

    # Second failure — accept document and advance without asking manually.
    # Fields that come from documents (ocr_fills) are never collected via text input.
    return base_msg, True


def _handle_media_file(session, driver, media_path: str, pending_fields: list[dict]) -> str:
    """Handle a received media file in normal mode."""
    current_file_field = next((f for f in pending_fields if f['kind'] == 'file'), None)
    if not current_file_field:
        reply = f"{_rand_confirm_file()} Continuando com as informações... 😊\n\n{_SIGNATURE}"
        _append_history(session, 'driver', '[arquivo recebido]')
        _append_history(session, 'ema', reply)
        return reply

    # Save + run OCR
    ocr_msg, ocr_ok, missing_req = _save_and_ocr(session, driver, media_path, current_file_field)

    # Only retry (ask for clearer photo) if OCR extracted NOTHING at all.
    # Partial extraction (ocr_ok=True but some fields missing) → accept and advance.
    # NEVER add document-fillable fields (ocr_fills) to manual_collect.
    if missing_req and not ocr_ok:
        ocr_msg, should_advance = _handle_ocr_failure(session, driver, current_file_field, missing_req, ocr_msg)
        if not should_advance:
            # First retry — ask for a clearer photo
            _append_history(session, 'driver', f'[{current_file_field["label"]} enviado — OCR incompleto]')
            full_reply = f"{_strip_signature(ocr_msg)}\n\n{_SIGNATURE}"
            _append_history(session, 'ema', full_reply)
            return full_reply
        # 2nd failure → fall through and advance to next field

    # OCR done — clear any stale manual queue for this document's fills
    if current_file_field.get('ocr_fills'):
        manual = [k for k in list(_get_staged(session, 'manual_collect') or [])
                  if k not in current_file_field['ocr_fills']]
        _set_staged(session, 'manual_collect', manual)

    # Compute next field — skip document-fillable fields (skip_if_filled=True)
    # so we never ask manually for CPF, RG, address, plate, etc.
    all_missing = driver_missing_fields(driver)
    remaining   = [f for f in all_missing if not f.get('skip_if_filled')]
    next_field  = remaining[0] if remaining else None

    if next_field:
        next_val = _get_current_value(driver, next_field['key'])
        follow   = f"{_ask_field_text(next_field, next_val)}\n\n{_SIGNATURE}"
        reply    = f"{_strip_signature(ocr_msg)}\n\n{follow}"
    else:
        _append_history(session, 'driver', f'[{current_file_field["label"]} enviado]')
        _append_history(session, 'ema', ocr_msg)
        return ocr_msg + '\n\n' + _request_final_confirmation(session, driver)

    _append_history(session, 'driver', f'[{current_file_field["label"]} enviado]')
    _append_history(session, 'ema', reply)
    return reply


def _process_manual_collect(session, driver, text: str, manual_queue: list) -> str:
    """Handle text input for manual field-by-field collection (after OCR failure).
    Validates each field in Python and advances the queue reliably.
    """
    # Filter out already-filled fields
    manual_queue = [k for k in manual_queue if _get_current_value(driver, k) in (None, '')]
    if not manual_queue:
        _set_staged(session, 'manual_collect', [])
        # Re-enter normal flow
        missing = driver_missing_fields(driver)
        if missing:
            next_f   = missing[0]
            next_val = _get_current_value(driver, next_f['key'])
            reply    = _ask_field_text(next_f, next_val) + f"\n\n{_SIGNATURE}"
            _append_history(session, 'driver', text)
            _append_history(session, 'ema', reply)
            return reply
        return _request_final_confirmation(session, driver)

    current_key = manual_queue[0]
    field_def   = FIELD_MAP.get(current_key)
    if not field_def:
        _set_staged(session, 'manual_collect', manual_queue[1:])
        return _process_manual_collect(session, driver, text, manual_queue[1:])

    kind    = field_def['kind']
    label   = field_def['label']
    value   = text.strip()
    applied = False
    error_reply = ''

    if kind == 'cpf':
        cleaned = re.sub(r'\D', '', value)
        if len(cleaned) == 11 and validate_cpf(cleaned):
            formatted_cpf = format_cpf(cleaned)
            from models import Driver as _DrM
            conflict = _DrM.query.filter(
                _DrM.cpf == formatted_cpf,
                _DrM.id  != driver.id
            ).first()
            if conflict:
                logger.warning(
                    f"[EMA manual] CPF {formatted_cpf} conflito: era do motorista ID={conflict.id} "
                    f"({conflict.name}) — transferido para driver {driver.id} ({driver.name})"
                )
                conflict.cpf = None
            driver.cpf = formatted_cpf
            applied    = True
        else:
            error_reply = f"⚠️ CPF inválido. Me manda os 11 dígitos (com ou sem pontos), tá?"

    elif kind == 'date':
        parsed_date = None
        for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d', '%d/%m/%y'):
            try:
                parsed_date = datetime.strptime(value, fmt).date()
                break
            except ValueError:
                pass
        if parsed_date:
            setattr(driver, current_key, parsed_date)
            applied = True
        else:
            error_reply = f"⚠️ Data inválida. Me manda no formato *DD/MM/AAAA*, ex: 15/03/1985."

    elif kind == 'cep':
        cleaned = re.sub(r'\D', '', value)
        if len(cleaned) == 8:
            addr = lookup_cep(cleaned)
            if addr:
                driver.cep          = addr['cep']
                driver.street       = addr['street']
                driver.neighborhood = addr['neighborhood']
                driver.city         = addr['city']
                driver.state        = addr['state']
            else:
                driver.cep = cleaned
            applied = True
        else:
            error_reply = "⚠️ CEP inválido. Me manda os 8 dígitos, ex: 01310100."

    elif kind == 'year':
        try:
            yr = int(re.sub(r'\D', '', value))
            if 1970 <= yr <= 2030:
                setattr(driver, current_key, yr)
                applied = True
            else:
                error_reply = "⚠️ Ano fora do intervalo. Manda entre 1970 e 2030."
        except (ValueError, TypeError):
            error_reply = f"⚠️ Ano inválido. Me manda só o número, ex: 2018."

    elif kind == 'choice':
        norm = value.lower().strip()
        if norm in TRUCK_TYPES:
            driver.truck_type = norm
            applied = True
        else:
            error_reply = f"⚠️ Tipo inválido. Opções: {', '.join(TRUCK_TYPES)}."

    elif kind == 'bool':
        norm = value.lower().strip()
        if norm in ('sim', 's', 'yes', 'y', '1', 'true'):
            setattr(driver, current_key, True)
            applied = True
        elif norm in ('não', 'nao', 'n', 'no', '0', 'false'):
            setattr(driver, current_key, False)
            applied = True
        else:
            error_reply = f"Me responde *Sim* ou *Não* pra mim? 😊"

    elif kind == 'email':
        # Email é opcional — aceita "não tenho" / "nao" / "sem email"
        _sem_email = {'não tenho', 'nao tenho', 'nao', 'não', 'n', 'sem email', 'nenhum', '-', 'pular', 'skip'}
        if value.lower().strip() in _sem_email:
            # Deixa campo vazio — sem erro, avança
            applied = True
        elif '@' in value and '.' in value.split('@')[-1]:
            driver.email = value.lower().strip()
            applied = True
        else:
            error_reply = (
                "⚠️ E-mail inválido. Me manda um e-mail correto (ex: nome@gmail.com) "
                "ou manda *não tenho* se não tiver — não é obrigatório! 😊"
            )

    else:
        # Generic text
        if value:
            setattr(driver, current_key, value)
            applied = True
        else:
            error_reply = f"Me manda o *{label}*, por favor."

    _append_history(session, 'driver', text)

    if error_reply:
        _append_history(session, 'ema', error_reply)
        return error_reply

    # Advance queue — skip already-filled fields
    remaining_queue = [k for k in manual_queue[1:] if _get_current_value(driver, k) in (None, '')]
    _set_staged(session, 'manual_collect', remaining_queue)

    if remaining_queue:
        next_key   = remaining_queue[0]
        next_def   = FIELD_MAP.get(next_key, {})
        next_label = next_def.get('label', next_key)
        reply = f"{_rand_confirm()} Agora: qual é o seu *{next_label}*?\n\n{_SIGNATURE}"
    else:
        # All manual fields done — check remaining via normal flow
        missing = driver_missing_fields(driver)
        if missing:
            nf  = missing[0]
            nv  = _get_current_value(driver, nf['key'])
            reply = f"{_rand_confirm()} {_ask_field_text(nf, nv)}\n\n{_SIGNATURE}"
        else:
            reply = f"{_rand_confirm()} Dados completos! Vou confirmar tudo com você.\n\n{_SIGNATURE}"

    _append_history(session, 'ema', reply)
    return reply


def _save_media_to_field(session, driver, media_path: str, field_def: dict) -> tuple[str, str]:
    """Save a received media file to the driver record.
    Deletes the previous file for this field (if any) before saving the new one.
    Returns (confirmation_message, saved_dest_path)."""
    import shutil, uuid
    from flask import current_app
    root = current_app.root_path

    # Delete the old file for this field before replacing it
    old_rel = getattr(driver, field_def['key'], None)
    if old_rel:
        old_abs = os.path.join(root, 'static', old_rel)
        try:
            if os.path.isfile(old_abs):
                os.remove(old_abs)
                logger.info(f"[EMA] Arquivo anterior removido: {old_abs}")
        except Exception as exc:
            logger.warning(f"[EMA] Não foi possível remover arquivo antigo {old_abs}: {exc}")

    ext       = os.path.splitext(media_path)[1] or '.jpg'
    dest_name = f"driver_{driver.id}_{field_def['key']}_{uuid.uuid4().hex[:8]}{ext}"
    dest_dir  = os.path.join(root, 'static', 'uploads', 'drivers')
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, dest_name)
    shutil.move(media_path, dest_path)
    rel_path  = os.path.join('uploads', 'drivers', dest_name)
    setattr(driver, field_def['key'], rel_path)
    return f"✅ *{field_def['label']}* recebido!", dest_path


def _pdf_to_jpeg_bytes(pdf_path: str) -> bytes | None:
    """Convert first page of a PDF to JPEG bytes using PyMuPDF.
    Returns None if PyMuPDF is unavailable or conversion fails.
    """
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        if doc.page_count == 0:
            return None
        page = doc[0]
        mat  = fitz.Matrix(3.0, 3.0)  # 3x zoom for better OCR quality
        pix  = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
        return pix.tobytes("jpeg")
    except Exception as exc:
        logger.warning(f"[EMA OCR] PDF→JPEG falhou: {exc}")
        return None


def _extract_from_image(image_path: str, doc_key: str) -> dict:
    """Use Gemini Vision to extract structured data from a document photo.
    Returns a dict of field_key→raw_value (only non-null fields).
    For PDFs, first renders page-1 to JPEG via PyMuPDF before sending to Gemini.
    """
    import base64
    prompt = _OCR_PROMPTS.get(doc_key)
    if not prompt:
        return {}
    try:
        ext = os.path.splitext(image_path)[1].lower().lstrip('.')

        if ext == 'pdf':
            image_bytes = _pdf_to_jpeg_bytes(image_path)
            if not image_bytes:
                logger.warning(f"[EMA OCR] Não foi possível converter PDF para imagem: {image_path}")
                return {}
            mime_type = 'image/jpeg'
        else:
            mime_map = {
                'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
                'gif': 'image/gif',  'webp': 'image/webp',
            }
            mime_type = mime_map.get(ext, 'image/jpeg')
            with open(image_path, 'rb') as fh:
                image_bytes = fh.read()

        b64_data = base64.standard_b64encode(image_bytes).decode('utf-8')
        base_url = (os.environ.get('AI_INTEGRATIONS_GEMINI_BASE_URL') or '').rstrip('/')
        api_key = os.environ.get('AI_INTEGRATIONS_GEMINI_API_KEY')
        if not base_url or not api_key:
            raise RuntimeError('Integração Gemini não configurada para OCR')

        response = requests.post(
            f'{base_url}/models/gemini-2.5-flash:generateContent',
            headers={
                'x-goog-api-key': api_key,
                'Content-Type': 'application/json',
            },
            json={
                'contents': [{
                    'role': 'user',
                    'parts': [
                        {'inline_data': {'mime_type': mime_type, 'data': b64_data}},
                        {'text': prompt},
                    ],
                }],
                'generationConfig': {
                    'maxOutputTokens': 1024,
                    'temperature': 0.1,
                },
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        raw = payload['candidates'][0]['content']['parts'][0]['text']
        logger.info(f"[EMA OCR] Resposta bruta ({doc_key}): {raw[:400]}")
        # Strip markdown code fences if present
        raw = raw.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.M)
        raw = re.sub(r'```\s*$', '', raw, flags=re.M).strip()
        # Try to extract the first JSON object block from the response
        json_match = re.search(r'\{.*?\}', raw, re.DOTALL)
        if json_match:
            raw = json_match.group(0)
        data = json.loads(raw)
        result = {k: v for k, v in data.items() if v not in (None, 'null', 'None', '', 'null')}
        logger.info(f"[EMA OCR] Campos extraídos ({doc_key}): {list(result.keys())}")
        return result
    except Exception as exc:
        logger.warning(f"[EMA OCR] Extração falhou ({doc_key}): {exc}")
        return {}


def _apply_ocr_data(driver, raw_extracted: dict) -> list[str]:
    """Validate and apply OCR-extracted data to driver fields.
    Returns a list of human-readable summary lines for the fields that were applied.
    """
    summary = []
    for key, value in raw_extracted.items():
        if key not in FIELD_MAP or value is None:
            continue
        fdef = FIELD_MAP[key]
        kind = fdef['kind']
        try:
            if kind == 'cpf':
                cleaned = re.sub(r'\D', '', str(value))
                if len(cleaned) == 11 and validate_cpf(cleaned):
                    formatted = format_cpf(cleaned)
                    # Check uniqueness: CPF may already belong to another driver
                    from models import Driver as _Driver
                    conflict = _Driver.query.filter(
                        _Driver.cpf == formatted,
                        _Driver.id  != driver.id
                    ).first()
                    if conflict:
                        # Document (CNH) is physical proof — force-apply CPF to this driver,
                        # clear it from the conflicting driver so DB constraint is respected.
                        logger.warning(
                            f"[EMA OCR] CPF {formatted} conflito: era do motorista ID={conflict.id} "
                            f"({conflict.name}) — transferido para driver {driver.id} ({driver.name})"
                        )
                        conflict.cpf = None  # clear from conflicting driver
                    setattr(driver, key, formatted)
                    summary.append(f"  • *CPF*: {formatted}")
            elif kind == 'date':
                parsed_date = None
                for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
                    try:
                        parsed_date = datetime.strptime(str(value), fmt).date()
                        break
                    except ValueError:
                        pass
                if parsed_date:
                    setattr(driver, key, parsed_date)
                    summary.append(f"  • *{fdef['label']}*: {parsed_date.strftime('%d/%m/%Y')}")
            elif kind == 'year':
                yr = int(str(value).strip())
                if 1970 <= yr <= datetime.utcnow().year + 2:
                    setattr(driver, key, yr)
                    summary.append(f"  • *{fdef['label']}*: {yr}")
            elif kind == 'cep':
                cep_clean = re.sub(r'\D', '', str(value))
                if len(cep_clean) == 8:
                    setattr(driver, 'cep', cep_clean)
                    summary.append(f"  • *CEP*: {cep_clean}")
                    addr = lookup_cep(cep_clean)
                    if addr:
                        for af in ('street', 'neighborhood', 'city', 'state'):
                            if addr.get(af):
                                setattr(driver, af, addr[af])
                        summary.append(f"  • *Endereço*: {addr.get('street','')} — "
                                       f"{addr.get('city','')}/{addr.get('state','')}")
            else:
                setattr(driver, key, str(value))
                summary.append(f"  • *{fdef['label']}*: {value}")
        except Exception as exc:
            logger.warning(f"[EMA OCR] Falha ao aplicar {key}={value}: {exc}")
    return summary


def _save_and_ocr(session, driver, media_path: str, field_def: dict) -> tuple[str, bool, list]:
    """Save document, run OCR if available, apply extracted data.
    Returns (reply_message, ocr_succeeded, missing_required_keys).
    - ocr_succeeded: True when at least one field was extracted.
    - missing_required_keys: list of required ocr_fills keys still empty after OCR.
    """
    save_msg, dest_path = _save_media_to_field(session, driver, media_path, field_def)

    ocr_keys = field_def.get('ocr_fills', [])
    if not ocr_keys:
        return save_msg, False, []

    def _still_missing_required():
        return [
            k for k in ocr_keys
            if k in FIELD_MAP and FIELD_MAP[k].get('required')
            and _get_current_value(driver, k) in (None, '')
        ]

    extracted_raw = _extract_from_image(dest_path, field_def['key'])
    if extracted_raw:
        summary_lines = _apply_ocr_data(driver, extracted_raw)
        missing_req = _still_missing_required()
        if summary_lines:
            summary_text = '\n'.join(summary_lines)
            save_msg = (f"{save_msg}\n\n"
                        f"Li o documento e identifiquei automaticamente:\n{summary_text}\n\n"
                        f"Essas informações foram preenchidas no cadastro.")
            return save_msg, True, missing_req
        else:
            save_msg += "\n\n_(Documento recebido, mas não consegui extrair dados legíveis.)_"
            return save_msg, False, missing_req
    else:
        save_msg += "\n\n_(Não consegui ler o documento automaticamente.)_"
        return save_msg, False, _still_missing_required()


# ── Gemini helper ──────────────────────────────────────────────────────────

def _call_gemini(system_prompt: str, user_content: str) -> tuple[str, dict, bool]:
    """Call Groq Llama 3.3 70B and return (reply, updates, step_complete). Never raises."""
    try:
        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user',   'content': user_content},
        ]
        raw = _groq_generate(messages, max_tokens=1024)
        raw = re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=re.M)
        raw = re.sub(r'```\s*$', '', raw, flags=re.M).strip()
        parsed = json.loads(raw)
        return (
            parsed.get('reply', ''),
            parsed.get('updates', {}),
            parsed.get('step_complete', False),
        )
    except Exception as e:
        logger.error(f"[EMA] Groq/Llama error: {e}")
        return (
            f"Xiii, deu um probleminha aqui. Pode mandar de novo? 🙏\n\n{_SIGNATURE}",
            {},
            False,
        )


# ── Apply validated updates to driver model ────────────────────────────────

def _apply_updates(driver, updates: dict, reply_ref: list):
    """Apply Gemini-validated updates to driver fields. reply_ref receives extra text to append."""
    for field_key, value in updates.items():
        if field_key not in FIELD_MAP:
            continue
        fdef = FIELD_MAP[field_key]
        try:
            kind = fdef['kind']
            if kind == 'cpf':
                if not validate_cpf(str(value)):
                    continue
                value = format_cpf(str(value))
            elif kind == 'cep':
                addr = lookup_cep(str(value))
                if addr:
                    driver.street       = addr['street']
                    driver.neighborhood = addr['neighborhood']
                    driver.city         = addr['city']
                    driver.state        = addr['state']
                    driver.cep          = addr['cep']
                    reply_ref.append(
                        f"\n📍 Endereço encontrado: *{addr['street']}, "
                        f"{addr['neighborhood']} — {addr['city']}/{addr['state']}*\nEstá correto?"
                    )
                    continue
                else:
                    continue
            elif kind == 'date':
                from datetime import date
                if isinstance(value, str):
                    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
                        try:
                            value = datetime.strptime(value, fmt).date()
                            break
                        except ValueError:
                            pass
            elif kind == 'year':
                value = int(value)
            elif kind == 'bool':
                if isinstance(value, str):
                    value = value.lower() in ('true', 'sim', '1', 's', 'yes')
            elif kind == 'choice' and field_key == 'truck_type':
                if str(value).lower() not in TRUCK_TYPES:
                    continue
                value = str(value).lower()
            setattr(driver, field_key, value)
        except Exception as ex:
            logger.warning(f"[EMA] Erro ao aplicar {field_key}={value}: {ex}")


def _sync_session_field(session, driver):
    """Update session.current_field and status based on remaining missing fields (normal mode)."""
    remaining = driver_missing_fields(driver)
    if remaining:
        session.current_field = remaining[0]['key']
        session.status        = 'active'
    else:
        session.status        = 'completed'
        session.completed_at  = datetime.utcnow()
        session.current_field = None


# ── History helpers ────────────────────────────────────────────────────────

def _append_history(session, role: str, text: str):
    from sqlalchemy.orm.attributes import flag_modified
    hist = list(session.history or [])
    now  = datetime.utcnow()
    hist.append({'role': role, 'text': text, 'ts': now.isoformat()})
    session.history    = hist
    session.updated_at = now
    flag_modified(session, 'history')
    # Track last driver message timestamp (used for inactivity timeout)
    if role == 'driver':
        session.last_driver_msg_at = now
        session.reminder_sent_at   = None  # reset reminder when driver responds


def _format_history(history: list) -> str:
    lines = []
    for h in history[-20:]:
        prefix = 'Motorista' if h['role'] == 'driver' else 'EMA'
        lines.append(f"{prefix}: {h['text']}")
    return '\n'.join(lines)


# ── Greeting messages ──────────────────────────────────────────────────────

def greeting_message(driver, revalidation: bool = False) -> str:
    """Return the opening message sent when a session starts."""
    name = driver.name.split()[0].capitalize()

    applicable = [f for f in FIELD_DEFS if _is_field_applicable(f, driver)]
    first = applicable[0] if applicable else None

    if revalidation:
        # Warm opening with value proposition for revalidation
        intro_variants = [
            (f"Oi, {name}! 👋 Aqui é a *EMA*, da equipe da *EMALOG*.\n\n"
             f"A EMALOG conecta motoristas parceiros a fretes por todo o Brasil 🚛 — "
             f"e pra garantir que você receba as melhores oportunidades, "
             f"precisamos deixar seu cadastro atualizado.\n\n"
             f"É rapidinho! Vou aproveitar seus documentos pra preencher o máximo automático."),
            (f"E aí, {name}! 🤙 Aqui é a *EMA*, da *EMALOG*.\n\n"
             f"Tamos atualizando o cadastro dos nossos motoristas parceiros pra "
             f"garantir que os fretes certos cheguem pra você. "
             f"Vou usar seus documentos pra agilizar tudo — menos pergunta, mais frete! 🚛"),
            (f"Olá, {name}! 👋 Sou a *EMA*, da equipe da *EMALOG*.\n\n"
             f"A gente conecta motoristas a fretes por todo o Brasil, e pra isso "
             f"o cadastro precisa tá em dia. Vou confirmar seus dados rapidinho — "
             f"uso seus documentos pra preencher automático e faço menos perguntas! 💪"),
        ]
        base = random.choice(intro_variants)
    else:
        # Normal mode — first time
        intro_variants = [
            (f"Oi, {name}! 👋 Aqui é a *EMA*, da *EMALOG*.\n\n"
             f"A EMALOG trabalha conectando motoristas autônomos a fretes por todo o Brasil 🚛 — "
             f"e pra começar a receber ofertas de frete, precisamos completar seu cadastro.\n\n"
             f"Vou usar seus documentos pra preencher tudo automático. Vai ser rápido!"),
            (f"Boa, {name}! 🤙 Aqui é a *EMA*, da equipe da *EMALOG*.\n\n"
             f"A gente tá cadastrando novos motoristas parceiros pra receber fretes "
             f"por todo o Brasil. Vou completar seu cadastro agora — uso seus documentos "
             f"pra facilitar ao máximo! 🚛"),
        ]
        base = random.choice(intro_variants)

    if first:
        current_val = _get_current_value(driver, first['key'])
        base += f"\n\n{_ask_field_text(first, current_val)}"
    else:
        base += f"\n\nSeu cadastro já tá completo! 🎉"

    base += f"\n\n{_SIGNATURE}"
    return base
