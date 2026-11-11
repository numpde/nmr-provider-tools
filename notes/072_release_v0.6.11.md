# provider-core 0.6.11

The acquisition bundle now derives its Linux identity implementation from the single published linux-idmap 0.3.3 wheel. That release reconciles the 0.3.2 source-quality and process-cleanup corrections with the optional ProbeResources contract on container-tools main. Its public resource type is linux_idmap.probe.ProbeResources; prior requests retain their omitted-policy behavior.

The SDK host identity adapter keeps its existing request policy. The exact upstream wheel, its source inventory and the generated acquisition bundle remain bound to the release record; providers consume those verified release bytes. The standalone dummy contribution depends on this SDK version.

The maintained preparation lane qualifies the exact wheel, source archive, locks, acquisition bundle and wheelhouse tool. Published older versions retain their original artifacts and meanings.
