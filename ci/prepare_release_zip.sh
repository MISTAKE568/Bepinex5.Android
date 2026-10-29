#!/usr/bin/env bash
# Run from the repository root. Only prepare/check the one attachment; no upload.
set -euo pipefail
pack_name="${1:?usage: bash ci/prepare_release_zip.sh PACK_NAME}"
case "$pack_name" in
  bep5-android-arm32-assets-?*) ;;
  *) printf 'invalid pack name\n' >&2; exit 1 ;;
esac
case "$pack_name" in
  *[!A-Za-z0-9._-]*) printf 'unsafe pack name\n' >&2; exit 1 ;;
esac
python assets.py verify "dist/$pack_name"
(
  cd "dist/$pack_name"
  sha256sum --check SHA256SUMS
  # Print complete digests as well as the per-file verification verdicts.
  while IFS= read -r line; do printf '%s\n' "$line"; done < SHA256SUMS
)
(
  cd dist
  # zip updates an existing archive; refuse that to avoid stale members.
  test ! -e "$pack_name.zip"
  zip -r "$pack_name.zip" "$pack_name"
  zip -T "$pack_name.zip"
  sha256sum "$pack_name.zip"
)
