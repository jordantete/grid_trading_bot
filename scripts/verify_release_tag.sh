#!/usr/bin/env bash
# Usage: scripts/verify_release_tag.sh <tag> <sha>, run from the repository root.
set -euo pipefail

tag="${1:?usage: verify_release_tag.sh <tag> <sha>}"
sha="${2:?usage: verify_release_tag.sh <tag> <sha>}"

version="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n 1)"
if [ -z "$version" ]; then
    echo "no version found in pyproject.toml" >&2
    exit 1
fi

if [ "$tag" != "v$version" ]; then
    echo "tag $tag does not match pyproject.toml version $version" >&2
    exit 1
fi

if ! git merge-base --is-ancestor "$sha" origin/master; then
    echo "commit $sha is not on origin/master" >&2
    exit 1
fi

echo "tag $tag matches version $version on origin/master"
