"""Conversion des vidéos importées vers un format optimal pour le Pi 3.

Cible : H.264 (décodé matériellement), yuv420p, conteneur MP4, résolution et
cadence d'origine conservées (sauf au-delà de 1080p, limite du décodeur).

Les fichiers envoyés arrivent dans media/.incoming/ ; une file de conversion
unique les traite un par un et dépose le résultat dans media/. Au redémarrage
du backend, les fichiers restés dans .incoming/ sont remis en file.
"""
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from common import MEDIA_DIR, fps_of

INCOMING_DIR = MEDIA_DIR / ".incoming"

MAX_W, MAX_H = 1920, 1080
MAX_BITRATE = 25_000_000       # au-delà, risque de saccades à la lecture
AUDIO_COPY = {"aac", "mp3"}
# dispositions que ffmpeg sait réduire en stéréo (-ac 2)
STANDARD_LAYOUTS = {"mono", "stereo", "2.1", "3.0", "3.0(back)", "3.1", "4.0",
                    "quad", "quad(side)", "4.1", "5.0", "5.0(side)", "5.1",
                    "5.1(side)", "6.0", "6.1", "7.0", "7.1", "7.1(wide)"}
SILENCE_DB = -60

# Encodeur matériel du Pi 3 : ~5x plus rapide que x264 à qualité équivalente
# (mesuré : 1080p25, ~2,5 s de conversion par seconde de vidéo).
# x264 ne sert qu'en secours si l'encodeur matériel refuse la source.
ENCODERS = ("h264_v4l2m2m", "libx264")


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", "-show_entries",
         "format=duration,bit_rate,format_name:stream=codec_type,codec_name,"
         "profile,pix_fmt,width,height,avg_frame_rate,r_frame_rate,field_order,"
         "channels,channel_layout",
         str(path)],
        capture_output=True, text=True, errors="replace", timeout=30,
    ).stdout
    data = json.loads(out or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    return video, audio, fmt


def plan(path):
    """Renvoie (action, raisons) : action = copy | remux | transcode."""
    video, audio, fmt = probe(path)
    if not video:
        raise ValueError("aucune piste vidéo détectée")
    reasons = []
    if video.get("codec_name") != "h264":
        reasons.append(f"codec {video.get('codec_name')}")
    if video.get("pix_fmt") != "yuv420p":
        reasons.append(f"format de pixel {video.get('pix_fmt')}")
    if (video.get("width") or 0) > MAX_W or (video.get("height") or 0) > MAX_H:
        reasons.append(f"résolution {video['width']}×{video['height']}")
    if video.get("field_order") not in (None, "progressive", "unknown"):
        reasons.append("vidéo entrelacée")
    if int(fmt.get("bit_rate") or 0) > MAX_BITRATE:
        reasons.append(f"débit {int(fmt['bit_rate']) // 1_000_000} Mb/s")
    if reasons:
        return "transcode", reasons
    audio_ok = not audio or audio.get("codec_name") in AUDIO_COPY
    if "mp4" not in fmt.get("format_name", "") or not audio_ok:
        return "remux", ["conteneur ou audio à adapter"]
    return "copy", []


def audio_mix(path, audio):
    """Choix des canaux pour une piste audio multicanale non standard.

    Renvoie (filtre ffmpeg, explication) ou (None, None) si ffmpeg sait faire
    le mixage stéréo seul. Sinon on garde les deux premiers canaux non
    silencieux (exports pro : canal 1 muet, paire stéréo en 2-3, etc.).
    """
    channels = int(audio.get("channels") or 0) if audio else 0
    if channels <= 2 or audio.get("channel_layout") in STANDARD_LAYOUTS:
        return None, None
    # niveau de chaque canal sur les 3 premières minutes
    err = subprocess.run(
        ["nice", "-n", "19", "ffmpeg", "-hide_banner", "-nostats", "-t", "180",
         "-i", str(path), "-map", "0:a:0", "-af",
         "astats=metadata=0:measure_perchannel=RMS_level:measure_overall=none",
         "-f", "null", "-"],
        capture_output=True, text=True, errors="replace", timeout=600,
    ).stderr
    levels = []
    for line in err.splitlines():
        if "RMS level dB:" in line:
            value = line.rsplit(":", 1)[1].strip()
            levels.append(float("-inf") if "inf" in value else float(value))
    active = [i for i, db in enumerate(levels[:channels]) if db > SILENCE_DB]
    if not active:
        return "pan=stereo|c0=c0|c1=c1", f"audio {channels} canaux silencieux"
    if len(active) == 1:
        c = active[0]
        return (f"pan=stereo|c0=c{c}|c1=c{c}",
                f"audio {channels} canaux : canal {c + 1} seul utilisé")
    a, b = active[:2]
    note = f"audio {channels} canaux : canaux {a + 1} et {b + 1} utilisés en stéréo"
    ignored = [str(i + 1) for i in active[2:]]
    if ignored:
        note += f", canal {', '.join(ignored)} ignoré"
    return f"pan=stereo|c0=c{a}|c1=c{b}", note


def video_bitrate(video, fps):
    """~0,2 bit par pixel : 1080p25 -> ~10 Mb/s."""
    pixels = (video.get("width") or MAX_W) * (video.get("height") or MAX_H)
    return int(max(2e6, min(16e6, pixels * fps * 0.2)))


def build_command(src, dst, action, encoder=ENCODERS[0], audio_filter=None):
    video, audio, _ = probe(src)
    cmd = ["nice", "-n", "19", "ffmpeg", "-hide_banner", "-loglevel", "error",
           "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
           "-progress", "pipe:1", "-nostats"]
    if action == "transcode":
        fps = fps_of(video) or 25
        if encoder == "libx264":
            cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                    "-profile:v", "high", "-level:v", "4.1"]
        else:
            cmd += ["-c:v", encoder, "-b:v", str(video_bitrate(video, fps))]
        cmd += ["-pix_fmt", "yuv420p"]
        filters = []
        if video.get("field_order") not in (None, "progressive", "unknown"):
            filters.append("bwdif")
        if (video.get("width") or 0) > MAX_W or (video.get("height") or 0) > MAX_H:
            filters.append(f"scale={MAX_W}:{MAX_H}:force_original_aspect_ratio=decrease"
                           ":force_divisible_by=2")
        if filters:
            cmd += ["-vf", ",".join(filters)]
        # cadence d'origine conservée (pas de -r) ; image clé toutes les ~2 s
        cmd += ["-g", str(round(fps * 2))]
    else:
        cmd += ["-c:v", "copy"]
    if audio and audio.get("codec_name") in AUDIO_COPY:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-af", audio_filter] if audio_filter else ["-ac", "2"]
    cmd += ["-movflags", "+faststart", "-f", "mp4", str(dst)]
    return cmd


class Job:
    def __init__(self, src, job_id=None):
        # fichier source : .incoming/<id>--<nom d'origine>
        self.id = job_id or uuid.uuid4().hex[:8]
        self.src = src
        self.original = src.name.split("--", 1)[-1]
        self.name = final_name(self.original)   # nom final dans media/
        self.state = "queued"     # queued | analyzing | converting | done | error
        self.progress = 0.0
        self.eta = None
        self.reasons = []
        self.error = None
        self.proc = None
        self.cancelled = False
        self.finished = None

    def to_dict(self):
        return {"id": self.id, "name": self.name, "original": self.original,
                "state": self.state,
                "progress": self.progress, "eta": self.eta,
                "reasons": self.reasons, "error": self.error}


class Converter:
    def __init__(self, on_done):
        self.on_done = on_done    # appelé avec le nom du média prêt
        self.jobs = []
        self.lock = threading.Lock()
        self.wakeup = threading.Event()
        INCOMING_DIR.mkdir(parents=True, exist_ok=True)
        for f in sorted(INCOMING_DIR.iterdir(), key=lambda f: f.stat().st_mtime):
            if f.name.startswith("."):          # envoi interrompu
                f.unlink()
            elif "--" in f.name:                # reprise après redémarrage
                self.jobs.append(Job(f, f.name.split("--", 1)[0]))
        threading.Thread(target=self._worker, daemon=True).start()
        self.wakeup.set()

    def add(self, uploaded, original_name):
        job_id = uuid.uuid4().hex[:8]
        src = INCOMING_DIR / f"{job_id}--{original_name}"
        os.replace(uploaded, src)
        job = Job(src, job_id)
        # un nouvel envoi du même fichier remplace la conversion précédente
        for j in self.list():
            if j["name"] == job.name:
                self.remove(j["id"])
        with self.lock:
            self.jobs.append(job)
        self.wakeup.set()
        return job

    def list(self):
        with self.lock:
            return [j.to_dict() for j in self.jobs]

    def pending_names(self):
        with self.lock:
            return {j.name for j in self.jobs if j.state not in ("done", "error")}

    def retry(self, job_id):
        with self.lock:
            job = next((j for j in self.jobs if j.id == job_id), None)
            if not job or job.state != "error" or not job.src.exists():
                return False
            job.state, job.error, job.progress, job.eta = "queued", None, 0.0, None
        self.wakeup.set()
        return True

    def remove(self, job_id):
        with self.lock:
            job = next((j for j in self.jobs if j.id == job_id), None)
            if not job:
                return False
            job.cancelled = True
            if job.proc:
                job.proc.terminate()
            if job.state != "converting":
                self.jobs.remove(job)
                job.src.unlink(missing_ok=True)
        return True

    def _worker(self):
        while True:
            self.wakeup.wait()
            self.wakeup.clear()
            while job := self._next():
                self._run(job)
                with self.lock:
                    if job.cancelled and job in self.jobs:
                        self.jobs.remove(job)
                        job.src.unlink(missing_ok=True)
                    # on ne garde les conversions terminées que 10 min à l'écran
                    now = time.time()
                    self.jobs = [j for j in self.jobs
                                 if j.state != "done" or now - j.finished < 600]

    def _next(self):
        with self.lock:
            return next((j for j in self.jobs if j.state == "queued"), None)

    def _run(self, job):
        tmp = MEDIA_DIR / f".{job.name}.part"
        try:
            job.state = "analyzing"
            action, job.reasons = plan(job.src)
            _, audio, fmt = probe(job.src)
            duration = float(fmt.get("duration") or 0)
            audio_filter, note = audio_mix(job.src, audio)
            if note:
                job.reasons.append(note)
                if action == "copy":
                    action = "remux"

            if action == "copy":
                os.replace(job.src, tmp)
            else:
                job.state = "converting"
                encoders = ENCODERS if action == "transcode" else ENCODERS[:1]
                for encoder in encoders:
                    cmd = build_command(job.src, tmp, action, encoder, audio_filter)
                    err = self._ffmpeg(job, cmd, duration)
                    if err is None or job.cancelled:
                        break
                if job.cancelled:
                    return
                if err is not None:
                    raise RuntimeError(err)
                job.src.unlink(missing_ok=True)

            os.replace(tmp, MEDIA_DIR / job.name)
            job.state, job.progress, job.eta = "done", 1.0, None
            self.on_done(job.name)
        except Exception as e:
            job.state, job.error = "error", str(e)
        finally:
            job.proc = None
            job.finished = time.time()
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _ffmpeg(job, cmd, duration):
        """Lance ffmpeg en suivant la progression ; renvoie None ou l'erreur."""
        job.progress, job.eta = 0.0, None
        start = time.monotonic()
        job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, errors="replace")
        for line in job.proc.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.isdigit() and duration:
                job.progress = min(1.0, int(value) / 1e6 / duration)
                elapsed = time.monotonic() - start
                if job.progress > 0.02:
                    job.eta = elapsed / job.progress - elapsed
        err = job.proc.stderr.read().strip()
        if job.proc.wait() == 0:
            return None
        return err.splitlines()[-1] if err else "échec de ffmpeg"


def final_name(filename):
    return Path(filename).stem + ".mp4"
