#!/usr/bin/env bash
# Admit the Docker image and process fields consumed by delivery

source "$root/operations/docker/validation/upstream/image_ids.sh"

delivery_image_fields_match() {
    local observed=$1 expected_image=$2 expected_role=$3
    [[ $observed =~ ^"$expected_image $expected_role "([0-9a-f]{64})$ ]]
}

delivery_container_process_fields_match() {
    local observed=$1 expected_state=$2
    [[ $observed =~ ^"$expected_state"\|[0-9]+\|(true|false)$ ]]
}
