# Match the complete Docker state record against the caller's expected fields
docker_container_state_matches() {
    local receipt=$1 expected_running=$2 expected_exit=$3
    cmp -s "$receipt" <(printf '%s %s\n' "$expected_running" "$expected_exit")
}
