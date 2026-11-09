"""Upkeep: SUNI notices what keeps going wrong with her and writes it down.

Phase 1 (this package as it stands) only WATCHES. It turns what diagnostics.py
reports, the errors in her logs, and the things she told someone she could not
do into incidents — one row per distinct problem, counted, with the evidence —
and shows them in the admin panel's Upkeep tab. It changes nothing: no
restarts, no config writes, no code. Later phases propose fixes, and every one
of them is approved by a person before it runs and again before it merges.

The part that judges a fix is never something the fixing can edit. That rule
is enforced from phase 3; phase 1 has nothing to protect it from yet.
"""
