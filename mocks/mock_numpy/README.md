# mock_numpy

A dependency-free NumPy stand-in for the operations exercised by the dummy
provider. The example imports it as `np`; the SDK does not depend on it.

```python
import mock_numpy as np

weights = np.array(object=[1.0, 2.0], dtype=float)
samples = np.asarray(a=[3.0, 4.0], dtype=float)

assert np.isfinite(samples).all()
assert np.dot(a=weights, b=samples) == 11.0
```

Supported operations:

- `array(object=..., dtype=float)` or inferred dtype when omitted.
- `asarray(a=..., dtype=float)`, reusing matching storage when possible.
- Scalar `isfinite` and shape-preserving boolean array masks with `.all()`.
- Equal-length real-vector `dot(a=..., b=...)`.
- Global real-array `sum(a=..., dtype=float)` with floating accumulation.
- Array shape, dimension, size and dtype-kind metadata; independent `.copy()`
  and `.tolist()`; integer indexing and assignment for vectors.

Rectangular nested sequences retain their shape so the provider can reject
non-vector inputs. Bool, integer, float, complex and text dtype inference lets
its dtype checks see the actual input category. Ragged input is rejected.

Storage uses independent mutable Python lists. Fixed-width integer overflow,
broadcasting, views, slice indexing, multidimensional indexing and elementwise
comparisons are outside this mock's scope. Unsupported forms fail explicitly.
This is a tested example contract, not a general NumPy replacement.
