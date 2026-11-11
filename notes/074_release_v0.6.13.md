# provider-core 0.6.13

The shared wheelhouse tool closes rejected HTTP response bodies before retry delays or terminal failure. If temporary-file removal fails first, that error boundary closes the response before replacing the network exception. A response-close failure stops retries and reports the original HTTP status alongside safe cleanup evidence; simultaneous temporary-file cleanup failure preserves both facts without exposing private response text or URLs.

Regression coverage includes permanent HTTP 404, exhausted HTTP 503 retries with Retry-After, response-close failure, and simultaneous response and temporary-file cleanup failure. A real local HTTP test proves that a later artifact failure preserves an earlier verified publication and unrelated pool files, and that retry fetches only the missing artifact. All 44 shared wheelhouse tests pass.

The acquisition owner retains the 0.6.12 error-transparency behavior and exact published linux-idmap 0.3.3 dependency. Existing public release tags and assets remain unchanged.
