#!/usr/bin/env bash
# Interface inventory and controlling-terminal choices follow Magnet:
# https://github.com/numpde/magnet-deploy/blob/0619d9ffd92a4326758b442c532aa09496161098/scripts/provider-deployment.sh#L905-L923
# https://github.com/numpde/magnet-deploy/blob/0619d9ffd92a4326758b442c532aa09496161098/scripts/operator-input.sh#L19-L92
# Retain this repository's available-interface admission, without a routing
# default or connectivity guess. The proxy binds IO to the selected device.

acquisition_interface_available() {
    [[ $1 =~ ^[A-Za-z0-9_.:-]{1,15}$ && $1 != . && $1 != .. && -d /sys/class/net/$1 ]]
}

acquisition_interface_description() {
    if [[ $1 == system ]]; then printf '%s' 'normal system routing'; return; fi
    local path=/sys/class/net/$1 state type kind=interface
    # Linux include/uapi/linux/if_arp.h defines the sysfs ARPHRD type values:
    # ARPHRD_ETHER=1 and ARPHRD_LOOPBACK=772.
    if ! IFS= read -r state 2>/dev/null < "$path/operstate"; then state=unavailable; fi
    if ! IFS= read -r type 2>/dev/null < "$path/type"; then type=unavailable; fi
    if [[ -d $path/wireless ]]; then kind=wireless
    elif [[ -d $path/bridge ]]; then kind=bridge
    elif [[ $type == 772 ]]; then kind=loopback
    elif [[ $type == 1 && -e $path/device ]]; then kind=Ethernet
    elif [[ $type == 1 ]]; then kind='virtual Ethernet'
    fi
    printf '%s, state: %s' "$kind" "$state"
}

select_acquisition_interface() {
    local tty_fd path name answer index
    local -a choices=() adapters=() other=()
    if [[ -v NETWORK_INTERFACE_INPUT && -n $NETWORK_INTERFACE_INPUT ]] || [[ -v NETWORK_INTERFACE_INPUT_ORIGIN && $NETWORK_INTERFACE_INPUT_ORIGIN == 'command line' ]]; then
        interface=
        if [[ -v NETWORK_INTERFACE_INPUT ]]; then interface=$NETWORK_INTERFACE_INPUT; fi
    else
        for path in /sys/class/net/*; do
            name=${path##*/}
            if acquisition_interface_available "$name"; then
                # A backing sysfs device includes VM adapters; it is not a
                # connectivity test. Every other interface remains selectable.
                if [[ -e $path/device ]]; then adapters+=("$name"); else other+=("$name"); fi
            fi
        done
        choices=("${adapters[@]}" "${other[@]}" system)
        if ! { exec {tty_fd}<>/dev/tty; } 2>/dev/null; then
            printf '%s\n' 'Cannot acquire artifacts or packages without an interactive terminal: supply NETWORK_INTERFACE=<interface> on the make command line.' >&2
            for name in "${choices[@]}"; do
                printf '  NETWORK_INTERFACE=%s  # %s\n' "$name" "$(acquisition_interface_description "$name")" >&2
            done
            return 2
        fi
        if [[ ${#choices[@]} == 0 ]]; then
            exec {tty_fd}>&-
            printf '%s\n' 'Cannot select NETWORK_INTERFACE: no available network interfaces were found.' >&2
            return 2
        fi
        printf 'Select NETWORK_INTERFACE (no default):\n' >&"$tty_fd"
        printf 'Choose an interface or system routing. Link state does not establish connectivity.\n' >&"$tty_fd"
        if [[ ${#adapters[@]} != 0 ]]; then printf 'Adapters (hardware or VM):\n' >&"$tty_fd"; fi
        for index in "${!choices[@]}"; do
            if [[ $index == "${#adapters[@]}" ]]; then printf 'Other interfaces and system routing:\n' >&"$tty_fd"; fi
            printf '  %d) %s [%s]\n' "$((index + 1))" "${choices[index]}" \
                "$(acquisition_interface_description "${choices[index]}")" >&"$tty_fd"
        done
        printf 'Choice [1-%d]: ' "${#choices[@]}" >&"$tty_fd"
        if ! IFS= read -r answer <&"$tty_fd"; then
            exec {tty_fd}>&-
            printf '%s\n' 'NETWORK_INTERFACE input ended before a choice was read.' >&2
            return 2
        fi
        exec {tty_fd}>&-
        interface=
        # Compare displayed numbers as strings; user input never enters shell
        # arithmetic, array subscripts or command evaluation.
        for index in "${!choices[@]}"; do
            if [[ $answer == "$((index + 1))" ]]; then interface=${choices[index]}; break; fi
        done
        if [[ -z $interface ]]; then
            printf '%s\n' 'NETWORK_INTERFACE selection must be one listed number; there is no default.' >&2
            return 2
        fi
    fi
    if [[ $interface != system ]] && ! acquisition_interface_available "$interface"; then
        printf 'Cannot acquire artifacts or packages: NETWORK_INTERFACE=%q is not an available network interface.\n' "$interface" >&2
        return 2
    fi
    # Wrappers resolve before image inspection. Later check/run children receive
    # this same value and validate it again without prompting or choosing a route.
    export NETWORK_INTERFACE_INPUT=$interface
}
