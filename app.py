import os
import re
import json
import requests
from pathlib import Path
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from openai import OpenAI
from threading import Thread
import time

PHONE_RE = re.compile(r'(?:\+?976[\s-]?)?[89]\d{7}')

# Initialize Flask app
app = Flask(__name__)
_secret_key = os.environ.get('SECRET_KEY')
if not _secret_key:
    raise RuntimeError("SECRET_KEY environment variable is required")
app.config['SECRET_KEY'] = _secret_key
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///magic_bot.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize extensions
db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Initialize OpenAI client
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# Facebook API credentials
FACEBOOK_PAGE_ID = os.environ.get('FACEBOOK_PAGE_ID', '')
FACEBOOK_ACCESS_TOKEN = os.environ.get('FACEBOOK_ACCESS_TOKEN', '')
if not FACEBOOK_ACCESS_TOKEN:
    raise RuntimeError("FACEBOOK_ACCESS_TOKEN environment variable is required")
GOOGLE_FORM_URL = os.environ.get('GOOGLE_FORM_URL', '')

# Load training content
TRAINING_PATH = Path(__file__).parent / 'pasted_content.txt'
try:
    TRAINING_CONTENT = TRAINING_PATH.read_text(encoding='utf-8')
except FileNotFoundError:
    TRAINING_CONTENT = "Манай сургалтын төв нь 2007 оноос хойш үйл ажиллагаа явуулж байгаа."

# ===================== DATABASE MODELS =====================

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
    time = db.Column(db.String(50), nullable=False)  # e.g., "10:00-13:00"
    price = db.Column(db.Float, nullable=False)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
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
    facebook_user = db.relationship('FacebookUser', backref=db.backref('admin_issues', lazy=True))
    assigned_user = db.relationship('User', backref=db.backref('assigned_issues', lazy=True))

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

# ===================== LOGIN MANAGER =====================

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# ===================== FACEBOOK API HELPERS =====================

def send_facebook_message(recipient_id, message_text):
    """Send a message via Facebook Messenger API"""
    url = f"https://graph.facebook.com/v18.0/me/messages"
    headers = {"Content-Type": "application/json"}
    params = {"access_token": FACEBOOK_ACCESS_TOKEN}
    data = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text}
    }
    try:
        response = requests.post(url, json=data, headers=headers, params=params)
        return response.status_code == 200
    except Exception as e:
        print(f"Error sending message: {e}")
        return False

def get_facebook_user_info(facebook_id):
    """Get user info from Facebook"""
    url = f"https://graph.facebook.com/v18.0/{facebook_id}"
    params = {"fields": "name,email", "access_token": FACEBOOK_ACCESS_TOKEN}
    try:
        response = requests.get(url, params=params)
        if response.status_code == 200:
            return response.json()
        return {}
    except Exception as e:
        print(f"Error getting user info: {e}")
        return {}

def get_recent_messages():
    """Poll for recent messages from Facebook Messenger"""
    url = f"https://graph.facebook.com/v18.0/{FACEBOOK_PAGE_ID}/conversations"
    params = {
        "fields": "id,senders,participants,former_participants,wallpaper,snippet,updated_time,message_count,unread_count,subject,can_reply,former_participants,info,link,name,email,page_name,wallpaper,former_participants",
        "access_token": FACEBOOK_ACCESS_TOKEN
    }
    try:
        response = requests.get(url, params=params)
        if response.status_code == 200:
            return response.json().get('data', [])
        return []
    except Exception as e:
        print(f"Error getting messages: {e}")
        return []

def get_page_posts():
    """Poll for recent posts from Facebook Page"""
    url = f"https://graph.facebook.com/v18.0/{FACEBOOK_PAGE_ID}/feed"
    params = {
        "fields": "id,message,created_time,type,story",
        "limit": 10,
        "access_token": FACEBOOK_ACCESS_TOKEN
    }
    try:
        response = requests.get(url, params=params)
        if response.status_code == 200:
            return response.json().get('data', [])
        return []
    except Exception as e:
        print(f"Error getting posts: {e}")
        return []

def post_comment_on_page(post_id, comment_text):
    """Post a comment on a Facebook Page post"""
    url = f"https://graph.facebook.com/v18.0/{post_id}/comments"
    params = {"access_token": FACEBOOK_ACCESS_TOKEN}
    data = {"message": comment_text}
    try:
        response = requests.post(url, json=data, params=params)
        return response.status_code == 201
    except Exception as e:
        print(f"Error posting comment: {e}")
        return False

# ===================== LLM HELPERS =====================

def build_system_prompt():
    """Build system prompt with training and FAQ knowledge"""
    faqs = FAQ.query.all()
    faq_text = "\n".join([f"Q: {faq.question}\nA: {faq.answer}" for faq in faqs])
    
    courses = Course.query.filter_by(is_active=True).all()
    courses_text = "\n".join([
        f"- {c.name} ({c.course_type}): {c.price}₮, эхлэх: {c.start_date.strftime('%Y-%m-%d')}, цаг: {c.time}"
        for c in courses
    ])
    
    system_prompt = f"""Та мэргэжлийн сэтгэл судлаач болон маркетер юм. Монгол хэлээр амьд хүн шиг, ойлгомжтой, халуун, туслахын сэтгэлтэй хариулт өгнө.

СУРГАЛТЫН ТӨВИЙН МЭДЭЭЛЭЛ:
{TRAINING_CONTENT}

ИДЭВХТЭЙ АНГИУД:
{courses_text}

ТҮГЭЭМЭЛ АСУУЛТУУД:
{faq_text}

ЧУХАЛ ДҮРМҮҮД:
1. Монгол хэлээр л хариулна. Англи хэл хэрэглэхгүй.
2. Сургалтын давуу талуудыг сайн ойлгож, зөвлөгөө өгнө.
3. Хэрэглэгч бүртгүүлэхийг хүсвэл: "Та утасны дугаараа үлдээнэ үү, бас энэ линкээр бүртгүүлнэ үү" гэж хэлээд Google Form линкийг илгээнө.
4. Утасны дугаараа үлдээсэн хэрэглэгчид: "Одоо таныг бүртгэлийн ажилтантай холбож өгье" гэж мэссеж илгээнө.
5. Шийдэж чадахгүй асуудал ирвэл админд шилжүүлнө.
6. Амьд хүн шиг, байгалийн хэлээр ярилцана."""
    
    return system_prompt

def generate_bot_response(user_message, conversation_history):
    """Generate bot response using OpenAI"""
    try:
        messages = [{"role": "system", "content": build_system_prompt()}]
        messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
            max_tokens=500
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"Error generating response: {e}")
        return "Уучлаарай, одоо хариулт өгөх боломжгүй байна. Дараа нь дахин оролдоно уу."

def analyze_and_comment_on_post(post_content):
    """Analyze post and generate appropriate comment"""
    try:
        analysis_prompt = f"""Энэ Facebook постыг анализ хийж, тохирох коммент бичнэ үү.

ПОСТ КОНТЕНТ:
{post_content}

ДҮРМҮҮД:
1. Сургалттай холбоотой: "Сургалтын мэдээллийг танд чатаар илгээсэн шүү, та чатаа шалгаарай"
2. Баяр ёслол, шагнал: Баяр хүргэх коммент
3. Бусад: тохирох, сонирхолтой коммент

Монгол хэлээр л хариулна. Коммент текст л өгнө, бусад зүйл бичихгүй."""
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": analysis_prompt}],
            temperature=0.7,
            max_tokens=200
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"Error analyzing post: {e}")
        return None

# ===================== POLLING BACKGROUND TASK =====================

def polling_task():
    """Background task to poll Facebook posts and auto-comment.

    Messages are handled via the /webhook endpoint, not here.
    Runs only when ENABLE_POLLING=true and requires an app context for db access.
    """
    while True:
        try:
            with app.app_context():
                posts = get_page_posts()
                for post in posts:
                    post_id = post.get('id')
                    existing = PagePost.query.filter_by(facebook_post_id=post_id).first()

                    if not existing:
                        post_content = post.get('message') or post.get('story', '')
                        comment_text = analyze_and_comment_on_post(post_content)

                        if comment_text:
                            if post_comment_on_page(post_id, comment_text):
                                page_post = PagePost(
                                    facebook_post_id=post_id,
                                    content=post_content,
                                    comment_posted=True,
                                    comment_text=comment_text
                                )
                                db.session.add(page_post)
                                db.session.commit()

            time.sleep(60)
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(60)

# ===================== ROUTES =====================

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('dashboard'))
        else:
            return render_template('login.html', error='Invalid credentials')
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
@admin_required
def dashboard():
    leads_count = FacebookUser.query.filter_by(is_lead=True).count()
    open_issues = AdminIssue.query.filter_by(status='open').count()
    total_messages = Message.query.count()
    
    return render_template('dashboard.html', 
                         leads_count=leads_count,
                         open_issues=open_issues,
                         total_messages=total_messages)

@app.route('/courses', methods=['GET', 'POST'])
@login_required
@admin_required
def courses():
    if request.method == 'POST':
        data = request.get_json()
        action = data.get('action')
        
        if action == 'add':
            course = Course(
                name=data.get('name'),
                course_type=data.get('course_type'),
                start_date=datetime.fromisoformat(data.get('start_date')),
                time=data.get('time'),
                price=float(data.get('price')),
                description=data.get('description')
            )
            db.session.add(course)
            db.session.commit()
            return jsonify({'success': True, 'id': course.id})
        
        elif action == 'edit':
            course = Course.query.get(data.get('id'))
            if course:
                course.name = data.get('name')
                course.course_type = data.get('course_type')
                course.start_date = datetime.fromisoformat(data.get('start_date'))
                course.time = data.get('time')
                course.price = float(data.get('price'))
                course.description = data.get('description')
                db.session.commit()
                return jsonify({'success': True})
        
        elif action == 'delete':
            course = Course.query.get(data.get('id'))
            if course:
                db.session.delete(course)
                db.session.commit()
                return jsonify({'success': True})
    
    courses = Course.query.all()
    return render_template('courses.html', courses=courses)

@app.route('/faq', methods=['GET', 'POST'])
@login_required
@admin_required
def faq():
    if request.method == 'POST':
        data = request.get_json()
        action = data.get('action')
        
        if action == 'add':
            faq_item = FAQ(
                question=data.get('question'),
                answer=data.get('answer'),
                category=data.get('category')
            )
            db.session.add(faq_item)
            db.session.commit()
            return jsonify({'success': True, 'id': faq_item.id})
        
        elif action == 'edit':
            faq_item = FAQ.query.get(data.get('id'))
            if faq_item:
                faq_item.question = data.get('question')
                faq_item.answer = data.get('answer')
                faq_item.category = data.get('category')
                db.session.commit()
                return jsonify({'success': True})
        
        elif action == 'delete':
            faq_item = FAQ.query.get(data.get('id'))
            if faq_item:
                db.session.delete(faq_item)
                db.session.commit()
                return jsonify({'success': True})
    
    faqs = FAQ.query.all()
    return render_template('faq.html', faqs=faqs)

@app.route('/leads')
@login_required
@admin_required
def leads():
    leads = FacebookUser.query.filter_by(is_lead=True).all()
    return render_template('leads.html', leads=leads)

@app.route('/issues', methods=['GET', 'POST'])
@login_required
@admin_required
def issues():
    if request.method == 'POST':
        data = request.get_json()
        action = data.get('action')
        
        if action == 'update_status':
            issue = AdminIssue.query.get(data.get('id'))
            if issue:
                issue.status = data.get('status')
                if data.get('status') == 'resolved':
                    issue.resolved_at = datetime.utcnow()
                db.session.commit()
                return jsonify({'success': True})
    
    issues = AdminIssue.query.filter_by(status='open').all()
    return render_template('issues.html', issues=issues)

@app.route('/logs')
@login_required
@admin_required
def logs():
    messages = Message.query.order_by(Message.created_at.desc()).limit(100).all()
    return render_template('logs.html', messages=messages)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
@admin_required
def settings():
    if request.method == 'POST':
        data = request.get_json()
        for key, value in data.items():
            setting = GeneralSetting.query.filter_by(key=key).first()
            if setting:
                setting.value = value
            else:
                setting = GeneralSetting(key=key, value=value)
            db.session.add(setting)
        db.session.commit()
        return jsonify({'success': True})
    
    settings = {s.key: s.value for s in GeneralSetting.query.all()}
    return render_template('settings.html', settings=settings)

@app.route('/webhook', methods=['POST'])
def webhook():
    """Facebook Messenger webhook"""
    data = request.get_json()
    
    if data.get('object') == 'page':
        for entry in data.get('entry', []):
            for messaging_event in entry.get('messaging', []):
                sender_id = messaging_event.get('sender', {}).get('id')
                recipient_id = messaging_event.get('recipient', {}).get('id')
                
                if messaging_event.get('message'):
                    message_text = messaging_event['message'].get('text')
                    
                    # Get or create Facebook user
                    fb_user = FacebookUser.query.filter_by(facebook_id=sender_id).first()
                    if not fb_user:
                        user_info = get_facebook_user_info(sender_id)
                        fb_user = FacebookUser(
                            facebook_id=sender_id,
                            name=user_info.get('name', 'Unknown')
                        )
                        db.session.add(fb_user)
                        db.session.commit()
                    
                    # Save user message
                    user_msg = Message(
                        facebook_user_id=fb_user.id,
                        sender='user',
                        content=message_text
                    )
                    db.session.add(user_msg)
                    db.session.commit()
                    
                    # Get conversation history
                    history = Message.query.filter_by(facebook_user_id=fb_user.id).order_by(Message.created_at).all()
                    conversation = [
                        {"role": "user" if m.sender == 'user' else "assistant", "content": m.content}
                        for m in history[-10:]  # Last 10 messages
                    ]
                    
                    # Generate bot response
                    bot_response = generate_bot_response(message_text, conversation)
                    
                    # Save bot message
                    bot_msg = Message(
                        facebook_user_id=fb_user.id,
                        sender='bot',
                        content=bot_response
                    )
                    db.session.add(bot_msg)
                    db.session.commit()
                    
                    # Send response via Facebook
                    send_facebook_message(sender_id, bot_response)
                    
                    # Check if phone number was provided
                    phone_match = PHONE_RE.search(message_text)
                    if phone_match and not fb_user.is_lead:
                        fb_user.phone = phone_match.group(0)
                        fb_user.is_lead = True
                        fb_user.lead_status = 'contacted'
                        db.session.commit()

                        # Send handoff message
                        handoff_msg = "Одоо таныг бүртгэлийн ажилтантай холбож өгье. Түр хүлээнэ үү..."
                        send_facebook_message(sender_id, handoff_msg)
    
    return jsonify({'status': 'ok'}), 200

@app.route('/webhook', methods=['GET'])
def webhook_verify():
    """Facebook webhook verification"""
    verify_token = os.getenv('VERIFY_TOKEN', 'magic_bot_verify_token')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    
    if token == verify_token:
        return challenge
    return 'Invalid token', 403

# ===================== INITIALIZATION =====================

def init_db():
    """Initialize database tables and seed the admin user from env."""
    with app.app_context():
        db.create_all()

        if User.query.filter_by(username='admin').first():
            return

        initial_password = os.environ.get('INITIAL_ADMIN_PASSWORD')
        if not initial_password:
            print("INITIAL_ADMIN_PASSWORD not set — skipping default admin creation. "
                  "Set it and redeploy to create the admin user.")
            return

        admin = User(
            username='admin',
            password=generate_password_hash(initial_password),
            email=os.environ.get('ADMIN_EMAIL', 'admin@magicfinance.mn'),
            role='admin',
        )
        db.session.add(admin)
        db.session.commit()
        print("Default admin user created with username 'admin'.")


# Run at import so gunicorn workers initialize the DB on boot.
init_db()

# Optional background polling for Page post auto-commenting.
# Disabled by default to avoid duplicate work across gunicorn workers.
if os.environ.get('ENABLE_POLLING', 'false').lower() == 'true':
    Thread(target=polling_task, daemon=True).start()


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
