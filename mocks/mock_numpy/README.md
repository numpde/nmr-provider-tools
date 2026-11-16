# mock_numpy

The dummy provider needs scalar `isfinite` checks. This package supplies that
one function using the standard library, without NumPy or provider-core.

```python
import mock_numpy as np

np.isfinite(1.0)  # True
np.isfinite(float("inf"))  # False
```

The supported input is one Python integer or float. Arrays, tensors and other
NumPy operations are outside this mock's scope. Extend it when an example needs
another operation, together with a test for that use.
