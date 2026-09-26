# CI and Results

How to run the smoke tests in CI, and the format of the results that
`test.py` and `smoke.sh` write.


## Running in CI

Pelican's own CI can run these tests; this repository ships no workflow
of its own. A job needs a Linux host that meets the
[requirements](../README.md#requirements), and network access to GitHub
and the image registries.

- **What to test** is given the usual way: `PELICAN_TAG` names the
  release whose binaries, metadata verifier, and default images are
  used; `IMAGE_ORIGIN`, `IMAGE_CACHE`, `IMAGE_DIRECTOR`, and
  `IMAGE_REGISTRY` name images the job built and loaded into the local
  daemon. To test a client the job built, run `./fed.sh init` once, then
  copy the client over `framework/var/bin/linux/pelican`; `smoke.sh`
  keeps `framework/var/bin/` across its resets.
- **What to run:** `./smoke.sh -o DIR SHAPE...`. `./smoke.sh --names`
  lists the shapes one per line, for a matrix of one shape per job.
- **What comes back:** the exit status (non-zero if any shape failed),
  and, in `DIR`, each shape's log, its results in `results/<shape>/`,
  and, merged over the shapes, `results.tsv`, `junit.xml` (for a test
  reporter), and `summary.md` (for `$GITHUB_STEP_SUMMARY`). See
  [Results](#results).

For example, as steps of a job in a matrix over the shapes:

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

Each suite writes `framework/var/results/<suite>.tsv`, one row per test
case, with a header row:

```
suite   case   status   seconds   note
```

`status` is `PASS`, `FAIL`, `SKIP`, or `INCONCLUSIVE`. A note's
backslashes, tabs, carriage returns, and newlines are written as `\\`,
`\t`, `\r`, and `\n`, so each row is one line. The suite is the suite's
name, with `-<tag>` after it if `RESULTS_TAG` names a tag, as `smoke.sh`
does to tell apart two runs of one suite. A suite that stops early
records why, as a `FAIL` row named `setup` or `aborted`. A run of
everything first removes earlier runs' results. `transfers` has a row
per scenario, and `auth` and `owners` one per request and per document their `keys`
check fetches. To read one:

```sh
column -t -s "$(printf '\t')" framework/var/results/transfers.tsv
```

`smoke.sh` records its own steps as the suite `smoke`, keeps each
shape's results in `DIR/results/<shape>/`, and merges them with
`framework/report.py`:

```sh
framework/report.py merge DIR/results/*/ >results.tsv   # adds a shape column
framework/report.py junit results.tsv >junit.xml
framework/report.py markdown results.tsv >summary.md
```
