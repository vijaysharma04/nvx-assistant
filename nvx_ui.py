# Copyright (c) 2026 NVX One (nvxone.com). All rights reserved. Proprietary - see LICENSE. No copying or reuse without written permission.
"""Drawing helpers for NVX Assistant: rounded cards, buttons, pills and line icons.

Everything is rendered with Pillow at a higher resolution and scaled down, so edges stay smooth
at any Windows display scale.
"""
import os
import tkinter as tk

from PIL import Image, ImageDraw, ImageFont, ImageTk

# Light theme. Orange is the NVX brand colour and is used sparingly (selection, progress, updates).
BG = '#f4f5f7'
SURFACE = '#ffffff'
SIDE = '#eceef2'
SIDE_HOVER = '#e2e5ea'
LINE = '#e1e4e9'
TEXT = '#14171c'
MUTED = '#5f6b7a'
FAINT = '#98a2b3'
ACCENT = '#ff7a00'
ACCENT_SOFT = '#fff2e5'
DARK = '#14171c'
DARK_HOVER = '#2c323b'
BAD = '#d92d20'
BAD_SOFT = '#fef3f2'

FONT_DIR = os.path.join(os.environ.get('WINDIR', 'C:/Windows'), 'Fonts')
_fonts = {}


def pil_font(px, weight='regular'):
    """Segoe UI at a pixel size, for text drawn inside images (buttons, pills)."""
    key = (px, weight)
    if key not in _fonts:
        name = {'regular': 'segoeui.ttf', 'semibold': 'seguisb.ttf', 'bold': 'segoeuib.ttf'}[weight]
        try:
            _fonts[key] = ImageFont.truetype(os.path.join(FONT_DIR, name), px)
        except OSError:
            _fonts[key] = ImageFont.load_default()
    return _fonts[key]


def rounded(w, h, radius, fill, outline=None, backdrop=SURFACE, line=1):
    """A rounded rectangle on a solid backdrop (so it can sit on any flat parent)."""
    w, h = max(2, int(w)), max(2, int(h))
    ss = 3 if w * h < 160000 else 2
    img = Image.new('RGB', (w * ss, h * ss), backdrop)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=radius * ss, fill=outline or fill)
    if outline:
        d.rounded_rectangle((line * ss, line * ss, w * ss - 1 - line * ss, h * ss - 1 - line * ss),
                            radius=max(0, radius - line) * ss, fill=fill)
    return img.resize((w, h), Image.LANCZOS)


def icon(name, size, color):
    """A simple line icon on a 24-unit grid, returned as RGBA."""
    ss = 4
    s = size * ss
    img = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = s / 24.0
    w = max(1, int(round(1.9 * u)))

    def line(*pts):
        d.line([(x * u, y * u) for x, y in pts], fill=color, width=w, joint='curve')
        for x, y in (pts[0], pts[-1]):
            d.ellipse((x * u - w / 2, y * u - w / 2, x * u + w / 2, y * u + w / 2), fill=color)

    def circle(cx, cy, r):
        d.ellipse(((cx - r) * u, (cy - r) * u, (cx + r) * u, (cy + r) * u), outline=color, width=w)

    def box(x0, y0, x1, y1, r):
        d.rounded_rectangle((x0 * u, y0 * u, x1 * u, y1 * u), radius=r * u, outline=color, width=w)

    def dot(cx, cy, r):
        d.ellipse(((cx - r) * u, (cy - r) * u, (cx + r) * u, (cy + r) * u), fill=color)

    if name == 'back':
        line((15, 5), (8, 12), (15, 19))
    elif name == 'firmware':
        circle(12, 12, 9)
        line((12, 16.5), (12, 8))
        line((8.2, 11.6), (12, 7.8), (15.8, 11.6))
    elif name == 'logs':
        box(5, 3, 19, 21, 2.2)
        line((9, 8), (15, 8))
        line((9, 12), (15, 12))
        line((9, 16), (13, 16))
    elif name == 'info':
        circle(12, 12, 9)
        line((12, 11.2), (12, 16.6))
        dot(12, 7.7, 1.15)
    elif name == 'folder':
        line((3, 7), (3, 18.5), (21, 18.5), (21, 8.5), (12, 8.5), (10, 5.5), (3, 5.5), (3, 7))
    elif name == 'download':
        line((12, 4), (12, 15))
        line((7.5, 11), (12, 15.5), (16.5, 11))
        line((5, 20), (19, 20))
    elif name == 'check':
        line((5, 12.5), (10, 17.5), (19, 7))
    elif name == 'copy':
        box(9, 9, 20, 20, 2)
        line((15, 5.5), (15, 4), (4, 4), (4, 15), (5.5, 15))
    elif name == 'usb':
        box(7.5, 10, 16.5, 21, 2)
        line((10, 10), (10, 3.5), (14, 3.5), (14, 10))
        dot(12, 15.5, 1.3)
    elif name == 'x':
        line((6, 6), (18, 18))
        line((18, 6), (6, 18))
    elif name == 'drone':
        for cx, cy in ((5.5, 5.5), (18.5, 5.5), (5.5, 18.5), (18.5, 18.5)):
            circle(cx, cy, 3)
        line((7.7, 7.7), (10, 10))
        line((16.3, 7.7), (14, 10))
        line((7.7, 16.3), (10, 14))
        line((16.3, 16.3), (14, 14))
        box(9.5, 9.5, 14.5, 14.5, 1.4)
    return img.resize((size, size), Image.LANCZOS)


def on_colour(rgba, backdrop):
    """Flattens an RGBA image onto a solid colour."""
    base = Image.new('RGB', rgba.size, backdrop)
    base.paste(rgba, (0, 0), rgba)
    return base


def pill_image(text, fg, fill, backdrop, k, outline=None):
    """A small rounded status tag with text."""
    font = pil_font(int(round(12 * k)), 'semibold')
    tw = int(font.getlength(text))
    h = int(round(24 * k))
    w = tw + int(round(22 * k))
    img = rounded(w, h, h // 2, fill, outline, backdrop)
    ImageDraw.Draw(img).text((w / 2, h / 2 - k), text, font=font, fill=fg, anchor='mm')
    return img


class Card(tk.Frame):
    """White rounded panel with a hairline border. Put content in `.body`."""

    def __init__(self, parent, k, fill=SURFACE, border=LINE, radius=10, padx=20, pady=16):
        super().__init__(parent, bg=parent.cget('bg'), bd=0, highlightthickness=0)
        self.k, self.fill, self.border, self.radius = k, fill, border, radius
        self._size = None
        self._image = None
        self.back = tk.Label(self, bd=0, bg=parent.cget('bg'))
        self.back.place(x=0, y=0, relwidth=1, relheight=1)
        self.body = tk.Frame(self, bg=fill, bd=0)
        self.body.pack(fill='both', expand=True, padx=int(padx * k), pady=int(pady * k))
        self.bind('<Configure>', self._redraw)

    def _redraw(self, event):
        size = (event.width, event.height)
        if size == self._size or min(size) < 8:
            return
        self._size = size
        self._image = ImageTk.PhotoImage(rounded(size[0], size[1], int(self.radius * self.k), self.fill,
                                                 self.border, self.cget('bg')))
        self.back.configure(image=self._image)


class Button(tk.Label):
    """Rounded button. kind: primary (dark), secondary (outlined), danger (red outline)."""

    KINDS = {
        'primary': (DARK, DARK_HOVER, '#ffffff', None),
        'secondary': (SURFACE, '#f1f3f5', TEXT, '#cdd2da'),
        'danger': (SURFACE, BAD_SOFT, BAD, '#f0b4ae'),
    }

    def __init__(self, parent, k, text, command, kind='secondary', icon_name=None, min_width=0, height=34):
        self.command = command
        fill, hover, fg, border = self.KINDS[kind]
        backdrop = parent.cget('bg')
        font = pil_font(int(round(13 * k)), 'semibold')
        pad = int(16 * k)
        h = int(height * k)
        isz = int(16 * k)
        tw = int(font.getlength(text))
        content = tw + (isz + int(8 * k) if icon_name else 0)
        w = max(int(min_width * k), content + 2 * pad)

        def make(colour):
            img = rounded(w, h, int(7 * k), colour, border, backdrop)
            x = (w - content) // 2
            if icon_name:
                glyph = icon(icon_name, isz, fg)
                img.paste(glyph, (x, (h - isz) // 2), glyph)
                x += isz + int(8 * k)
            ImageDraw.Draw(img).text((x, h / 2 - k), text, font=font, fill=fg, anchor='lm')
            return ImageTk.PhotoImage(img)

        self._normal, self._hover = make(fill), make(hover)
        super().__init__(parent, image=self._normal, bg=backdrop, bd=0, cursor='hand2')
        self.bind('<Enter>', lambda _e: self.configure(image=self._hover))
        self.bind('<Leave>', lambda _e: self.configure(image=self._normal))
        self.bind('<ButtonRelease-1>', self._click)

    def _click(self, event):
        if 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height() and self.command:
            self.command()


class NavItem(tk.Frame):
    """Sidebar entry: accent bar, icon, label. Selected = white with the orange bar."""

    def __init__(self, parent, k, icon_name, text, command, font):
        super().__init__(parent, bg=SIDE, cursor='hand2')
        self.k, self.icon_name, self.selected = k, icon_name, False
        self.bar = tk.Frame(self, bg=SIDE, width=max(3, int(3 * k)))
        self.bar.pack(side='left', fill='y')
        size = int(18 * k)
        self._icons = {}
        for state, colour, backdrop in (('normal', MUTED, SIDE), ('hover', TEXT, SIDE_HOVER), ('selected', TEXT, SURFACE)):
            self._icons[state] = ImageTk.PhotoImage(on_colour(icon(icon_name, size, colour), backdrop))
        self.glyph = tk.Label(self, image=self._icons['normal'], bg=SIDE, bd=0)
        self.glyph.pack(side='left', padx=(int(17 * k), int(12 * k)), pady=int(11 * k))
        self.label = tk.Label(self, text=text, bg=SIDE, fg=MUTED, font=font, anchor='w')
        self.label.pack(side='left', fill='x', expand=True)
        self.badge = tk.Label(self, text='', bg=SIDE, fg=FAINT, font=font)
        self.badge.pack(side='right', padx=(0, int(16 * k)))
        for widget in (self, self.glyph, self.label, self.badge, self.bar):
            widget.bind('<Button-1>', lambda _e: command())
            widget.bind('<Enter>', lambda _e: self._paint(True))
            widget.bind('<Leave>', lambda _e: self._paint(False))

    def set_selected(self, selected):
        self.selected = selected
        self._paint(False)

    def set_badge(self, text, colour=FAINT):
        self.badge.configure(text=text, fg=colour)

    def _paint(self, hover):
        state = 'selected' if self.selected else 'hover' if hover else 'normal'
        bg = {'selected': SURFACE, 'hover': SIDE_HOVER, 'normal': SIDE}[state]
        for widget in (self, self.glyph, self.label, self.badge):
            widget.configure(bg=bg)
        self.glyph.configure(image=self._icons[state])
        self.label.configure(fg=MUTED if state == 'normal' else TEXT)
        self.bar.configure(bg=ACCENT if self.selected else bg)
