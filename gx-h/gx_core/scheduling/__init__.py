"""Scheduling as pure decisions over a snapshot of stored state.

``snapshot.load`` reads the facts of one run; ``dispatch.plan`` and
``dispatch.admit_claim`` say what may be claimed now; ``routing.route`` says what a
failure changes; ``stoprules.evaluate`` says whether the run goes on. Only ``load``
touches the store, and it writes nothing. The same snapshot gives the same decision.
"""
