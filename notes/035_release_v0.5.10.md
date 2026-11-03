# provider-core 0.5.10

The shared wheelhouse publisher allows builds to read existing cached artifacts
while a different artifact is downloading. Simultaneous acquisitions remain
serialized, and published artifacts are still checked and installed without
replacing existing files. A build that needs an artifact still being downloaded
must wait for acquisition to finish or report it missing.
