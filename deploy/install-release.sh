#!/bin/sh
# Install one verified quoth release into its own immutable, versioned venv.
#
#   deploy/install-release.sh --assets DIR --sums FILE [--prefix DIR] [--python VERSION] [--no-activate]
#   deploy/install-release.sh --activate X.Y.Z [--prefix DIR]      # rollback / roll forward only
#
# --prefix is the directory that holds every installed version and the
# quoth-current link; it defaults to $QUOTH_PREFIX and one of the two is required.
#
# DIR holds the three release assets exactly as published (wheel, sdist,
# quoth-<ver>-requirements.txt). FILE is a sha256sum-format list of the
# expected digests, taken from the accepted release record, never from DIR.
# The venv is built under <prefix>/quoth-<ver>.partial and renamed to
# <prefix>/quoth-<ver> only after every check passes. An existing target
# is never touched.
#
# Fixed path: every caller (s6 services, cron scripts, the Hermes MCP entry, the
# control container) uses <prefix>/quoth-current/bin/..., a relative symlink to the active
# version directory. By default a successful install activates the new version by
# replacing that symlink atomically (rename over it). --no-activate installs only.
# --activate X.Y.Z repoints the symlink at an already installed version and does
# nothing else: rollback is `--activate <previous version>`, then restart the
# sidecar and control services so they exec the new target.
set -eu

prefix=${QUOTH_PREFIX:-}
python_version=3.13
assets=
sums=
activate=1
activate_only=

while [ $# -gt 0 ]; do
  case "$1" in
    --assets) assets=$2; shift 2 ;;
    --sums) sums=$2; shift 2 ;;
    --prefix) prefix=$2; shift 2 ;;
    --python) python_version=$2; shift 2 ;;
    --no-activate) activate=0; shift ;;
    --activate) activate_only=$2; shift 2 ;;
    *) echo "install-release: unknown argument $1" >&2; exit 2 ;;
  esac
done
fail() { echo "install-release: $*" >&2; exit 1; }
[ -n "$prefix" ] || { echo "install-release: --prefix (or QUOTH_PREFIX) is required" >&2; exit 2; }

# Atomically point <prefix>/quoth-current at quoth-$1 (relative link, so it
# resolves the same inside and outside a container that mounts <prefix> elsewhere).
activate_version() {
  dir=$prefix/quoth-$1
  case "$1" in *[!0-9A-Za-z.+-]*|'') fail "invalid version '$1'" ;; esac
  [ -d "$dir" ] && [ ! -L "$dir" ] || fail "$dir is not an installed version"
  [ -x "$dir/bin/quoth" ] || fail "$dir/bin/quoth missing"
  "$dir/bin/python" -c 'import quoth' || fail "$dir does not run on this interpreter"
  link=$prefix/quoth-current
  if [ -e "$link" ] && [ ! -L "$link" ]; then fail "$link exists and is not a symlink"; fi
  tmp=$prefix/.quoth-current.$$
  rm -f "$tmp"
  ln -s "quoth-$1" "$tmp"
  # rename(2) over the old link: readers see the old or the new target, never neither.
  "$dir/bin/python" -I -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' "$tmp" "$link" \
    || { rm -f "$tmp"; fail "could not replace $link"; }
  echo "active: $link -> quoth-$1"
}

if [ -n "$activate_only" ]; then
  [ -z "$assets" ] && [ -z "$sums" ] || { echo "install-release: --activate takes no --assets/--sums" >&2; exit 2; }
  activate_version "$activate_only"
  exit 0
fi

[ -n "$assets" ] && [ -n "$sums" ] || { echo "install-release: --assets and --sums are required" >&2; exit 2; }
command -v uv >/dev/null || { echo "install-release: uv is required" >&2; exit 2; }

set -- "$assets"/quoth-*-py3-none-any.whl
[ $# -eq 1 ] && [ -f "$1" ] || fail "expected exactly one wheel in $assets"
wheel=$1
version=${wheel##*/quoth-}
version=${version%-py3-none-any.whl}
reqs=$assets/quoth-$version-requirements.txt
[ -f "$reqs" ] || fail "missing $reqs"

# Every installed file must match the externally supplied digest list.
for file in "$wheel" "$reqs"; do
  name=${file##*/}
  expected=$(awk -v n="$name" '{f=$2; sub(/^\*/, "", f); sub(/^.*\//, "", f); if (f == n) print $1}' "$sums")
  [ "$(printf '%s\n' "$expected" | grep -c .)" -eq 1 ] || fail "$name must appear exactly once in $sums"
  if command -v sha256sum >/dev/null; then
    actual=$(sha256sum "$file" | awk '{print $1}')
  else
    actual=$(shasum -a 256 "$file" | awk '{print $1}')
  fi
  [ "$actual" = "$expected" ] || fail "sha256 mismatch for $name"
done

target=$prefix/quoth-$version
partial=$target.partial
[ ! -e "$target" ] || fail "$target already exists; releases are immutable, choose a new version"
mkdir -p "$prefix"
rm -rf "$partial"

# Relocatable: console scripts keep working after the partial -> final rename.
uv venv --quiet --relocatable --python "$python_version" "$partial"
uv pip sync --quiet --require-hashes --python "$partial/bin/python" "$reqs"
uv pip install --quiet --no-deps --python "$partial/bin/python" "$wheel"

installed=$("$partial/bin/python" -c 'import quoth; print(quoth.__version__)')
[ "$installed" = "$version" ] || fail "installed version $installed != release $version"
"$partial/bin/quoth" --help >/dev/null
"$partial/bin/python" -c 'import onnxruntime, tokenizers' || fail "dense extra not importable"

mv "$partial" "$target"
"$target/bin/quoth" --help >/dev/null
chmod -R a-w "$target"

echo "installed quoth $version at $target"
if [ "$activate" -eq 1 ]; then
  activate_version "$version"
  echo "next: restart the sidecar and control services; run $prefix/quoth-current/bin/quoth health"
else
  echo "not activated; later: $0 --activate $version --prefix $prefix"
fi
