# shellcheck shell=bash disable=SC2034
# Settings for the example scripts. Copy to ~/.config/novatek-mapillary/config.sh (or point
# NOVATEK_MAPILLARY_CONFIG at your copy) and fill in. Your copy holds where you live and stay:
# keep it out of any public repository.

# Where a trip lives: raw/ (the card copies), desc/ (descriptions), desc/upload/ (pruned).
STORE=/run/media/$USER/MyDrive/dashcam/my-trip
# Filesystem label of that drive, for remounting it read-only if it drops off USB mid-upload.
DRIVE_LABEL=MyDrive
# Your Mapillary username (run `mapillary_tools authenticate` once first).
MAPILLARY_USER=your-mapillary-name
# Camera clock UTC offset, or auto.
CAMERA_TZ=auto

# Privacy zones passed to `novatek-mapillary prune`. Home and every overnight stop:
PRUNE_ARGS=(
  # --exclude-near=-33.0000,147.0000,2000    # home
  # --exclude-near=-32.9000,147.1800,2000    # night 1
  # --cut-after-entering=-33.0500,147.1800,1000   # joining the motorway into the destination city
)

# Optional: a second copy of the descriptions (they're small; the video isn't).
BACKUP_DIR=
