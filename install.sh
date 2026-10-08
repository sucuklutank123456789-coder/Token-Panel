#!/usr/bin/env bash
# Token Panel installer for most Linux distributions.
#
#   ./install.sh               install for the current user
#   ./install.sh --autostart   also start it on login
#   ./install.sh --pip         skip distro packages, get PySide6 from PyPI
#   ./install.sh --yes         don't ask before installing distro packages
#   ./install.sh --uninstall   remove it again
#
# Installs into ~/.local (no root needed for the app itself). PySide6 comes from the
# distribution when available, otherwise from PyPI into a private virtual environment.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
APP_DIR="$DATA_HOME/tokenpanel"
VENV="$APP_DIR/venv"
BIN_DIR="$HOME/.local/bin"
DESKTOP_FILE="$DATA_HOME/applications/tokenpanel.desktop"
ICON_FILE="$DATA_HOME/icons/hicolor/scalable/apps/tokenpanel.svg"
AUTOSTART_FILE="$CONFIG_HOME/autostart/tokenpanel.desktop"
OS_RELEASE="${OS_RELEASE:-/etc/os-release}"

AUTOSTART=0
USE_PIP=0
ASSUME_YES=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --autostart) AUTOSTART=1 ;;
    --pip) USE_PIP=1 ;;
    -y | --yes) ASSUME_YES=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h | --help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

info() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$UNINSTALL" = 1 ]; then
  info "Removing Token Panel"
  pkill -f "$VENV/bin/tokenpanel" 2>/dev/null || true
  rm -rf "$APP_DIR"
  rm -f "$BIN_DIR/tokenpanel" "$DESKTOP_FILE" "$ICON_FILE" "$AUTOSTART_FILE"
  info "Done. Settings in ~/.config/tokenpanel were kept."
  exit 0
fi

# --- Distribution ------------------------------------------------------------------
ID="" ID_LIKE=""
if [ -r "$OS_RELEASE" ]; then
  # shellcheck disable=SC1090
  . "$OS_RELEASE"
fi
family=""
for id in $ID $ID_LIKE; do
  case "$id" in
    arch | manjaro | endeavouros | cachyos | garuda | artix) family=arch ;;
    fedora | rhel | centos | rocky | almalinux | nobara | ultramarine) family=fedora ;;
    opensuse* | suse | sles) family=suse ;;
    debian | ubuntu | linuxmint | pop | elementary | zorin | kali | raspbian | neon) family=debian ;;
    alpine) family=alpine ;;
    void) family=void ;;
  esac
  [ -n "$family" ] && break
done
info "Distribution: ${PRETTY_NAME:-${ID:-unknown}} (${family:-unknown family})"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null && SUDO="sudo" || SUDO=""
fi

APT_UPDATED=0
pkg_install() {
  # Installs distribution packages; returns non-zero if that is not possible.
  [ "$#" -gt 0 ] || return 0
  if [ "$(id -u)" -ne 0 ] && [ -z "$SUDO" ]; then
    warn "sudo is not available; can't install: $*"
    return 1
  fi
  local yes=()
  info "Installing system packages: $*"
  case "$family" in
    arch)
      [ "$ASSUME_YES" = 1 ] && yes=(--noconfirm)
      $SUDO pacman -S --needed ${yes[@]+"${yes[@]}"} "$@" ;;
    fedora)
      [ "$ASSUME_YES" = 1 ] && yes=(-y)
      $SUDO dnf install ${yes[@]+"${yes[@]}"} "$@" ;;
    suse)
      [ "$ASSUME_YES" = 1 ] && yes=(--non-interactive)
      $SUDO zypper ${yes[@]+"${yes[@]}"} install "$@" ;;
    debian)
      [ "$ASSUME_YES" = 1 ] && yes=(-y)
      apt_update
      $SUDO apt-get install ${yes[@]+"${yes[@]}"} "$@" ;;
    alpine)
      $SUDO apk add "$@" ;;
    void)
      [ "$ASSUME_YES" = 1 ] && yes=(-y)
      $SUDO xbps-install ${yes[@]+"${yes[@]}"} "$@" ;;
    *) return 1 ;;
  esac
}

apt_update() {
  if [ "$APT_UPDATED" = 0 ]; then
    APT_UPDATED=1
    $SUDO apt-get update -qq || true
  fi
}
apt_has() {
  apt_update
  [ -n "$(apt-cache policy "$1" 2>/dev/null | sed -n 's/^ *Candidate: //p' | grep -v '(none)')" ]
}

# --- Python ------------------------------------------------------------------------
PY=""
python_ok() {
  # Picks python3, or a newer python3.X where the default one is too old (RHEL 9, Leap 15).
  local cand tmp rc
  for cand in python3 python3.13 python3.12 python3.11 python3.10; do
    command -v "$cand" >/dev/null || continue
    "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null || continue
    # Some distributions ship venv without ensurepip (Debian: python3-venv); try it for real.
    tmp="$(mktemp -d)"
    "$cand" -m venv "$tmp/v" >/dev/null 2>&1 && rc=0 || rc=1
    rm -rf "$tmp"
    if [ "$rc" = 0 ]; then
      PY="$(command -v "$cand")"
      return 0
    fi
  done
  return 1
}
if ! python_ok; then
  case "$family" in
    arch) pkg_install python || true ;;
    fedora) pkg_install python3 || true; python_ok || pkg_install python3.11 || true ;;
    suse) pkg_install python3 || true; python_ok || pkg_install python311 || true ;;
    debian) pkg_install python3 python3-venv || true ;;
    alpine) pkg_install python3 || true ;;
    void) pkg_install python3 || true ;;
  esac
fi
python_ok || die "Python 3.10 or newer with the venv module is required (on Debian/Ubuntu: python3-venv)."
info "Python: $("$PY" --version)"

# --- PySide6 -----------------------------------------------------------------------
pyside_ok() {
  "$1" -c 'import PySide6, PySide6.QtWidgets, sys
v = tuple(int(x) for x in PySide6.__version__.split(".")[:2])
sys.exit(v < (6, 4))' 2>/dev/null
}

if [ "$USE_PIP" = 0 ] && ! pyside_ok "$PY"; then
  case "$family" in
    arch) pkg_install pyside6 || true ;;
    fedora) pkg_install python3-pyside6 || true ;;
    suse) pkg_install python3-pyside6 || true ;;
    alpine) pkg_install py3-pyside6 || true ;;
    debian)
      if apt_has python3-pyside6.qtwidgets; then
        pkg_install python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets || true
      else
        info "This release has no PySide6 package; it will come from PyPI."
      fi ;;
  esac
fi

info "Creating environment in $VENV"
mkdir -p "$APP_DIR"
rm -rf "$VENV"
if [ "$USE_PIP" = 0 ] && pyside_ok "$PY"; then
  "$PY" -m venv --system-site-packages "$VENV"
  info "Using the distribution's PySide6 $("$PY" -c 'import PySide6; print(PySide6.__version__)')"
else
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip

if ! pyside_ok "$VENV/bin/python"; then
  # PyPI wheels need a few X11/EGL libraries that minimal installs may lack.
  case "$family" in
    debian)
      libs=()
      for p in libglib2.0-0t64 libglib2.0-0 libegl1 libgl1 libfontconfig1 libdbus-1-3 libxkbcommon0 \
        libxkbcommon-x11-0 libx11-xcb1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
        libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-xkb1; do
        # libglib was renamed to libglib2.0-0t64 in Ubuntu 24.04 / Debian 13.
        [ "$p" = libglib2.0-0 ] && [ "${libs[0]:-}" = libglib2.0-0t64 ] && continue
        apt_has "$p" && libs+=("$p")
      done
      pkg_install "${libs[@]}" || true ;;
    fedora)
      pkg_install glib2 mesa-libEGL mesa-libGL fontconfig dbus-libs libxkbcommon libxkbcommon-x11 \
        xcb-util-cursor xcb-util-wm xcb-util-image xcb-util-keysyms xcb-util-renderutil || true ;;
    suse)
      pkg_install libglib-2_0-0 libEGL1 libGL1 fontconfig libdbus-1-3 libxkbcommon0 libxkbcommon-x11-0 \
        libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 || true ;;
  esac
  info "Installing PySide6 from PyPI (about 200 MB)"
  "$VENV/bin/python" -m pip install --quiet "PySide6>=6.4" || die "Could not install PySide6 from PyPI."
fi

# Codex compresses logs older than 7 days (.jsonl.zst); Python 3.14+ reads them by itself.
if ! "$VENV/bin/python" -c 'import compression.zstd' 2>/dev/null &&
  ! "$VENV/bin/python" -c 'import zstandard' 2>/dev/null; then
  "$VENV/bin/python" -m pip install --quiet zstandard ||
    warn "Could not install zstandard; Codex logs older than 7 days will be missing."
fi

info "Installing Token Panel"
build="$(mktemp -d)"
trap 'rm -rf "$build"' EXIT
cp -r "$SRC/pyproject.toml" "$SRC/README.md" "$SRC/LICENSE" "$SRC/tokenpanel" "$build/"
"$VENV/bin/python" -m pip install --quiet --no-deps "$build" || die "Installing the app failed."

mkdir -p "$BIN_DIR" "$(dirname "$DESKTOP_FILE")" "$(dirname "$ICON_FILE")"
ln -sf "$VENV/bin/tokenpanel" "$BIN_DIR/tokenpanel"
install -m644 "$SRC/data/tokenpanel.svg" "$ICON_FILE"
sed "s|^Exec=.*|Exec=$BIN_DIR/tokenpanel|" "$SRC/data/tokenpanel.desktop" >"$DESKTOP_FILE"
if [ "$AUTOSTART" = 1 ]; then
  mkdir -p "$(dirname "$AUTOSTART_FILE")"
  cp "$DESKTOP_FILE" "$AUTOSTART_FILE"
  info "Autostart enabled"
fi
command -v update-desktop-database >/dev/null && update-desktop-database -q "$(dirname "$DESKTOP_FILE")" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$DATA_HOME/icons/hicolor" 2>/dev/null || true

# --- Check -------------------------------------------------------------------------
QT_QPA_PLATFORM=offscreen "$VENV/bin/python" -c 'import tokenpanel.ui' ||
  die "Token Panel was installed but Qt failed to load; see the error above."
"$BIN_DIR/tokenpanel" --dump --range today >/dev/null || die "Reading the logs failed."

info "Installed. Start it from the app menu (Token Panel) or run: tokenpanel"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not in PATH; run it as $BIN_DIR/tokenpanel or add that directory to PATH." ;;
esac
case "${XDG_CURRENT_DESKTOP:-}" in
  *GNOME*)
    if [ "${ID:-}" != ubuntu ]; then
      warn "GNOME shows tray icons only with the AppIndicator extension (package: gnome-shell-extension-appindicator)."
    fi ;;
esac
