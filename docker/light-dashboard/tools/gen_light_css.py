#!/usr/bin/env python3
"""Emit the light-theme override stylesheet for the light-dashboard page.

The page is buildless: every colour is a Tailwind arbitrary value baked into a
class name (bg-[#11111b]), so there is no theme layer to swap. This walks the
markup, collects the colour utilities actually used, and writes one override
rule per class under html[data-theme="light"] — Catppuccin Mocha mapped to its
Latte counterpart.
"""
import re
import sys
from collections import OrderedDict

SRC = sys.argv[1]

# Mocha hex -> Latte hex. Some hexes mean different things depending on the
# utility they are used with (#585b70 is muted *text* in 139 places and a
# surface in a handful), so a utility-specific value wins over the default.
DEFAULT = {
    # base / surfaces
    '#11111b': '#eff1f5',  # page
    '#181825': '#ffffff',  # cards, sidebar
    '#1e1e2e': '#e6e9ef',
    '#313244': '#dce0e8',
    '#45475a': '#bcc0cc',
    '#585b70': '#acb0be',
    # text
    '#cdd6f4': '#4c4f69',
    '#bac2de': '#4c4f69',
    '#a6adc8': '#5c5f77',
    '#9399b2': '#6c6f85',
    '#7f849c': '#7c7f93',
    '#6c7086': '#8c8fa1',
    '#000000': '#eff1f5',
    '#ffffff': '#eff1f5',
    # accents
    '#b4befe': '#7287fd',  # lavender
    '#89b4fa': '#1e66f5',  # blue
    '#a6e3a1': '#40a02b',  # green
    '#f9e2af': '#df8e1d',  # yellow
    '#fab387': '#fe640b',  # peach
    '#f38ba8': '#d20f39',  # red
    '#cba6f7': '#8839ef',  # mauve
    '#f5c2e7': '#ea76cb',  # pink
    '#89dceb': '#04a5e5',  # sky
    # stray tailwind-palette leftovers
    '#9ca3af': '#6c6f85',
    '#6b7280': '#7c7f93',
    '#d1d5db': '#5c5f77',
    '#374151': '#ccd0da',
    '#2d3748': '#ccd0da',
    '#111827': '#ffffff',
    '#0b0f19': '#eff1f5',
    '#ef4444': '#d20f39',
    '#f87171': '#e64553',
    '#10b981': '#179299',
    '#34d399': '#40a02b',
    '#3b82f6': '#1e66f5',
}

# (utility, hex) -> Latte hex, overriding DEFAULT.
SPECIFIC = {
    ('text', '#585b70'): '#7c7f93',   # muted label, not a surface
    ('text', '#45475a'): '#9ca0b0',
    ('text', '#313244'): '#acb0be',
    ('placeholder', '#585b70'): '#9ca0b0',
    ('border', '#313244'): '#ccd0da',  # a border needs more bite than a fill
    ('border', '#585b70'): '#acb0be',
    ('border', '#45475a'): '#bcc0cc',
    ('text', '#11111b'): '#eff1f5',   # sits on a saturated accent fill
    ('text', '#1e1e2e'): '#e6e9ef',
    ('text', '#000000'): '#eff1f5',
    ('text', '#ffffff'): '#4c4f69',
}

# Translucent fills whose job survives the flip only if the value is rewritten
# by hand. A dark wash over a saturated row still reads as "pressed" in light
# mode, so it stays dark; a dark wash used as an inset panel on a dark card
# has to become a light wash on a white one, or it disappears.
PCT_SPECIFIC = {
    ('bg', '#11111b', '15'): 'rgb(17 17 27 / 0.15)',
    ('bg', '#11111b', '50'): 'rgb(204 208 218 / 0.55)',
    ('bg', '#11111b', '60'): 'rgb(204 208 218 / 0.6)',
    ('bg', '#181825', '50'): 'rgb(220 224 232 / 0.5)',
    ('bg', '#181825', '60'): 'rgb(220 224 232 / 0.6)',
}

PROP = {
    'bg': 'background-color',
    'text': 'color',
    'border': 'border-color',
    'divide': 'border-color',
    'placeholder': 'color',
    'ring': '--tw-ring-color',
    'fill': 'fill',
    'stroke': 'stroke',
    'from': '--tw-gradient-from',
    'to': '--tw-gradient-to',
}

TOKEN = re.compile(
    r'\b(bg|text|border|divide|placeholder|ring|fill|stroke|from|to)'
    r'-\[(#[0-9a-fA-F]{6})\](/(\d+))?')


def rgba(hexval, pct):
    r, g, b = (int(hexval[i:i + 2], 16) for i in (1, 3, 5))
    return f'rgb({r} {g} {b} / {int(pct) / 100:g})'


def esc(cls):
    return cls.replace('[', r'\[').replace(']', r'\]').replace('#', r'\#').replace('/', r'\/').replace('.', r'\.')


src = open(SRC).read()
seen = OrderedDict()
for m in TOKEN.finditer(src):
    util, hexval, _, pct = m.groups()
    seen[(util, hexval.lower(), pct)] = True

missing = sorted({h for (_, h, _) in seen if h not in DEFAULT})
if missing:
    sys.exit('no light value for: ' + ', '.join(missing))

lines = []
for (util, hexval, pct) in sorted(seen, key=lambda k: (k[0], k[1], k[2] or '')):
    light = SPECIFIC.get((util, hexval), DEFAULT[hexval])
    value = PCT_SPECIFIC.get((util, hexval, pct)) or (rgba(light, pct) if pct else light)
    cls = f'{util}-[{hexval}]' + (f'/{pct}' if pct else '')
    sel = f'.{esc(cls)}'
    if util == 'divide':
        sel += ' > :not([hidden]) ~ :not([hidden])'
    elif util == 'placeholder':
        sel += '::placeholder'
    lines.append(f'        html[data-theme="light"] {sel} {{ {PROP[util]}: {value}; }}')

block = '\n'.join(lines)
BEGIN = '/* @generated-light-overrides */'
END = '/* @end-light-overrides */'
if BEGIN in src and END in src:
    head, rest = src.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    open(SRC, 'w').write(f'{head}{BEGIN}\n{block}\n        {END}{tail}')
    print(f'{len(lines)} rules written into {SRC}', file=sys.stderr)
else:
    print(block)
