"""Every custom `btn-*` class a template uses must have a CSS rule somewhere.

`btn-brand` was used on 10 buttons (Хадгалах, Ангилах, add-unit, …) from the
Phase 4/5 admin rebuild on, but never defined — the buttons rendered as plain
text. Bootstrap's own button classes are exempt; anything else must be styled.
"""
import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / 'templates'

BOOTSTRAP_BUTTON_CLASSES = {
    'btn-primary', 'btn-secondary', 'btn-success', 'btn-danger', 'btn-warning',
    'btn-info', 'btn-light', 'btn-dark', 'btn-link', 'btn-sm', 'btn-lg',
    'btn-close', 'btn-close-white', 'btn-group', 'btn-group-sm', 'btn-group-lg',
    'btn-group-vertical', 'btn-toolbar', 'btn-check',
}


def test_every_custom_button_class_is_styled():
    sources = {p: p.read_text(encoding='utf-8') for p in TEMPLATES.rglob('*.html')}
    used = set()
    for text in sources.values():
        for classes in re.findall(r'class="([^"]*)"', text):
            used.update(c for c in classes.split() if c.startswith('btn-'))
    custom = {c for c in used
              if c not in BOOTSTRAP_BUTTON_CLASSES and not c.startswith('btn-outline-')}
    css = '\n'.join(sources.values())
    unstyled = sorted(c for c in custom if not re.search(r'\.' + re.escape(c) + r'\b[^{]*\{', css))
    assert unstyled == [], f'button classes used but never defined: {unstyled}'
