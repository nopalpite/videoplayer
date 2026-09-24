#!/usr/bin/env python3
"""Lecteur vidéo plein écran piloté par GPIO (mpv + gpiod).

Modes :
  - loop        : un seul média (vidéo ou image) en boucle, avec ou sans son.
  - interactive : un média d'accroche en boucle ; un bouton GPIO lance une
                  vidéo, puis retour à l'accroche quand elle est terminée.

Le backend web communique avec ce processus via un socket unix
(voir common.player_request) : commandes status, reload, trigger.
"""
import datetime
import json
import logging
import os
import queue
import signal
import socketserver
import subprocess
import threading
import time

import gpiod
import mpv
from gpiod.line import Bias, Direction, Edge

from common import (DATA_DIR, MEDIA_DIR, SOCKET_PATH, SUBTITLE_SIZES,
                    load_config, media_kind)

log = logging.getLogger("player")

PRESS_LOCKOUT = 0.3  # s : ignore les appuis trop rapprochés (rebonds, double appui)

# Boucle sans coupure : au lieu de revenir au début du fichier (loop-file de
# mpv : le décodeur matériel est vidé, image figée ~200 ms), on fait lire au
# démultiplexeur « concat » une liste qui répète la vidéo. Le décodeur reçoit
# un flux continu. mpv ne reboucle la liste qu'au bout de LOOP_HOURS.
LOOP_LIST = DATA_DIR / "loop.ffconcat"
LOOP_HOURS = 24
LOOP_MAX_ENTRIES = 20000


_durations = {}  # (chemin, date de modification) -> durée


def media_duration(path):
    key = (str(path), path.stat().st_mtime)
    if key not in _durations:
        _durations[key] = _probe_duration(path)
    return _durations[key]


def _probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, errors="replace", timeout=30).stdout
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def write_loop_list(path, duration):
    count = min(LOOP_MAX_ENTRIES, max(2, int(LOOP_HOURS * 3600 / duration)))
    # « duration » impose un décalage exact entre deux passages
    quoted = str(path).replace("'", "'\\''")   # échappement ffconcat
    entry = f"file '{quoted}'\nduration {duration:.6f}\n"
    content = "ffconcat version 1.0\n" + entry * count
    try:
        if LOOP_LIST.read_text() == content:
            return   # même vidéo : inutile de réécrire la carte SD
    except FileNotFoundError:
        pass
    tmp = LOOP_LIST.with_suffix(".tmp")
    tmp.write_text(content)
    os.replace(tmp, LOOP_LIST)


class GpioWatcher:
    """Surveille les broches d'entrée et signale chaque appui."""

    def __init__(self, gpios, active_low, on_press):
        self.stop_event = threading.Event()
        self.gpios = sorted(gpios)
        self.request = None
        if not gpios:
            return
        settings = gpiod.LineSettings(
            direction=Direction.INPUT,
            edge_detection=Edge.FALLING if active_low else Edge.RISING,
            bias=Bias.PULL_UP if active_low else Bias.PULL_DOWN,
            debounce_period=datetime.timedelta(milliseconds=20),
        )
        self.request = gpiod.request_lines(
            "/dev/gpiochip0", consumer="videoplayer",
            config={tuple(gpios): settings},
        )
        self.on_press = on_press
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            if self.request.wait_edge_events(datetime.timedelta(milliseconds=500)):
                for ev in self.request.read_edge_events():
                    self.on_press(ev.line_offset)

    def close(self):
        self.stop_event.set()
        if self.request:
            self.thread.join()
            self.request.release()


class Player:
    def __init__(self):
        self.events = queue.Queue()
        self.cfg = None
        self.state = "idle"        # idle | loop | attract | triggered
        self.current = None        # nom du média affiché
        self.current_sub = None    # sous-titres associés au média affiché
        self.current_gpio = None   # broche ayant lancé la vidéo en cours
        self.loop_len = None       # durée d'un passage en boucle continue
        self.loop_index = 0        # numéro du passage en cours (sous-titres)
        self.last_press = 0.0
        self.last_error = None
        self.watcher = None

        self.mpv = mpv.MPV(
            vo="gpu", gpu_context="drm", hwdec="v4l2m2m", ao="alsa",
            # Pi 3 : la vidéo décodée doit aller sur le plan principal ;
            # sur le plan overlay (défaut de mpv) chaque image est refusée
            # par le pilote vc4 et l'écran reste noir.
            drm_drmprime_video_plane="primary", drm_draw_plane="overlay",
            log_handler=self._mpv_log, loglevel="error",
            fullscreen=True, keep_open="yes", idle="yes", force_window="yes",
            image_display_duration="inf", background_color="#000000",
            osc=False, osd_level=0, input_default_bindings=False,
            sub_font="DejaVu Sans", sub_margin_y=50, sub_auto="no",
            input_vo_keyboard=False, cursor_autohide="always", terminal=False,
        )
        self.mpv.observe_property(
            "eof-reached", lambda _n, v: v and self.events.put(("eof",)))
        self.mpv.observe_property("time-pos", self._time_pos_changed)

        @self.mpv.event_callback("end-file")
        def _end_file(event):
            data = getattr(event, "data", None)
            if getattr(data, "reason", None) == mpv.MpvEventEndFile.ERROR:
                self.events.put(("error",))

    @staticmethod
    def _mpv_log(level, component, message):
        if "TTY" not in message and "VT switcher" not in message:
            log.error("mpv[%s] %s", component, message.strip())

    # --- boucle principale : toutes les transitions passent par ici ---------

    def run(self):
        self.events.put(("reload",))
        while True:
            try:
                ev, *args = self.events.get(timeout=1)
            except queue.Empty:
                continue
            try:
                if ev == "quit":
                    break
                getattr(self, "_on_" + ev)(*args)
            except Exception as e:  # une erreur ne doit jamais arrêter le lecteur
                log.exception("erreur sur l'évènement %s", ev)
                self.last_error = str(e)
        self._shutdown()

    def _on_reload(self):
        if self.watcher:
            self.watcher.close()
            self.watcher = None
        self.cfg = load_config()
        self.last_error = None
        self.mpv.volume = max(0, min(100, int(self.cfg.get("volume", 100))))
        self.mpv.audio_device = self.cfg.get("audio_device") or "auto"
        style = self.cfg["subtitle_style"]
        self.mpv.sub_font_size = SUBTITLE_SIZES.get(style.get("size"), 48)
        if style.get("background"):
            self.mpv.sub_border_style = "opaque-box"
            self.mpv.sub_back_color = "#99000000"   # noir à 60 %
        else:
            self.mpv.sub_border_style = "outline-and-shadow"

        if self.cfg["mode"] == "interactive":
            inter = self.cfg["interactive"]
            gpios = [t["gpio"] for t in inter["triggers"] if t.get("media")]
            try:
                self.watcher = GpioWatcher(gpios, inter["active_low"],
                                           self._gpio_pressed)
            except OSError as e:
                self.last_error = f"GPIO indisponible : {e}"
                log.error(self.last_error)
            self._show_attract()
        else:
            loop = self.cfg["loop"]
            self._play(loop["media"], loop=True, muted=loop["muted"])
            self.state = "loop" if self.current else "idle"
        log.info("configuration chargée : mode=%s", self.cfg["mode"])

    def _on_button(self, gpio):
        if not self.cfg or self.cfg["mode"] != "interactive":
            return
        inter = self.cfg["interactive"]
        media = next((t["media"] for t in inter["triggers"]
                      if t["gpio"] == gpio), None)
        if not media:
            return
        if self.state == "triggered" and not inter["interruptible"]:
            log.info("GPIO%d ignorée : vidéo en cours non interruptible", gpio)
            return
        log.info("GPIO%d -> %s", gpio, media)
        if self._play(media, loop=False, muted=inter["triggers_muted"]):
            self.state = "triggered"
            self.current_gpio = gpio

    def _on_eof(self):
        # vérifie que la fin concerne bien la vidéo déclenchée en cours
        if self.state == "triggered" and self.mpv.eof_reached:
            self._show_attract()

    def _on_loop_pass(self, index):
        # boucle continue : les horodatages ne reviennent pas à zéro, on décale
        # donc les sous-titres d'un passage à chaque tour
        if self.loop_len and index != self.loop_index:
            self.loop_index = index
            self.mpv.sub_delay = index * self.loop_len

    def _on_error(self):
        self.last_error = f"lecture impossible : {self.current}"
        log.error(self.last_error)
        if self.state == "triggered":
            self._show_attract()

    # --- actions -------------------------------------------------------------

    def _show_attract(self):
        inter = self.cfg["interactive"]
        self.current_gpio = None
        if self._play(inter["attract"], loop=True, muted=inter["attract_muted"]):
            self.state = "attract"
        else:
            self.state = "idle"

    def _play(self, media, loop, muted):
        kind = media_kind(media) if media else None
        if kind not in ("video", "image") or not (MEDIA_DIR / media).is_file():
            if media:
                self.last_error = f"média introuvable : {media}"
                log.error(self.last_error)
            self.mpv.command("stop")  # écran noir
            self.current = None
            return False
        path = MEDIA_DIR / media
        self.mpv.mute = bool(muted)
        self.mpv.loop_file = "inf" if loop else "no"
        sub = self.cfg["subtitles"].get(media) if kind == "video" else None
        subs = [str(MEDIA_DIR / sub)] if sub and (MEDIA_DIR / sub).is_file() else []
        self.mpv["sub-files"] = subs   # pris en compte au chargement du fichier
        self.current_sub = sub if subs else None
        self.mpv.sub_delay = 0
        self.loop_index = 0

        duration = media_duration(path) if loop and kind == "video" else 0
        if duration > 0.5:
            write_loop_list(path, duration)
            self.loop_len = duration
            # format détecté par l'en-tête « ffconcat » : ne pas forcer
            # demuxer-lavf-format, qui s'appliquerait aussi aux sous-titres
            self.mpv.demuxer_lavf_o = "safe=0"
            target = LOOP_LIST
        else:
            self.loop_len = None
            self.mpv.demuxer_lavf_o = ""
            target = path
        self.mpv.command("loadfile", str(target), "replace")
        self.current = media
        return True

    def _time_pos_changed(self, _name, pos):
        if self.loop_len and self.current_sub and pos is not None:
            index = int(pos // self.loop_len)
            if index != self.loop_index:
                self.events.put(("loop_pass", index))

    def _gpio_pressed(self, gpio):
        now = time.monotonic()
        if now - self.last_press < PRESS_LOCKOUT:
            return
        self.last_press = now
        self.events.put(("button", gpio))

    def _shutdown(self):
        if self.watcher:
            self.watcher.close()
        self.mpv.terminate()

    # --- état pour le backend web ---------------------------------------------

    def status(self):
        try:
            devices = [{"name": "auto", "description": "Automatique"}]
            for d in self.mpv.audio_device_list:
                # plughw : convertit le format si besoin, une entrée par carte
                if d["name"].startswith("alsa/plughw:"):
                    label = ("HDMI" if "hdmi" in d["name"].lower() else
                             "Prise jack" if "Headphones" in d["name"] else
                             d["description"])
                    devices.append({"name": d["name"], "description": label})
            position = self.mpv.time_pos
            duration = self.mpv.duration
        except Exception:
            devices, position, duration = [], None, None
        return {
            "state": self.state,
            "mode": self.cfg["mode"] if self.cfg else None,
            "media": self.current,
            "subtitles": self.current_sub,
            "gpio": self.current_gpio,
            "position": position,
            "duration": duration,
            "watched_gpios": self.watcher.gpios if self.watcher else [],
            "last_error": self.last_error,
            "audio_devices": devices,
        }


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        player = self.server.player
        try:
            req = json.loads(self.rfile.readline())
            cmd = req.get("cmd")
            if cmd == "status":
                resp = player.status()
            elif cmd == "reload":
                player.events.put(("reload",))
                resp = {"ok": True}
            elif cmd == "trigger":
                player.events.put(("button", int(req["gpio"])))
                resp = {"ok": True}
            else:
                resp = {"error": f"commande inconnue : {cmd}"}
        except Exception as e:
            resp = {"error": str(e)}
        try:
            self.wfile.write((json.dumps(resp) + "\n").encode())
        except BrokenPipeError:
            pass  # le client a abandonné (délai dépassé pendant le démarrage)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    player = Player()

    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    server = socketserver.ThreadingUnixStreamServer(SOCKET_PATH, ControlHandler)
    server.daemon_threads = True
    server.player = player
    threading.Thread(target=server.serve_forever, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: player.events.put(("quit",)))

    try:
        player.run()
    finally:
        server.shutdown()
        os.unlink(SOCKET_PATH)


if __name__ == "__main__":
    main()
