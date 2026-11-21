# Selected consumer source checks

Run an existing detector against explicitly selected Python files:

```sh
make tests/repository/quality DOCKER_CONTEXT=science PRIVACY_TESTS=0 \
    TEST_PATTERN=test_negated_call_parentheses_posture.py \
    CHECK_SOURCE_ROOT=/home/ra/repos/nmrpeak-repro \
    CHECK_SOURCE_FILES='nmrpeak_provider/analysis_session.py nmrpeak_provider/provider_main.py nmrpeak_provider/scientific_execution.py nmrpeak_provider/nmrpeak_binding.py'
```

Use `tests/repository/quality/report` with the same arguments and redirect stdout
to an HTML file for the existing report. Failures retain a nonzero exit status.
The report records the consumer root and selected filenames alongside the SDK
checker revision. Selecting consumer source does not change its SDK runtime pin.

If the SDK checkout's admitted checker is missing, use its maintained preparation
sequence in `make help` (`checks/prepare` acquires dependencies; offline
`checks/image/build` uses an already prepared bootstrap and wheelhouse).
`checks/dependencies/check` only verifies the cache; it does not prepare an image.
Selector edits use mounted checker code and do not require a new image.

Both source arguments must be Make command-line assignments on one quality target,
with one explicit `TEST_PATTERN`. The root must be an absolute local directory.
Files are space-separated relative `.py` paths; filenames containing whitespace
or commas, traversal, symlink components, missing files and private directories
(`.git`, `.agents`, `.aws`, `.codex`, `secrets`, `release-artifacts`) are refused.
Bootstrap copies, dependency directories and generated caches are also excluded.
Add related unit test paths explicitly; they are inspected, not executed.

Only the named consumer files are mounted, individually and read-only. Other
consumer files and credentials are not mounted. The SDK's public checker source
and fixtures remain available, with no container network or writable host mount.
As with the normal source-check lane, this runs trusted SDK detectors, not arbitrary
consumer code in an adversarial sandbox.

This mode supports generic detectors using `python_sources()`; explicit files
replace its SDK-specific production/test directory selection. Checks that inspect
SDK layout, deployment artifacts or fixture paths through the source root are
not consumer checks. This is not a whole-repository compatibility assessment;
choose the detector module appropriate to the selected code. Consumer imports
and dependencies need not be installed.
