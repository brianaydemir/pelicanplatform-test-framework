# CI and Results

Running the [smoke tests](glossary.md#smoke-tests) in CI, and the format of
the results that [`test.py`](../test.py) and [`smoke.sh`](../smoke.sh)
write.

## Running in CI

This repository ships no CI workflow. A job needs a Linux host that meets
the [requirements](../README.md#requirements), and network access to GitHub
and the image registries.

- **What to test:** `PELICAN_TAG` names the release whose binaries, metadata
  verifier, and default images are used. `IMAGE_ORIGIN`, `IMAGE_CACHE`,
  `IMAGE_DIRECTOR`, and `IMAGE_REGISTRY` name images that the job built and
  loaded into the local daemon ([knobs](configuration.md#knobs)). To test a
  client that the job built, run `./fed.sh init` once, then copy the client
  over `framework/var/bin/linux/pelican`; each of `smoke.sh`'s copies of
  the checkout takes `framework/var/bin/` with it
  ([Client binary](../README.md#client-binary)).
- **What to run:** `./smoke.sh -o DIR SHAPE...`. `./smoke.sh --names` lists
  the [shapes](glossary.md#shape), one per line, for a matrix of one shape
  per job. `-j N` runs N copies of the checkout at once, as the host's
  memory allows: a shape each, or, with fewer shapes than copies, each
  shape's suites shared out among them.
- **What comes back:** the exit status, non-zero if any shape failed, and in
  `DIR`:
  - each shape's log, and its results in `results/<shape>/`;
  - each test run's [logs](../README.md#logs), in `logs/<shape>/<time>/`;
  - `<shape>.compose.log`, every service's log, for a shape that failed;
  - merged over the shapes, `results.tsv`, `junit.xml` (for a test
    reporter), and `summary.md` (for `$GITHUB_STEP_SUMMARY`).

For example, as steps of a
[GitHub Actions](https://docs.github.com/en/actions) job in a matrix over
the shapes:

```yaml
- run: ./fed.sh init
- run: ./smoke.sh -o smoke-results ${{ matrix.shape }}
  env:
    IMAGE_ORIGIN: pelican-origin:ci
    IMAGE_CACHE: pelican-cache:ci
    IMAGE_DIRECTOR: pelican-director:ci
    IMAGE_REGISTRY: pelican-registry:ci
- if: always()
  run: cat smoke-results/summary.md >>"$GITHUB_STEP_SUMMARY"
- if: always()
  uses: actions/upload-artifact@v4
  with:
    name: smoke-${{ matrix.shape }}
    path: smoke-results/
```

## Results

Each [suite](glossary.md#suite) writes `framework/var/results/<suite>.tsv`,
and its SciTokens [pass](glossary.md#pass) `<suite>-scitokens.tsv`
([token profiles](tests/README.md#token-profiles)): a header row, then one
row per [case](glossary.md#case).

```
suite   case   status   seconds   note
```

- `suite` is the suite's name, with `-scitokens` for its SciTokens pass, and
  `-<tag>` if `RESULTS_TAG` names a tag, as `smoke.sh` does to tell apart
  two runs of one suite.
- `status` is `PASS`, `FAIL`, `SKIP`, or `INCONCLUSIVE`.
- `note` escapes backslashes, tabs, carriage returns, and newlines as `\\`,
  `\t`, `\r`, and `\n`, so each row is one line.

A suite that stops early records why, as a `FAIL` row named `setup` or
`aborted`. One reason is the [watchdog](glossary.md#watchdog), which
abandons a pass that prints nothing for 20 minutes, or runs for 90
(`STALL_SECONDS` and `BUDGET_SECONDS` in `test.py`). A run of every suite
first removes earlier results with the same tag.

To read one:

```sh
column -t -s "$(printf '\t')" framework/var/results/transfers.tsv
```

`smoke.sh` records its own steps as the suite `smoke`, keeps each shape's
results in `DIR/results/<shape>/`, and merges them with
[`framework/report.py`](../framework/report.py). For a shape whose suites
it shared out among copies, it first combines the copies' results, each
case once, with its worst status (federation's required scenarios, and its
own steps, are each copy's):

```sh
framework/report.py combine DIR/results/<shape>/ PART_DIR...  # one shape's
framework/report.py merge DIR/results/*/ >results.tsv   # adds a shape column
framework/report.py junit results.tsv >junit.xml
framework/report.py markdown results.tsv >summary.md
```
