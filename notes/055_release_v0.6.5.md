# provider-core 0.6.5

Interface-bound acquisition could select the host's merged DNS configuration even when those resolvers were unreachable through the selected interface. The shared acquisition owner now captures configured resolvers on the host before starting its gateway and passes that private file read-only to the proxy.

- Named interfaces prefer NetworkManager device DNS, then systemd-resolved link DNS. If neither supplies usable IPv4 resolvers, the existing configured global resolver files remain the fallback. The `system` route uses global configuration.
- The first usable configured set is captured once per scope. Query failures do not switch sources, append public resolvers or remove interface binding.
- The gateway needs no host manager tools or management sockets. The same stdlib `acquisition_tool/dns.py` helper is included in the source bundle for provider-owned build launchers; they need not duplicate discovery policy.
- `--resolver-file` is explicit authority for the proxy. An unusable file fails before readiness instead of falling back to container DNS configuration.

The existing service proxy retains its resolver behavior when no capture is supplied. This release requires no API proxy/outbox restart. The interpreter's 0.6.4 late-acceptance correction is unchanged.

DNS configuration is fixed for an acquisition scope. DHCP or manager changes apply to its next scope; this is not a live DNS refresh service. IPv6-only DNS remains unsupported by the existing IPv4 acquisition transport.
