# provider-core 0.6.12

Acquisition preserves a completed worker's failure status when its removal identity cannot be admitted. Client-configuration cleanup preserves an existing failure and otherwise returns its actual cleanup failure status. Identity and retirement checks remain strict; a failed cleanup does not authorize releasing unretired readers.

Cleanup stderr is drained before reporting, so a full public pipe or verbose utility cannot interrupt cleanup. The stderr report includes the underlying cause alongside operation context and status, including cleanup failures after caller cancellation. Only the first 4096 captured stderr bytes are rendered; escaping control characters can expand the displayed text, and truncation is explicit. The captured prefix is retained with private permissions. Operations already retained for diagnostics or unresolved ownership also receive a rendered error record before stderr delivery; this records prepared error content, not a delivery acknowledgment. Settled operations are not retained merely for reporting.

The acquisition bundle continues to use the exact published linux-idmap 0.3.3 wheel. Earlier SDK and container-tools release tags and artifacts remain unchanged.
