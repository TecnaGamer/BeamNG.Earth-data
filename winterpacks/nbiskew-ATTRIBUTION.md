# nbiskew: the US bridge inventory's skews

Derived from the **National Bridge Inventory** (NBI) of the US Department of Transportation, Federal Highway
Administration: the delimited files of every state, <https://www.fhwa.dot.gov/bridge/nbi/ascii.cfm>. A work of the US
federal government, in the public domain.

Kept per structure carrying a route (item 5A, record type 1): its coordinates (items 16 and 17), its skew angle (item
34, degrees; 99 the inventory's "major variation" code, kept as it is) and its structure length (item 49, metres, kept
in decimetres). Nothing else of the records is kept. Built by `tools/build_nbiskew.py`; BeamNG.Earth's server reads it
to cut its bridge decks' ends at the angle the structure was built with.
