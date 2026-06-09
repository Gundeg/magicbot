"""SQLAlchemy models for MagicBot.

All persistent state lives here. Route handlers import models from this
module instead of from app.py so the routes file stays focused on HTTP
glue and the model layer stays a single source of truth.
"""
from datetime import datetime
from flask_login import UserMixin

from extensions import db


class User(UserMixin, db.Model):
    """Admin user model"""
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    role = db.Column(db.String(20), default='admin')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class FacebookUser(db.Model):
    """Facebook user/conversation participant"""
    id = db.Column(db.Integer, primary_key=True)
    facebook_id = db.Column(db.String(100), unique=True, nullable=False)
    name = db.Column(db.String(255))
    phone = db.Column(db.String(20))
    is_lead = db.Column(db.Boolean, default=False)
    lead_status = db.Column(db.String(50), default='new')  # new, contacted, converted
    # Funnel state: 'curious' -> 'exploring_courses' -> 'pricing' -> 'ready'
    funnel_stage = db.Column(db.String(30), default='curious', index=True)
    # Last time we sent a proactive nudge (kept null until first nudge fires)
    last_nudge_at = db.Column(db.DateTime)
    # Last time we sent a "still in queue" reassurance during a handoff
    # mute window. Used to rate-limit the auto-ack so the user feels
    # acknowledged without getting spammed.
    last_mute_ack_at = db.Column(db.DateTime)
    # When set and > now(), the bot will not auto-reply to this user — a human
    # has been pinged and should take over. Cleared automatically once expired.
    bot_muted_until = db.Column(db.DateTime, index=True)
    # AI-classified topic of the conversation (updated after every bot reply)
    conversation_topic = db.Column(db.String(100))
    # Free-text staff note captured on a terminal action (drop reason, etc.).
    # Convenient short-term copy; the durable record is the audit log, which
    # outlives cleanup_old_records' 60-day purge of dropped leads.
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Message(db.Model):
    """Chat message history"""
    id = db.Column(db.Integer, primary_key=True)
    facebook_user_id = db.Column(
        db.Integer, db.ForeignKey('facebook_user.id'), nullable=False, index=True
    )
    sender = db.Column(db.String(20), nullable=False)  # 'user' or 'bot'
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    # Facebook's per-message id. Set on inbound 'user' rows and used as an
    # idempotency key: Facebook delivers "at least once" and retries the same
    # event (same mid) when our webhook is slow, so a UNIQUE index lets a retry
    # be detected and dropped instead of re-processed. NULL on bot rows and on
    # legacy rows; multiple NULLs are allowed under a SQL unique index.
    mid = db.Column(db.String(255), unique=True, index=True)
    facebook_user = db.relationship('FacebookUser', backref=db.backref('messages', lazy=True))


class Course(db.Model):
    """Training course"""
    id = db.Column(db.Integer, primary_key=True)
    # Admin-assigned external course number (e.g. 1881). Unique per Course;
    # the same human-readable name can repeat across different schedules,
    # so this is the stable identifier admins use to disambiguate.
    course_number = db.Column(db.Integer, unique=True)
    name = db.Column(db.String(255), nullable=False)
    # Restricted to the four allowed types, enforced in the admin POST.
    course_type = db.Column(db.String(100), nullable=False)
    # Nullable for self-paced '100% Online' courses where there is no fixed start.
    start_date = db.Column(db.DateTime)
    end_date = db.Column(db.DateTime)
    time = db.Column(db.String(50), nullable=False)  # e.g., "10:00-13:00"
    price = db.Column(db.Float, nullable=False)
    description = db.Column(db.Text)
    # Total course duration in days, when fixed. Quoted by the bot when set.
    duration_days = db.Column(db.Integer)
    is_active = db.Column(db.Boolean, default=True)
    status_note = db.Column(db.String(255))  # reason for pause / admin note visible to AI
    is_recurring = db.Column(db.Boolean, default=False)
    schedule_template = db.Column(db.Text)  # JSON: {"interval_weeks": 4, "day_of_week": 0}
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class FAQ(db.Model):
    """Frequently asked questions"""
    id = db.Column(db.Integer, primary_key=True)
    question = db.Column(db.Text, nullable=False)
    answer = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AdminIssue(db.Model):
    """Issues assigned to admin (unresolved by bot)"""
    id = db.Column(db.Integer, primary_key=True)
    facebook_user_id = db.Column(
        db.Integer, db.ForeignKey('facebook_user.id'), nullable=False, index=True
    )
    issue_type = db.Column(db.String(50), nullable=False, index=True)  # 'unresolved_query', 'complaint', 'suggestion'
    content = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='open', index=True)  # 'open', 'in_progress', 'resolved'
    assigned_to = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime)
    updated_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    updated_at = db.Column(db.DateTime)
    notes = db.Column(db.Text)
    facebook_user = db.relationship('FacebookUser', backref=db.backref('admin_issues', lazy=True))
    assigned_user = db.relationship('User', foreign_keys=[assigned_to], backref=db.backref('assigned_issues', lazy=True))
    updated_by = db.relationship('User', foreign_keys=[updated_by_id], backref=db.backref('updated_issues', lazy=True))


class PagePost(db.Model):
    """Facebook Page posts (for auto-commenting)"""
    id = db.Column(db.Integer, primary_key=True)
    facebook_post_id = db.Column(db.String(100), unique=True, nullable=False)
    content = db.Column(db.Text)
    post_type = db.Column(db.String(50))  # 'training', 'celebration', 'other'
    comment_posted = db.Column(db.Boolean, default=False)
    comment_text = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class GeneralSetting(db.Model):
    """General training center settings"""
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TeamMember(db.Model):
    """Staff/teacher directory the bot can reference when clients ask
    who teaches what or who handles a given topic."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(120))         # e.g. "Багш", "Хариуцагч", "Зөвлөх"
    specialty = db.Column(db.String(255))    # e.g. "Татварын асуудал, Magic Finance"
    bio = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TrainingSnippet(db.Model):
    """Additive, per-topic training notes. Managers add a new row when a
    real chat surfaces something the bot got wrong, instead of editing one
    giant blob and risking overwrite of someone else's work."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(100))           # free-form tag
    priority = db.Column(db.String(20), default='normal')  # 'high' | 'normal'
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BusinessLine(db.Model):
    """Magic Group-ын охин компаниуд болон тэдгээрийн бүтээгдэхүүн/үйлчилгээ.
    Magic Choice (сургалт), Magic Consulting Audit (аудит, CPA),
    Magic Cloud (програм хангамжийн лиценз). Each line has an action
    telling the bot whether to answer briefly or refer to staff.

    `product_type` picks which item schema the BU's drill-in page uses:
    'Course' (scheduled), 'Service' (ongoing), or 'Product' (license-style).
    Set per-BU so admins can add new units without code changes."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text)
    action = db.Column(db.String(20), default='refer')  # 'answer' | 'refer'
    contact_info = db.Column(db.String(255))            # e.g. phone, email, dept
    address = db.Column(db.Text)
    email = db.Column(db.String(200))
    established_year = db.Column(db.Integer)
    # Drives the items grid on the BU drill-in page. Course / Service / Product.
    # CHECK constraint enforced at the DB layer via the migration; we don't
    # repeat it here so the model doesn't drift if values are ever added.
    product_type = db.Column(db.String(20), nullable=False, default='Product')
    is_active = db.Column(db.Boolean, default=True)
    status_note = db.Column(db.String(255))  # reason for pause / admin note visible to AI
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Product(db.Model):
    """A distinct product offered under a BusinessLine. Used when one line
    sells several separable things (Magic Finance + Microsoft + Kaspersky
    under the Program line). Training-style services use Course instead
    since they need scheduling fields.

    The bot does not quote prices; questions about purchase, support,
    docs, etc. get answered with the matching ProductLink. Prices live
    only in the request-form workflow operated by humans."""
    id = db.Column(db.Integer, primary_key=True)
    business_line_id = db.Column(
        db.Integer, db.ForeignKey('business_line.id'), nullable=False
    )
    # Admin-input external code. Same disambiguation pattern as Course —
    # the name can repeat (e.g. multiple Microsoft license SKUs) but the
    # number stays unique across all Products.
    product_number = db.Column(db.Integer, unique=True)
    name = db.Column(db.String(200), nullable=False)
    vendor = db.Column(db.String(120))
    description = db.Column(db.Text)
    # Promote one product per line to the top of the bot's mental list.
    is_main_product = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    status_note = db.Column(db.String(255))
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    business_line = db.relationship(
        'BusinessLine',
        backref=db.backref('products', lazy=True, cascade='all, delete-orphan')
    )


class ProductLink(db.Model):
    """One resource link attached to a Product — purchase form, support
    ticket URL, manual, community forum, download page, etc.

    Shape is mirrored by CourseLink and ServiceLink so the admin edit
    partial (templates/business/partials/_links_editor.html) renders the
    same form for every item type. `description` is what users see in the
    bot's reply; `note` is admin-only context never surfaced to chat."""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(
        db.Integer, db.ForeignKey('product.id'), nullable=False
    )
    description = db.Column(db.String(200), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    note = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    product = db.relationship(
        'Product',
        backref=db.backref('links', lazy=True, cascade='all, delete-orphan',
                           order_by='ProductLink.sort_order')
    )


class CourseLink(db.Model):
    """One resource link attached to a Course — registration form, syllabus,
    exam form, etc. Same shape as ProductLink; see its docstring."""
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(
        db.Integer, db.ForeignKey('course.id'), nullable=False
    )
    description = db.Column(db.String(200), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    note = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    course = db.relationship(
        'Course',
        backref=db.backref('links', lazy=True, cascade='all, delete-orphan',
                           order_by='CourseLink.sort_order')
    )


class ServiceLink(db.Model):
    """One resource link attached to a Service — quote request, sample
    report, scheduling page, etc. Same shape as ProductLink."""
    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(
        db.Integer, db.ForeignKey('service.id'), nullable=False
    )
    description = db.Column(db.String(200), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    note = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    service = db.relationship(
        'Service',
        backref=db.backref('links', lazy=True, cascade='all, delete-orphan',
                           order_by='ServiceLink.sort_order')
    )


class HandoffKeyword(db.Model):
    """Keyword/phrase that triggers a human handoff. Replaces the hardcoded
    HANDOFF_KEYWORDS_EXPLICIT and HANDOFF_KEYWORDS_FRUSTRATION lists."""
    id = db.Column(db.Integer, primary_key=True)
    keyword = db.Column(db.String(200), nullable=False)
    keyword_type = db.Column(db.String(20), default='explicit')  # 'explicit' | 'frustration'
    is_active = db.Column(db.Boolean, default=True)
    note = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AuditEntry(db.Model):
    """Append-only log of admin actions. Lets a super_admin answer "who changed
    or deleted X, and when?" without having to read application logs.

    Stored fields are deliberately denormalized (actor_username, entity_label)
    so the log stays readable after the referenced admin or entity is deleted.
    """
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    actor_username = db.Column(db.String(80))           # snapshot of username at the time
    action = db.Column(db.String(60), nullable=False)   # e.g. 'course.toggle', 'admin.delete'
    entity_type = db.Column(db.String(40))              # e.g. 'course', 'business_line'
    entity_id = db.Column(db.Integer)
    entity_label = db.Column(db.String(255))            # snapshot label for readability
    detail = db.Column(db.Text)                         # short Mongolian description
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class Service(db.Model):
    """Catalog of consulting / audit / CPA offerings under Magic Consulting Audit.
    Mirrors the shape of Course (catalog item) but without scheduling — services
    are an ongoing offering, not a dated class."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    price = db.Column(db.Float)             # nullable: many services are quoted per engagement
    duration = db.Column(db.String(100))    # free-form, e.g. "1-2 долоо хоног", "тогтмол"
    is_active = db.Column(db.Boolean, default=True)
    status_note = db.Column(db.String(255))
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# Software model removed in Phase 1 of the admin IA reorg. Its rows were
# migrated to Product rows under the Magic Cloud business line; see
# migrations/versions/0002_phase_1_ia_reorg.py and
# scripts/apply_phase_1_migration.py for the migration details.


class ConversationTopic(db.Model):
    """Topic tag attached to a FacebookUser based on what they've asked about.

    The single-string `FacebookUser.conversation_topic` column is too narrow
    for clients who ask about several services in one chat. This table lets
    us attach many topics per user, each with first/last_seen timestamps and
    a short evidence snippet so an admin can see "this client asked about
    audit on 2026-05-10 and training on 2026-05-15".

    `topic` is the human-visible name of an active BusinessLine, Product,
    Service, or Course at the time of classification — denormalized so the
    tag stays readable even after the source row is renamed or deleted.
    `topic_kind` distinguishes which catalog the topic came from so the
    admin UI can group / filter.
    """
    id = db.Column(db.Integer, primary_key=True)
    facebook_user_id = db.Column(
        db.Integer, db.ForeignKey('facebook_user.id'), nullable=False, index=True
    )
    topic = db.Column(db.String(200), nullable=False)
    topic_kind = db.Column(db.String(20), nullable=False, index=True)
    evidence = db.Column(db.Text)
    first_seen_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    facebook_user = db.relationship(
        'FacebookUser',
        backref=db.backref('topics', lazy=True, cascade='all, delete-orphan'),
    )
    __table_args__ = (
        db.UniqueConstraint('facebook_user_id', 'topic', name='uq_conv_topic_user_topic'),
    )


class ChatQuestionCluster(db.Model):
    """LLM-grouped themes from real user chat messages, populated weekly by
    services.cluster_chat_questions(). Surfaces FAQ candidates: each row is
    a cluster of similar user questions ("price questions", "schedule
    questions", ...) the admin can one-click promote into the curated FAQ.

    Wholesale-replaced on every clustering run, so `id` is not stable
    across runs. `count` is the number of source messages that mapped to
    this cluster in the most recent run.
    """
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    representative_question = db.Column(db.Text, nullable=False)
    sample_questions = db.Column(db.Text, nullable=False)  # JSON array
    count = db.Column(db.Integer, nullable=False, default=0)
    first_seen_at = db.Column(db.DateTime)
    last_seen_at = db.Column(db.DateTime)
    # Set when admin clicks "Promote to FAQ" so the cluster doesn't keep
    # nagging on subsequent runs. Cleared if the FAQ row is deleted.
    promoted_to_faq_id = db.Column(db.Integer, db.ForeignKey('faq.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
