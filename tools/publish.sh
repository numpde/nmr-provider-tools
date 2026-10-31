#!/usr/bin/env bash
# GitHub-only releases follow pay-to-play-wire 4c92a3b tools/ops/publish.sh.
# Prepare freezes clean source and qualifies exact bytes. Publish consumes that
# reviewable directory, uploads a draft, then verifies downloaded bytes first.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd -- "$root"
[[ $# == 1 && $1 =~ ^(prepare|release)$ ]] || exit 2
action=$1
[[ -v TAG_INPUT_ORIGIN && $TAG_INPUT_ORIGIN == 'command line' && -v TAG_INPUT && $TAG_INPUT =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] \
    || { printf '%s\n' 'Supply TAG=vX.Y.Z on the Make command line.' >&2; exit 2; }
tag=$TAG_INPUT
version=${tag#v}
source "$root/tools/inference_native.sh"
inference_diagnostic=''
inference_retirement_confirmed=true
inference_cancellation_status=0
scratch=''
finish() {
    local status=$? report_status=0 message=$inference_diagnostic
    trap - EXIT
    if [[ -n $scratch ]]; then message+="Release inputs and receipts retained at $scratch."$'\n'; fi
    if "$inference_retirement_confirmed" && [[ -n $message ]]; then
        inference_native 'release reporting' 10 stdio printf '%s' "$message" || report_status=$?
        if [[ $status == 0 ]]; then status=$report_status; fi
    fi
    exit "$status"
}
trap finish EXIT
native() {
    local operation=$1 budget=$2 status=0
    shift 2
    inference_native "$operation" "$budget" stdio "$@" || status=$?
    if ! "$inference_retirement_confirmed"; then
        if [[ $status == 0 ]]; then status=2; fi
        exit "$status"
    fi
    return "$status"
}
nested() {
    local operation=$1 status=0
    shift
    controller_native "$operation" untimed stdio "$@" || status=$?
    if ! "$inference_retirement_confirmed"; then
        if [[ $status == 0 ]]; then status=2; fi
        exit "$status"
    fi
    return "$status"
}
fail() { printf '%s\n' "$1" >&2; exit 2; }
private_directory() {
    [[ $1 == /* && $1 != / && ! $1 =~ [[:cntrl:],] && $(realpath -e -- "$1") == "$1" \
        && -d $1 && ! -L $1 && $(stat -c '%u:%a' -- "$1") == "$(id -u):700" ]] \
        || fail 'Release inputs require a canonical caller-owned mode 0700 directory below root without mount delimiters.'
}
require_clean_source() {
    local checkout_status
    checkout_status=$(git status --porcelain) || fail 'Cannot inspect Git checkout status; no clean-source claim was established.'
    [[ -z $checkout_status ]] || fail 'Commit or remove pending checkout changes before preparing or publishing a release.'
}
require_unchanged_source() {
    local observed
    observed=$(git rev-parse HEAD) || fail 'Cannot inspect the source revision; retain these release inputs.'
    [[ $observed == "$commit" ]] || fail 'The source revision changed during release preparation; retain these inputs.'
    require_clean_source
}
require_clean_source
commit=$(git rev-parse HEAD) || fail 'Cannot inspect the committed source revision.'
shopt -s nullglob
notes=("$root"/notes/*_release_"$tag".md)
[[ ${#notes[@]} == 1 && -f ${notes[0]} && ! -L ${notes[0]} ]] \
    || fail 'Write exactly one authored notes/NNN_release_vX.Y.Z.md for this tag before committing the release source.'
wheel=provider_core-$version-py3-none-any.whl
sdist=provider_core-$version.tar.gz
lock=provider_core-$version-install.lock
native_lock=provider_core-$version-native-install.lock
record=provider_core-$version-release.json
manifest=provider_core-$version-sha256.json
names=("$wheel" "$sdist" "$lock" "$native_lock" "$record" "$manifest")

if [[ $action == prepare ]]; then
    [[ -v OUTPUT_INPUT_ORIGIN && $OUTPUT_INPUT_ORIGIN == 'command line' && -v OUTPUT_INPUT && -n $OUTPUT_INPUT ]] \
        || fail 'Select OUTPUT as an existing empty private directory on the Make command line.'
    private_directory "$OUTPUT_INPUT"
    [[ -z $(find "$OUTPUT_INPUT" -mindepth 1 -maxdepth 1 -print -quit) ]] || fail 'Release preparation OUTPUT must be empty; preserve preceding evidence separately.'
    scratch=$OUTPUT_INPUT
    source "$root/tools/wheelhouse_path.sh"
    select_wheelhouse
    require_wheelhouse_pool
    source "$root/tools/docker_context.sh"
    select_docker_context
    mkdir -m 700 -- "$scratch/artifacts"
    cp -- "${notes[0]}" "$scratch/release-notes.md"
    nested 'release package build' env WHEELHOUSE_INPUT_ORIGIN='command line' WHEELHOUSE_INPUT="$wheelhouse_directory" OUTPUT_INPUT_ORIGIN='command line' OUTPUT_INPUT="$scratch/artifacts" bash "$root/tools/provider_core.sh" build > "$scratch/release-build.log" 2>&1
    [[ -f $scratch/artifacts/$wheel && -f $scratch/artifacts/$sdist ]] || fail 'TAG must match the built package version.'
    nested 'exact release artifact qualification' env WHEELHOUSE_INPUT_ORIGIN='command line' WHEELHOUSE_INPUT="$wheelhouse_directory" ARTIFACTS_INPUT_ORIGIN='command line' ARTIFACTS_INPUT="$scratch/artifacts" bash "$root/tools/provider_core.sh" package > "$scratch/release-test.log" 2>&1
    require_unchanged_source
    printf 'Prepared exact artifacts, logs and authored release notes at %s. Review them, then use publish/release TAG=%s ARTIFACTS=%q.\n' "$scratch" "$tag" "$scratch"
    exit
fi

[[ -v ARTIFACTS_INPUT_ORIGIN && $ARTIFACTS_INPUT_ORIGIN == 'command line' && -v ARTIFACTS_INPUT && -n $ARTIFACTS_INPUT ]] \
    || fail 'Select ARTIFACTS as the reviewed publish/prepare output directory on the Make command line.'
private_directory "$ARTIFACTS_INPUT"
publication_inputs=$ARTIFACTS_INPUT
umask 077
scratch=$(mktemp -d -t nmr-provider-publication.XXXXXXXX)
mkdir -m 700 -- "$scratch/assets"
for name in "${names[@]}"; do
    native 'capture qualified artifact' 30 cp --no-dereference -- "$publication_inputs/artifacts/$name" "$scratch/assets/$name"
    [[ -f $scratch/assets/$name && ! -L $scratch/assets/$name ]] || fail 'Every release artifact must be a regular file without a link.'
done
for name in release-build.log release-test.log release-notes.md; do
    native 'capture release evidence' 30 cp --no-dereference -- "$publication_inputs/$name" "$scratch/$name"
    [[ -f $scratch/$name && ! -L $scratch/$name ]] || fail 'Release logs and authored notes must be regular files without links.'
done
cmp -s -- "${notes[0]}" "$scratch/release-notes.md" || fail 'Prepared release prose differs from the committed authored note; prepare the intended source again.'
command -v jq >/dev/null || fail 'Install the jq host prerequisite before publishing.'
[[ $(jq -er '.source_revision' "$scratch/assets/$record") == "$commit" ]] || fail 'Prepared artifacts must identify the current committed source.'

verify_qualified_artifacts() {
    local marker checksum name extra
    local -A qualified=()
    while read -r marker checksum name extra; do
        [[ $marker == QUALIFIED_SHA256 ]] || continue
        case "$name" in "$wheel"|"$sdist"|"$lock"|"$native_lock"|"$record"|"$manifest") ;; *) fail 'Qualification returned an unexpected artifact name.' ;; esac
        [[ -z $extra && $checksum =~ ^[0-9a-f]{64}$ && ! -v qualified["$name"] ]] \
            || fail 'Qualification returned a malformed or duplicate artifact receipt.'
        qualified["$name"]=$checksum
    done < "$scratch/release-test.log"
    [[ ${#qualified[@]} == 6 ]] || fail 'Exact installed-artifact qualification did not return all six artifact hashes.'
    for name in "${names[@]}"; do
        [[ $(sha256sum "$scratch/assets/$name" | cut -d ' ' -f1) == "${qualified[$name]}" ]] \
            || fail 'Release artifact bytes differ from the successful qualification receipt.'
    done
}
verify_qualified_artifacts
mapfile -t push_urls < <(git remote get-url --push --all origin)
[[ ${#push_urls[@]} == 1 ]] || fail 'Origin must have exactly one GitHub push URL.'
push_url=${push_urls[0]}
case "$push_url" in
    git@github.com:*|https://github.com/*|ssh://git@github.com/*) ;;
    *) fail 'Origin must use a canonical github.com push URL without embedded credentials.' ;;
esac
routing_status=0
native 'Git URL routing inspection' 30 git config --null --get-regexp '^url\..*\.(insteadof|pushinsteadof)$' > "$scratch/url-rewrites" || routing_status=$?
[[ $routing_status == 0 || $routing_status == 1 ]] || fail 'Cannot inspect Git URL routing; retain the response before retrying.'
while IFS= read -r -d '' rule; do
    [[ $rule == *$'\n'* ]] || fail 'Git returned malformed URL routing configuration.'
    prefix=${rule#*$'\n'}
    [[ $push_url != "$prefix"* ]] || fail 'A Git URL rewrite matches the captured destination; choose canonical routing before publication.'
done < "$scratch/url-rewrites"
native 'GitHub repository inspection' 30 gh repo view "$push_url" --json nameWithOwner --jq .nameWithOwner > "$scratch/repository"
repository=$(cat "$scratch/repository")
[[ $repository == numpde/nmr-provider-tools ]] || fail 'Origin must identify numpde/nmr-provider-tools.'
resource_status=0
native 'existing GitHub release inspection' 30 gh api --hostname github.com --include "repos/$repository/releases/tags/$tag" > "$scratch/existing-release" 2> "$scratch/existing-release.stderr" || resource_status=$?
if [[ $resource_status == 0 ]]; then fail 'This GitHub release already exists; published assets must never be replaced.'; fi
[[ $resource_status == 1 ]] && head -n 1 "$scratch/existing-release" | tr -d '\r' | grep -Eq '^HTTP/[0-9.]+ 404( |$)' \
    || fail 'Cannot establish release absence; inspect the retained response before retrying.'
if git show-ref --verify --quiet "refs/tags/$tag"; then
    [[ $(git rev-parse "$tag^{}") == "$commit" ]] || fail 'The existing local tag identifies another source commit.'
else
    git tag -a "$tag" "$commit" -m "Release provider-core $version"
fi
require_unchanged_source
# Use the existing gh identity for this HTTPS push only; never rewrite host
# credential configuration or carry tokens into build/test containers.
native 'release tag push' 120 git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push "$push_url" "refs/tags/$tag:refs/tags/$tag"
native 'draft release creation' 120 gh release create "$tag" --repo "$repository" --verify-tag --draft --title "provider-core $version" \
    --notes-file "$scratch/release-notes.md" "$scratch/assets/"* "$scratch/release-build.log" "$scratch/release-test.log"
native 'uploaded draft acquisition' 120 gh release download "$tag" --repo "$repository" --dir "$scratch/downloaded"
downloaded=("$scratch/downloaded/"*)
[[ ${#downloaded[@]} == 8 ]] || fail 'Downloaded draft asset set differs from the eight qualified public files.'
for name in "${names[@]}"; do
    cmp -- "$scratch/assets/$name" "$scratch/downloaded/$name" \
        || fail 'Downloaded artifact differs from the qualified bytes; retain the draft for inspection.'
done
for name in release-build.log release-test.log; do
    cmp -- "$scratch/$name" "$scratch/downloaded/$name" \
        || fail 'Downloaded qualification log differs; retain the draft for inspection.'
done
native 'verified release publication' 30 gh release edit "$tag" --repo "$repository" --draft=false
printf 'Published https://github.com/%s/releases/tag/%s\n' "$repository" "$tag"
