# provider-core 0.6.4

Retiring an interpretation while provider preparation was already running could
discard a later accepted result. The conversation checked its retired state
before recognizing `ToolFinished`, leaving the shared interpreter without the
accepted preparation even though the provider had completed it.

The conversation now retains completion from already-admitted preparation
before enforcing retirement for further activity. Late model replies still
cannot start preparation, and retirement still prevents subsequent turns and
handoffs. Provider callbacks, native settlement and the original run task remain
provider-owned; retained acceptance does not authorize execution after Job stop.

The regression holds an admitted preparation, retires model I/O, then completes
preparation with `ToolFinished(None)`. It verifies retained acceptance, one
dispatch and no successor model. Existing installed-package qualification also
covers late replies, fixed deadlines, cleanup failures and native-only imports.
