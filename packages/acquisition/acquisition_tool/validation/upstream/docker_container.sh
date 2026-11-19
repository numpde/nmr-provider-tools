# Check the projected Docker state fields against the caller's expectations
_acq_docker_state_matches() {
    local running=$1 exit_code=$2 extra=$3 expected_running=$4 expected_exit=$5
    [[ $running == "$expected_running" && $exit_code =~ ^[0-9]+$ && -z $extra && $exit_code == "$expected_exit" ]]
}

_acq_docker_running_matches() {
    local receipt=$1 expected_running=$2
    cmp -s "$receipt" <(printf '%s\n' "$expected_running")
}

_acq_docker_network_matches() {
    local receipt=$1 expected_network=$2
    cmp -s "$receipt" <(printf '%s\n' "$expected_network")
}

_acq_docker_network_running_matches() {
    local receipt=$1 expected_network=$2 expected_running=$3
    cmp -s "$receipt" <(printf '%s %s\n' "$expected_network" "$expected_running")
}
