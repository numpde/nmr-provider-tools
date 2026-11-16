# Scientific library stand-ins

These small packages supply only the library operations used by provider
examples. They have their own tests and no SDK dependency. Provider registration,
execution ownership and lifecycle wiring stay in `examples/`.

`mock_numpy` currently supplies scalar `isfinite`; the dummy provider imports it
as `np`. Add a Torch stand-in when an example first needs a Torch operation.
