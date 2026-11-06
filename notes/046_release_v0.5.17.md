# provider-core 0.5.17

The shared acquisition HTTP/CONNECT proxy now supports a private Unix listener and a bounded loopback-to-Unix relay. Launchers can give a download worker the relay's disconnected network namespace while the gateway owns the selected device or explicit system route. Existing TCP proxy invocations remain valid.

Both transports live in the same standard-library module, so a verified three-file source snapshot can bootstrap acquisition before the wheel pool or wheelhouse tool exists. Consumer launchers still own container identity, readiness and worker-to-relay-to-gateway retirement.

HTTP/CONNECT and failure tests exercise the new transport. The maintained live qualification passed on both deployment engines with a read-only Unix socket grant, matching relay/worker network namespaces, successful proxied traffic and no direct external route. Each provider's launcher adoption is qualified separately; a wheel upgrade alone does not enforce container isolation.
