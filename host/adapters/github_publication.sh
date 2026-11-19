#!/usr/bin/env bash
# GitHub CLI commands for the release publisher's native process owner

github_release_url() {
    printf 'https://github.com/%s/releases/tag/%s\n' "$1" "$2"
}

github_publication_command() {
    local action=$1
    shift
    case "$action" in
        inspect-repository)
            exec gh repo view "$1" --json nameWithOwner --jq .nameWithOwner
            ;;
        inspect-release)
            exec gh api --hostname github.com --include "repos/$1/releases/tags/$2"
            ;;
        push-tag)
            # Use the CLI identity for this push without changing host credential configuration
            exec git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push "$1" "$2"
            ;;
        create-draft)
            local repository=$1 tag=$2 title=$3 notes=$4
            shift 4
            exec gh release create "$tag" --repo "$repository" --verify-tag --draft --title "$title" --notes-file "$notes" "$@"
            ;;
        download-draft)
            exec gh release download "$2" --repo "$1" --dir "$3"
            ;;
        publish-draft)
            exec gh release edit "$2" --repo "$1" --draft=false
            ;;
        *) return 2 ;;
    esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    set -euo pipefail
    github_publication_command "$@"
fi
