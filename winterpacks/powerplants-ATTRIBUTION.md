# powerplants: the world's thermal power plants and their mapped towers and chimneys

A derived database, available under the **Open Database License 1.0** (<https://opendatacommons.org/licenses/odbl/1-0/>),
built by `tools/build_powerplants.py` (a pinned copy of BeamNG.Earth's) from:

- **OpenStreetMap**, © OpenStreetMap contributors (ODbL 1.0, <https://www.openstreetmap.org/copyright>): the mapped
  power plants (power=plant) as distributed by GMWID / Geomermaids (<https://gmwid.geomermaids.com/>), with the
  operating US plants EIA-860M lists and OpenStreetMap lacks (U.S. Energy Information Administration, public domain);
  and the mapped chimneys and cooling towers (man_made=chimney, man_made=cooling_tower, tower:type=cooling) through
  the Overpass API.
- **Global Power Plant Database** v1.3.0, Global Energy Observatory, Google, KTH Royal Institute of Technology in
  Stockholm, Enipedia, World Resources Institute (CC BY 4.0).
- **Global Coal Plant Tracker**, Global Energy Monitor (CC BY 4.0), the copy in GreenInfo Network's coal-tracker-client
  (August 2023).
- **GeoNuclearData** by Cristian Stoica, from the IAEA's PRIS (ODbL).
- **powerplantmatching** by PyPSA (CC BY 4.0).
- The plant rules and the hand-checked list of demolished and still-standing sites from devo343's Earth Power Plants
  add-on (used with the author's permission).

Kept per plant: its point, fuel, size, state (running, or closed and still standing), closing year, reach and name;
per mapped tower or chimney: its point, kind, mapped height and diameter, its OpenStreetMap element and plant.
BeamNG.Earth reads it for its world map's power plant layer and its plants' steam plumes, lights and generic buildings.
