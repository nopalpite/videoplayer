"""Éléments partagés entre le lecteur (player.py) et le backend web (web.py)."""
import json
import os
import re
import socket
import subprocess
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
MEDIA_DIR = BASE / "media"
DATA_DIR = BASE / "data"
CONFIG_FILE = DATA_DIR / "config.json"
SOCKET_PATH = str(DATA_DIR / "player.sock")

VIDEO_EXT = {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm", ".mpg", ".mpeg", ".ts"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif"}
SUBTITLE_EXT = {".srt", ".vtt", ".ass"}

# taille des sous-titres (pixels mpv, référence 720 lignes)
SUBTITLE_SIZES = {"small": 36, "medium": 48, "large": 64}

# BCM -> broche physique du connecteur 40 broches
PHYSICAL = {
    2: 3, 3: 5, 4: 7, 5: 29, 6: 31, 7: 26, 8: 24, 9: 21, 10: 19, 11: 23,
    12: 32, 13: 33, 14: 8, 15: 10, 16: 36, 17: 11, 18: 12, 19: 35, 20: 38,
    21: 40, 22: 15, 23: 16, 24: 18, 25: 22, 26: 37, 27: 13,
}

DEFAULT_CONFIG = {
    "mode": "loop",                # "loop" | "interactive"
    "volume": 100,
    "audio_device": "auto",
    "loop": {"media": None, "muted": False},
    "interactive": {
        "attract": None,
        "attract_muted": True,
        "triggers_muted": False,
        "interruptible": True,
        "active_low": True,        # bouton relié à GND, pull-up interne
        "triggers": [],            # [{"gpio": 17, "media": "video.mp4"}]
    },
    "subtitles": {},               # {"video.mp4": "video.srt"}
    "subtitle_style": {"size": "medium", "background": True},
}


def media_kind(name):
    ext = Path(name).suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    if ext in SUBTITLE_EXT:
        return "subtitle"
    return None


def fps_of(stream):
    """Cadence d'une piste vidéo ffprobe (ex. "30000/1001" -> 29.97)."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = (stream.get(key) or "0/0").partition("/")
        if den and float(den):
            return float(num) / float(den)
    return 0


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        saved = json.loads(CONFIG_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return cfg
    for key, value in saved.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # écriture atomique : le lecteur ne lit jamais un fichier à moitié écrit
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)


def reserved_gpios():
    """GPIO utilisées par une fonction alternative (UART, I2C, SPI...)."""
    out = subprocess.run(["pinctrl", "get", "2-27"],
                         capture_output=True, text=True).stdout
    reserved = {}
    for line in out.splitlines():
        m = re.match(r"^\s*(\d+):\s+(\S+).*=\s*(.*)$", line)
        if m and m.group(2) not in ("ip", "op"):
            reserved[int(m.group(1))] = m.group(3).strip()
    return reserved


def available_gpios():
    reserved = reserved_gpios()
    return [
        {"gpio": bcm, "pin": pin}
        for bcm, pin in sorted(PHYSICAL.items())
        if bcm not in reserved
    ]


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


def player_request(cmd, **args):
    """Envoie une commande au lecteur via le socket unix, renvoie sa réponse."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(3)
        s.connect(SOCKET_PATH)
        s.sendall((json.dumps({"cmd": cmd, **args}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data)
