"""What test.py, its suites (framework/suites/), and
framework/init-data.py share.

  blocks       the blocks suite's sizes and ranges, and checking a range
  common       paths, messages, the command line, and generated/
  credentials  the credentials the tests present, and what each allows
  director     asking a director where it would send a request
  report       one row per test case, in framework/var/results/
  session      what every suite shares in one run: tokens, the client
  stores       what the origins hold, and putting objects straight there
  transfers    the transfers suite's scenarios, batches, and verdicts
  web          HTTP(S) requests straight to a server
  webdav       PROPFIND, and reading the listings it gets

Python 3.9 and its standard library only: that is what AlmaLinux 9 (the
dev container) and macOS ship. The unit tests in tests/ need no
federation:

  python3 -B -m unittest discover -s framework -t framework
"""
