"""Schema migration, training-data linter, and seed routines.

Extracted from services/__init__.py for navigability. Cross-module
references to services-level helpers (get_setting, get_bot_persona,
get_training_content, SELF_PACED_COURSE_TYPE, ALLOWED_COURSE_TYPES)
go through ``<name>`` rather than direct imports — see the
matching comment in services/_prompt.py for the rationale.
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta

from extensions import db
from models import (BusinessLine, ChatQuestionCluster, Course, FAQ,
                    HandoffKeyword, Product, ProductLink, Service,
                    ServiceLink, TrainingSnippet)

import services as _svc

logger = logging.getLogger(__name__)


# ===================== SCHEMA MIGRATION =====================

def ensure_schema():
    """Idempotent schema migration for columns added after the first deploy.

    SQLite-style ALTER TABLE ADD COLUMN works on Postgres too, so this stays
    portable if the user later switches the SQLALCHEMY_DATABASE_URI.
    """
    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(db.engine)
    if 'facebook_user' not in inspector.get_table_names():
        return  # create_all() will handle a brand-new DB

    def add_columns(table, additions):
        existing = {col['name'] for col in inspector.get_columns(table)}
        with db.engine.begin() as conn:
            for col, clause in additions.items():
                if col not in existing:
                    try:
                        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {clause}")
                        logger.info("Schema migration: %s ADD COLUMN %s", table, col)
                    except Exception as e:
                        logger.info("Schema migration skipped (%s.%s): %s", table, col, e)

    add_columns('facebook_user', {
        'funnel_stage': "funnel_stage VARCHAR(30) DEFAULT 'curious'",
        'last_nudge_at': 'last_nudge_at DATETIME',
        'bot_muted_until': 'bot_muted_until DATETIME',
        'conversation_topic': 'conversation_topic VARCHAR(100)',
        'last_mute_ack_at': 'last_mute_ack_at DATETIME',
    })

    add_columns('admin_issue', {
        'updated_by_id': 'updated_by_id INTEGER REFERENCES user(id)',
        'updated_at': 'updated_at DATETIME',
        'notes': 'notes TEXT',
    })

    add_columns('course', {
        'end_date': 'end_date DATETIME',
        'is_recurring': 'is_recurring BOOLEAN DEFAULT 0',
        'schedule_template': 'schedule_template TEXT',
        'status_note': 'status_note VARCHAR(255)',
        # Admin-assigned external code. Left nullable so existing rows don't
        # block the migration; uniqueness is enforced by the admin POST.
        'course_number': 'course_number INTEGER',
        'duration_days': 'duration_days INTEGER',
    })

    # ensure_schema() is the legacy migration path (pre-Alembic). Going forward,
    # schema changes go through migrations/versions/. We keep the existing
    # entries here so old DBs that boot without ever applying Alembic still
    # self-heal. The 5 BU columns (signup_form_url, signup_phone, exam_form_url,
    # num_products_or_services, total_clients_or_users) were removed in Phase 1
    # and intentionally NOT added back here.
    add_columns('business_line', {
        'status_note': 'status_note VARCHAR(255)',
        'address': 'address TEXT',
        'email': 'email VARCHAR(200)',
        'established_year': 'established_year INTEGER',
    })

    # Product + ProductLink were added after the initial schema; create them
    # on existing DBs that never ran create_all() against the new tables.
    if 'product' not in inspector.get_table_names():
        with db.engine.begin() as conn:
            try:
                conn.exec_driver_sql(
                    "CREATE TABLE product ("
                    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "  business_line_id INTEGER NOT NULL REFERENCES business_line(id),"
                    "  product_number INTEGER UNIQUE,"
                    "  name VARCHAR(200) NOT NULL,"
                    "  vendor VARCHAR(120),"
                    "  description TEXT,"
                    "  is_main_product BOOLEAN DEFAULT 0,"
                    "  is_active BOOLEAN DEFAULT 1,"
                    "  status_note VARCHAR(255),"
                    "  sort_order INTEGER DEFAULT 0,"
                    "  created_at DATETIME,"
                    "  updated_at DATETIME"
                    ")"
                )
                logger.info("Schema migration: created product table")
            except Exception as e:
                logger.info("Schema migration skipped (product table): %s", e)
    if 'product_link' not in inspector.get_table_names():
        with db.engine.begin() as conn:
            try:
                conn.exec_driver_sql(
                    "CREATE TABLE product_link ("
                    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "  product_id INTEGER NOT NULL REFERENCES product(id),"
                    "  kind VARCHAR(40) NOT NULL,"
                    "  label VARCHAR(160),"
                    "  url VARCHAR(500) NOT NULL,"
                    "  is_active BOOLEAN DEFAULT 1,"
                    "  sort_order INTEGER DEFAULT 0,"
                    "  created_at DATETIME"
                    ")"
                )
                logger.info("Schema migration: created product_link table")
            except Exception as e:
                logger.info("Schema migration skipped (product_link table): %s", e)

    if 'handoff_keyword' not in inspector.get_table_names():
        with db.engine.begin() as conn:
            try:
                conn.exec_driver_sql(
                    "CREATE TABLE handoff_keyword ("
                    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "  keyword VARCHAR(200) NOT NULL,"
                    "  keyword_type VARCHAR(20) DEFAULT 'explicit',"
                    "  is_active BOOLEAN DEFAULT 1,"
                    "  note VARCHAR(255),"
                    "  created_at DATETIME"
                    ")"
                )
                logger.info("Schema migration: created handoff_keyword table")
            except Exception as e:
                logger.info("Schema migration skipped (handoff_keyword table): %s", e)



# ===================== TRAINING-DATA LINTER =====================

# Module-level regexes used by lint_training_data() to spot prices / phones /
# URLs that drift between unstructured prose and the structured catalogs.
_PRICE_RE = re.compile(r'(\d{1,3}(?:[, ]\d{3})+|\d{4,})\s*₮')
_PHONE_RE = re.compile(r'\b\d{4}[\s-]?\d{4}\b')
_URL_RE = re.compile(r'https?://[^\s)"\'<>]+')
_PRICE_THRESHOLD = 1000  # ignore tiny numbers like "100% Online — 100"


def _normalize_phone(s):
    return re.sub(r'[\s-]', '', s)


def _normalize_int(s):
    return int(re.sub(r'[^\d]', '', s))


def lint_training_data():
    """Cross-reference unstructured prose (FAQ answers, persona, training
    intro, snippets) against the structured catalogs (Course, BusinessLine).

    Flags prices/phones/URLs that show up in prose but don't match any
    structured field — usually a sign the prose got out of sync with the
    catalog. Returns a list of findings ordered worst-first."""
    findings = []

    course_prices = {int(c.price) for c in Course.query.all() if c.price}
    biz_phones = set()
    biz_urls = set()
    biz_text_prices = set()
    # Pull authoritative phone numbers + URLs the bot is allowed to quote.
    # Sources: per-BU contact_info, plus every active ProductLink under each BU.
    # Used by the lint check below to flag text references that don't match.
    for b in BusinessLine.query.all():
        for m in _PHONE_RE.finditer(b.contact_info or ''):
            biz_phones.add(_normalize_phone(m.group()))
        if b.description:
            for m in _PRICE_RE.finditer(b.description):
                try:
                    biz_text_prices.add(_normalize_int(m.group(1)))
                except ValueError:
                    pass
        for p in (b.products or []):
            for l in (p.links or []):
                if l.is_active and l.url:
                    biz_urls.add(l.url.strip())

    expected_prices = course_prices | biz_text_prices

    sources = []
    for faq in FAQ.query.all():
        sources.append(('FAQ', faq.id, faq.question or '', faq.answer or ''))
    sources.append(('Persona', 0, 'bot_persona', _svc.get_bot_persona()))
    sources.append(('Training', 0, 'training_content', _svc.get_training_content()))
    for s in TrainingSnippet.query.filter_by(is_active=True).all():
        sources.append(('Snippet', s.id, s.title or '', s.body or ''))

    for kind, sid, label, text in sources:
        if not text:
            continue
        for m in _PRICE_RE.finditer(text):
            try:
                value = _normalize_int(m.group(1))
            except ValueError:
                continue
            if value < _PRICE_THRESHOLD:
                continue
            if value not in expected_prices:
                findings.append({
                    'severity': 'warning',
                    'kind': 'price_drift',
                    'source_kind': kind,
                    'source_id': sid,
                    'label': label[:80],
                    'detail': (
                        f'"{m.group()}" гэсэн үнэ ангийн каталог болон '
                        'үйлчилгээний тайлбарт олдсонгүй.'
                    ),
                })
        for m in _PHONE_RE.finditer(text):
            ph_norm = _normalize_phone(m.group())
            if ph_norm not in biz_phones:
                findings.append({
                    'severity': 'warning',
                    'kind': 'phone_drift',
                    'source_kind': kind,
                    'source_id': sid,
                    'label': label[:80],
                    'detail': (
                        f'"{m.group()}" дугаар үйлчилгээний contact_info-д '
                        'бүртгэлтэй биш.'
                    ),
                })
        for m in _URL_RE.finditer(text):
            url = m.group().rstrip('.,);')
            if url not in biz_urls:
                findings.append({
                    'severity': 'info',
                    'kind': 'url_drift',
                    'source_kind': kind,
                    'source_id': sid,
                    'label': label[:80],
                    'detail': (
                        f'{url} линк бизнесийн чиглэлийн product_link-д '
                        'бүртгэлтэй биш.'
                    ),
                })

    today = datetime.utcnow().date()
    for c in Course.query.filter_by(is_active=True).all():
        if c.course_type != _svc.SELF_PACED_COURSE_TYPE and not c.is_recurring:
            ref = c.end_date or c.start_date
            if ref and ref.date() < today:
                findings.append({
                    'severity': 'error',
                    'kind': 'stale_course',
                    'source_kind': 'Course',
                    'source_id': c.id,
                    'label': c.name[:80],
                    'detail': (
                        f'Идэвхтэй боловч огноо ({ref.strftime("%Y-%m-%d")}) өнгөрсөн. '
                        '"Огноо шинэчлэх / Архивлах" товчийг дарна уу.'
                    ),
                })
        if c.course_type == _svc.SELF_PACED_COURSE_TYPE and (c.start_date or c.end_date):
            findings.append({
                'severity': 'warning',
                'kind': 'self_paced_has_dates',
                'source_kind': 'Course',
                'source_id': c.id,
                'label': c.name[:80],
                'detail': '100% Online анги нь огноотой байх ёсгүй. Засаж дахин хадгална уу.',
            })

    severity_order = {'error': 0, 'warning': 1, 'info': 2}
    findings.sort(key=lambda f: (severity_order.get(f['severity'], 9),
                                  f['source_kind'], f['source_id']))
    return findings


# ===================== SEED / RECURRING =====================

def advance_recurring_courses():
    """For each is_recurring Course past its end_date, set next start/end dates.
    First Monday >= 4 weeks after current end_date."""
    now = datetime.utcnow()
    updated = 0
    for course in Course.query.filter_by(is_recurring=True, is_active=True).all():
        ref_date = course.end_date or course.start_date
        if not ref_date or ref_date > now:
            continue
        candidate = ref_date + timedelta(weeks=4)
        days_until_monday = (7 - candidate.weekday()) % 7
        next_start = candidate + timedelta(days=days_until_monday)
        if course.end_date and course.start_date:
            duration = course.end_date - course.start_date
        else:
            duration = timedelta(weeks=4)
        course.start_date = next_start
        course.end_date = next_start + duration
        updated += 1
    if updated:
        db.session.commit()
        logger.info("Recurring courses: advanced %s course(s).", updated)
    return updated


def archive_past_courses():
    """Flip non-recurring, non-self-paced active Courses whose end_date (or
    start_date when no end is set) has already passed to is_active=False.

    Run from the admin "Refresh dates" button so a one-click pass keeps the
    bot from quoting last month's classroom session as if it were upcoming.
    Returns how many courses were flipped."""
    now = datetime.utcnow()
    archived = 0
    candidates = Course.query.filter_by(is_active=True, is_recurring=False).all()
    for course in candidates:
        if course.course_type == _svc.SELF_PACED_COURSE_TYPE:
            continue
        reference = course.end_date or course.start_date
        if not reference:
            continue
        if reference < now:
            course.is_active = False
            if not course.status_note:
                course.status_note = (
                    f"Автомат архивлагдсан ({reference.strftime('%Y-%m-%d')}-ны "
                    "огноо өнгөрсний дараа). Шинээр нээх бол огноог шинэчилнэ үү."
                )
            archived += 1
    if archived:
        db.session.commit()
        logger.info("Archived %s past course(s).", archived)
    return archived


def seed_products():
    """Seed 3 starter Products under the Program / Magic Finance business
    line so admins have rows to edit-in-place rather than create from
    scratch. Idempotent: skipped when Products already exist, or when
    SEED_DEFAULTS=false (i.e. non-MagicBot deployments)."""
    if os.environ.get('SEED_DEFAULTS', 'true').strip().lower() != 'true':
        return
    if Product.query.count() > 0:
        return

    # Match in Python because SQLite's ILIKE doesn't case-fold Cyrillic
    # correctly, which would otherwise miss names like "Программ ба License".
    needles = ('magic finance', 'программ', 'software', 'magic cloud')
    program_line = None
    for line in BusinessLine.query.all():
        if any(n in (line.name or '').lower() for n in needles):
            program_line = line
            break
    if not program_line:
        logger.info("seed_products: no program/software BusinessLine found — skipping.")
        return

    starters = [
        {
            'product_number': 2001,
            'name': 'Magic Finance',
            'vendor': 'Magic Cloud LLC',
            'description': (
                'Санхүү, татварын тайлан гаргахад зориулсан программ. 90 орчим '
                'төрлийн тайлан гаргах боломжтой (Санхүүгийн, Татварын, '
                'Удирдлагын, Туслах). Сургалтын төгсөгчдөд үнэгүй license '
                'олгоно: Танхим (100% танхим) ба Багштай онлайн → 1 жил; '
                'Хосолсон ба 100% Онлайн → 6 сар.'
            ),
            'is_main_product': True,
            'sort_order': 0,
        },
        {
            'product_number': 2002,
            'name': 'Microsoft license',
            'vendor': 'Microsoft',
            'description': 'Microsoft программ хангамжийн license. Дэлгэрэнгүйг ажилтнаас лавлана уу.',
            'is_main_product': False,
            'sort_order': 1,
        },
        {
            'product_number': 2003,
            'name': 'Kaspersky license',
            'vendor': 'Kaspersky',
            'description': 'Kaspersky аюулгүй байдлын программ хангамжийн license. Дэлгэрэнгүйг ажилтнаас лавлана уу.',
            'is_main_product': False,
            'sort_order': 2,
        },
    ]
    for s in starters:
        db.session.add(Product(business_line_id=program_line.id, **s))
    db.session.commit()
    logger.info("Seeded %s starter products under '%s'.", len(starters), program_line.name)


_DEFAULT_HANDOFF_KEYWORDS_EXPLICIT = [
    # Cyrillic
    'ажилтан', 'оператор', 'менежер', 'админтай', 'жинхэнэ хүн',
    'хүнтэй', 'хүн рүү', 'хүн руу', 'хүний хариу', 'хүнээр',
    'хүнтэй яр', 'хүнтэй ярь', 'хүнтэй холб',
    # Latin transliterations users commonly type from a phone without
    # the Mongolian keyboard layout. Substrings work because the matcher
    # does `if keyword in lower(message_text)`.
    'azhiltan', 'azhilten', 'operator', 'menejer', 'admintai',
    'hun-tei', 'huntei', 'hun ruu', 'hun-ruu', 'hunii hariu',
    'huntei yar', 'huntei chatla', 'huntei chin chatla',
    # English equivalents
    'live agent', 'real agent', 'real person', 'human agent',
    'speak to human', 'talk to human', 'talk to a person',
    'operator please', 'real human',
]
_DEFAULT_HANDOFF_KEYWORDS_FRUSTRATION = [
    'болохгүй байна', 'ойлгохгүй', 'ойлгомжгүй', 'муухай', 'үнэхээр муу',
    'гомдол', 'буруу хариу', 'хариулж чадахгүй', 'юу яриад байгаа',
    'хэрэггүй бот', 'утгагүй', 'ойлгосонгүй',
]


def seed_discovery_phrasing_snippets():
    """Seed TrainingSnippet rows that map common customer phrasings (in
    Cyrillic and Latin transliteration) to the right service / product
    / course. Helps gpt-4o-mini route reliably even when customers don't
    use the exact service name.

    Idempotent: matches by snippet title — re-running is a no-op for
    existing rows, and adds only the missing ones. Returns a multi-line
    log suitable for surfacing via the admin endpoint."""
    log = []

    seeds = [
        {
            'title': 'Аудит асуултын чиглэл',
            'category': 'service-routing',
            'priority': 'high',
            'body': (
                "Хэрэглэгч 'аудит хийдэг үү?', 'audit baigaa yu?', "
                "'санхүүгийн тайлан баталгаажуулах', 'санхүүгийн аудит', "
                "'auditiin uilchilgee', 'audit hiilgemeer', 'аудитийн "
                "үйлчилгээ', 'нягтлан баталгаажуулах' гэх мэт асуувал — "
                "Тийм ээ, бид аудитын үйлчилгээ үзүүлдэг (Magic Consulting "
                "Audit). Үйлчилгээний дэлгэрэнгүй болон захиалгын формыг "
                "хариултдаа оруул."
            ),
        },
        {
            'title': 'Татвар, тайлан, нягтлан outsource асуултын чиглэл',
            'category': 'service-routing',
            'priority': 'high',
            'body': (
                "Хэрэглэгч 'татварын тайлан гаргах', 'тайлан гаргаж "
                "өгөх', 'тайлан хийж өгөх', 'нягтлан outsource', "
                "'нягтлагийн outsource', 'татвар бодуулах', 'tatvariin "
                "tailan', 'tailan gargaj uguh', 'nyagtlan outsource', "
                "'tatvariin medeelel', 'санхүүгийн тайлан хийж өгдөг үү?' "
                "гэх мэт асуувал — Татварын мэргэшсэн зөвлөх үйлчилгээ "
                "рүү чиглүүл (Magic Consulting Audit-ын дотор), форм "
                "линкийг хариултдаа оруул."
            ),
        },
        {
            'title': 'Magic Finance програмын асуултын чиглэл',
            'category': 'product-routing',
            'priority': 'normal',
            'body': (
                "Magic Finance програмын тухай асуултуудыг таних:\n"
                "  • 'татаж авах', 'татах', 'шинэ хувилбар', 'татвар "
                "татах', 'download', 'татацгаах' → татах хуудасны линк\n"
                "  • 'заавар', 'гарын авлага', 'хэрхэн ашиглах', 'help', "
                "'instruction', 'video' → help center линк\n"
                "  • 'код', 'лиценз сунгуулах', 'хугацаа сунгах', 'код "
                "авах', 'license renewal', 'code avah' → renewal линк\n"
                "  • 'тайлан шалгуулах', 'файл шалгуулах', 'алдаатай "
                "тайлан', 'support', 'ticket', 'дэмжлэг' → support "
                "ticket линк\n"
                "  • 'хэрэглэгчийн групп', 'facebook групп', 'community' "
                "→ Facebook групп линк\n"
                "  • 'шинэчлэлт', 'update', 'версия', 'шинэ боломж' → "
                "шинэчлэлтийн мэдээллийн линк\n"
                "Бүгдийг Magic Finance бүтээгдэхүүний холбоосуудаас сонг."
            ),
        },
        {
            'title': 'Microsoft Office license асуулт',
            'category': 'product-routing',
            'priority': 'normal',
            'body': (
                "'MS Office', 'Microsoft Office', 'Word', 'Excel', "
                "'PowerPoint license', 'офис license', 'microsoftiin "
                "license', 'office program' гэх мэт асуувал Microsoft "
                "license бүтээгдэхүүний захиалгын форм линкийг өг. "
                "Үнэ, нөхцлийн дэлгэрэнгүй ажилтан хариулна."
            ),
        },
        {
            'title': 'Сургалтад бүртгүүлэх асуултын чиглэл',
            'category': 'course-routing',
            'priority': 'high',
            'body': (
                "Хэрэглэгч 'бүртгүүлэх', 'элсэх', 'яаж бүртгүүлэх', "
                "'шууд бүртгүүлэх боломжтой юу?', 'register hiih', "
                "'register hiimer', 'burtguulj boloh uu?', 'enroll', "
                "'элсэлт' гэх мэт асуувал ЭХЛЭЭД БҮРТГЭЛИЙН ЛИНКийг "
                "үндсэн хариулт болгож үзүүл — энэ нь өөрөө бөглөж "
                "бүртгүүлэх форм. Эсвэл утсаа үлдээвэл ажилтан "
                "холбогдоно гэдгийг хоёрдогч сонголт болгож нэм. "
                "Хэрэглэгчээс заавал утас ШААРДАХГҮЙ — форм линк нь "
                "хүчинтэй бие даасан зам."
            ),
        },
        {
            'title': 'Анги, хичээл тодруулах асуулт',
            'category': 'course-routing',
            'priority': 'normal',
            'body': (
                "'Хичээл хэдэн цагт?', 'танхимаар үзэх боломжтой юу?', "
                "'онлайн боломжтой юу?', 'когорт', 'хэдэн долоо "
                "хоног үргэлжилдэг вэ?', 'hicheel hed tsagt?', 'class "
                "schedule', 'tankhim baigaa yu?' гэх мэт асуултанд "
                "Канонокал ангийн жагсаалтаас хариулна. Жагсаалтад "
                "байхгүй цаг, төрөл, огноо БҮҮ ЗОХИО."
            ),
        },
        {
            'title': 'Бизнес/компанид зориулсан асуулт',
            'category': 'service-routing',
            'priority': 'normal',
            'body': (
                "'Компанид сургалт', 'дотоод сургалт', 'corporate "
                "training', 'команд', 'бөөнөөр', 'b2b' гэх мэт "
                "асуувал manai mergejilten holbogdoh saanal tavi. "
                "Энэ нь BU-н түвшин дэх захиалгын асуудал тул "
                "ажилтанд чиглүүлэх нь зөв."
            ),
        },
    ]

    existing_titles = {
        t for (t,) in db.session.query(TrainingSnippet.title).all() if t
    }
    inserted = 0
    for s in seeds:
        if s['title'] in existing_titles:
            log.append(f"SKIPPED (already exists): {s['title']}")
            continue
        db.session.add(TrainingSnippet(
            title=s['title'],
            body=s['body'],
            category=s['category'],
            priority=s['priority'],
            is_active=True,
        ))
        existing_titles.add(s['title'])
        inserted += 1
        log.append(f"Added: {s['title']} (priority={s['priority']})")
    db.session.commit()
    log.append(f"\nTotal added: {inserted}. Total skipped: {len(seeds) - inserted}.")
    return '\n'.join(log)


def seed_default_magic_links():
    """One-shot helper that wires Magic Financial Group's known set of
    product/service URLs into the catalog. Idempotent: matches links by
    URL so re-running doesn't create duplicates. Returns a multi-line
    log of what it did, suitable for surfacing to the admin via the
    /admin/api/seed-default-links endpoint."""
    from models import (BusinessLine, Course, CourseLink, Product, ProductLink,
                        Service, ServiceLink)

    log = []

    # The course registration form is a global setting (it lives at the top
    # of the system prompt as БҮРТГЭЛИЙН ЛИНК). Set it first so the bot
    # surfaces it for any "how do I register?" question.
    course_form_url = (
        'https://docs.google.com/forms/d/e/'
        '1FAIpQLSejDvCSqo6J5cgqrdZdnzttz-1ahobmypNr0wLlPTRGehtEog/viewform'
    )
    existing = GeneralSetting.query.filter_by(key='google_form_url').first()
    if existing:
        if (existing.value or '').strip() != course_form_url:
            existing.value = course_form_url
            log.append(f'Updated GeneralSetting.google_form_url -> {course_form_url}')
        else:
            log.append('GeneralSetting.google_form_url already set; left unchanged.')
    else:
        db.session.add(GeneralSetting(key='google_form_url', value=course_form_url))
        log.append(f'Created GeneralSetting.google_form_url = {course_form_url}')

    # ----- Product / Service / Course link maps -----
    # Each entry: (item-finder, link-table-class, fk-attr, [(description, url, note)...])
    # Item finders take a session and return the SQLAlchemy row to attach to.

    def _find_product(name_substrings):
        for s in name_substrings:
            row = Product.query.filter(Product.name.ilike(f'%{s}%')).order_by(Product.id.asc()).first()
            if row:
                return row
        return None

    def _find_service(name_substrings):
        for s in name_substrings:
            row = Service.query.filter(Service.name.ilike(f'%{s}%')).order_by(Service.id.asc()).first()
            if row:
                return row
        return None

    plan = [
        # ----- Magic Finance product (Magic Cloud BU) -----
        ('product', ['Magic Finance', 'magic finance'], [
            ('Програмын шинэ хувилбар татах',
             'https://magicgroup.mn/mn/download',
             'Magic Finance програмын хамгийн сүүлийн хувилбарыг татаж авах хуудас.'),
            ('Хэрэглэгчдийн Facebook групп',
             'https://www.facebook.com/groups/magicfinanceusers',
             'Magic Finance хэрэглэгчдийн нийгэмлэг — заавар, асуултын хариулт энд олдоно.'),
            ('Заавар, гарын авлага',
             'https://help.magicfinance.mn/',
             'Албан ёсны help center — функц бүрийн заавар, видеотой.'),
            ('Шинэчлэлтийн мэдээлэл',
             'https://magicgroup.mn/mn/category/magicfinance-update',
             'Програмын шинэ хувилбар, нэмэгдсэн боломжуудын мэдээлэл.'),
            ('Лиценз сунгуулах, код авах',
             'https://magicfinance.hamt.mn/code.php/code/codec',
             'Лицензийн хугацаа сунгах эсвэл шинээр код авах форм.'),
            ('Тайлан / файл шалгуулах',
             'https://magicfinance.hamt.mn/code.php/ticket/send',
             'Гарсан тайлан, файлын алдааг шалгуулах техник дэмжлэгийн ticket илгээх форм.'),
        ]),
        # ----- Microsoft license product -----
        ('product', ['Microsoft license', 'Microsoft', 'MS Office'], [
            ('MS Office лиценз авах',
             'https://forms.office.com/pages/responsepage.aspx?id=0XXBWo5_eEuS8Pz5UCuyi8YXwrWr81RCpchKwHza4p5UNlRCR0lLUVZHV1NLMU85TURTNTJENFYxSS4u&route=shorturl',
             'Microsoft Office license-ийг манайхаас худалдан авах захиалгын форм.'),
        ]),
        # ----- Audit service (Magic Consulting Audit BU) -----
        # Matches "Санхүүгийн тайлан баталгаажуулах аудитын үйлчилгээ"
        ('service', ['аудит', 'audit'], [
            ('Аудит хийлгэх захиалгын форм',
             'https://share.teamforms.app/form/MDkyNzU3M2QtNjJhNC00MjRiLWI3ODEtMDExMzUyZDRhZDUzOjVhYzE3NWQxLTdmOGUtNGI3OC05MmYwLWZjZjk1MDJiYjI4YjoyMWRlMWRjNi0zYzFmLTQ3ZWEtYmE4Ny0zMGFkZWVlMjM5MmY=',
             'Аудитийн үйлчилгээ авах захиалгын форм — Magic Consulting Audit.'),
        ]),
        # ----- Tax-consulting / report-filing service -----
        # Matches "Татварын мэргэшсэн зөвлөх үйлчилгээ" — the form labelled
        # "Тайлан гаргуулая" goes to the tax-consulting service because tax
        # reports (татварын тайлан) are filed through that channel.
        ('service', ['татвар', 'мэргэшсэн зөвлөх', 'tax'], [
            ('Тайлан гаргуулах захиалгын форм',
             'https://share.teamforms.app/form/ZmU3YzZjMjItNTA0ZC00NWE5LTkwYjMtYWQ2Mjk4MzI5YjkwOjVhYzE3NWQxLTdmOGUtNGI3OC05MmYwLWZjZjk1MDJiYjI4YjpmMDlkNjE0Ni1jZjU5LTRmNzgtYWZlOS0wMTQyMmNjYWM3Yzk=',
             'Татварын / санхүүгийн тайлан гаргуулах захиалгын форм.'),
        ]),
    ]

    for kind, name_subs, links_to_add in plan:
        if kind == 'product':
            item = _find_product(name_subs)
            LinkModel = ProductLink
            fk_field = 'product_id'
        else:
            item = _find_service(name_subs)
            LinkModel = ServiceLink
            fk_field = 'service_id'
        if not item:
            log.append(
                f'SKIPPED {kind} {"/".join(name_subs)}: no matching item found '
                f'(add it via the admin panel, then re-run).'
            )
            continue
        existing_urls = {l.url for l in LinkModel.query.filter_by(
            **{fk_field: item.id}).all()}
        added = 0
        for description, url, note in links_to_add:
            if url in existing_urls:
                continue
            db.session.add(LinkModel(
                **{fk_field: item.id},
                description=description,
                url=url,
                note=note,
                is_active=True,
                sort_order=0,
            ))
            added += 1
        log.append(
            f'{kind} "{item.name}" (#{item.id}): added {added} new link(s), '
            f'{len(existing_urls)} already present.'
        )

    db.session.commit()
    return '\n'.join(log)


def seed_handoff_keywords():
    """Ensure the default handoff keywords exist. Upsert by keyword text:
    missing defaults are added with is_active=True; existing rows (whether
    admin-customized or already-seeded) are left untouched. Lets us add
    new defaults in code without resetting any admin tweaks on prod."""
    inserted = 0
    existing_keywords = {
        k.lower() for k in db.session.query(HandoffKeyword.keyword).all()
        for k in (k[0],)
        if k
    }
    for kw in _DEFAULT_HANDOFF_KEYWORDS_EXPLICIT:
        if kw.lower() not in existing_keywords:
            db.session.add(HandoffKeyword(keyword=kw.lower(), keyword_type='explicit', is_active=True))
            existing_keywords.add(kw.lower())
            inserted += 1
    for kw in _DEFAULT_HANDOFF_KEYWORDS_FRUSTRATION:
        if kw.lower() not in existing_keywords:
            db.session.add(HandoffKeyword(keyword=kw.lower(), keyword_type='frustration', is_active=True))
            existing_keywords.add(kw.lower())
            inserted += 1
    if inserted:
        db.session.commit()
        logger.info("Seeded %s new default handoff keyword(s).", inserted)


def seed_courses_and_faqs():
    """Populate Courses and FAQs with Magic Financial Group defaults if empty.
    Skipped entirely when SEED_DEFAULTS=false."""
    if os.environ.get('SEED_DEFAULTS', 'true').strip().lower() != 'true':
        logger.info("SEED_DEFAULTS=false — skipping default course/FAQ seed.")
        return

    default_start = datetime.utcnow() + timedelta(days=14)

    if Course.query.count() == 0:
        courses = [
            Course(
                name='Нягтлан-Нярвын хосолсон сургалт — 100% Онлайн',
                course_type='100% Online',
                start_date=default_start,
                time='Хүссэн үедээ судлах',
                price=360000,
                description=(
                    'Бие даан судлах онлайн сургалт. Видео хичээл, бодлогууд, '
                    'Magic Finance программын 6 сарын үнэгүй эрх багтана.'
                ),
                is_active=True,
            ),
            Course(
                name='Нягтлан-Нярвын хосолсон сургалт — Хосолсон хэлбэр',
                course_type='Hybrid',
                start_date=default_start,
                time='Даваа/Лхагва/Баасан танхимд, бусад өдөр онлайн',
                price=440000,
                description=(
                    '7 хоногийн 3 өдөр танхимаар, үлдсэн өдрүүдэд онлайнаар '
                    'хичээллэх уян хатан хөтөлбөр.'
                ),
                is_active=True,
            ),
            Course(
                name='Нягтлан-Нярвын хосолсон сургалт — Багштай онлайн',
                course_type='Online with Teacher',
                start_date=default_start,
                time='Даваа-Баасан онлайн',
                price=660000,
                description=(
                    '1-5 дахь өдөр шууд багштай онлайн хичээл, асуулт-хариулт, '
                    'бодит жишээний дадлагатай хөтөлбөр.'
                ),
                is_active=True,
            ),
            Course(
                name='Нягтлан-Нярвын хосолсон сургалт — Танхим',
                course_type='Classroom',
                start_date=default_start,
                time='Даваа-Баасан, өглөө 10:00-13:00 эсвэл орой 18:00-21:00',
                price=880000,
                description=(
                    'UB Tower Plus, 5 давхар 509 тоот танхимд тогтмол '
                    'хичээллэх бүрэн хэмжээний сургалт. Багштай шууд харилцана.'
                ),
                is_active=True,
            ),
        ]
        for c in courses:
            db.session.add(c)
        logger.info('Seeded %s default courses.', len(courses))

    if FAQ.query.count() == 0:
        faqs = [
            FAQ(question='Сургалт хэдэн долоо хоног үргэлжилдэг вэ?',
                answer=(
                    'Сургалт нийт 4 долоо хоног үргэлжилнэ. '
                    '1-р долоо хоногт нярвын тайлан гаргахыг сурна. '
                    '2-р долоо хоногт санхүүгийн тайлан, '
                    '3-р долоо хоногт татварын хичээл, '
                    '4-р долоо хоногт Magic Finance программ дээр '
                    'тайлан гаргахыг үздэг.'
                ),
                category='Хөтөлбөр'),
            FAQ(question='Сургалтын үнэ хэд вэ?',
                answer=(
                    '100% Онлайн — 360,000₮, Хосолсон хэлбэр — 440,000₮, '
                    'Багштай онлайн — 660,000₮, Танхим — 880,000₮. '
                    'PocketZero-оор 4-6 хуваан, хүүгүй шимтгэлгүй төлөх '
                    'боломжтой.'
                ),
                category='Төлбөр'),
            FAQ(question='Хичээл хэдэн цагт ордог вэ?',
                answer=(
                    'Өглөөний анги 10:00–13:00, оройн анги 18:00–21:00 '
                    'хооронд хичээллэдэг. Та цагаа сонгоход тань туслана.'
                ),
                category='Цаг'),
            FAQ(question='Сургалтын төв хаана байрладаг вэ?',
                answer=(
                    'Манай сургалт БЗД 13-р хороолол, Натурын замд байрлах '
                    'UB Tower Plus, 5 давхар, 509 тоотод явагддаг.'
                ),
                category='Хаяг'),
            FAQ(question='Сургалтын дараа сертификат олгодог уу?',
                answer=(
                    'Тийм. 4 долоо хоногийн хичээл дуусаад шалгалт өгсний '
                    'дараа Мэжик Санхүүгийн Группын албан ёсны сертификат '
                    'олгоно. Мөн Magic Finance программыг 6 сар үнэгүй '
                    'ашиглах эрх бэлэглэнэ.'
                ),
                category='Сертификат'),
            FAQ(question='Ямар мэргэжилтэй хүн сурч болох вэ?',
                answer=(
                    'Та ямар ч мэргэжилтэй, ямар ч түвшний мэдлэгтэй байсан '
                    'хамаагүй. Бид эхнээс нь ойлгомжтой, бодит жишээн дээр '
                    'тулгуурлан заадаг тул шинэхэн суралцагч ч амжилттай '
                    'төгсөж чадна.'
                ),
                category='Бүртгэл'),
            FAQ(question='Төлбөрөө хуваан төлж болох уу?',
                answer=(
                    'Болно. PocketZero апп ашиглан 4-6 хуваан, хүүгүй '
                    'шимтгэлгүй төлөх боломжтой. Эсвэл сургалтын эхэнд '
                    'хагасыг нь төлж, хичээл явагдах хугацаандаа үлдсэнийг '
                    'нөхөн төлж болно.'
                ),
                category='Төлбөр'),
            FAQ(question='Magic Finance программ гэж юу вэ?',
                answer=(
                    'Magic Finance бол санхүүгийн тайлан гаргахад '
                    'зориулагдсан, манай өөрсдийн хөгжүүлсэн программ. '
                    'Гар аргаар хийдэг ажлыг 80% хүртэл хөнгөвчилж, '
                    'хяналтаа сайжруулна. Суралцагч бүрт 6 сар үнэгүй '
                    'ашиглах эрх олгоно.'
                ),
                category='Программ'),
        ]
        for f in faqs:
            db.session.add(f)
        logger.info('Seeded %s default FAQs.', len(faqs))

    db.session.commit()


