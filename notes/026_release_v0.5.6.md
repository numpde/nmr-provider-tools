# provider-core 0.5.6

If the shared outbox cannot collect its optional queue summary, it now reports
a warning with the SQLite error category and, when available, code. The observation remains
unknown and the outbox continues accepting and publishing work. The warning
does not include the database error text or queued result contents.

Condition recovery now expresses absent retry advice explicitly; retry timing
is unchanged. This package release does not itself update a running service.
