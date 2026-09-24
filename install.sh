#!/usr/bin/env bash
# Installateur DarkSign : d'une carte Raspberry Pi OS Lite (Trixie) fraîche à
# un lecteur qui démarre directement sur l'animation.
#
#   curl -fsSL https://raw.githubusercontent.com/nopalpite/videoplayer/main/install.sh | sudo bash
# ou, depuis un dépôt cloné :
#   sudo ./install.sh [options]
#
# Options :
#   --user NOM    utilisateur qui fait tourner le lecteur (défaut : celui qui
#                 lance sudo, sinon « pi »)
#   --dir CHEMIN  dossier d'installation (défaut : ~NOM/videoplayer)
#   --reset       efface médias, sous-titres et configuration d'une
#                 installation existante (retour à l'état « premier démarrage »)
#   --reboot      redémarre à la fin sans demander
#   --dry-run     affiche ce qui serait fait, sans rien modifier
#   --force       ignore les vérifications de matériel et de version
#
# Ré-exécutable sans risque : une installation existante est mise à jour, ses
# médias et sa configuration sont conservés (sauf --reset).
set -euo pipefail

REPO_URL="${DARKSIGN_REPO:-https://github.com/nopalpite/videoplayer.git}"
BRANCH="${DARKSIGN_BRANCH:-main}"
PACKAGES=(git mpv python3-mpv python3-flask python3-libgpiod python3-pil
          python3-qrcode python3-numpy fonts-inter ffmpeg avahi-daemon
          raspi-utils-core rsync)
BOOT_PARAMS=(quiet loglevel=3 logo.nologo vt.global_cursor_default=0
             consoleblank=0 systemd.show_status=false rd.udev.log_level=3
             udev.log_level=3)
BACKUP_SUFFIX=".avant-darksign"

TARGET_USER="${SUDO_USER:-}"
INSTALL_DIR=""
RESET=0; REBOOT=0; DRY_RUN=0; FORCE=0

# --- affichage -----------------------------------------------------------------
if [ -t 1 ]; then B=$'\e[1m'; A=$'\e[33m'; R=$'\e[31m'; G=$'\e[32m'; N=$'\e[0m'
else B=""; A=""; R=""; G=""; N=""; fi
step=0
title() { step=$((step + 1)); echo; echo "${B}${A}[$step]${N}${B} $*${N}"; }
info()  { echo "    $*"; }
warn()  { echo "    ${A}!${N} $*"; }
die()   { echo "${R}Erreur :${N} $*" >&2; exit 1; }
run()   { if [ "$DRY_RUN" = 1 ]; then echo "    (simulation) $*"; else "$@"; fi; }
confirm() {   # confirm "question" : oui si --reboot/--reset déjà explicites ou TTY
    [ -r /dev/tty ] || return 1
    local answer; read -r -p "    $1 [o/N] " answer < /dev/tty || return 1
    [[ "$answer" =~ ^[oOyY] ]]
}

# --- options -------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --user) TARGET_USER="$2"; shift 2 ;;
        --dir) INSTALL_DIR="$2"; shift 2 ;;
        --reset) RESET=1; shift ;;
        --reboot) REBOOT=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null \
                   | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "option inconnue : $1 (voir --help)" ;;
    esac
done

echo "${B}darksign${N} — installation du lecteur"

# --- 1. vérifications --------------------------------------------------------------
title "Vérifications"
[ "$(id -u)" = 0 ] || die "lancez l'installateur avec sudo."
TARGET_USER="${TARGET_USER:-pi}"
[ "$TARGET_USER" != root ] || die "le lecteur ne doit pas tourner en root : utilisez --user."
id "$TARGET_USER" >/dev/null 2>&1 || die "l'utilisateur « $TARGET_USER » n'existe pas."
TARGET_GROUP="$(id -gn "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
INSTALL_DIR="${INSTALL_DIR:-$TARGET_HOME/videoplayer}"
info "utilisateur : $TARGET_USER    dossier : $INSTALL_DIR"

model="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
if [[ "$model" != *"Raspberry Pi"* ]]; then
    [ "$FORCE" = 1 ] || die "ce n'est pas un Raspberry Pi (« ${model:-inconnu} ») : --force pour passer outre."
    warn "matériel non reconnu : « ${model:-inconnu} »"
else
    info "matériel : $model"
    [[ "$model" == *"Raspberry Pi 3"* ]] || warn "testé sur Raspberry Pi 3 ; sur ce modèle, le décodage matériel et l'affichage peuvent différer."
fi

. /etc/os-release
if [ "${VERSION_ID:-}" != 13 ]; then
    # mpv ≥ 0.38 (syntaxe loadfile) et libgpiod 2 sont requis : Debian 13 « Trixie »
    [ "$FORCE" = 1 ] || die "Raspberry Pi OS « Trixie » (Debian 13) requis, trouvé : ${PRETTY_NAME:-inconnu}."
    warn "version non prise en charge : ${PRETTY_NAME:-inconnue}"
else
    info "système : $PRETTY_NAME"
fi

BOOT_DIR=/boot/firmware
[ -f "$BOOT_DIR/cmdline.txt" ] || BOOT_DIR=/boot
[ -f "$BOOT_DIR/cmdline.txt" ] || die "cmdline.txt introuvable (ni /boot/firmware, ni /boot)."

# --- 2. paquets ------------------------------------------------------------------
title "Paquets"
export DEBIAN_FRONTEND=noninteractive
run apt-get update -qq
run apt-get install -y -qq --no-install-recommends "${PACKAGES[@]}"
info "installés : ${PACKAGES[*]}"

# --- 3. code ---------------------------------------------------------------------
title "Code du lecteur"
SRC_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    candidate="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [ -f "$candidate/player.py" ] && SRC_DIR="$candidate"
fi
if [ -n "$SRC_DIR" ] && [ "$SRC_DIR" = "$(realpath -m "$INSTALL_DIR")" ]; then
    info "installation sur place : $INSTALL_DIR"
elif [ -n "$SRC_DIR" ]; then
    # copie du dépôt local (modifications comprises), jamais les médias ni l'état
    info "copie de $SRC_DIR vers $INSTALL_DIR"
    run mkdir -p "$INSTALL_DIR"
    run rsync -a --exclude media/ --exclude data/ --exclude __pycache__/ \
        "$SRC_DIR/" "$INSTALL_DIR/"
elif [ -d "$INSTALL_DIR/.git" ]; then
    info "mise à jour du dépôt existant"
    run sudo -u "$TARGET_USER" git -C "$INSTALL_DIR" pull --ff-only
elif [ -e "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
    die "$INSTALL_DIR existe déjà et n'est pas un dépôt darksign : choisissez --dir."
else
    info "clonage de $REPO_URL"
    run mkdir -p "$INSTALL_DIR"      # dossier préparé : il peut être hors du home
    run chown "$TARGET_USER:$TARGET_GROUP" "$INSTALL_DIR"
    run sudo -u "$TARGET_USER" git clone -q --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

# --- 4. état « premier démarrage » ------------------------------------------------
title "Médias et configuration"
if [ "$RESET" = 1 ] && { [ -d "$INSTALL_DIR/media" ] || [ -d "$INSTALL_DIR/data" ]; }; then
    count=$(find "$INSTALL_DIR/media" -maxdepth 1 -type f 2>/dev/null | wc -l)
    warn "--reset : $count fichier(s) de médias et la configuration vont être effacés."
    if [ "$DRY_RUN" = 1 ] || [ ! -r /dev/tty ] || confirm "Confirmer l'effacement ?"; then
        run systemctl stop videoplayer videoplayer-web 2>/dev/null || true
        run rm -rf "$INSTALL_DIR/media" "$INSTALL_DIR/data"
    else
        die "effacement annulé."
    fi
fi
if [ -d "$INSTALL_DIR/media" ] && [ -n "$(ls -A "$INSTALL_DIR/media" 2>/dev/null)" ]; then
    info "installation existante : médias et configuration conservés (--reset pour repartir de zéro)"
else
    info "aucun média, aucun sous-titre, configuration vierge : écran d'accueil au démarrage"
fi
run mkdir -p "$INSTALL_DIR/media" "$INSTALL_DIR/data"
run chown -R "$TARGET_USER:$TARGET_GROUP" "$INSTALL_DIR"

# --- 5. droits -------------------------------------------------------------------
title "Droits de l'utilisateur"
for group in video render audio gpio; do
    if getent group "$group" >/dev/null; then
        run usermod -aG "$group" "$TARGET_USER"
    else
        warn "groupe « $group » absent"
    fi
done
info "$TARGET_USER : accès à l'écran (video, render), au son (audio) et aux GPIO (gpio)"

# --- 6. services -----------------------------------------------------------------
title "Services"
for unit in videoplayer videoplayer-web; do
    template="$INSTALL_DIR/systemd/$unit.service.in"
    [ -f "$template" ] || [ "$DRY_RUN" = 1 ] || die "modèle introuvable : $template"
    if [ "$DRY_RUN" = 1 ]; then
        echo "    (simulation) $template -> /etc/systemd/system/$unit.service"
    else
        sed -e "s#@USER@#$TARGET_USER#g" -e "s#@GROUP@#$TARGET_GROUP#g" \
            -e "s#@DIR@#$INSTALL_DIR#g" "$template" > "/etc/systemd/system/$unit.service"
    fi
done
run systemctl daemon-reload
run systemctl enable -q videoplayer videoplayer-web
info "videoplayer (lecteur, démarré dès que l'écran est prêt) et videoplayer-web (port 8080)"
for unit in videoplayer videoplayer-web; do   # mise à jour : nouveau code chargé
    if systemctl is-active -q "$unit"; then
        run systemctl restart "$unit"
        info "$unit relancé"
    fi
done

# --- 7. démarrage silencieux --------------------------------------------------------
title "Démarrage silencieux"
backup() {   # garde une seule sauvegarde : l'original d'avant darksign
    [ -f "$1$BACKUP_SUFFIX" ] || run cp -p "$1" "$1$BACKUP_SUFFIX"
}
cmdline="$BOOT_DIR/cmdline.txt"
backup "$cmdline"
current="$(tr -d '\n' < "$cmdline")"
new="$(echo "$current" | sed -E 's/(^| )console=tty1( |$)/\1console=tty3\2/')"
for param in "${BOOT_PARAMS[@]}"; do
    [[ " $new " == *" $param "* ]] || new="$new $param"
done
if [ "$new" != "$current" ]; then
    [[ "$new" == *"root="* ]] || die "cmdline.txt inattendu (pas de root=), rien n'est modifié."
    if [ "$DRY_RUN" = 1 ]; then echo "    (simulation) cmdline.txt : $new"
    else echo "$new" > "$cmdline"; fi
    info "noyau : messages sur tty3, logos et curseur masqués"
else
    info "cmdline.txt déjà configuré"
fi

config="$BOOT_DIR/config.txt"
backup "$config"
if ! grep -qE '^\s*dtoverlay=vc4-kms-v3d' "$config"; then
    warn "pilote graphique KMS absent de config.txt : ajout de dtoverlay=vc4-kms-v3d"
    [ "$DRY_RUN" = 1 ] || printf '\n[all]\ndtoverlay=vc4-kms-v3d\n' >> "$config"
fi
if ! grep -qE '^\s*disable_splash=1' "$config"; then
    [ "$DRY_RUN" = 1 ] || printf '\n[all]\n# darksign : pas d'"'"'écran arc-en-ciel au démarrage\ndisable_splash=1\n' >> "$config"
    info "écran arc-en-ciel du firmware désactivé"
else
    info "config.txt déjà configuré"
fi

run systemctl disable -q getty@tty1.service 2>/dev/null || true
info "invite de connexion à l'écran désactivée (SSH reste disponible)"
if [ "$(systemctl get-default)" = graphical.target ]; then
    warn "image avec bureau : le bureau est désactivé, il occuperait l'écran"
    run systemctl set-default multi-user.target
    run systemctl disable -q display-manager.service 2>/dev/null || true
fi

# journal conservé entre les redémarrages : diagnostic après un incident
if ! grep -rqs '^Storage=persistent' /etc/systemd/journald.conf.d/; then
    run mkdir -p /etc/systemd/journald.conf.d /var/log/journal
    if [ "$DRY_RUN" = 0 ]; then
        printf '[Journal]\nStorage=persistent\nSystemMaxUse=100M\n' \
            > /etc/systemd/journald.conf.d/darksign.conf
    fi
    run systemctl restart systemd-journald
    info "journal système conservé entre les redémarrages (100 Mo max)"
fi

# --- fin -------------------------------------------------------------------------
echo
echo "${G}${B}Installation terminée.${N}"
host="$(hostname)"
addr="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "    Au démarrage : écran noir, animation darksign, puis l'écran d'accueil."
echo "    Administration : http://${addr:-<adresse-du-pi>}:8080  ou  http://$host.local:8080"
echo "    Sauvegardes de la configuration de démarrage : $BOOT_DIR/*$BACKUP_SUFFIX"
if [ "$DRY_RUN" = 1 ]; then
    echo "    (simulation : rien n'a été modifié)"
elif [ "$REBOOT" = 1 ] || confirm "Redémarrer maintenant pour lancer le lecteur ?"; then
    echo "    Redémarrage…"
    systemctl reboot
else
    echo "    Redémarrez pour terminer : sudo reboot"
fi
