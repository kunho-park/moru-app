#!/usr/bin/env bash
# Sync desktop/engine versions and cut a release tag.
#
#   scripts/bump-version.sh 0.2.0            # bump + commit
#   scripts/bump-version.sh 0.2.0 --tag      # bump + commit + tag v0.2.0
#
# Release then = `git push origin main v0.2.0` — the tag triggers
# .github/workflows/release.yml (Windows NSIS + macOS dmg -> GitHub Release).
set -euo pipefail

VERSION="${1:?usage: bump-version.sh <semver> [--tag]}"
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "error: '$VERSION' is not X.Y.Z" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

node -e "
  const fs = require('fs');
  const path = 'desktop/package.json';
  const pkg = JSON.parse(fs.readFileSync(path, 'utf8'));
  pkg.version = '$VERSION';
  fs.writeFileSync(path, JSON.stringify(pkg, null, 2) + '\n');
"
sed -i.bak "s/^version = \".*\"/version = \"$VERSION\"/" engine/pyproject.toml
sed -i.bak "s/^__version__ = \".*\"/__version__ = \"$VERSION\"/" engine/src/moru_engine/__init__.py
rm -f engine/pyproject.toml.bak engine/src/moru_engine/__init__.py.bak

# uv.lock records the project version; keep it in sync.
(cd engine && uv lock --offline >/dev/null 2>&1 || uv lock >/dev/null)

git add desktop/package.json engine/pyproject.toml engine/src/moru_engine/__init__.py engine/uv.lock
git commit -m "release: v$VERSION"

if [[ "${2:-}" == "--tag" ]]; then
  git tag "v$VERSION"
  echo "tagged v$VERSION — push with: git push origin HEAD v$VERSION"
else
  echo "bumped to $VERSION — tag with: git tag v$VERSION"
fi
