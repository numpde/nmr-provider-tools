# provider-core 0.5.11

The wheelhouse publisher again supports acquiring an already complete selection
from a read-only pool. It checks cached artifacts before opening the acquisition
lock; missing artifacts still use serialized, verified publication.
