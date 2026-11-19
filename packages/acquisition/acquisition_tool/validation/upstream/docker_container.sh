# Check the projected Docker state fields against the caller's expectations
_acq_docker_state_matches() {
    local running=$1 exit_code=$2 extra=$3 expected_running=$4 expected_exit=$5
    [[ $running == "$expected_running" && $exit_code =~ ^[0-9]+$ && -z $extra && $exit_code == "$expected_exit" ]]
}
