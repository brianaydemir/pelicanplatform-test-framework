# Notes for agents

## Running the tests

Every suite against one shape (`./fed.sh test`) takes about an hour. For
faster feedback, share the shape's suites out among copies of the checkout,
each running a federation of its own, with `smoke.sh -j`:

```sh
./smoke.sh -j 4 -p origin-s3 -p topo-multi-cache   # the shape of these presets
./smoke.sh -j 4 default                            # a shape that smoke.sh --names lists
```

Use `-j 4`. Given fewer shapes than copies, `smoke.sh` gives each copy a
part of a shape's suites (`test.py --part I/K`); given more, each copy
takes whole shapes in turn. The copies hold the working tree, uncommitted changes
included, and run beside a federation in the checkout. Logs and results go
to `smoke-logs/<time>/`, or `-o DIR`.

To check one area instead, name it: `./fed.sh test SUITE` or
`./fed.sh test 'SUITE/SCENARIO*'` (`./fed.sh test -l` lists them).
`smoke.sh` takes the same after `-t`, and shares what they select out among
the copies:

```sh
./smoke.sh -j 4 -p director-discovery -t blocks -t 'client/plugin-*'
```
