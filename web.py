#!/usr/bin/env python3
"""Backend web d'administration du lecteur vidéo."""
import json
import os
import subprocess

from flask import Flask, jsonify, render_template, request
from PIL import Image, ImageOps
from werkzeug.utils import secure_filename

from common import (IMAGE_DURATION, IMAGE_DURATION_MAX, MEDIA_DIR,
                    SUBTITLE_SIZES, available_gpios, fps_of, load_config,
                    media_kind, player_request, save_config)
from transcode import INCOMING_DIR, Converter

app = Flask(__name__)
PORT = 8080

_probe_cache = {}  # nom -> (mtime, infos)


def probe(path):
    """Durée, résolution et codec d'un média (via ffprobe, mis en cache)."""
    mtime = path.stat().st_mtime
    cached = _probe_cache.get(path.name)
    if cached and cached[0] == mtime:
        return cached[1]
    info = {}
    if media_kind(path.name) == "subtitle":
        text = path.read_text(errors="replace")
        info = {"cues": text.count("-->") or text.count("Dialogue:")}
        _probe_cache[path.name] = (mtime, info)
        return info
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-of", "json",
             "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate",
             str(path)],
            capture_output=True, text=True, errors="replace", timeout=15,
        ).stdout
        data = json.loads(out)
        video = next((s for s in data.get("streams", [])
                      if s.get("codec_type") == "video"), {})
        info = {
            "fps": round(fps_of(video), 2) if video else None,
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
        }
        if media_kind(path.name) == "video":
            info["duration"] = float(data.get("format", {}).get("duration", 0)) or None
    except (subprocess.SubprocessError, ValueError):
        pass
    _probe_cache[path.name] = (mtime, info)
    return info


def warnings_for(kind, info):
    warn = []
    if kind == "video":
        if info.get("codec") and info["codec"] != "h264":
            warn.append(f"codec {info['codec']} : pas de décodage matériel sur ce Pi, "
                        "risque de saccades (préférez du H.264)")
        if (info.get("width") or 0) > 1920 or (info.get("height") or 0) > 1080:
            warn.append("résolution supérieure à 1080p : non supportée en matériel")
        elif (info.get("height") or 0) > 720 and (info.get("fps") or 0) > 30:
            warn.append(f"{info['fps']:g} i/s en 1080p : au-delà des 30 i/s garantis "
                        "par le décodeur du Pi 3, vérifiez la fluidité")
    if kind == "image" and ((info.get("width") or 0) > 2048 or (info.get("height") or 0) > 2048):
        warn.append("image trop grande pour le GPU du Pi (2048 px max) : renvoyez-la "
                    "pour qu'elle soit redimensionnée")
    return warn


def list_media():
    items = []
    for path in sorted(MEDIA_DIR.iterdir(), key=lambda p: p.name.lower()):
        kind = media_kind(path.name)
        if not kind or not path.is_file():
            continue
        info = probe(path)
        items.append({
            "name": path.name, "kind": kind, "size": path.stat().st_size,
            **info, "warnings": warnings_for(kind, info),
        })
    return items


def media_usage(cfg, name):
    uses = []
    if any(it["media"] == name for it in cfg["loop"]["items"]):
        uses.append("playlist")
    if cfg["interactive"]["attract"] == name:
        uses.append("accroche")
    for t in cfg["interactive"]["triggers"]:
        if t["media"] == name:
            uses.append(f"GPIO{t['gpio']}")
    for video, sub in cfg["subtitles"].items():
        if sub == name:
            uses.append(f"sous-titres de {video}")
    return uses


def prepare_image(path):
    """Adapte une image à l'écran : rotation EXIF appliquée, 1920×1080 maximum.

    Le GPU du Pi 3 refuse les textures de plus de 2048 pixels : une photo plus
    grande s'affiche mal. L'image n'est réécrite que si elle doit changer.
    """
    with Image.open(path) as im:
        fmt = im.format
        if getattr(im, "n_frames", 1) > 1:
            return   # GIF animé : laissé tel quel
        if fmt == "JPEG":
            im.draft("RGB", (1920, 1080))   # décodage réduit : moins de mémoire
        orientation = im.getexif().get(0x0112, 1)
        too_big = im.width > 1920 or im.height > 1080
        if orientation == 1 and not too_big and im.mode in ("RGB", "RGBA", "L"):
            return
        out = ImageOps.exif_transpose(im)
        if out.mode not in ("RGB", "RGBA", "L"):
            out = out.convert("RGBA" if "A" in out.mode else "RGB")
        out.thumbnail((1920, 1080), Image.LANCZOS)
        if fmt == "JPEG" and out.mode == "RGBA":
            out = out.convert("RGB")
        options = {"quality": 92} if fmt in ("JPEG", "WEBP") else {}
        out.save(path, format=fmt, **options)


def normalize_subtitle(raw, name):
    """Vérifie un fichier de sous-titres et le convertit en UTF-8."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")   # fichiers Windows
    marker = "[Events]" if name.lower().endswith(".ass") else "-->"
    if marker not in text:
        raise ValueError("fichier de sous-titres illisible ou vide")
    return text.replace("\r\n", "\n").encode("utf-8")


def player_status():
    try:
        return player_request("status")
    except OSError:
        return None


def reload_player():
    try:
        player_request("reload")
    except OSError:
        pass


def media_ready(name):
    if media_usage(load_config(), name):
        reload_player()  # le fichier remplacé est en cours d'utilisation


converter = Converter(on_done=media_ready)


def playlist_item(it):
    """Entrée de playlist nettoyée : répétitions (vidéo) ou durée (image)."""
    if media_kind(it["media"]) == "image":
        duration = float(it.get("duration") or IMAGE_DURATION)
        return {"media": it["media"],
                "duration": round(max(1, min(IMAGE_DURATION_MAX, duration)), 1)}
    return {"media": it["media"], "repeat": max(1, min(999, int(it.get("repeat") or 1)))}


def validate(cfg):
    errors = []
    media = {m["name"]: m["kind"] for m in list_media()}
    gpios = {g["gpio"] for g in available_gpios()}

    if cfg.get("mode") not in ("loop", "interactive"):
        errors.append("mode inconnu")
    try:
        cfg["volume"] = max(0, min(100, int(cfg.get("volume", 100))))
    except (TypeError, ValueError):
        errors.append("volume invalide")

    playable = {n for n, k in media.items() if k in ("video", "image")}
    items = cfg["loop"]["items"]
    for it in items:
        if it["media"] not in playable:
            errors.append(f"playlist : média introuvable ({it['media']})")
    if cfg["mode"] == "loop" and not items:
        errors.append("playlist : ajoutez au moins un média")

    inter = cfg["interactive"]
    if inter.get("attract") and inter["attract"] not in playable:
        errors.append(f"accroche : média introuvable ({inter['attract']})")
    seen = set()
    for t in inter["triggers"]:
        gpio = t.get("gpio")
        if gpio not in gpios:
            errors.append(f"GPIO{gpio} n'est pas disponible")
        if gpio in seen:
            errors.append(f"GPIO{gpio} est utilisée plusieurs fois")
        seen.add(gpio)
        if media.get(t.get("media")) != "video":
            errors.append(f"GPIO{gpio} : choisissez une vidéo")

    for video, sub in cfg["subtitles"].items():
        if media.get(video) != "video" or media.get(sub) != "subtitle":
            errors.append(f"sous-titres invalides pour {video}")
    style = cfg["subtitle_style"]
    if style.get("size") not in SUBTITLE_SIZES:
        errors.append("taille de sous-titres inconnue")
    style["background"] = bool(style.get("background"))
    return errors


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def api_state():
    return jsonify(
        config=load_config(),
        media=list_media(),
        gpios=available_gpios(),
        player=player_status(),
        jobs=converter.list(),
    )


@app.get("/api/status")
def api_status():
    return jsonify(player=player_status(), jobs=converter.list())


@app.post("/api/config")
def api_config():
    cfg = load_config()
    body = request.get_json(force=True)
    for key in ("mode", "volume", "audio_device"):
        if key in body:
            cfg[key] = body[key]
    for key in ("loop", "interactive", "subtitle_style"):
        if isinstance(body.get(key), dict):
            cfg[key].update(body[key])
    if isinstance(body.get("subtitles"), dict):
        # association vidéo -> sous-titres ; une valeur vide retire l'association
        cfg["subtitles"] = {v: s for v, s in body["subtitles"].items() if s}
    try:
        cfg["loop"]["items"] = [playlist_item(it)
                                for it in cfg["loop"]["items"] if it.get("media")]
    except (TypeError, ValueError, KeyError):
        return jsonify(errors=["playlist : répétitions ou durée invalide"]), 400
    cfg["interactive"]["triggers"] = [
        {"gpio": int(t["gpio"]), "media": t.get("media")}
        for t in cfg["interactive"]["triggers"]
    ]
    errors = validate(cfg)
    if errors:
        return jsonify(errors=errors), 400
    save_config(cfg)
    reload_player()
    return jsonify(ok=True)


@app.put("/api/media/<path:filename>")
def api_upload(filename):
    # Le fichier est envoyé brut et écrit directement sur la carte SD :
    # pas de copie temporaire en RAM (/tmp), indispensable pour les grosses vidéos.
    # Les vidéos passent par la file de conversion, les images sont prêtes.
    name = secure_filename(filename)
    kind = media_kind(name)
    if not name or not kind:
        return jsonify(error="format non supporté"), 400
    if kind == "subtitle":
        limit = 5 * 1024 * 1024
        raw = request.stream.read(limit + 1)
        if len(raw) > limit:
            return jsonify(error="fichier de sous-titres trop gros"), 400
        try:
            data = normalize_subtitle(raw, name)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        tmp = MEDIA_DIR / f".{os.urandom(4).hex()}.upload"
        tmp.write_bytes(data)
        os.replace(tmp, MEDIA_DIR / name)
        media_ready(name)   # rechargement si ces sous-titres sont affichés
        return jsonify(ok=True, name=name)

    dest_dir = INCOMING_DIR if kind == "video" else MEDIA_DIR
    tmp = dest_dir / f".{os.urandom(4).hex()}.upload"
    try:
        with open(tmp, "wb") as f:
            while chunk := request.stream.read(1024 * 1024):
                f.write(chunk)
        if kind == "video":
            job = converter.add(tmp, name)
            return jsonify(ok=True, name=job.name, job=job.id)
        try:
            prepare_image(tmp)
        except OSError as e:
            return jsonify(error=f"image illisible : {e}"), 400
        os.replace(tmp, dest_dir / name)
    finally:
        tmp.unlink(missing_ok=True)
    media_ready(name)
    return jsonify(ok=True, name=name)


@app.post("/api/jobs/<job_id>/retry")
def api_retry_job(job_id):
    return jsonify(ok=converter.retry(job_id))


@app.delete("/api/jobs/<job_id>")
def api_cancel_job(job_id):
    return jsonify(ok=converter.remove(job_id))


@app.delete("/api/media/<path:name>")
def api_delete(name):
    path = MEDIA_DIR / secure_filename(name)
    if not path.is_file():
        return jsonify(error="fichier introuvable"), 404
    cfg = load_config()
    uses = media_usage(cfg, path.name)
    if uses:
        return jsonify(error=f"utilisé par : {', '.join(uses)}"), 409
    path.unlink()
    if cfg["subtitles"].pop(path.name, None):   # vidéo supprimée
        save_config(cfg)
    return jsonify(ok=True)


@app.post("/api/trigger/<int:gpio>")
def api_trigger(gpio):
    try:
        return jsonify(player_request("trigger", gpio=gpio))
    except OSError:
        return jsonify(error="lecteur injoignable"), 503


if __name__ == "__main__":
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
