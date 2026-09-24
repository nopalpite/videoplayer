# Lecteur vidéo GPIO

Lecture plein écran sur la sortie HDMI du Pi (mpv, sans bureau) pilotée par
des boutons sur les GPIO, administrée depuis une interface web.

- `player.py` : lecteur (mpv + gpiod), service `videoplayer`
- `web.py` : interface d'administration sur le port 8080, service `videoplayer-web`
- `common.py` : configuration, médias, GPIO disponibles, dialogue avec le lecteur
- `transcode.py` : file de conversion des vidéos importées
- `media/` : vidéos et images envoyées depuis l'interface
- `data/config.json` : configuration (écrite par l'interface)

Les deux processus dialoguent via le socket unix `data/player.sock`
(commandes `status`, `reload`, `trigger`).

## Installation

    sudo apt install mpv python3-mpv python3-flask python3-libgpiod python3-pil ffmpeg
    sudo cp systemd/*.service /etc/systemd/system/
    sudo systemctl enable --now videoplayer videoplayer-web

Testé sur Raspberry Pi 3 (Raspberry Pi OS Trixie Lite, sans bureau, noyau KMS).
L'utilisateur `pi` doit être dans les groupes `video`, `render`, `audio` et `gpio`.
Les dossiers `media/` et `data/` sont créés au premier lancement.

Pour tester sans bouton câblé : [gpio-web](https://github.com/nopalpite/gpio-web).

## Modes

- **Boucle simple** : un média (vidéo ou image) en boucle, avec ou sans son.
- **Interactif** : une accroche (vidéo ou image) tourne en boucle ; un appui
  sur un bouton lance la vidéo associée, puis retour à l'accroche à la fin.
  Option : un appui peut ou non interrompre la vidéo en cours.

## Matériel

Par défaut, bouton entre la GPIO et GND (pull-up interne activé par le
lecteur). Le câblage vers 3V3 (pull-down) est sélectionnable dans l'interface.
Les GPIO occupées par une fonction (UART, I2C, SPI...) ne sont pas proposées.

## Conversion automatique des vidéos

Chaque vidéo importée est analysée puis, si besoin, convertie en H.264
(décodé matériellement par le Pi 3) dans un MP4, en conservant la résolution
et la cadence d'origine. Seule exception : au-delà de 1080p, l'image est
réduite à 1080p, limite du décodeur. Une vidéo déjà adaptée est gardée telle
quelle, sans réencodage.

- Encodeur matériel du Pi (`h264_v4l2m2m`), ~10 Mb/s en 1080p25 ;
  x264 en secours si l'encodeur matériel refuse la source.
- Compter ~2,5 s de conversion par seconde de vidéo 1080p (plus pour du
  HEVC ou du ProRes, dont le décodage est logiciel).
- La conversion tourne en priorité basse : la lecture en cours n'est pas
  perturbée. Une seule conversion à la fois, les autres attendent.
- Le fichier d'origine est supprimé une fois converti. Il est gardé dans
  `media/.incoming/` tant que la conversion n'est pas terminée : après un
  redémarrage, elle reprend automatiquement.

## Sous-titres

Fichiers .srt (ainsi que .vtt et .ass) envoyés depuis la médiathèque, puis
associés à une vidéo (un fichier par vidéo). Un .srt portant le même nom
qu'une vidéo lui est proposé automatiquement. Les fichiers sont convertis en
UTF-8 à l'envoi (les .srt Windows en cp1252 sont gérés). Taille et fond sombre
se règlent dans l'interface ; police DejaVu Sans.

## Commandes utiles

    sudo systemctl restart videoplayer videoplayer-web
    journalctl -u videoplayer -f
