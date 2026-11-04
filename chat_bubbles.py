#!/usr/bin/env python3
"""Lynx-Tx: BATC chat as iMessage-style blue bubbles on the programme.

The same look as Lynx-Enigma2's (Justin, 2 Nov 2026: "Bloody perfect!"):
Apple blue, white text, the sender and time above in pale blue, a tail
curled off the bottom right, each bubble as wide as its message and set
against the right-hand edge, newest at the bottom. Drawn at four times the
size and shrunk, so the edges are smooth.

render(messages, height) gives one RGBA picture of the whole stack, for
the switcher's gdkpixbufoverlay. Sizes are worked at 1080 lines and
scaled to the programme's height, so 1080p and 2160p look the same.

  python3 chat_bubbles.py      # writes /tmp/chat_preview.png
"""
from PIL import Image, ImageDraw, ImageFont

BLUE = (10, 132, 255, 255)
NAME = (214, 232, 255, 255)
TEXT = (255, 255, 255, 255)
# At 1080 lines (the Enigma2 box's sizes).
W, PAD, INSET, WHO, LINE, TAIL, TIP, RADIUS, GAP = 640, 16, 26, 38, 36, 12, 14, 34, 12
NAME_PT, TEXT_PT, LINES = 26, 30, 5
SS = 4
FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf")
BOLD = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")
_fonts = {}


def _font(bold, px):
    key = (bold, px)
    if key not in _fonts:
        for path in (BOLD if bold else FONTS):
            try:
                _fonts[key] = ImageFont.truetype(path, px)
                break
            except OSError:
                continue
        else:
            _fonts[key] = ImageFont.load_default()
    return _fonts[key]


def _wrap(text, font, width):
    """Lines no wider than width, breaking at spaces (long words cut)."""
    lines, line = [], ""
    for word in text.split():
        trial = (line + " " + word).strip()
        if font.getlength(trial) <= width:
            line = trial
            continue
        if line:
            lines.append(line)
        while font.getlength(word) > width:          # a very long word
            cut = len(word)
            while cut > 1 and font.getlength(word[:cut]) > width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        line = word
    if line:
        lines.append(line)
    if len(lines) > LINES:
        lines = lines[:LINES]
        while lines[-1] and font.getlength(lines[-1] + "...") > width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "..."
    return lines or [""]


def _bubble(d, x0, y0, w, h, k):
    """The body and the tail, on a drawing at SS times the size."""
    r = RADIUS * k
    right = x0 + w
    d.rounded_rectangle([x0, y0, right, y0 + h - 1], radius=r, fill=BLUE)

    def bez(p0, c, p1, n=24):
        return [((1 - u) ** 2 * p0[0] + 2 * (1 - u) * u * c[0] + u * u * p1[0],
                 (1 - u) ** 2 * p0[1] + 2 * (1 - u) * u * c[1] + u * u * p1[1])
                for u in [i / float(n) for i in range(n + 1)]]
    by = y0 + h
    top = (right, by - 40 * k)
    tip = (right + 13 * k, by + TAIL * k - 1)
    foot = (right - 30 * k, by - 1)
    d.polygon(bez(top, (right, by), tip) + bez(tip, (right - 6 * k, by + 4 * k), foot)
              + [(right - 30 * k, by - 40 * k)], fill=BLUE)


def render(messages, height=1080):
    """messages: [(name, text, hhmm)], oldest first. One RGBA picture, its
    right edge to go against the programme's right-hand margin."""
    s = height / 1080.0
    px = lambda v: max(1, int(round(v * s)))
    name_f, text_f = _font(True, px(NAME_PT)), _font(False, px(TEXT_PT))
    text_w = px(W - TIP - 2 * INSET)
    laid = []
    for name, text, hhmm in messages:
        who = "%s   %s" % (name, hhmm)
        lines = _wrap(text, text_f, text_w)
        inner = max([name_f.getlength(who)] + [text_f.getlength(l) for l in lines])
        body_w = int(min(text_w, inner + px(4)) + 2 * px(INSET))
        body_w = max(body_w, px(RADIUS * 2 + 10))
        body_h = px(PAD * 2 + WHO) + len(lines) * px(LINE)
        laid.append((who, lines, body_w, body_h))
    if not laid:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    full_w = px(W)
    full_h = sum(b[3] + px(TAIL) for b in laid) + px(GAP) * (len(laid) - 1)
    big = Image.new("RGBA", (full_w * SS, full_h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    y = 0
    for who, lines, body_w, body_h in laid:
        x = full_w - px(TIP) - body_w                 # right-aligned, the tail beyond
        _bubble(d, x * SS, y * SS, body_w * SS, body_h * SS, s * SS)
        y += body_h + px(TAIL) + px(GAP)
    img = big.resize((full_w, full_h), Image.LANCZOS)
    # Text drawn at full size afterwards: sharper than shrinking it.
    d = ImageDraw.Draw(img)
    y = 0
    for who, lines, body_w, body_h in laid:
        x = full_w - px(TIP) - body_w + px(INSET)
        d.text((x, y + px(PAD) + px(4)), who, font=name_f, fill=NAME)
        ty = y + px(PAD) + px(WHO)
        for line in lines:
            d.text((x, ty), line, font=text_f, fill=TEXT)
            ty += px(LINE)
        y += body_h + px(TAIL) + px(GAP)
    return img


def lines_of(text, height=1080):
    """How many lines a message wraps to, for how long it stays up."""
    s = height / 1080.0
    return len(_wrap(text, _font(False, max(1, int(round(TEXT_PT * s)))),
                     max(1, int(round((W - TIP - 2 * INSET) * s)))))


if __name__ == "__main__":
    demo = [("M0DTS_Rob", "Well I managed to access briefly just now with 100 W into a 25 element Yagi from IO94LI", "17:36"),
            ("G8YTZ", "?", "17:37"),
            ("KD6ILO_Mario", "LabTech_Studio", "18:35")]
    bg = Image.new("RGBA", (1920, 1080), (40, 70, 40, 255))
    pic = render(demo, 1080)
    bg.alpha_composite(pic, (1920 - pic.width - 40, 40))
    bg.save("/tmp/chat_preview.png")
    print("preview at /tmp/chat_preview.png", pic.size)
