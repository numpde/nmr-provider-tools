# provider-core 0.5.5

The shared proxy now holds a bounded number of connections when forwarding
capacity is busy. The admission delay is zero through half capacity and rises
linearly to seven seconds at full capacity. The shared settings file can adjust
the maximum delay and select ready-first or strict FIFO ordering, with changes
applied live to new connections and the waiting queue respectively. A caller's
existing exchange timeout includes this wait. Sustained overload, a full waiting
queue or a caller timeout can still make the connection unavailable.

The shared proxy remains independent of providers and delivery. Installing the
package or changing settings alone does not update an already running proxy;
the updated runtime and its deployment require separate verification.
