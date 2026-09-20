"""HFlow test package.

Tests are grouped by what they can prove, not by which module they import:

* ``test_contracts.py``  - admission and data contracts (pure, no I/O beyond paths)
* ``test_controller.py`` - the controller state machine against the fake driver
* ``test_cli.py``        - the command surface and its zero-model guarantees

There is no ``live/`` directory yet on purpose: no real Harness driver exists in
this build, so a live test would assert nothing.
"""
