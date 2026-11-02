"""Reply-quality eval: run a fixed set of customer scenarios through the REAL
prompt + model path and score the replies on the things that matter to Magic
(short, no filler particles, link when registering, handoff only for real
knowledge gaps, no price dump). Run it before and after ANY persona / snippet /
prompt-rule change so the change is measured, not guessed.

    python scripts/eval_replies.py            # list scenarios, no model calls
    python scripts/eval_replies.py --run      # ~12 Claude calls (costs money)
    python scripts/eval_replies.py --run --persona my_persona.txt

Uses an in-memory DB seeded with Magic's defaults plus placeholder registration
links (prod has real CourseLinks; the default seed has none). The persona comes
from --persona, else the code default — paste the live one from the admin UI
into a file to eval what prod actually runs.
"""
import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FILLERS = re.compile(r'\b(шүү|дээ|даа|шдээ)\b', re.I)

# (name, message, history, session_state, funnel_stage, expectations)
SCENARIOS = [
    ('үнэ (шинэ яриа)', 'Сайн уу, сургалтын үнэ хэд вэ?', [], 'new', 'curious',
     dict(max_chars=400, handoff=False, max_prices=2)),
    ('айсан эхлэгч', 'Би нягтлангийн мэдлэг огт байхгүй, 42 настай. Чадах болов уу?',
     [('user', 'Сайн уу'), ('assistant', 'Сайн байна уу! Танд юугаар туслах вэ?')],
     'active', 'curious', dict(max_chars=450, handoff=False)),
    ('үнийн эсэргүүцэл', 'Үнэтэй юм аа... бодоод үзье.',
     [('user', 'Хосолсон ангийн үнэ хэд вэ?'),
      ('assistant', 'Хосолсон хэлбэр 440,000₮. Долоо хоногт 3 өдөр танхимд, бусад өдөр онлайнаар. Танд энэ хуваарь тохирох уу?')],
     'active', 'pricing', dict(max_chars=400, handoff=False)),
    ('платформд нэвтрэх', 'Платформ руу нэвтэрч чадахгүй байна, нууц үг минь буруу гээд байна',
     [('user', 'Сайн уу'), ('assistant', 'Сайн байна уу! Танд юугаар туслах вэ?')],
     'active', 'curious', dict(max_chars=400, handoff=True, category='knowledge_gap')),
    ('бүртгүүлье', 'За тэгвэл онлайн ангид бүртгүүлье',
     [('user', 'Би ажилтай, орой л завтай'),
      ('assistant', 'Тэгвэл 100% онлайн хувилбар танд тохирно: бүртгүүлсэн өдрөөсөө хүссэн цагтаа үзнэ. Сонирхолтой юу?')],
     'active', 'ready', dict(max_chars=450, handoff=False, link=True)),
    ('хүнтэй ярих хүсэлт', 'Би ботоор биш, жинхэнэ хүнтэй ярьмаар байна',
     [('user', 'Сайн уу'), ('assistant', 'Сайн байна уу! Танд юугаар туслах вэ?')],
     'active', 'curious', dict(handoff=True, category='wants_human')),
]


def seed_links():
    from extensions import db
    import services
    from models import Course, CourseLink, GeneralSetting
    if CourseLink.query.count() == 0:
        for c in Course.query.all():
            db.session.add(CourseLink(course_id=c.id, description='Сургалтанд сууя, бүртгүүлье',
                                      url=f'https://magicfinance.mn/register/{c.id}'))
    if not services.get_setting('business_website_url', ''):
        db.session.add(GeneralSetting(key='business_website_url', value='https://magicfinance.mn'))
    db.session.commit()


def set_persona(text):
    from extensions import db
    from models import GeneralSetting
    row = GeneralSetting.query.filter_by(key='bot_persona').first()
    if row is None:
        db.session.add(GeneralSetting(key='bot_persona', value=text))
    else:
        row.value = text
    db.session.commit()


def score(reply, detail, exp):
    checks = {}
    if 'max_chars' in exp:
        checks[f"urt<={exp['max_chars']}"] = len(reply) <= exp['max_chars']
    checks['sul_uggui'] = not FILLERS.search(reply)
    if 'handoff' in exp:
        checks['handoff' if exp['handoff'] else 'handoff_baihgui'] = (detail is not None) == exp['handoff']
    if exp.get('category'):
        checks[f"category={exp['category']}"] = bool(detail) and detail['category'] == exp['category']
    if exp.get('link'):
        checks['link'] = 'magicfinance.mn' in reply
    if 'max_prices' in exp:
        checks[f"une<={exp['max_prices']}"] = reply.count('₮') <= exp['max_prices']
    return checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true', help='actually call the model')
    ap.add_argument('--persona', help='file with the persona text to evaluate')
    args = ap.parse_args()

    if not args.run:
        print('Scenarios (add --run to call the model):')
        for name, msg, *_ in SCENARIOS:
            print(f'  - {name}: {msg}')
        return 0

    for k, v in {'SECRET_KEY': 'eval', 'FACEBOOK_ACCESS_TOKEN': 'eval', 'FACEBOOK_APP_SECRET': 'eval',
                 'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:', 'RATELIMIT_ENABLED': 'False',
                 'INITIAL_ADMIN_PASSWORD': 'eval-password-12345'}.items():
        os.environ.setdefault(k, v)
    os.environ.pop('FLASK_SKIP_INIT_DB', None)
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')

    from app import app
    import services

    passed = total = 0
    with app.app_context():
        seed_links()
        if args.persona:
            set_persona(Path(args.persona).read_text(encoding='utf-8').strip())
        for name, msg, hist, state, stage, exp in SCENARIOS:
            history = [{'role': r, 'content': c} for r, c in hist]
            raw = services.generate_bot_response(msg, history, session_state=state, funnel_stage=stage)
            reply, detail = services.extract_handoff(raw)
            checks = score(reply, detail, exp)
            passed += sum(checks.values())
            total += len(checks)
            print(f"\n--- {name} | {len(reply)} тэмдэгт | handoff={detail}")
            print(reply)
            print('    ' + '  '.join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items()))
    print(f"\nSCORE {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == '__main__':
    sys.exit(main())
