"""Bot-driven course registration into the centre's GreenSoft (call24) form.

The registration form the bot used to link customers to is a GreenSoft form
(https://forms.greensoft.mn/<site>/form/<id>) that feeds the centre's call24
CRM. Instead of sending the customer away to fill it, the reply model collects
the fields in the conversation ("Би таны бүртгэлийг үүсгэе…"), confirms them,
and calls the `submit_registration` tool; the webhook then POSTs the fields to
the form exactly as the form's own widget does (multipart, same field names,
the honeypot left empty), mirrors the lead in Work Tasks and pings Telegram.

Verified live 2026-11-03 with one approved test entry (logid 362): the form
accepts a server-side POST — no captcha, only client-side spam checks.

Configuration (env):
  REGISTRATION_FORM_URL    the form page URL, e.g. https://forms.greensoft.mn/1955/form/5
                           (unset -> the tool is not offered; the bot falls back
                           to the registration link + phone options)
  REGISTRATION_FORM_TOKEN  the `token` the widget sends to /formapi (public, it
                           is embedded in the widget script); default = current

Field names on the form (as of 2026-11): lastname, firstname, phone, email,
gs_company, plus three selects identified by their labels (салбар / сургалт /
ээлж). The definition is read from /formapi so option VALUES are always the
form's current ones; the model only ever talks in labels.
"""
import base64
import json
import logging
import os
import re
import time

import requests

logger = logging.getLogger(__name__)

REGISTRATION_FORM_URL = (os.environ.get('REGISTRATION_FORM_URL') or '').strip()
REGISTRATION_FORM_TOKEN = (
    os.environ.get('REGISTRATION_FORM_TOKEN') or 'eWqLUTJUwYNA8sA5Y77Ny9yv84hVV9'
).strip()
_URL_RE = re.compile(r'^(https?://[^/]+)/(\d+)/form/(\d+)/?$')
_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) MagicBot/1.0'
DEFINITION_TTL_SECONDS = 3600
REQUEST_TIMEOUT = 20

# Which select on the form a tool argument fills, found by a keyword in the
# select's label — robust to the form owner renaming/reordering the selects.
_SELECT_KEYWORDS = {'branch': 'салбар', 'course': 'сургалт', 'shift': 'ээлж'}
# Text inputs: tool argument -> form input name.
_TEXT_FIELDS = {
    'lastname': 'lastname', 'firstname': 'firstname', 'phone': 'phone',
    'email': 'email', 'company': 'gs_company',
}


def is_configured():
    return bool(_URL_RE.match(REGISTRATION_FORM_URL))


def _parts():
    m = _URL_RE.match(REGISTRATION_FORM_URL)
    return m.group(1), m.group(2), m.group(3)      # base, site id, form id


# ----------------------------------------------------------------- definition

_definition_cache = {'at': 0.0, 'value': None}


def load_form_definition(force=False):
    """Read the form's field definition from /formapi (cached for an hour).

    Returns {'selects': [{'name', 'label', 'required', 'options': {label: value},
    'default': value}]} or None when the form can't be read. Text fields are
    not listed — their names are fixed (see _TEXT_FIELDS)."""
    if not is_configured():
        return None
    now = time.time()
    if (not force and _definition_cache['value']
            and now - _definition_cache['at'] < DEFINITION_TTL_SECONDS):
        return _definition_cache['value']
    base, site, form = _parts()
    try:
        resp = requests.get(
            f"{base}/{site}/formapi/{form}", params={'token': REGISTRATION_FORM_TOKEN},
            headers={'User-Agent': _USER_AGENT, 'Accept': 'application/json',
                     'Referer': REGISTRATION_FORM_URL},
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json()
        inputs = data.get('inputs')
        if isinstance(inputs, str):
            inputs = json.loads(inputs)
        if not isinstance(inputs, list):
            raise ValueError(f"unexpected formapi reply: {str(data)[:200]}")
    except Exception as e:
        logger.warning("registration: could not read the form definition: %s", e)
        return _definition_cache['value']   # stale is better than nothing
    selects = []
    for f in inputs:
        if not isinstance(f, dict) or f.get('type') != 'select':
            continue
        values = f.get('values') or []
        if isinstance(values, str):
            try:
                values = json.loads(values)
            except ValueError:
                values = []
        options, default = {}, None
        for v in values:
            if not isinstance(v, dict):
                continue
            label = str(v.get('label') or '').strip()
            value = str(v.get('value') if v.get('value') is not None else '')
            if label:
                options[label] = value
            if v.get('selected') and default is None:
                default = value
        selects.append({
            'name': f.get('name'), 'label': str(f.get('label') or ''),
            'required': str(f.get('required')).lower() == 'true',
            'options': options, 'default': default,
        })
    value = {'selects': selects}
    _definition_cache.update(at=now, value=value)
    return value


def _norm(s):
    return re.sub(r'\s+', ' ', str(s or '')).strip().lower()


def _match_option(select, wanted):
    """Map the model's label text to the form's option value: exact label,
    then startswith / contains either way, else the form's default."""
    w = _norm(wanted)
    if w:
        for label, value in select['options'].items():
            if _norm(label) == w:
                return value
        for label, value in select['options'].items():
            n = _norm(label)
            if n.startswith(w) or w.startswith(n) or w in n or n in w:
                return value
    if select['default'] is not None:
        return select['default']
    return next(iter(select['options'].values()), '')


def build_payload(data, definition):
    """The multipart fields the form's widget would send for this customer."""
    base, site, form = _parts()
    payload = {
        'siteid': site,
        'formid': form,
        # the widget stamps the page-load time; its anti-spam check wants a
        # few seconds between load and submit
        '_gs_form_token': str(int((time.time() - 15) * 1000)),
        '_gs_full_refferer': REGISTRATION_FORM_URL,
        '_gs_full_screensize': '1920x1080',
        '_gs_full_useragent': _USER_AGENT,
        'mainInputs': json.dumps({'siteid': int(site), 'memberid': '', 'formid': int(form)}),
        'formdesc': '',                       # honeypot — must stay empty
    }
    for arg, field in _TEXT_FIELDS.items():
        payload[field] = str(data.get(arg) or '').strip()
    for arg, keyword in _SELECT_KEYWORDS.items():
        select = next((s for s in definition['selects'] if keyword in _norm(s['label'])), None)
        if select and select['name']:
            payload[select['name']] = _match_option(select, data.get(arg))
    return payload


def submit_registration_form(data):
    """POST one registration. Returns (ok, info): info is the form's log id on
    success, else a short error string. Never raises."""
    if not is_configured():
        return False, 'REGISTRATION_FORM_URL not configured'
    definition = load_form_definition()
    if not definition:
        return False, 'form definition unavailable'
    try:
        payload = build_payload(data, definition)
        resp = requests.post(
            REGISTRATION_FORM_URL,
            files={k: (None, v) for k, v in payload.items()},
            headers={'User-Agent': _USER_AGENT, 'Referer': REGISTRATION_FORM_URL,
                     'Origin': _parts()[0], 'X-Requested-With': 'XMLHttpRequest',
                     'Accept': 'application/json, text/javascript, */*; q=0.01'},
            timeout=REQUEST_TIMEOUT,
        )
        body = resp.json() if 'json' in (resp.headers.get('content-type') or '') else {}
    except Exception as e:
        logger.error("registration: submit failed: %s", e)
        return False, f'submit error: {e}'[:200]
    if resp.status_code == 200 and body.get('status') == 'ok' and body.get('logid'):
        logger.info("registration: form accepted logid=%s member_id=%s",
                    body.get('logid'), body.get('member_id'))
        return True, str(body.get('logid'))
    logger.error("registration: form rejected status=%s body=%s", resp.status_code, resp.text[:300])
    return False, f"HTTP {resp.status_code}: {resp.text[:120]}"


# ------------------------------------------------------------------ marker

# generate_bot_response prefixes the reply with this hidden marker when the
# model called submit_registration; the webhook strips it, submits, and
# mirrors the lead. Base64url keeps the JSON safe inside the brackets.
_MARKER_RE = re.compile(r'\[REGISTER:([A-Za-z0-9_=-]+)\]')
REGISTRATION_FIELDS = ('lastname', 'firstname', 'phone', 'email', 'company',
                       'branch', 'course', 'shift')


def format_registration_marker(args):
    data = {k: str(args.get(k) or '').strip()[:200] for k in REGISTRATION_FIELDS}
    raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
    return f"[REGISTER:{base64.urlsafe_b64encode(raw).decode('ascii')}]"


def extract_registration(bot_response):
    """Strip the registration marker. Returns (cleaned_text, data_or_None)."""
    if not bot_response or '[REGISTER:' not in bot_response:
        return bot_response, None
    found = _MARKER_RE.search(bot_response)
    if not found:
        return bot_response, None
    try:
        data = json.loads(base64.urlsafe_b64decode(found.group(1).encode('ascii')).decode('utf-8'))
    except Exception:
        data = None
    cleaned = _MARKER_RE.sub('', bot_response).strip()
    return cleaned, data


def describe(data, ok, info):
    """Staff-facing summary (Telegram + lead note)."""
    if ok:
        status = f"form-д орсон ✅ (№{info})"
    else:
        status = f"form руу илгээж ЧАДСАНГҮЙ ⚠️ — гараар бүртгэнэ үү ({info})"
    lines = [
        f"Овог, нэр: {data.get('lastname', '')} {data.get('firstname', '')}".strip(),
        f"Утас: {data.get('phone', '')}",
        f"Имэйл: {data.get('email', '')}",
        f"Сургалт: {data.get('course', '') or '—'} | Ээлж: {data.get('shift', '') or '—'} | "
        f"Салбар: {data.get('branch', '') or 'Улаанбаатар'}",
    ]
    if data.get('company'):
        lines.append(f"Байгууллага: {data['company']}")
    lines.append(f"Бүртгэлийн form: {status}")
    return '\n'.join(lines)


# ----------------------------------------------------------- model-facing

SUBMIT_REGISTRATION_TOOL = {
    "name": "submit_registration",
    "description": (
        "Create the customer's course registration in the centre's registration "
        "system. Call it ONLY after you have collected every required field in "
        "the conversation AND the customer confirmed the summary. Required: "
        "lastname (овог), firstname (нэр), phone, email, shift. The email is "
        "mandatory because the contract and the online-lesson access are sent "
        "by email — explain that when asking. Use the option labels exactly as "
        "listed in the descriptions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "lastname": {"type": "string", "description": "Овог (father's name)"},
            "firstname": {"type": "string", "description": "Нэр"},
            "phone": {"type": "string", "description": "Mongolian phone number, 8 digits"},
            "email": {"type": "string", "description": "Email address"},
            "company": {"type": "string", "description": "Байгууллагын нэр, optional"},
            "branch": {
                "type": "string",
                "description": "Салбар: 'Улаанбаатар' (default), 'Эрдэнэт' or 'Дархан'",
            },
            "course": {
                "type": "string",
                "description": (
                    "Сонирхож буй сургалт: 'Танхимын сургалт', 'Танхим онлайн хосолсон "
                    "сургалт', '100% онлайн сургалт' or 'Magic Finance программын сургалт'"
                ),
            },
            "shift": {"type": "string", "description": "Суралцах ээлж: 'Өглөө', 'Өдөр' or 'Орой'"},
            "reply_to_customer": {
                "type": "string",
                "description": (
                    "Warm Mongolian confirmation to send now: the registration is "
                    "created, the contract and online-lesson access will arrive by "
                    "email, staff will contact them; one short sentence inviting "
                    "further questions."
                ),
            },
        },
        "required": ["lastname", "firstname", "phone", "email", "shift", "reply_to_customer"],
    },
}

REGISTRATION_RULE = (
    "БҮРТГЭЛ ҮҮСГЭХ (submit_registration) — хэрэглэгч бүртгүүлэх шийдвэрээ "
    "хэлсэн үед ('бүртгүүлье', 'бүртгэл хийе', 'тэгье суух гэж байна'):\n"
    "  • 'Би таны сургалтын бүртгэлийг үүсгэж байна. Хүсвэл та бүртгэлийн "
    "маягтын линкээр өөрөө ч бөглөж болно: <линк>' гэж эхэл — линкийг дээрх "
    "жагсаалтаас тухайн ангийн бүртгэлийн линк (байхгүй бол вэбсайт) гэж "
    "ав. Дараа нь мэдээллийг НЭГ УДААД НЭГ асуултаар цуглуул: (1) овог, нэр; (2) утасны "
    "дугаар; (3) имэйл хаяг — 'Гэрээ болон цахим хичээлийн эрх имэйлээр "
    "илгээгддэг тул имэйл хаяг заавал хэрэгтэй' гэж тайлбарла; (4) сонирхож "
    "буй сургалт (яриан дээр аль хэдийн тодорхой бол бүү асуу); (5) суралцах "
    "ээлж: Өглөө / Өдөр / Орой; (6) салбар — зөвхөн хэрэглэгч Улаанбаатараас "
    "гадуур гэж хэлсэн бол асуу (Эрдэнэт / Дархан), үгүй бол Улаанбаатар.\n"
    "  • Яриан дээр аль хэдийн мэдэгдсэн зүйлийг (нэр, утас, сонгосон анги) "
    "ДАХИН БҮҮ АСУУ — шууд ашигла.\n"
    "  • Бүгд цугларсны дараа товч жагсааж 'Зөв үү?' гэж нэг удаа "
    "баталгаажуул. Хэрэглэгч зөвшөөрвөл л submit_registration дууд; "
    "reply_to_customer-т бүртгэл үүссэн, гэрээ + хичээлийн эрх имэйлээр "
    "очно, ажилтан холбогдоно гэж дулаахан хэл.\n"
    "  • Хэрэглэгч имэйл өгөхөөс татгалзвал шалтгааныг нэг удаа тайлбарла; "
    "дахин татгалзвал утасны дугаараар lead болгон үлдээж бүртгэлийн "
    "линкийг өг (ажилтан холбогдоно гэж хэл).\n"
    "  • Энэ дүрэм бусад газар буй 'бүртгэлийн линк + утас' санал болгох "
    "заалтыг ОРЛОНО: линкийг эхлэхдээ нэг удаа сануулахад хангалттай. "
    "Хэрэглэгч 'өөрөө бөглөнө' гэвэл цуглуулахаа зогсоож, линкээр нь "
    "үргэлжлүүл.\n"
    "  • submit_registration-ийг нэг хэрэглэгчид ЗӨВХӨН НЭГ УДАА дууд. "
    "Ярианы түүхэнд чи аль хэдийн 'бүртгэл үүслээ' гэж хэлсэн байвал "
    "бүртгэл үүссэн гэсэн үг — 'баярлалаа', 'за' гэх мэт дараагийн "
    "мессежид дахин БҮҮ дууд, зүгээр л эелдгээр хариул."
)

# How long after an accepted registration the webhook refuses a second
# submission for the same customer (the model can re-call the tool).
REGISTRATION_DEDUP_HOURS = 24

REGISTRATION_ALREADY_DONE_REPLY = (
    "Таны бүртгэл аль хэдийн үүссэн байгаа. Гэрээ болон цахим хичээлийн эрх "
    "имэйлээр тань очно, манай ажилтан холбогдоно. Өөр асуух зүйл байвал "
    "бичээрэй."
)

REGISTRATION_FALLBACK_REPLY = (
    "Бүртгэлийн мэдээллийг тань хүлээн авлаа, баярлалаа. Манай ажилтан "
    "баталгаажуулаад гэрээ болон цахим хичээлийн эрхийг имэйлээр тань "
    "илгээнэ. Энэ хооронд асуух зүйл байвал надад бичээрэй."
)
