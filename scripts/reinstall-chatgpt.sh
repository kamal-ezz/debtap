#!/usr/bin/env bash
# Run as your desktop user, not with sudo. Pacman keeps its normal confirmation.
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
build_only=no
if [[ ${1:-} == --build-only ]]; then build_only=yes; shift; fi
if (( $# > 1 )) || [[ ${1:-} == --help ]]; then
    printf 'Usage: %s [--build-only] [path/to/chatgpt_amd64.deb]\n' "$0"
    exit 0
fi
deb=${1:-$HOME/Downloads/chatgpt_amd64.deb}
[[ $EUID != 0 ]] || { printf 'Run this script as your desktop user, without sudo.\n' >&2; exit 1; }
[[ -f $deb ]] || { printf 'Package not found: %s\n' "$deb" >&2; exit 1; }
for tool in python3 bash bsdtar mktemp; do
    command -v "$tool" >/dev/null || { printf 'Missing command: %s\n' "$tool" >&2; exit 1; }
done
if [[ $build_only == no ]]; then
    for tool in sudo pacman xdg-settings xdg-mime pgrep; do
        command -v "$tool" >/dev/null || { printf 'Missing command: %s\n' "$tool" >&2; exit 1; }
    done
    [[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]] || { printf 'Run from a terminal in your desktop session.\n' >&2; exit 1; }
    firefox_desktop=${FIREFOX_DESKTOP:-firefox.desktop}
    [[ $firefox_desktop != */* && $firefox_desktop == *.desktop ]] || exit 1
    [[ -f /usr/share/applications/$firefox_desktop || -f ${XDG_DATA_HOME:-$HOME/.local/share}/applications/$firefox_desktop ]] || {
        printf 'Firefox desktop entry not found: %s. Install Firefox first.\n' "$firefox_desktop" >&2; exit 1;
    }
fi

# Keep logs, the package, PKGBUILD, and cleaned install script for inspection.
work=$(mktemp -d "${TMPDIR:-/tmp}/chatgpt-rebuild.XXXXXXXX")
printf 'Build and logs: %s\n' "$work"
printf '[1/5] Using the fixed debtap checkout: %s\n' "$repo"
bash -n "$repo/debtap"
# A short path for the editor avoids word splitting in debtap's editor prompt.
editor_dir=$(mktemp -d /tmp/chatgpt-editor.XXXXXXXX)
trap 'rm -rf -- "$editor_dir"' EXIT
cat > "$editor_dir/editor" <<'EDITOR'
#!/usr/bin/env bash
exec python3 "$CHATGPT_ADAPTER" "$@"
EDITOR
chmod +x "$editor_dir/editor"
export CHATGPT_ADAPTER="$repo/scripts/adapt-chatgpt-install.py"
printf '[2/5] Rebuilding ChatGPT; [3/5] adapting its install script...\n'
# -q skips metadata questions; input 3 selects the scripted metadata editor.
printf '3' | EDITOR="$editor_dir/editor" bash "$repo/debtap" -q -p -o "$work" "$deb" 2>&1 | tee "$work/build.log"
shopt -s nullglob
packages=("$work"/chatgpt-*.pkg.tar.zst)
(( ${#packages[@]} == 1 )) || { printf 'Expected one ChatGPT package; see build.log.\n' >&2; exit 1; }
package=${packages[0]}
bsdtar -xOf "$package" .INSTALL > "$work/checked.INSTALL"
python3 "$CHATGPT_ADAPTER" --check "$work/checked.INSTALL"
printf 'Cleaned install script: %s\nPackage: %s\n' "$work/checked.INSTALL" "$package"
if [[ $build_only == yes ]]; then
    printf 'Build-only complete. Installation and desktop settings were not changed.\n'
    exit 0
fi

printf '[4/5] Reinstalling the rebuilt package...\n'
sudo pacman -U "$package" 2>&1 | tee "$work/install.log"
if grep -Eq 'error: command failed to execute correctly|syntax error|post_install: command not found' "$work/install.log"; then
    printf 'An install hook failed. See %s/install.log before continuing.\n' "$work" >&2
    exit 1
fi
[[ $(pacman -Q chatgpt) == "$(pacman -Qp "$package")" ]] || { printf 'Installed package version does not match the build.\n' >&2; exit 1; }

printf '[5/5] Restoring Firefox and launching ChatGPT...\n'
# Preserve existing per-user association files before changing HTTP/HTTPS.
config=${XDG_CONFIG_HOME:-$HOME/.config}
data=${XDG_DATA_HOME:-$HOME/.local/share}
mkdir -p "$work/desktop-backup"
for file in "$config"/mimeapps.list "$config"/*-mimeapps.list "$data"/applications/mimeapps.list; do
    [[ -f $file ]] || continue
    cp -- "$file" "$work/desktop-backup/$(printf '%s' "$file" | tr / _)"
done
xdg-settings set default-web-browser "$firefox_desktop"
xdg-mime default "$firefox_desktop" x-scheme-handler/http
xdg-mime default "$firefox_desktop" x-scheme-handler/https
[[ $(xdg-settings get default-web-browser) == "$firefox_desktop" ]] || { printf 'Desktop did not apply the Firefox preference.\n' >&2; exit 1; }
for scheme in http https; do
    [[ $(xdg-mime query default "x-scheme-handler/$scheme") == "$firefox_desktop" ]] || exit 1
done
command -v chatgpt >/dev/null || { printf 'ChatGPT launcher was not installed.\n' >&2; exit 1; }
nohup chatgpt > "$work/launch.log" 2>&1 </dev/null &
launch_pid=$!
sleep 5
if kill -0 "$launch_pid" 2>/dev/null || pgrep -u "$(id -u)" -x chatgpt >/dev/null; then
    printf 'ChatGPT is running. Check its window; launch log: %s/launch.log\n' "$work"
else
    printf 'ChatGPT exited. See %s/launch.log for details.\n' "$work" >&2
    exit 1
fi
printf 'Firefox is restored. Package and logs are retained in %s\n' "$work"
