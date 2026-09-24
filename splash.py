"""Écran d'accueil affiché tant qu'aucun contenu n'est programmé.

Il indique où se connecter pour administrer le lecteur (adresse IP, nom
.local et QR code). Il est régénéré quand l'adresse réseau change.
"""
import socket
import subprocess

import qrcode
from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
FONT_DIR = "/usr/share/fonts/opentype/inter/"

BG = (15, 17, 21)
CARD = (26, 30, 37)
TEXT = (236, 239, 243)
MUTED = (139, 149, 161)
ACCENT = (96, 165, 250)
LINE = (42, 49, 58)


def font(name, size):
    try:
        return ImageFont.truetype(FONT_DIR + name, size)
    except OSError:  # police Inter absente : repli sur DejaVu
        bold = "Bold" in name or "SemiBold" in name
        return ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf"
            % ("-Bold" if bold else ""), size)


def network_addresses():
    """Adresses IPv4 du Pi (hors boucle locale), interface filaire en tête."""
    out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                         capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        parts = line.split()
        iface, addr = parts[1], parts[3].split("/")[0]
        found.append((0 if iface.startswith(("eth", "en")) else 1, addr))
    return [addr for _, addr in sorted(found)]


def mdns_available():
    return subprocess.run(["systemctl", "is-active", "--quiet", "avahi-daemon"]
                          ).returncode == 0


def wrap(draw, text, fnt, width):
    words, lines, line = text.split(), [], ""
    for word in words:
        test = f"{line} {word}".strip()
        if draw.textlength(test, font=fnt) <= width:
            line = test
        else:
            lines.append(line)
            line = word
    lines.append(line)
    return lines


def render(path, addresses, port, hostname=None):
    hostname = hostname or socket.gethostname()
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    left, col_w = 160, 980
    y = top = 130

    d.text((left, y), "LECTEUR VIDÉO", font=font("Inter-SemiBold.otf", 26),
           fill=ACCENT)
    y += 52
    d.text((left, y), "Prêt à être configuré",
           font=font("InterDisplay-SemiBold.otf", 76), fill=TEXT)
    y += 118

    body = font("Inter-Regular.otf", 30)
    intro = ("Aucun contenu n'est encore programmé. Depuis un ordinateur ou un "
             "téléphone connecté au même réseau, ouvrez l'interface "
             "d'administration :") if addresses else (
             "Aucun contenu n'est encore programmé, et le lecteur n'est pas "
             "encore connecté au réseau. Branchez un câble Ethernet ou "
             "configurez le Wi-Fi : l'adresse d'administration apparaîtra ici.")
    for line in wrap(d, intro, body, col_w):
        d.text((left, y), line, font=body, fill=MUTED)
        y += 44
    y += 28

    url = f"http://{addresses[0]}:{port}" if addresses else None
    if url:
        d.rounded_rectangle((left, y, left + col_w, y + 128), radius=18,
                            fill=CARD, outline=LINE, width=2)
        d.text((left + 44, y + 64), url, anchor="lm",
               font=font("InterDisplay-SemiBold.otf", 58), fill=ACCENT)
        alt = []
        if mdns_available():
            alt.append(f"http://{hostname}.local:{port}")
        alt += [f"http://{a}:{port}" for a in addresses[1:]]
        y += 146
        if alt:
            d.text((left + 4, y), "ou  " + "   ·   ".join(alt),
                   font=font("Inter-Regular.otf", 28), fill=MUTED)
        y += 76
    else:
        d.text((left, y), "En attente du réseau…",
               font=font("InterDisplay-SemiBold.otf", 60), fill=TEXT)
        y += 150

    steps = ["Envoyez vos vidéos et images dans la médiathèque",
             "Choisissez le mode : boucle simple ou interactif (boutons)",
             "Enregistrez : la lecture démarre sur cet écran"]
    step_font = font("Inter-Regular.otf", 30)
    num_font = font("Inter-SemiBold.otf", 26)
    for i, step in enumerate(steps, 1):
        cy = y + 22
        d.ellipse((left, cy - 22, left + 44, cy + 22), outline=ACCENT, width=3)
        d.text((left + 22, cy), str(i), font=num_font, fill=ACCENT, anchor="mm")
        d.text((left + 72, cy), step, font=step_font, fill=TEXT, anchor="lm")
        y += 62
    bottom = y

    if url:
        qr = qrcode.QRCode(border=0, box_size=1,
                           error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(url)
        qr.make(fit=True)
        modules = qr.modules_count
        size = modules * (360 // modules)   # modules de taille entière : net
        code = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        code = code.resize((size, size), Image.NEAREST)
        pad = 36
        caption = 74
        cx = W - 160 - pad - size
        cy = int(top + (bottom - top - size - caption) / 2)
        d.rounded_rectangle((cx - pad, cy - pad, cx + size + pad, cy + size + pad),
                            radius=24, fill=(255, 255, 255))
        img.paste(code, (cx, cy))
        d.text((cx + size / 2, cy + size + pad + 44), "Scanner pour administrer",
               font=font("Inter-Medium.otf", 30), fill=MUTED, anchor="mm")

    d.line((left, H - 120, W - 160, H - 120), fill=LINE, width=2)
    footer = font("Inter-Regular.otf", 24)
    d.text((left, H - 80), f"{hostname}  ·  {', '.join(addresses) or 'hors réseau'}",
           font=footer, fill=MUTED, anchor="lm")
    d.text((W - 160, H - 80),
           "Cet écran disparaît dès qu'un contenu est programmé",
           font=footer, fill=MUTED, anchor="rm")

    img.save(path)
