# Provider-core 0.6.2

Make shared acquisition cleanup survive Bash errexit. Cleanup authority now belongs to the owning process and remains available after callback function locals unwind. The owner checks its process identity before retiring resources, resets state for each session, and preserves caller EXIT behavior and original failure or cancellation status. Callbacks do not need special error guards to keep cleanup safe.

This release also carries the immutable-bootstrap and one-probe identity mapping corrections described in the 0.6.1 candidate note. Use 0.6.2 for shared acquisition adoption; 0.6.0 has a repeated-use defect, and the 0.6.1 publication was held as a draft after the callback cleanup failure was reproduced.

The repository adds a general check for bytecode suppression in literal Python launches, including isolated Python's environment semantics. Dynamic or split argument construction remains outside that static check; real repeated-session tests verify the complete source snapshot remains unchanged.
