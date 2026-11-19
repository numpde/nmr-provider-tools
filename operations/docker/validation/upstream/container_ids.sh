#!/usr/bin/env bash
# Admit the full Docker container ID spelling used by native receipts
docker_full_container_id() {
    [[ $1 =~ ^[0-9a-f]{64}$ ]]
}
