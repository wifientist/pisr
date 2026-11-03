#!/usr/bin/env bash
# PRE-RENAME SHIM — the tool was PISR before 2026-11 and this file's name is
# still referenced from outside the repository: by the systemd unit installed
# as ~/.config/systemd/user/pisr-update.service (ExecStart=%h/app/deploy/
# pisr-update.sh), and by the ~/pisr-*-status symlinks deploy/README.md had
# operators create. A deploy that deleted it would leave the timer firing at a
# missing file, which is the auto-deploy silently stopping, from then on.
#
# Delete once the box has been cut over; see deploy/README.md, "Renaming a box
# from PISR".
exec "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/prism-deploy-status.sh" "$@"
