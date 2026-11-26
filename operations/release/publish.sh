#!/usr/bin/env bash
# GitHub-only releases follow pay-to-play-wire 4c92a3b tools/ops/publish.sh.
# Prepare freezes clean source and qualifies exact bytes. Publish consumes that
# reviewable directory, uploads a draft, then verifies downloaded bytes first.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
cd -- "$root"
[[ $# == 1 && $1 =~ ^(prepare|release)$ ]] || exit 2
action=$1
[[ -v TAG_INPUT_ORIGIN && $TAG_INPUT_ORIGIN == 'command line' && -v TAG_INPUT && $TAG_INPUT =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] \
    || { printf '%s\n' 'Supply TAG=vX.Y.Z on the Make command line.' >&2; exit 2; }
tag=$TAG_INPUT
version=${tag#v}
source "$root/host/tools/inference_native.sh"
inference_diagnostic=''
inference_retirement_confirmed=true
inference_cancellation_status=0
scratch=''
disposable_scratch=false
finish() {
    local status=$? report_status=0 message=$inference_diagnostic
    trap - EXIT
    if [[ -n $scratch ]]; then
        if "$disposable_scratch" && "$inference_retirement_confirmed"; then
            if rm -rf -- "$scratch"; then :; else
                message+="Cannot remove temporary publication files at $scratch."$'\n'
                if [[ $status == 0 ]]; then status=2; fi
            fi
        else
            message+="Release inputs and receipts retained at $scratch."$'\n'
        fi
    fi
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
report_preparation_failure() {
    local stage=$1 status=$2 log=$3 summary="$scratch/release-failures.txt"
    printf 'provider-core %s failed (exit %s). Full log: %s\n' "$stage" "$status" "$log" >&2
    if grep -q '^=== PROVIDER-CORE FAILURE SUMMARY' "$log"; then
        sed -n '/^=== PROVIDER-CORE FAILURE SUMMARY/,/^=== END PROVIDER-CORE FAILURE SUMMARY ===/p' "$log" > "$summary"
    else
        tail -n 35 -- "$log" > "$summary"
    fi
    printf 'Failure index: %s\n' "$summary" >&2
    cat -- "$summary" >&2
}
private_directory() {
    local label=$1 directory=$2
    # Keep one diagnostic for missing paths instead of leaking realpath -e errors.
    if [[ $directory != /* || $directory == / || $directory =~ [[:cntrl:],] \
          || ! -d $directory || -L $directory ]]; then
        if [[ $label == OUTPUT ]]; then
            fail "OUTPUT must be an existing canonical absolute directory below /, without symlinks or mount delimiters. Create a fresh one with: mkdir -m 700 -- release-artifacts/$tag"
        fi
        fail 'ARTIFACTS must be the existing reviewed publish/prepare OUTPUT root (with artifacts/ and qualification logs); do not create a new empty directory.'
    fi
    if [[ $(realpath -e -- "$directory") != "$directory" \
          || $(stat -c '%u:%a' -- "$directory") != "$(id -u):700" ]]; then
        if [[ $label == OUTPUT ]]; then
            fail 'OUTPUT must be canonical, owned by the invoking user, and mode 0700. Preserve prior release evidence and choose another empty destination.'
        fi
        fail 'ARTIFACTS must be the canonical mode-0700 reviewed publish/prepare OUTPUT root, owned by this user; correct the selected prepared directory rather than creating an empty one.'
    fi
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
acquisition=acquisition-owner-$version.tar.gz
metadata=$wheel.metadata
listing=provider_core-$version-simple.html
names=("$wheel" "$metadata" "$listing" "$sdist" "$lock" "$native_lock" "$acquisition" "$record" "$manifest")

if [[ $action == prepare ]]; then
    if [[ ! -v OUTPUT_INPUT_ORIGIN || $OUTPUT_INPUT_ORIGIN != 'command line' || ! -v OUTPUT_INPUT || -z $OUTPUT_INPUT ]]; then
        printf '%s\n' 'publish/prepare requires OUTPUT=<absolute path to an existing empty caller-owned mode-0700 directory>.' >&2
        printf 'For TAG=%s, from the repository root:\n' "$tag" >&2
        printf '  mkdir -m 700 -- release-artifacts/%s\n' "$tag" >&2
        printf '  make publish/prepare TAG=%s DOCKER_CONTEXT=<context> WHEELHOUSE=<cache-dir> OUTPUT="$PWD/release-artifacts/%s"\n' "$tag" "$tag" >&2
        printf '%s\n' 'WHEELHOUSE is a separate dependency cache. OUTPUT receives artifacts/, release-build.log, release-test.log, and release-notes.md.' >&2
        exit 2
    fi
    private_directory OUTPUT "$OUTPUT_INPUT"
    [[ -z $(find "$OUTPUT_INPUT" -mindepth 1 -maxdepth 1 -print -quit) ]] || fail 'OUTPUT must be empty; no files were written. Preserve previous evidence and use another empty directory with mode 0700.'
    scratch=$OUTPUT_INPUT
    source "$root/host/tools/wheelhouse_path.sh"
    select_wheelhouse
    require_wheelhouse_pool
    source "$root/operations/docker/docker_context.sh"
    select_docker_context
    mkdir -m 700 -- "$scratch/artifacts"
    cp -- "${notes[0]}" "$scratch/release-notes.md"
    if nested 'release package build' env WHEELHOUSE_INPUT_ORIGIN='command line' WHEELHOUSE_INPUT="$wheelhouse_directory" OUTPUT_INPUT_ORIGIN='command line' OUTPUT_INPUT="$scratch/artifacts" bash "$root/packages/provider-core/sdk_build/provider_core.sh" build > "$scratch/release-build.log" 2>&1; then
        :
    else
        status=$?
        report_preparation_failure build "$status" "$scratch/release-build.log"
        exit "$status"
    fi
    [[ -f $scratch/artifacts/$wheel && -f $scratch/artifacts/$sdist ]] || fail 'TAG must match the built package version.'
    if nested 'exact release artifact qualification' env WHEELHOUSE_INPUT_ORIGIN='command line' WHEELHOUSE_INPUT="$wheelhouse_directory" ARTIFACTS_INPUT_ORIGIN='command line' ARTIFACTS_INPUT="$scratch/artifacts" bash "$root/packages/provider-core/sdk_build/provider_core.sh" package > "$scratch/release-test.log" 2>&1; then
        :
    else
        status=$?
        report_preparation_failure qualification "$status" "$scratch/release-test.log"
        exit "$status"
    fi
    require_unchanged_source
    printf 'Prepared %s in %s. Review artifacts/, release-build.log, release-test.log and release-notes.md, then run:\n  make publish/release TAG=%s ARTIFACTS=%q\n' "$tag" "$scratch" "$tag" "$scratch"
    exit
fi

if [[ ! -v ARTIFACTS_INPUT_ORIGIN || $ARTIFACTS_INPUT_ORIGIN != 'command line' || ! -v ARTIFACTS_INPUT || -z $ARTIFACTS_INPUT ]]; then
    fail "publish/release requires ARTIFACTS=<absolute path to the reviewed publish/prepare OUTPUT directory>. Example: make publish/release TAG=$tag ARTIFACTS=\"\$PWD/release-artifacts/$tag\". Supply the whole directory, not its artifacts/ child."
fi
private_directory ARTIFACTS "$ARTIFACTS_INPUT"
[[ -d $ARTIFACTS_INPUT/artifacts && -f $ARTIFACTS_INPUT/release-build.log \
    && -f $ARTIFACTS_INPUT/release-test.log && -f $ARTIFACTS_INPUT/release-notes.md ]] \
    || fail 'ARTIFACTS must point to the whole reviewed publish/prepare OUTPUT root containing artifacts/, release-build.log, release-test.log and release-notes.md; do not select the artifacts/ child.'
publication_inputs=$ARTIFACTS_INPUT
umask 077
scratch=$(mktemp -d -t nmr-provider-publication.XXXXXXXX)
# The reviewed preparation directory owns the qualified bytes; these copies
# exist only while publishing and verifying the remote assets.
disposable_scratch=true
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
        case "$name" in "$wheel"|"$metadata"|"$listing"|"$sdist"|"$lock"|"$native_lock"|"$acquisition"|"$record"|"$manifest") ;; *) fail 'Qualification returned an unexpected artifact name.' ;; esac
        [[ -z $extra && $checksum =~ ^[0-9a-f]{64}$ && ! -v qualified["$name"] ]] \
            || fail 'Qualification returned a malformed or duplicate artifact receipt.'
        qualified["$name"]=$checksum
    done < "$scratch/release-test.log"
    [[ ${#qualified[@]} == ${#names[@]} ]] || fail 'Exact installed-artifact qualification did not return every artifact hash.'
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
github_publication=$root/host/adapters/github_publication.sh
source "$github_publication"
source "$root/host/validation/upstream/github_release.sh"
native 'GitHub repository inspection' 30 bash "$github_publication" inspect-repository "$push_url" > "$scratch/repository"
repository=$(cat "$scratch/repository")
[[ $repository == numpde/nmr-provider-tools ]] || fail 'Origin must identify numpde/nmr-provider-tools.'
resource_status=0
native 'existing GitHub release inspection' 30 bash "$github_publication" inspect-release "$repository" "$tag" \
    > "$scratch/existing-release" 2> "$scratch/existing-release.stderr" || resource_status=$?
case "$(github_release_lookup_result "$resource_status" "$scratch/existing-release")" in
    exists) fail 'This GitHub release already exists; published assets must never be replaced.' ;;
    absent) ;;
    *) fail 'Cannot establish release absence; inspect the retained response before retrying.' ;;
esac
if git show-ref --verify --quiet "refs/tags/$tag"; then
    [[ $(git rev-parse "$tag^{}") == "$commit" ]] || fail 'The existing local tag identifies another source commit.'
else
    git tag -a "$tag" "$commit" -m "Release provider-core $version"
fi
require_unchanged_source
native 'release tag push' 120 bash "$github_publication" push-tag "$push_url" "refs/tags/$tag:refs/tags/$tag"
native 'draft release creation' 120 bash "$github_publication" create-draft "$repository" "$tag" "provider-core $version" \
    "$scratch/release-notes.md" "$scratch/assets/"* "$scratch/release-build.log" "$scratch/release-test.log"
native 'uploaded draft acquisition' 120 bash "$github_publication" download-draft "$repository" "$tag" "$scratch/downloaded"
downloaded=("$scratch/downloaded/"*)
[[ ${#downloaded[@]} == $((${#names[@]} + 2)) ]] || fail 'Downloaded draft asset set differs from the qualified public files.'
for name in "${names[@]}"; do
    cmp -- "$scratch/assets/$name" "$scratch/downloaded/$name" \
        || fail 'Downloaded artifact differs from the qualified bytes; retain the draft for inspection.'
done
for name in release-build.log release-test.log; do
    cmp -- "$scratch/$name" "$scratch/downloaded/$name" \
        || fail 'Downloaded qualification log differs; retain the draft for inspection.'
done
if ! native 'verified release publication' 30 bash "$github_publication" publish-draft "$repository" "$tag"; then
    fail 'Cannot confirm release publication; inspect the GitHub release before retrying. The checkout listing was not changed.'
fi
printf 'Published %s\n' "$(github_release_url "$repository" "$tag")"
# Source qualification is finished. The subsequent index commit pins these published bytes.
update_listing() (
    mkdir -p -- "$root/simple" || exit
    temporary=$(mktemp "$root/simple/.provider-core.XXXXXXXX") || exit
    trap 'rm -f -- "$temporary"' EXIT
    cp -- "$scratch/assets/$listing" "$temporary" &&
        chmod 644 "$temporary" &&
        mv -T -- "$temporary" "$root/simple/provider-core.html"
)
if ! update_listing; then
    printf 'The release is public, but updating simple/provider-core.html failed. Copy %s/artifacts/%s there, then commit and push the listing. Do not repeat release publication.\n' "$publication_inputs" "$listing" >&2
    exit 2
fi
printf '%s\n' 'Generated simple/provider-core.html from the published wheel. Commit and push this file, then pin that commit in the raw package-page URL.'
