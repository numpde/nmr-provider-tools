# Scientific library stand-ins

These small packages supply only the library operations used by provider
examples. They have their own tests and no SDK dependency. Provider registration,
execution ownership and lifecycle wiring stay in `examples/`.

`mock_numpy` supplies the array creation, finite masks, copying and floating
reductions exercised by the dummy provider, which imports it as `np`. Its README
states the supported subset. Add a Torch stand-in when an example first needs
a Torch operation.
