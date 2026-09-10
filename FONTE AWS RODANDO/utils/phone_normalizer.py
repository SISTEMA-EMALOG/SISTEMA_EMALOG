"""
Normalizador de telefones brasileiros para importação de leads.

Fluxo:
1. Tabela city→DDD (instantânea, cobre ~95% dos casos)
2. Tabela state→DDD padrão (capital) como fallback
3. Gemini em lote para cidades não encontradas na tabela

Detecta telefones sem DDD (8-9 dígitos) e adiciona o DDD correto
baseado na cidade/UF do lead.
"""

import re
import logging
import unicodedata

log = logging.getLogger(__name__)

# ── Normalização de texto ─────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Remove acentos e converte para minúsculas."""
    if not text:
        return ''
    nfkd = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


# ── DDD por estado (capital / DDD principal) ──────────────────────────────────

DDD_BY_STATE = {
    'AC': '68', 'AL': '82', 'AP': '96', 'AM': '92', 'BA': '71',
    'CE': '85', 'DF': '61', 'ES': '27', 'GO': '62', 'MA': '98',
    'MT': '65', 'MS': '67', 'MG': '31', 'PA': '91', 'PB': '83',
    'PR': '41', 'PE': '81', 'PI': '86', 'RJ': '21', 'RN': '84',
    'RS': '51', 'RO': '69', 'RR': '95', 'SC': '48', 'SP': '11',
    'SE': '79', 'TO': '63',
}

# ── Tabela principal: cidade normalizada → DDD ────────────────────────────────
# Inclui capitais, cidades com 100k+ hab. e principais polos logísticos

_DDD_CITY_RAW = {
    # ── São Paulo ─────────────────────────────────────────────────────────────
    'sao paulo': '11', 'santo andre': '11', 'sao bernardo do campo': '11',
    'sao caetano do sul': '11', 'diadema': '11', 'maua': '11',
    'guarulhos': '11', 'osasco': '11', 'carapicuiba': '11', 'barueri': '11',
    'taboao da serra': '11', 'cotia': '11', 'itapevi': '11', 'jandira': '11',
    'embu das artes': '11', 'mogi das cruzes': '11', 'suzano': '11',
    'ferraz de vasconcelos': '11', 'poá': '11', 'itaquaquecetuba': '11',
    'francisco morato': '11', 'franco da rocha': '11', 'caieiras': '11',
    'santana de parnaiba': '11', 'pirapora do bom jesus': '11',
    'aruja': '11', 'santa isabel': '11', 'biritiba mirim': '11',
    # DDD 12 — Vale do Paraíba
    'sao jose dos campos': '12', 'taubate': '12', 'jacareí': '12',
    'jacarei': '12', 'lorena': '12', 'pindamonhangaba': '12',
    'cruzeiro': '12', 'guaratingueta': '12', 'aparecida': '12',
    'campos do jordao': '12', 'sao sebastiao': '12', 'caraguatatuba': '12',
    'ubatuba': '12', 'ilhabela': '12', 'cacapava': '12',
    # DDD 13 — Baixada Santista
    'santos': '13', 'sao vicente': '13', 'cubatao': '13', 'guaruja': '13',
    'praia grande': '13', 'mongagua': '13', 'itanhaem': '13',
    'peruibe': '13', 'registro': '13', 'iguape': '13',
    # DDD 14 — Bauru
    'bauru': '14', 'marilia': '14', 'botucatu': '14', 'jau': '14',
    'lins': '14', 'avare': '14', 'pederneiras': '14', 'barra bonita': '14',
    # DDD 15 — Sorocaba
    'sorocaba': '15', 'itapetininga': '15', 'tatuí': '15', 'tatui': '15',
    'boituva': '15', 'pilar do sul': '15', 'sao roque': '15',
    'porto feliz': '15', 'itu': '15', 'salto': '15', 'cerqueira cesar': '15',
    # DDD 16 — Ribeirão Preto
    'ribeirao preto': '16', 'sao carlos': '16', 'araraquara': '16',
    'franca': '16', 'sertaozinho': '16', 'jaboticabal': '16',
    'catanduva': '16', 'olimpia': '16', 'bebedouro': '16',
    'pitangueiras': '16', 'batatais': '16', 'jardinopolis': '16',
    # DDD 17 — São José do Rio Preto
    'sao jose do rio preto': '17', 'aracatuba': '17', 'votuporanga': '17',
    'fernandopolis': '17', 'jales': '17', 'santa fe do sul': '17',
    'birigui': '17', 'penapolis': '17', 'mirassol': '17',
    # DDD 18 — Presidente Prudente
    'presidente prudente': '18', 'assis': '18', 'ourinhos': '18',
    'tupã': '18', 'tupa': '18', 'adamantina': '18', 'andradina': '18',
    'paraguacu paulista': '18', 'rancharia': '18',
    # DDD 19 — Campinas
    'campinas': '19', 'piracicaba': '19', 'limeira': '19',
    'americana': '19', 'santa barbara doeste': '19', 'rio claro': '19',
    'araras': '19', 'jundiai': '19', 'varzea paulista': '19',
    'campo limpo paulista': '19', 'sumare': '19', 'hortolândia': '19',
    'hortolandia': '19', 'indaiatuba': '19', 'itatiba': '19',
    'braganca paulista': '19', 'atibaia': '19', 'sao joao da boa vista': '19',
    'ribeirao pires': '19', 'mococa': '19', 'mogi guacu': '19',
    'mogi mirim': '19', 'sao jose do rio pardo': '19',

    # ── Rio de Janeiro ────────────────────────────────────────────────────────
    'rio de janeiro': '21', 'niteroi': '21', 'sao goncalo': '21',
    'duque de caxias': '21', 'nova iguacu': '21', 'belford roxo': '21',
    'sao joao de meriti': '21', 'nilopolitano': '21', 'nilopolis': '21',
    'mesquita': '21', 'queimados': '21', 'japeri': '21', 'seropedica': '21',
    'itaguai': '21', 'mangaratiba': '21', 'paracambi': '21', 'mage': '21',
    'nova friburgo': '22', 'petropolis': '21',
    'campos dos goytacazes': '22', 'macae': '22', 'cabo frio': '22',
    'armacao dos buzios': '22', 'araruama': '22', 'saquarema': '22',
    'volta redonda': '24', 'barra mansa': '24', 'resende': '24',
    'angra dos reis': '24', 'paraty': '24', 'barra do pirai': '24',

    # ── Espírito Santo ────────────────────────────────────────────────────────
    'vitoria': '27', 'vila velha': '27', 'cariacica': '27', 'serra': '27',
    'viana': '27', 'guarapari': '27', 'aracruz': '27', 'linhares': '27',
    'colatina': '27', 'sao mateus': '27',
    'cachoeiro de itapemirim': '28', 'marataizes': '28', 'itapemirim': '28',
    'presidente kennedy': '28', 'mimoso do sul': '28',

    # ── Minas Gerais ──────────────────────────────────────────────────────────
    'belo horizonte': '31', 'contagem': '31', 'betim': '31',
    'santa luzia': '31', 'ribeirão das neves': '31',
    'ribeirao das neves': '31', 'vespasiano': '31', 'sabara': '31',
    'nova lima': '31', 'brumadinho': '31', 'ibirite': '31',
    'sarzedo': '31', 'mario campos': '31', 'itauna': '37',
    'juiz de fora': '32', 'barbacena': '32', 'muriae': '32',
    'leopoldina': '32', 'ube': '32', 'ubá': '32',
    'governador valadares': '33', 'ipatinga': '31', 'coronel fabriciano': '31',
    'timoteo': '31', 'caratinga': '33', 'manhuacu': '33',
    'uberlandia': '34', 'uberaba': '34', 'araguari': '34',
    'ituiutaba': '34', 'patos de minas': '34',
    'pocos de caldas': '35', 'pouso alegre': '35', 'itajuba': '35',
    'varginha': '35', 'lavras': '35', 'tres coracoes': '35',
    'sao sebastiao do paraiso': '35',
    'divinopolis': '37', 'formiga': '37', 'para de minas': '37',
    'montes claros': '38', 'januaria': '38', 'bocaiuva': '38',

    # ── Paraná ────────────────────────────────────────────────────────────────
    'curitiba': '41', 'sao jose dos pinhais': '41', 'colombo': '41',
    'almirante tamandare': '41', 'campo largo': '41', 'araucaria': '41',
    'fazenda rio grande': '41', 'pinhais': '41', 'paranagua': '41',
    'morretes': '41', 'antonina': '41', 'guaratuba': '41',
    'ponta grossa': '42', 'apucarana': '43', 'londrina': '43',
    'cambe': '43', 'ibipora': '43', 'cornelio procopio': '43',
    'arapongas': '43', 'rolandia': '43',
    'maringa': '44', 'sarandi': '44', 'paiçandu': '44', 'paicandu': '44',
    'umuarama': '44', 'cianorte': '44',
    'cascavel': '45', 'foz do iguacu': '45', 'medianeira': '45',
    'toledo': '45', 'palmas': '46',
    'francisco beltrao': '46', 'pato branco': '46', 'dois vizinhos': '46',

    # ── Santa Catarina ────────────────────────────────────────────────────────
    'joinville': '47', 'jaraguá do sul': '47', 'jaragua do sul': '47',
    'blumenau': '47', 'gaspar': '47', 'brusque': '47', 'itajai': '47',
    'balneario camboriu': '47', 'navegantes': '47',
    'florianopolis': '48', 'sao jose': '48', 'palhoca': '48',
    'biguacu': '48', 'santo amaro da imperatriz': '48',
    'tubarao': '48', 'laguna': '48', 'criciuma': '48', 'ararangua': '48',
    'chapeco': '49', 'lages': '49', 'concordia': '49',
    'sao miguel do oeste': '49', 'xanxere': '49',

    # ── Rio Grande do Sul ─────────────────────────────────────────────────────
    'porto alegre': '51', 'canoas': '51', 'sao leopoldo': '51',
    'novo hamburgo': '51', 'gravataí': '51', 'gravataí': '51',
    'alvorada': '51', 'viamao': '51', 'cachoeirinha': '51',
    'esteio': '51', 'sapucaia do sul': '51', 'guaiba': '51',
    'sapiranga': '51', 'campo bom': '51', 'dois irmaos': '51',
    'pelotas': '53', 'rio grande': '53', 'bagé': '53', 'bage': '53',
    'caxias do sul': '54', 'bento goncalves': '54', 'gramado': '54',
    'canela': '54', 'farroupilha': '54', 'garibaldi': '54',
    'santa maria': '55', 'santa cruz do sul': '51', 'passo fundo': '54',
    'ijui': '55', 'uruguaiana': '55',

    # ── Distrito Federal ──────────────────────────────────────────────────────
    'brasilia': '61', 'ceilandia': '61', 'taguatinga': '61',
    'sobradinho': '61', 'planaltina': '61', 'gama': '61',
    'guara': '61', 'samambaia': '61', 'aguas claras': '61',

    # ── Goiás ─────────────────────────────────────────────────────────────────
    'goiania': '62', 'aparecida de goiania': '62', 'anapolis': '62',
    'trindade': '62', 'senador canedo': '62', 'goianira': '62',
    'rio verde': '64', 'itumbiara': '64', 'jatai': '64', 'catalao': '64',

    # ── Tocantins ─────────────────────────────────────────────────────────────
    'palmas': '63', 'araguaina': '63', 'guarai': '63',

    # ── Mato Grosso ───────────────────────────────────────────────────────────
    'cuiaba': '65', 'varzea grande': '65', 'sinop': '66',
    'rondonopolis': '66', 'lucas do rio verde': '65', 'tangara da serra': '65',

    # ── Mato Grosso do Sul ────────────────────────────────────────────────────
    'campo grande': '67', 'dourados': '67', 'tres lagoas': '67',
    'corumba': '67', 'ponta pora': '67', 'naviraí': '67', 'navarai': '67',

    # ── Acre ──────────────────────────────────────────────────────────────────
    'rio branco': '68', 'cruzeiro do sul': '68',

    # ── Rondônia ──────────────────────────────────────────────────────────────
    'porto velho': '69', 'ji-parana': '69', 'ji parana': '69',
    'ariquemes': '69', 'cacoal': '69',

    # ── Bahia ─────────────────────────────────────────────────────────────────
    'salvador': '71', 'lauro de freitas': '71', 'camacari': '71',
    'simoes filho': '71', 'dias davila': '71', 'sao francisco do conde': '71',
    'madre de deus': '71', 'candeias': '71', 'vera cruz': '71',
    'itabuna': '73', 'ilheus': '73', 'porto seguro': '73',
    'teixeira de freitas': '73', 'eunapolis': '73',
    'juazeiro': '74', 'paulo afonso': '75',
    'feira de santana': '75', 'alagoinhas': '75', 'cruz das almas': '75',
    'cachoeira': '75', 'santo antonio de jesus': '75',
    'barreiras': '77', 'luis eduardo magalhaes': '77',
    'vitoria da conquista': '77', 'jequie': '73',

    # ── Sergipe ───────────────────────────────────────────────────────────────
    'aracaju': '79', 'nossa senhora do socorro': '79', 'lagarto': '79',
    'itabaiana': '79', 'estancia': '79',

    # ── Pernambuco ────────────────────────────────────────────────────────────
    'recife': '81', 'olinda': '81', 'caruaru': '87', 'caruarú': '87',
    'jaboatao dos guararapes': '81', 'paulista': '81', 'camaragibe': '81',
    'sao lourenco da mata': '81', 'abreu e lima': '81',
    'petrolina': '87', 'garanhuns': '87', 'salgueiro': '87',

    # ── Alagoas ───────────────────────────────────────────────────────────────
    'maceio': '82', 'arapiraca': '82', 'palmeira dos indios': '82',

    # ── Paraíba ───────────────────────────────────────────────────────────────
    'joao pessoa': '83', 'campina grande': '83', 'patos': '83',

    # ── Rio Grande do Norte ───────────────────────────────────────────────────
    'natal': '84', 'mossoro': '84', 'parnamirim': '84', 'caicó': '84',

    # ── Ceará ─────────────────────────────────────────────────────────────────
    'fortaleza': '85', 'caucaia': '85', 'maranguape': '85',
    'maracanau': '85', 'eusebio': '85', 'aquiraz': '85',
    'juazeiro do norte': '88', 'crato': '88', 'sobral': '88',
    'iguatu': '88', 'quixada': '85',

    # ── Piauí ─────────────────────────────────────────────────────────────────
    'teresina': '86', 'parnaiba': '86', 'picos': '89',
    'floriano': '89', 'oeiras': '89',

    # ── Maranhão ──────────────────────────────────────────────────────────────
    'sao luis': '98', 'sao luís': '98', 'sao jose de ribamar': '98',
    'paço do lumiar': '98', 'paco do lumiar': '98',
    'imperatriz': '99', 'caxias': '99', 'balsas': '99',
    'timon': '99', 'codó': '99', 'codo': '99',

    # ── Pará ──────────────────────────────────────────────────────────────────
    'belem': '91', 'ananindeua': '91', 'maraba': '94', 'castanhal': '91',
    'braganca': '91', 'santarem': '93', 'altamira': '93',

    # ── Amazonas ──────────────────────────────────────────────────────────────
    'manaus': '92', 'parintins': '92', 'itacoatiara': '92',

    # ── Roraima ───────────────────────────────────────────────────────────────
    'boa vista': '95',

    # ── Amapá ────────────────────────────────────────────────────────────────
    'macapa': '96', 'santana': '96',
}

# Construir tabela normalizada (sem acentos)
DDD_BY_CITY: dict[str, str] = {
    _normalize(city): ddd for city, ddd in _DDD_CITY_RAW.items()
}


# ── Funções de telefone ───────────────────────────────────────────────────────

def _clean_digits(phone: str) -> str:
    """Remove tudo que não for dígito."""
    return re.sub(r'\D', '', phone or '')


def _needs_ddd(digits: str) -> bool:
    """
    Retorna True se o número parece estar sem DDD.
    - Fixo sem DDD: 8 dígitos
    - Celular sem DDD: 9 dígitos (começa com 9)
    Números com DDD têm 10 (fixo) ou 11 (celular) dígitos.
    """
    return len(digits) in (8, 9)


def _already_formatted(digits: str) -> bool:
    """Retorna True se o número já está completo (10-11 dígitos)."""
    return len(digits) in (10, 11)


def lookup_ddd(city: str, state: str) -> str | None:
    """
    Procura o DDD para uma cidade+estado.
    1. Tenta a tabela por cidade (normalizada)
    2. Fallback: DDD padrão do estado (capital)
    """
    city_norm = _normalize(city)
    if city_norm and city_norm in DDD_BY_CITY:
        return DDD_BY_CITY[city_norm]
    state_up = (state or '').upper().strip()[:2]
    return DDD_BY_STATE.get(state_up)


def apply_ddd(phone: str, ddd: str) -> str:
    """Prefixa o número com o DDD."""
    digits = _clean_digits(phone)
    return f'({ddd}) {digits}'


# ── Normalização em lote ──────────────────────────────────────────────────────

def normalize_phones_batch(valid_leads: list) -> tuple[list, int, list]:
    """
    Percorre todos os leads e normaliza telefones sem DDD.

    Retorna:
        (leads_atualizados, total_corrigidos, lista_de_alteracoes)

    Cada item da lista_de_alteracoes é um dict:
        { index, company_name, field, before, after, ddd, city, state }
    """
    corrections = []
    phone_fields = ['contact_phone', 'whatsapp']

    for i, ld in enumerate(valid_leads):
        city  = ld.get('city', '')
        state = ld.get('state', '')
        ddd   = lookup_ddd(city, state)

        if not ddd:
            continue

        for field in phone_fields:
            original = ld.get(field, '')
            if not original:
                continue
            digits = _clean_digits(original)
            if not _needs_ddd(digits):
                continue

            corrected = apply_ddd(original, ddd)
            ld[field] = corrected
            ld[f'_{field}_original'] = original  # guarda original para exibição
            corrections.append({
                'index':        i,
                'company_name': ld.get('company_name', ''),
                'field':        field,
                'before':       original,
                'after':        corrected,
                'ddd':          ddd,
                'city':         city,
                'state':        state,
            })
            log.debug(f'DDD adicionado: {original} → {corrected} ({city}/{state})')

    return valid_leads, len(corrections), corrections


def normalize_phones_with_ai(valid_leads: list) -> tuple[list, int, list]:
    """
    Versão aprimorada: tenta a tabela primeiro, depois usa Gemini
    para cidades não reconhecidas.

    Retorna: (leads_atualizados, total_corrigidos, lista_de_alteracoes)
    """
    phone_fields = ['contact_phone', 'whatsapp']

    # Primeira passagem: aplicar tabela
    unknown_indices = []
    for i, ld in enumerate(valid_leads):
        city  = ld.get('city', '')
        state = ld.get('state', '')
        ddd   = lookup_ddd(city, state)
        ld['_resolved_ddd'] = ddd

        if not ddd:
            has_phone_without_ddd = any(
                _needs_ddd(_clean_digits(ld.get(f, '')))
                for f in phone_fields if ld.get(f)
            )
            if has_phone_without_ddd:
                unknown_indices.append(i)

    # Segunda passagem: IA para cidades desconhecidas (uma chamada em lote)
    if unknown_indices:
        _ai_resolve_ddds(valid_leads, unknown_indices)

    # Aplicar DDDs resolvidos
    corrections = []
    for i, ld in enumerate(valid_leads):
        ddd = ld.pop('_resolved_ddd', None)
        if not ddd:
            continue
        for field in phone_fields:
            original = ld.get(field, '')
            if not original:
                continue
            digits = _clean_digits(original)
            if not _needs_ddd(digits):
                continue
            corrected = apply_ddd(original, ddd)
            ld[field] = corrected
            ld[f'_{field}_original'] = original
            corrections.append({
                'index':        i,
                'company_name': ld.get('company_name', ''),
                'field':        field,
                'before':       original,
                'after':        corrected,
                'ddd':          ddd,
                'city':         ld.get('city', ''),
                'state':        ld.get('state', ''),
            })

    return valid_leads, len(corrections), corrections


def _ai_resolve_ddds(valid_leads: list, indices: list) -> None:
    """
    Faz uma única chamada ao Gemini para resolver DDDs de cidades desconhecidas.
    Atualiza valid_leads[i]['_resolved_ddd'] in-place.
    """
    try:
        from google import genai
        from google.genai import types as gtypes
        import os, json as _json

        api_key = os.environ.get('AI_INTEGRATIONS_GEMINI_API_KEY')
        base_url = os.environ.get('AI_INTEGRATIONS_GEMINI_BASE_URL')
        if not api_key:
            log.warning('AI_INTEGRATIONS_GEMINI_API_KEY não configurada — resolução AI de DDDs ignorada')
            return

        client = genai.Client(api_key=api_key, http_options={'base_url': base_url} if base_url else {})

        # Montar lista de casos para o prompt
        cases = []
        for idx in indices:
            ld = valid_leads[idx]
            cases.append({
                'i':     idx,
                'city':  ld.get('city', ''),
                'state': ld.get('state', ''),
            })

        prompt = (
            'Você é um especialista em DDDs do Brasil. '
            'Para cada par cidade/estado abaixo, retorne o DDD correto (2 dígitos). '
            'Responda SOMENTE com JSON no formato: [{"i": <índice>, "ddd": "<XX>"}]\n\n'
            'Cidades:\n' + _json.dumps(cases, ensure_ascii=False)
        )

        response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=prompt,
            config=gtypes.GenerateContentConfig(temperature=0),
        )
        text = response.text.strip()
        # Extrair JSON da resposta
        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if not match:
            log.warning(f'Resposta IA inesperada: {text[:200]}')
            return

        results = _json.loads(match.group(0))
        for item in results:
            idx = item.get('i')
            ddd = str(item.get('ddd', '')).strip()
            if idx is not None and re.fullmatch(r'\d{2}', ddd):
                valid_leads[idx]['_resolved_ddd'] = ddd
                log.info(f'IA resolveu DDD: {valid_leads[idx].get("city")} → {ddd}')

    except Exception as e:
        log.warning(f'Erro na resolução de DDDs via IA: {e}')
