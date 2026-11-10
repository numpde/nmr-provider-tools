# provider-core 0.6.9

This release adds the typed scientific contribution and execution interfaces developed after 0.6.8, plus a bounded embedded host and a standalone dummy-provider example. Contributions declare scientific inputs, results, live state, lifecycle hooks and resource requirements through shared bindings. The embedded backend supports fixed local allocations; it does not provide process isolation, crash-durable execution, remote workers or a shared Jobs service.

Scientific rejection contracts remain lightweight for direct scientific use. Installed integration tests build the dummy contribution independently and exercise its ordinary tests outside both source trees. Additional lifecycle tests cover permission changes, concurrent Attempts, original-owner settlement, result handoff and shutdown.

Host operations now live under host/, contained helpers under tools/ and containers/, and release builds preserve the published acquisition-bundle member names. Build and source-distribution qualification use isolated writable staging. Shared delivery separates proxy and outbox Compose definitions and retains exact image ownership checks during retirement.

The acquisition bundle pins published linux-idmap 0.3.1, including its CLI inverse projection support. Its focused identity qualification uses the relocated authored host sources. Existing installed cache-GC timers require reinstallation through engine/cache/gc/install after moving the checkout because their systemd units retain an absolute launcher path.

The release preparation lane qualifies the exact wheel, source distribution, installation locks, acquisition bundle, wheelhouse tool and checksum records. Four-provider contribution-interface adoption, real model inference and production deployment remain separate work.
