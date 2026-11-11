# provider-core 0.6.14

The shared acquisition owner refuses another online worker when its existing relay did not complete initial route setup. The owner publishes the relay port only after readiness, strict port parsing, and final network and running-state validation succeed. Reusing a relay requires that published port.

An incomplete route produces an explicit refusal while preserving the original setup failure. A failed final relay-state check retains its private observation for inspection. Ordinary worker exit statuses remain independent from route setup: healthy routes can serve multiple workers, including workers that exit with status 7.

Regressions reproduce callback continuation after stopped-relay readiness and after invalid final relay state. Shared scope tests also verify retained operation and client resources when a native removal anchor cannot publish its exact identity; a later worker is refused after unknown worker-removal retirement.

The acquisition bundle retains the exact published linux-idmap 0.3.3 dependency.
