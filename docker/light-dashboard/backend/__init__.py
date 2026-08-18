"""The dashboard backend.

This file exists to make ``backend`` a regular package rather than a namespace
one, and that is not a formality. Without it, test discovery silently finds
nothing:

    $ python3 -m unittest discover -s . -p "test_*.py"
    Ran 0 tests in 0.000s

    OK

Both halves of that are wrong in the same direction — a green result that ran
none of the 35 tests in this directory. ``unittest`` refuses to descend into a
namespace package, so every test module had to be named on the command line,
which meant a new test file was never picked up by anything unless whoever
added it also remembered to add it to the invocation. A suite you can forget to
run is worse than no suite: it reports the tests that were remembered as if
they were all of them.

``uvicorn backend.main:app`` and the ``from . import ...`` imports throughout
worked before this file and work the same with it.
"""
