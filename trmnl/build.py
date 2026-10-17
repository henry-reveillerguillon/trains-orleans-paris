"""Fabrique les 4 gabarits TRMNL à partir de trains.liquid (seul change le nombre de lignes)."""
import os
here = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(here, "trains.liquid"), encoding="utf-8").read()
for view, rows in (("full", 6), ("half_vertical", 6), ("half_horizontal", 3), ("quadrant", 3)):
    with open(os.path.join(here, view + ".liquid"), "w", encoding="utf-8") as f:
        f.write("{%%- assign default_rows = %d -%%}\n" % rows + src)
print("ok")
