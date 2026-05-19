"""Prompt builder for the Mongolian Messenger bot.

Carved out of services/__init__.py to keep that file under control.
Contains the LLM system-prompt template, all section formatters that
read the structured catalog (BusinessLine / Course / Product / FAQ /
TrainingSnippet / TeamMember), and the SESSION_RULES / FUNNEL_RULES
tables that tune behaviour by funnel stage and session age.

Cross-module dependencies on services-level helpers (get_setting and
its specialised variants, HANDOFF_ADVISORY_RULE, ALLOWED_COURSE_TYPES,
SELF_PACED_COURSE_TYPE) are reached through ``_svc.<name>`` rather than
direct ``from services import X``. ``import services as _svc`` is safe
during the circular-import bind: it returns the partially-loaded
module object, which gains attributes as __init__.py continues to load;
by the time any function below runs, services is fully loaded and the
attribute lookups resolve.
"""
from datetime import datetime

from sqlalchemy.orm import joinedload

from extensions import db
from models import (BusinessLine, Course, FAQ, Product, Service, TeamMember,
                    TrainingSnippet)

import services as _svc


SESSION_RULES = {
    'new': (
        '0. Энэ бол хэрэглэгчтэй анх удаа ярилцаж байгаа явдал. Богино, '
        'дулаахан мэндчилгээгээр эхлээд (жишээ нь "Сайн байна уу!") шууд '
        'тэдний асуултанд ороорой.'
    ),
    'active': (
        '0. ЭНЭ БОЛ ҮРГЭЛЖИЛСЭН ЯРИА (active session). Сүүлийн 2 цагийн дотор '
        'хэрэглэгчтэй ярилцсан тул хариултын ЭХНИЙ үгээс ШУУД асуултын хариу '
        'руу ор. "Сайн байна уу", "Өглөөний мэнд", "Тавтай морил", "Танд '
        'юугаар туслах вэ?", өөрийгөө дахин танилцуулах гэх мэт мэндчилгээний '
        'үгийг ХЭЗЭЭ Ч БҮҮ ОРУУЛ — энэ дүрэм персонал доторх мэндчилгээний '
        'дүрмийг дарж байна. Өмнөх ярианыхаа контекстийг санаж буй мэт товч, '
        'ноорог байдлаар үргэлжлүүл.'
    ),
    'gap': (
        '0. Хэрэглэгчтэй өнөөдөр аль хэдийн ярилцаад, цөөн цагийн дараа эргэж '
        'ирлээ. Дахин "Сайн байна уу" эсвэл "Өглөөний мэнд" гэж БҮҮ мэндэл. '
        'Шууд асуултанд найрсагаар хариул. Хэрэгтэй үед "Үргэлжлүүлээд тусалъя" '
        'эсвэл "Дахин ирсэнд таатай байна" гэх мэт богино гүүр хэллэг л ашигла.'
    ),
    'returning': (
        '0. Хэрэглэгч 1+ хоногийн дараа эргэн ирлээ. Богино, дулаахан '
        'мэндчилгээгээр (жишээ нь "Сайн байна уу, эргэж ирсэнд таатай байна!") '
        'эхэлж, өмнө ярьсан зүйлээ зөөлөн санагдуулаад, "Юу тодруулах хэрэгтэй '
        'байна?" гэж асуу.'
    ),
}

FUNNEL_RULES = {
    'curious': (
        'ХЭРЭГЛЭГЧИЙН ҮЕ ШАТ: Сонирхож буй (discovery шат).\n'
        'Эхний 1-2 өгүүлбэрээр сургалтын үндсэн давуу талыг товч танилцуул '
        '(100% практик, 4 долоо хоног, сертификат, төгсөгчдөд Magic Finance '
        'программын үнэгүй license г.м.).\n'
        'Дараа нь зайлшгүй 1 НЭЭЛТТЭЙ АСУУЛТ асууж тэдний хэрэгцээг ойлгох '
        'хэрэгтэй. Дараах асуултуудаас тохирохыг сонго, заавал нэгийг л асуу '
        '(нэг хариултанд 2-3 асуулт битгий тулга):\n'
        '  - "Та өмнө нь нягтлан/санхүүгийн чиглэлээр ажиллаж байсан уу?"\n'
        '  - "Танд танхимаар суух уу, эсвэл онлайнаар суух нь илүү тохиромжтой вэ?"\n'
        '  - "Та одоо ажил/сургуультай юу? Ямар цагт суух нь илүү таатай вэ?"\n'
        '  - "Та энэ сургалтыг яагаад үзэх гэж байгаа вэ — карьерийн өөрчлөлт, '
        'одоогийн ажил, эсвэл өөрийн бизнест?"\n'
        'ЭНЭ ШАТАНД ҮНЭ, БҮРТГЭЛИЙН ЛИНК, УТАС ЛАВЛАХ ХҮСЭЛТИЙГ БҮҮ ТУЛГА. '
        'Гол зорилго: хэрэглэгчийг сонсож, тэдэнд тохирох ангийг олж өгөх. '
        'Зөвхөн хэрэглэгч өөрөө "бүртгүүлмээр" эсвэл "холбогдмоор" гэж '
        'хэлсэн үед л дараагийн шат руу шилжинэ.'
    ),
    'exploring_courses': (
        'ХЭРЭГЛЭГЧИЙН ҮЕ ШАТ: Сургалт сонгож буй.\n'
        'Хэрэглэгчийн хүсэлтэд тохирох тодорхой ангийн агуулга, 4 долоо '
        'хоногийн хөтөлбөр, цаг хуваарь, танхим/онлайн хэлбэрийг тодруулж '
        'тайлбарла.\n'
        'Хэрэв хэрэглэгчийн нөхцөл (цаг, түвшин, хэлбэр) тодорхойгүй хэвээр '
        'байвал НЭГ ТОДРУУЛАХ АСУУЛТ нэм (жишээ: "Танд өглөө/орой/амралтын '
        'өдрийн алин тохиромжтой вэ?"). Аль анги нь яагаад тэдэнд тохирох '
        'талаар санал бодлоо хэлж, эцсийн шийдвэрийг хэрэглэгчид үлдээ.\n'
        'Үнийн дэлгэрэнгүй болон бүртгэлийн линкийг ЗӨВХӨН хэрэглэгч '
        'тодорхой "үнэ хэд?" эсвэл "яаж бүртгүүлэх вэ?" гэж асуусан үед өг.'
    ),
    'pricing': (
        'ХЭРЭГЛЭГЧИЙН ҮЕ ШАТ: Үнэ судалж буй. Дөрвөн ангийн үнэ, PocketZero-оор '
        '4-6 хуваан хүүгүй төлөх боломж, Magic Finance программын 6 сарын '
        'үнэгүй ашиглах эрхийг онцолж тайлбарла. Төсөвт нь тохирох '
        'хувилбарыг санал болго. Хэрэглэгч сонирхлоо илэрхийлбэл БҮРТГЭЛИЙН '
        'ЛИНК + утас зэрэг хоёр сонголтыг өг.'
    ),
    'ready': (
        'ХЭРЭГЛЭГЧИЙН ҮЕ ШАТ: Бүртгүүлэхэд бэлэн. Богино урамшуулал ("Маш сайн '
        'сонголт!") хийж, БҮРТГЭЛИЙН ЛИНК-ийг хариултдаа оруул. Эсвэл утасны '
        'дугаараа үлдээгээрэй гэж нэм — хэрэглэгчид хоёр сонголтыг өг.'
    ),
}

# ===================== LLM PROMPT BUILDER =====================

def _format_training_snippets():
    """Build the additive-snippets section. High-priority snippets surface
    first so the model weights them more heavily. Returns '' when there are
    no active rows."""
    snippets = (TrainingSnippet.query
                .filter_by(is_active=True)
                .order_by(
                    db.case((TrainingSnippet.priority == 'high', 0), else_=1),
                    TrainingSnippet.sort_order.asc(),
                    TrainingSnippet.created_at.asc(),
                )
                .all())
    if not snippets:
        return ''
    lines = []
    for s in snippets:
        tag = f" [{s.category}]" if s.category else ''
        marker = '★ ' if s.priority == 'high' else ''
        lines.append(f"{marker}{s.title}{tag}:\n{s.body}")
    return "\n\n".join(lines)


def _format_team_members():
    members = (TeamMember.query
               .filter_by(is_active=True)
               .order_by(TeamMember.sort_order.asc(), TeamMember.id.asc())
               .all())
    if not members:
        return ''
    lines = []
    for m in members:
        parts = [m.name]
        if m.role:
            parts.append(f"({m.role})")
        line = ' '.join(parts)
        if m.specialty:
            line += f" — мэргэшил: {m.specialty}"
        if m.bio:
            line += f". {m.bio}"
        lines.append(f"- {line}")
    return "\n".join(lines)


def _format_product_entry(p):
    """One product under a business line, with its active ProductLinks
    listed beneath it. Each link's `kind` is preserved so the bot can
    semantically match user intent against it."""
    head = "  • "
    if p.product_number:
        head += f"[#{p.product_number}] "
    head += p.name
    if p.is_main_product:
        head += " ★main"
    if p.vendor:
        head += f" ({p.vendor})"
    lines = [head]
    if p.description:
        lines.append(f"    Тайлбар: {p.description}")
    if p.status_note:
        lines.append(f"    Тэмдэглэл: {p.status_note}")
    active_links = [l for l in p.links if l.is_active]
    if active_links:
        lines.append("    Холбоосууд:")
        for l in sorted(active_links, key=lambda x: (x.sort_order, x.id)):
            lines.append(f"      - {l.description}: {l.url}")
    return "\n".join(lines)


def _format_business_line_entry(b):
    """Single-line summary for a business line, with structured fields the
    admin filled in (address, sign-up channels, stats, child products)
    appended so the bot can quote them without parsing the freeform
    description. Address and main phone fall back to the global
    main_office_* settings when this line doesn't set its own."""
    parts = [f"- {b.name}"]
    if b.description:
        parts.append(f": {b.description}")

    extras = []
    if b.established_year:
        extras.append(f"үүсгэн байгуулагдсан: {b.established_year}")

    address = b.address or _svc.get_main_office_address()
    if address:
        # Tag whether the line is at its own address or the shared head
        # office, so the bot doesn't claim the line has a dedicated
        # address when it actually shares the main one.
        if b.address:
            extras.append(f"хаяг: {address}")
        else:
            extras.append(f"хаяг (Гол оффис): {address}")

    if b.contact_info:
        extras.append(f"холбоо барих: {b.contact_info}")
    else:
        main_phone = _svc.get_main_office_phone()
        if main_phone:
            extras.append(f"холбоо барих (Гол оффис): {main_phone}")

    if b.email:
        extras.append(f"имэйл: {b.email}")
    if extras:
        parts.append("\n  (" + "; ".join(extras) + ")")

    # Child Products (e.g. Magic Finance + Microsoft + Kaspersky under
    # the Program line). Sorted so the main product comes first.
    products = [p for p in (b.products or []) if p.is_active]
    if products:
        products.sort(key=lambda p: (not p.is_main_product, p.sort_order, p.id))
        parts.append("\n  Бүтээгдэхүүнүүд:\n" + "\n".join(_format_product_entry(p) for p in products))

    return "".join(parts)


def _format_service_entry(s):
    """One service in the catalog, with its active ServiceLinks listed beneath.
    Mirrors _format_product_entry so the bot sees services with the same shape
    as products and can quote the right form URL when the user asks about them."""
    parts = [f"- {s.name}"]
    if s.description:
        parts.append(f"  Тайлбар: {s.description}")
    if s.duration:
        parts.append(f"  Үргэлжлэх хугацаа: {s.duration}")
    if s.status_note:
        parts.append(f"  Тэмдэглэл: {s.status_note}")
    active_links = [l for l in (s.links or []) if l.is_active]
    if active_links:
        parts.append("  Холбоосууд:")
        for l in sorted(active_links, key=lambda x: (x.sort_order, x.id)):
            parts.append(f"    - {l.description}: {l.url}")
    return "\n".join(parts)


def _format_services():
    """Build the active-services block — audit, tax, CPA outsource, etc.
    Without this, the bot has no way to quote ServiceLink URLs (the request
    forms) and falls back to deferring to staff when a user asks about a
    service it should be confirming + offering directly."""
    services = (Service.query
                .options(joinedload(Service.links))
                .filter_by(is_active=True)
                .order_by(Service.sort_order.asc(), Service.id.asc())
                .all())
    if not services:
        return ''
    return "\n\n".join(_format_service_entry(s) for s in services)


def _format_business_lines():
    """Return (answer_block, refer_block, paused_block) — active lines split by
    action, plus a block for paused services so the bot knows not to sell them."""
    # Eager-load products and their links so the prompt build is 1 query
    # instead of 1 + (lines) + (products) + (products × links). Hot path:
    # this runs on every webhook message.
    all_lines = (BusinessLine.query
                 .options(joinedload(BusinessLine.products).joinedload(Product.links))
                 .order_by(BusinessLine.sort_order.asc(), BusinessLine.id.asc())
                 .all())
    answer, refer, paused = [], [], []
    for b in all_lines:
        if not b.is_active:
            note = f" ({b.status_note})" if b.status_note else " (түр зогссон)"
            paused.append(f"- {b.name}{note}")
            continue
        entry = _format_business_line_entry(b)
        if (b.action or 'refer') == 'answer':
            answer.append(entry)
        else:
            refer.append(entry)
    return "\n".join(answer), "\n".join(refer), "\n".join(paused)


def _format_course_entry(c, today_date):
    """Single line for a course in the canonical catalog block. Self-paced
    courses (100% Online) skip start/end dates; courses with a known
    duration_days quote it; admins' status_note is appended verbatim."""
    head = f"- [#{c.course_number}] " if c.course_number else "- "
    head += f"{c.name} ({c.course_type}): {int(c.price):,}₮"

    bits = [f"цаг: {c.time}"]
    if c.course_type != _svc.SELF_PACED_COURSE_TYPE:
        if c.start_date:
            bits.append(f"эхлэх: {c.start_date.strftime('%Y-%m-%d')}")
        if c.end_date:
            bits.append(f"дуусах: {c.end_date.strftime('%Y-%m-%d')}")
    else:
        bits.append("эхлэх: бүртгүүлсэн өдрөөс")
    if c.duration_days:
        bits.append(f"үргэлжлэх хугацаа: {c.duration_days} хоног")
    line = head + " | " + ", ".join(bits)
    if c.description:
        line += f"\n  Тайлбар: {c.description}"
    if c.status_note:
        line += f"\n  Тэмдэглэл: {c.status_note}"
    return line


def _format_courses_canonical():
    """Build the canonical course catalog block. Filters out non-recurring
    courses whose start_date is already in the past — they're stale and the
    bot should not be quoting them as upcoming. Sorts ascending so the
    nearest class is offered first."""
    today = datetime.utcnow().date()
    active = Course.query.filter_by(is_active=True).all()

    def is_quotable(c):
        if c.course_type == _svc.SELF_PACED_COURSE_TYPE:
            return True  # self-paced has no start date to be stale against
        if c.is_recurring:
            return True  # the refresh_dates flow keeps these current
        if not c.start_date:
            return True
        return c.start_date.date() >= today

    def sort_key(c):
        # Nearest upcoming start first; self-paced/no-date sort last.
        if not c.start_date:
            return (1, datetime.max)
        return (0, c.start_date)

    quotable = sorted([c for c in active if is_quotable(c)], key=sort_key)
    inactive_or_stale = [c for c in active if not is_quotable(c)]

    active_text = "\n".join(_format_course_entry(c, today) for c in quotable)

    paused_or_stale = []
    inactive = Course.query.filter_by(is_active=False).all()
    for c in inactive + inactive_or_stale:
        note = c.status_note or "түр зогссон"
        tag = f"#{c.course_number} " if c.course_number else ""
        paused_or_stale.append(f"- {tag}{c.name}: {note}")

    return active_text, "\n".join(paused_or_stale)


def _format_current_time_block():
    """Render a 'CURRENT TIME' block for the system prompt so the LLM can
    apply time-of-day rules in the persona (greetings, off-hours hints,
    "what time is it?" answers) against a real value instead of guessing.

    Server runs in UTC; Mongolia is UTC+8 with no DST, so we shift by 8.
    The block also names which bucket the persona rules apply to so the
    model doesn't have to map the hour itself.

    PRECEDENCE NOTE: this block tells the model to greet "per the persona
    rule" but SESSION_RULES['active'] / ['gap'] (injected later as the "0."
    item in ЧУХАЛ ДҮРМҮҮД) hard-override and forbid greetings on
    in-flight sessions. Personas effectively only choose the greeting for
    'new' / 'returning' sessions."""
    now_utc = datetime.utcnow()
    ub_hour = (now_utc.hour + 8) % 24
    ub_minute = now_utc.minute
    days_mn = ('Даваа', 'Мягмар', 'Лхагва', 'Пүрэв', 'Баасан', 'Бямба', 'Ням')
    weekday_mn = days_mn[now_utc.weekday()]
    if 6 <= ub_hour < 12:
        bucket = 'өглөө (06:00-12:00)'
    elif 12 <= ub_hour < 17:
        bucket = 'өдөр (12:00-17:00)'
    elif 17 <= ub_hour < 23:
        bucket = 'орой (17:00-23:00)'
    else:
        bucket = 'шөнө (23:00-06:00)'
    return (
        f"ОДООГИЙН ЦАГ (Улаанбаатарын цаг, UTC+8):\n"
        f"  {weekday_mn} гариг, {ub_hour:02d}:{ub_minute:02d}\n"
        f"  Цагийн ангилал: {bucket}\n"
        f"  Энэ цагт тохирох мэндчилгээг персонал дахь дүрмийн дагуу сонгоно — "
        f"ЗӨВХӨН чатын ЭХНИЙ хариултанд (session_state='new' буюу 'returning' "
        f"үед) л мэндэл. Үргэлжилсэн ярианы ДОТОР дахин 'сайн байна уу' гэх "
        f"мэт мэндчилгээг БҮҮ ДАВТА — хэрэглэгчид нэг сесст нэг л удаа мэндэлнэ. "
        f"Хэрэв хэрэглэгч 'одоо хэдэн цаг вэ?' гэж асуувал дээрх цагийг ашиглан хариулна.\n"
    )


def build_system_prompt(session_state='new', funnel_stage='curious', user_first_name='', handoff_pending=False):
    """Build system prompt with training, FAQ, session-state, funnel,
    and (optionally) handoff-advisory context."""
    training = _svc.get_training_content()
    persona = _svc.get_bot_persona()
    current_time_block = _format_current_time_block()
    faqs = FAQ.query.all()
    faq_text = "\n".join([f"Q: {faq.question}\nA: {faq.answer}" for faq in faqs])

    courses_text, paused_courses = _format_courses_canonical()

    snippets_text = _format_training_snippets()
    team_text = _format_team_members()
    answer_lines, refer_lines, paused_services = _format_business_lines()
    services_text = _format_services()

    google_form_url = _svc.get_google_form_url()
    if google_form_url:
        registration_block = (
            f"БҮРТГЭЛИЙН ЛИНК (ӨӨРӨӨ БӨГЛӨЖ БҮРТГҮҮЛЭХ ЗАМ):\n"
            f"{google_form_url}\n"
            f"Энэ нь хэрэглэгчийн ӨӨРИЙН биеэр одоо бөглөж бүртгүүлж "
            f"болох форм. Хэрэглэгч 'шууд бүртгүүлж болох уу?', "
            f"'яаж бүртгүүлэх вэ?', 'би одоо бүртгүүлмээр байна' гэх "
            f"мэт асуувал ЭНЭ ЛИНКИЙГ ҮНДСЭН ХАРИУЛТ БОЛГОЖ ӨГ. "
            f"Хэрэглэгчид 'утсаа үлдээ' гэж шаардахгүйгээр, форм "
            f"бөглөж бүртгүүлэх нь биеэ дааж шууд бүртгүүлэх "
            f"бодит сонголт юм. Утасны дугаар үлдээх нь ХОЁР ДАХЬ "
            f"сонголт (ажилтан тантай эргэж холбогдоно) — хоёуланг "
            f"нь нэг хариултанд оруулж, хэрэглэгчид сонгох эрхийг өг.\n"
        )
    else:
        registration_block = ""

    if user_first_name:
        name_block = (
            f"ХЭРЭГЛЭГЧИЙН НЭР: {user_first_name}\n"
            f"Хариултдаа тохиромжтой үед нэрээр нь хандаж болно (жишээ нь "
            f'"{user_first_name} аа,"). Гэхдээ нэрийг хэт олон бүү давт, '
            "байгалийн ярианд тохируулж хэрэглэ.\n"
        )
    else:
        name_block = ""

    session_rule = SESSION_RULES.get(session_state, SESSION_RULES['new'])
    funnel_rule = FUNNEL_RULES.get(funnel_stage, FUNNEL_RULES['curious'])

    snippets_section = (
        f"\nНЭМЭЛТ ТАЙЛБАР, ТОДРУУЛГА (★ = өндөр ач холбогдолтой):\n{snippets_text}\n"
        if snippets_text else ''
    )
    team_section = (
        f"\nМАНАЙ БАГ (хэрэглэгч багш/ажилтны талаар асуувал ашиглана):\n{team_text}\n"
        if team_text else ''
    )

    biz_section = ''
    if answer_lines or refer_lines:
        biz_section = "\nКОМПАНИЙН БУСАД ҮЙЛЧИЛГЭЭ:\n"
        if answer_lines:
            biz_section += (
                "Доорх үйлчилгээ — ТОВЧ хариулж болно. Хэрэглэгч асуухад "
                "доорх тайлбараас 1-2 өгүүлбэр бичээд, дэлгэрэнгүй үнэ, "
                "цагийн хувиараа ажилтнаас тодруулахыг санал болгоно:\n"
                f"{answer_lines}\n"
            )
        if refer_lines:
            biz_section += (
                "Доорх үйлчилгээний талаар хэрэглэгч асуувал ДАРААХ ДЭС "
                "ДАРААЛАЛЫГ БАРИМТАЛНА:\n"
                "  1) Доорх жагсаалтаас тухайн үйлчилгээний тайлбарыг "
                "ашиглан 1-2 богино өгүүлбэрээр \"Тийм ээ, бид энэ "
                "үйлчилгээг үзүүлдэг — ...\" гэж баталгаажуулна.\n"
                "  2) Доорх \"ҮЙЛЧИЛГЭЭНИЙ КАТАЛОГ\" хэсгээс энэ үйлчилгээний "
                "захиалгын линкийг (ServiceLink) ШУУД энэ хариултдаа оруул "
                "— ажилтны хариуг хүлээлгэлгүй хэрэглэгч өөрөө формоор "
                "захиалга өгөх боломжтой.\n"
                "  3) \"Танд энэ үйлчилгээтэй холбоотой тодруулах асуулт "
                "байна уу? Эсвэл утасны дугаараа үлдээвэл манай ажилтан "
                "эргэж холбогдоно\" гэж нэм. Хэрэглэгчид сонгох эрх өг.\n"
                "  4) Үнэ, тусгай нөхцөлийн дэлгэрэнгүй асуулт ирвэл "
                "ажилтан хариулна гэж зөвлөнө.\n"
                "  ✗ \"Таны асуултыг манай мэргэжилтэнд шилжүүлж байна\" "
                "гэж ЗААВАЛ БҮҮ хэл — линк бий бол шууд линкийг өг.\n"
                f"{refer_lines}\n"
            )
        if paused_services:
            biz_section += (
                "Доорх үйлчилгээнүүд ОДООГООР ТҮР ЗОГССОН байна. "
                "Хэрэглэгч эдгээрийн талаар асуувал зогссон гэдгийг шулуун хэлж, "
                "дахин нээгдэх үед мэдэгдэж болох эсэхийг тодруулна уу:\n"
                f"{paused_services}\n"
            )

    services_section = ''
    if services_text:
        services_section = (
            "\nҮЙЛЧИЛГЭЭНИЙ КАТАЛОГ (захиалгын форм/линкүүдтэй) — "
            "Хэрэглэгч аль нэг үйлчилгээний талаар асуухад доорх "
            "тайлбар + ХОЛБООСЫГ ШУУД хариултдаа ашигла. Линк бий "
            "бол ажилтан руу шилжүүлэхгүйгээр өөрөө хариулж болно:\n"
            f"{services_text}\n"
        )

    allowed_types_line = ", ".join(f'"{t}"' for t in _svc.ALLOWED_COURSE_TYPES)
    paused_courses_section = (
        f"\nТҮР ЗОГССОН / ХУУЧИРСАН АНГИУД (бүртгэл нээлттэй биш; "
        f"хэрэглэгч асуувал зогссон гэдгийг шулуун хэлж, эргэж нээгдэх үед "
        f"мэдэгдэнэ гэж хэлж болно):\n{paused_courses}\n"
        if paused_courses else ''
    )

    system_prompt = f"""{persona}

{current_time_block}
==========================
ИДЭВХТЭЙ АНГИУДЫН АЛБАН ЁСНЫ ЖАГСААЛТ (CANONICAL — энэ жагсаалтаас л үнэн зөв):
{courses_text}
{paused_courses_section}
ЭНЭ ЖАГСААЛТЫН ТУХАЙ ХАТУУ ДҮРЭМ:
- Ангийн нэр, цаг, эхлэх/дуусах огноо, үнэ, үргэлжлэх хугацааны тухай асуулт ирвэл ЗӨВХӨН энэ жагсаалтаас хариул.
- Жагсаалтад байхгүй цаг, үнэ, эсвэл огноо бүү зохио. Эргэлзвэл "Ажилтан танд эргэж лавлая" гэж хэлэн утас лав.
- Ангийн төрөл нь дараах нэрсээс л байна: {allowed_types_line}.
- Хэрэглэгч одоогийн ангийн цаг, огноо тохирохгүй гэвэл ЭНЭ ЖАГСААЛТААС өөр төрлийн (онлайн, оройн г.м.) эсвэл дараагийн нээгдсэн ангийг санал болго. Нэг анги л санал болгож үзээгүй мөртөө "өөр анги байхгүй" битгий хэл.
- Анги тус бүрд [#NNNN] гэсэн дугаар бий бол хариултдаа дурдвал админд хяналт тавихад тус болно (хэрэглэгчид заавал биш).
==========================

СУРГАЛТЫН ТӨВИЙН ЕРӨНХИЙ МЭДЭЭЛЭЛ:
{training}
{snippets_section}{team_section}{biz_section}{services_section}
ТҮГЭЭМЭЛ АСУУЛТУУД:
{faq_text}

{registration_block}{name_block}{funnel_rule}

{(_svc.HANDOFF_ADVISORY_RULE + chr(10) + chr(10)) if handoff_pending else ''}БОРЛУУЛАЛТЫН ЗАН ҮЙЛ (туршлагатай зөвлөгчийн загвар — найрсаг, тулгахгүй):

A. ИДЭВХТЭЙ СОНСОЛТ:
   Хэрэглэгчийн өгсөн гол үг/санааг хариултынхаа эхэнд 1 өгүүлбэрээр буцааж
   тодорхой болго ("Ойлголоо, та ... гэж хэлж байна"). Энэ нь хэрэглэгчийг
   "сонссон" гэж мэдрүүлэх ба үргэлжлүүлэхэд итгэл төрүүлнэ. Зөвхөн хэрэглэгч
   тодорхой санаа эсвэл нөхцөл бичсэн үед хэрэглэ — нэг үгийн мэндчилгээнд бүү
   ашигла.

B. ТОДОРХОЙ ӨВДӨЛТ/СААД ОЛОХ:
   Хариултыг бэлдэхээсээ өмнө "энэ хэрэглэгчийн ЯМАР асуудлыг шийдэхийг
   хүсэж байна?" гэж бод. Илэрхий биш бол шууд асуулт асуу — "Танд ямар
   асуудалд шийдэл хайж байна?" эсвэл "Энэ сургалт танд яагаад хэрэгтэй
   гэж бодов?". Зөв асуулт нь зөв шийдлээс илүү чухал.

C. ЭСРЭГ ХЭЛЛЭГ (objection handling) — ХЭРЭГЛЭГЧ ИЛЭРХИЙЛВЭЛ ЗАЙЛШГҮЙ
   ХАРИУЛНА:
   • "Үнэтэй / yntei / mungugui" → PocketZero-оор 4-6 хуваан хүүгүй төлж
     болохыг сануулж, Magic Finance программын 6 сар-1 жилийн ҮНЭГҮЙ
     license-ийг үнэ дэх давуу талаар онцол. "Сард 50,000₮ орчим
     өгөхөд тохиромжтой юу?" гэх мэт хувааж бод.
   • "Цаггүй / tsaggui / завгүй" → "100% Online" эсвэл оройн анги
     санал болго. Бие даан хийх хувилбарыг тайлбарла.
   • "Бодоод үзье / bodood uzye / эргэлзэж байна" → ШАХАЖ БҮҮ ОРУУЛ.
     "Мэдээж, чухал шийдвэр. Эргэлзээ юунд нь байгаа вэ?" гэх мэтээр
     тодорхойлох асуулт асуу. Хэрэв тэр шалтгаан нь дээрх objection-ы
     аль нэг бол түүнийг шийдэж өг.
   • "Дараа бүртгүүлнэ / daraa burtguulne" → дараагийн нээгдэх ангийн
     огноог тодорхой хэлээд "тэр үед сануулга илгээх үү?" гэж зөөлөн
     санал болго. Ангиудын онлайн хувилбар нь хүссэн үед эхлэх
     боломжтой гэж сануул.
   • "Танхим хол / tankhim khol / алслагдсан" → Hybrid эсвэл 100%
     Online хувилбарыг тайлбарлаж, тэр оршин суугаа газраас огт явахгүйгээр
     суух боломжийг онцол.
   • "Ажилтай / aiijiltai / busy" → оройн / weekend / онлайн хувилбар
     санал болго. Ажил эрхэлж байгаа хүмүүст таарах гэж онцолсон ангиудыг
     заана.

D. НИЙГМИЙН БАТАЛГАА (social proof) — БУУ ЗОХИО:
   Зөвхөн дээрх "СУРГАЛТЫН ТӨВИЙН ЕРӨНХИЙ МЭДЭЭЛЭЛ" блок, "НЭМЭЛТ
   ТАЙЛБАР, ТОДРУУЛГА" snippet-ууд, эсвэл "ТҮГЭЭМЭЛ АСУУЛТУУД" хэсэгт бий
   нь тодорхой тоо, төгсөгчийн түүх, бодит баримтыг иш тат. Дээрх блокт
   байхгүй бол ҮЛ ЗОХИО — "олон төгсөгч ажиллаж байна" гэх мэт ерөнхий
   үг ашиглаж болох ч тодорхой тоо бүү гарга.

E. TRIAL CLOSE — exploring_courses → pricing шилжих үе:
   Хэрэглэгчийн нөхцөл (цаг, түвшин, хэлбэр)-ийн дор хаяж 2-ыг ойлгосон
   үед ЗӨӨЛӨН тестийн асуулт асуу: "Хэрэв хуваариар таарвал [санал
   болгож буй ангид] суух сонирхолтой юу?" эсвэл "Үнэ, хуваариа таарах
   юм бол энэ анги танд хэр зэрэг тохирох вэ?". Хариу "тийм/яриад үзье"
   бол үнэ + бүртгэлийн линкийг өг. Хариу "үгүй/эргэлзэж байна" бол
   эсрэг хэллэгийн логикоор зөв objection-ыг олж тодор.

F. ӨМНӨХ ХАРИЛЦАА ГҮН:
   Хэрэглэгч өмнө нь юу хэлснийг иш татаж, шинэ асуултыг тэр контекстээр
   баяжуул. Жишээ: "Та эхэндээ ажилтай гэж хэлсэн — тиймээс оройн анги
   танд илүү тохиромжтой байж магадгүй." Богино санах ой = итгэлцлийн
   суурь.

ЧУХАЛ ДҮРМҮҮД:
{session_rule}
1. Хариултыг товч (3 өгүүлбэрээс ихгүй) бөгөөд тодорхой бичнэ. Урт жагсаалт оруулахаас зайлсхий.
2. Сургалтын давуу талыг хэт зар сурталчилгаа маягтай бус, итгэлтэй найз шиг зөвлөнө.
3. БҮРТГЭЛИЙН ЛИНК + УТАС ЗЭРЭГ САНАЛ — ЗӨВХӨН ДАРААХ ҮЕД АШИГЛА:
   (а) Хэрэглэгч өөрөө "бүртгүүлмээр", "холбогдмоор", "ажилтантай яримаар", "шууд бүртгүүлж болох уу?", "яаж бүртгүүлэх вэ?" гэх мэт тодорхой шилжих хүсэл илэрхийлсэн;
   (б) Хэрэглэгч pricing/ready үе шатанд ороод үнийн дэлгэрэнгүй асуусан;
   (в) Сургалтаас өөр чиглэлийн үйлчилгээ (audit, consulting, бүтээгдэхүүн г.м.)-ийн талаарх асуулт, хариулт нь "manai mergejilten" зэрэг чиглүүлгийн хариулт байх үед.

   Эдгээр үед БҮРТГЭЛИЙН ЛИНК (байвал) + "эсвэл утасны дугаараа үлдээгээрэй" гэсэн ХОЁР СОНГОЛТЫГ ЗЭРЭГ өг.

   ОНЦГОЙ КЕЙС — "шууд бүртгүүлж болох уу?" / "одоо яаж бүртгүүлэх вэ?" гэх мэт:
   Хариулт нь "Болохгүй, утсаа үлдээх ёстой" БҮҮ БАЙ. Тогтсон форм бий бол ШУУД ҮЗҮҮЛЭХ ХЭРЭГТЭЙ — энэ нь өөрийн биеэр бүртгүүлэх бодит зам. Жишээ хариулт: "Тийм ээ, та одоо энэ форм-оор шууд бүртгүүлж болно: [LINK]. Эсвэл утасны дугаараа үлдээвэл бүртгэлийн ажилтан тантай холбогдож бүртгэлийг хийнэ. Аль нь танд таатай вэ?"

   Хэрэглэгч "одоохондоо утсаа үлдээмээргүй байна" гэвэл шахаж бүү давт. Форм линкийг үзүүлж "хэдийд хүсвэл энэ замаар өөрөө бүртгүүлж болно" гэж тайвшруулна уу.

   Дискавери (curious/exploring_courses) шатанд хэзээ утас/линк тавихыг өмнөх FUNNEL RULE тодорхойлсон — давтан энд бүү бич.
4. Хэрэглэгч утасны дугаар бичсэн бол баярлал илэрхийлж, "Манай ажилтан удахгүй тантай холбогдоно" гэж мэдэгд.
5. Шийдэх боломжгүй буюу мэдэхгүй асуудал тулгарвал "Энэ асуудлыг манай ажилтан тантай эргэж холбогдож тодруулна" гэж хэлээд Дүрэм 3-ын дагуу хоёр сонголтыг өг.
6. Эмодзи цөөн (1-2) хэрэглэж, илүү гар бичмэл маяг бүү аватарла.
7. Өгүүлбэрүүдийн эхлэлийг сольж бай, "Сургалт..." гэх мэт ижил үгээр дандаа бүү эхэл.
8. Сургалтаас өөр чиглэлийн (компанийн бусад үйлчилгээ) асуултанд дээрх "КОМПАНИЙН БУСАД ҮЙЛЧИЛГЭЭ" хэсгийн дүрмийн дагуу хариулна. Жагсаалтад байхгүй чиглэл бол "Тантай ажилтан холбогдох уу?" гэж асууж дугаар лав.
9. БҮТЭЭГДЭХҮҮНИЙ ДҮРЭМ: Бот бүтээгдэхүүн (Magic Finance, Microsoft license, Kaspersky г.м.)-ийн үнэ, санал хэлэхгүй. Худалдан авах, дэмжлэг (ticket), гарын авлага (manual), community, татах (download) гэх мэт асуулт ирэхэд тухайн бүтээгдэхүүний "Холбоосууд" жагсаалтаас хэрэглэгчийн санаатай таарах нэгийг сонгож, тэр линкийг өгөөд "манай ажилтан тантай холбогдож үнэ, нөхцөл нь дэлгэрэнгүй хэлэлцэнэ" гэж нэм. Жагсаалтад тохирох холбоос байхгүй бол үнэ зохиохгүйгээр ажилтантай холбогдох санал тавь.
10. ҮЙЛЧИЛГЭЭГ ҮГҮЙСГЭХГҮЙ ДҮРЭМ:
   (а) Хэрэглэгчийн асуусан үйлчилгээ дээрх "КОМПАНИЙН БУСАД ҮЙЛЧИЛГЭЭ" жагсаалтад БАЙВАЛ — тэндхийн тайлбарыг ашиглан 1-2 өгүүлбэрээр баталгаажуул, дэс дарааллын дагуу үргэлжлүүл. ЗААВАЛ утас лавлахаас өмнө сонирхол тодруул.
   (б) Хэрэглэгчийн асуусан үйлчилгээ жагсаалтад БАЙХГҮЙ бол "бид санал болгохгүй", "манай төвд байхгүй", "энэ үйлчилгээ байхгүй" гэж ХЭЗЭЭ Ч БҮҮ ХЭЛ. Үүний оронд: "Энэ талаар манай мэргэжлийн ажилтан танд дэлгэрэнгүй хариулна — утасны дугаараа үлдээх үү?" гэж эелдгээр хариул.
   (в) "ТҮР ЗОГССОН" гэж тэмдэглэгдсэн үйлчилгээний хувьд "одоогоор зогссон" гэж шулуун хэл, дахин нээгдэх үед мэдэгдэж болохыг санал болго."""
    return system_prompt



