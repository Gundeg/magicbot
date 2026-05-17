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
    funnel_stage = db.Column(db.String(30), default='curious')
    # Last time we sent a proactive nudge (kept null until first nudge fires)
    last_nudge_at = db.Column(db.DateTime)
    # When set and > now(), the bot will not auto-reply to this user — a human
    # has been pinged and should take over. Cleared automatically once expired.
    bot_muted_until = db.Column(db.DateTime)
    # AI-classified topic of the conversation (updated after every bot reply)
    conversation_topic = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Message(db.Model):
    """Chat message history"""
    id = db.Column(db.Integer, primary_key=True)
    facebook_user_id = db.Column(db.Integer, db.ForeignKey('facebook_user.id'), nullable=False)
    sender = db.Column(db.String(20), nullable=False)  # 'user' or 'bot'
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    facebook_user = db.relationship('FacebookUser', backref=db.backref('messages', lazy=True))


class Course(db.Model):
    """Training course"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    course_type = db.Column(db.String(100), nullable=False)  # '100% Online', 'Hybrid', etc.
    start_date = db.Column(db.DateTime, nullable=False)
    end_date = db.Column(db.DateTime)
    time = db.Column(db.String(50), nullable=False)  # e.g., "10:00-13:00"
    price = db.Column(db.Float, nullable=False)
    description = db.Column(db.Text)
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
    facebook_user_id = db.Column(db.Integer, db.ForeignKey('facebook_user.id'), nullable=False)
    issue_type = db.Column(db.String(50), nullable=False)  # 'unresolved_query', 'complaint', 'suggestion'
    content = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='open')  # 'open', 'in_progress', 'resolved'
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
    telling the bot whether to answer briefly or refer to staff."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text)
    action = db.Column(db.String(20), default='refer')  # 'answer' | 'refer'
    contact_info = db.Column(db.String(255))            # e.g. phone, email, dept
    is_active = db.Column(db.Boolean, default=True)
    status_note = db.Column(db.String(255))  # reason for pause / admin note visible to AI
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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


class Software(db.Model):
    """Catalog of software licenses Magic Cloud resells (Magic Finance, Microsoft,
    Kaspersky, etc.). Keeps the same minimal shape as Service so both placeholder
    pages reuse one CRUD pattern."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    price = db.Column(db.Float)             # nullable: license pricing often per-seat / quoted
    vendor = db.Column(db.String(120))      # e.g. "Microsoft", "Kaspersky", "Magic Cloud"
    is_active = db.Column(db.Boolean, default=True)
    status_note = db.Column(db.String(255))
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
