# Admit the full content-addressed image ID used by the selected Docker profile
docker_full_image_id() {
    [[ $1 =~ ^sha256:[0-9a-f]{64}$ ]]
}

# Admit an image ID only when the projected label matches the caller's value
docker_image_id_with_label_matches() {
    local record=$1 expected_label=$2 destination=$3 reported_id
    reported_id=${record%% *}

    docker_full_image_id "$reported_id" && [[ $record == "$reported_id $expected_label" ]] || return 1
    printf -v "$destination" '%s' "$reported_id"
}
