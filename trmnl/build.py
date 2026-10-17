"""Prépare le plugin TRMNL : trains.liquid devient le gabarit commun (shared.liquid),
ajouté par TRMNL en tête de chaque format ; les 4 formats ne contiennent rien d'autre."""
import os
here = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(here, "trains.liquid"), encoding="utf-8").read()
with open(os.path.join(here, "shared.liquid"), "w", encoding="utf-8") as f:
    f.write(src)
for view in ("full", "half_vertical", "half_horizontal", "quadrant"):
    with open(os.path.join(here, view + ".liquid"), "w", encoding="utf-8") as f:
        f.write("{%- comment -%} Gabarit commun : shared.liquid (source : trains.liquid). {%- endcomment -%}\n")
print("ok")
