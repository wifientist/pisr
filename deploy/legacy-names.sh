# Sourced, never run. Lets the deploy scripts work on a box set up before the
# tool was renamed from PISR to PRISM (2026-11).
#
# Nothing on the box is renamed by a commit. ~/.config/pisr-update.env, the
# production env file, the installed systemd units and the container name all
# predate the rename and live outside the repository, so a script that only
# understood the new names would read every setting as unset — and this one
# derives the health URL, the env file and the container to inspect from those
# settings. Getting them wrong does not fail loudly: it rolls back a good
# commit and blames it.
#
# Each helper prefers the PRISM_ spelling and falls back to PISR_. Safe to
# delete once no box still carries a PISR_ name anywhere.

# PRISM_<X> := PISR_<X> for every PISR_ variable in the environment that has
# no PRISM_ counterpart. An explicitly set PRISM_ value always wins.
for _legacy_name in $(compgen -e); do
  case "$_legacy_name" in
    PISR_*)
      _legacy_new="PRISM_${_legacy_name#PISR_}"
      [ -n "${!_legacy_new+x}" ] || export "$_legacy_new=${!_legacy_name}"
      ;;
  esac
done
unset _legacy_name _legacy_new

# The env file a box set up before the rename uses, when none is named.
# `$1` is the new default; the old one is returned only if the new is absent.
legacy_env_file() {
  if [ -e "$1" ]; then printf '%s' "$1"; return; fi
  local old="${1//prism/pisr}"
  if [ -e "$old" ]; then printf '%s' "$old"; else printf '%s' "$1"; fi
}

# The name a variable had before the rename: PRISM_PORT -> PISR_PORT.
legacy_name() { printf 'PISR_%s' "${1#PRISM_}"; }
