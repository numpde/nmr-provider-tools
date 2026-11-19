# Interpret the GitHub CLI release lookup after the caller retires its process
github_release_lookup_result() {
    local status=$1 response=$2

    if [[ $status == 0 ]]; then
        printf '%s\n' exists
    elif [[ $status == 1 ]] && head -n 1 "$response" | tr -d '\r' | grep -Eq '^HTTP/[0-9.]+ 404( |$)'; then
        printf '%s\n' absent
    else
        printf '%s\n' unknown
    fi
}
