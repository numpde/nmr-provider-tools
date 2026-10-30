# provider-core 0.5.4

Condition recovery now honors the API's `Retry-After` floor after the failed
exchange retires. If that floor exceeds the recovery window, the condition is
not retried early; a stop remains able to interrupt the wait.

Shared delivery can publish a private aggregate operator observation of queue
use, pending age and paused publishers. Observation failure does not stop
delivery. The observation describes local retained work, not API acceptance;
the shared intake still exposes only submission. The deployment command also
reports observed local shutdown without inferring an in-flight API outcome.

Provider adoption and running deployment are verified separately from this
package release.
