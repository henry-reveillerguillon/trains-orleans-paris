"""Fabrique horaires.json : trains théoriques FROM → TO avec leurs dates de circulation,
tirés du GTFS SNCF. Ce petit fichier (quelques dizaines de Ko) sert d'URL de polling au
plugin TRMNL, dont la fonction Serverless ajoute le temps réel.

Usage : python3 horaires.py site/horaires.json
"""
import csv, io, json, os, sys, urllib.request, zipfile
from datetime import datetime, timezone

GTFS_URL = "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"
FROM_UIC = os.environ.get("FROM_UIC", "87543009")   # Orléans
TO_UIC = os.environ.get("TO_UIC", "87547000")       # Paris Austerlitz


def uic(s):
    s = (s or "").strip()
    return s[-8:] if len(s) >= 8 and s[-8:].isdigit() else ""


def main(out):
    req = urllib.request.Request(GTFS_URL, headers={"User-Agent": "trmnl-orleans-horaires/1.0"})
    z = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(req, timeout=180).read()))
    rd = lambda n: csv.DictReader(io.TextIOWrapper(z.open(n), encoding="utf-8-sig"))
    services = {}
    for r in rd("calendar_dates.txt"):
        if r["exception_type"] == "1":
            services.setdefault(r["service_id"], set()).add(r["date"])
    routes = {r["route_id"]: r for r in rd("routes.txt")}
    trips = {r["trip_id"]: r for r in rd("trips.txt")}
    names = {r["stop_id"]: r["stop_name"] for r in rd("stops.txt")}
    calls = {}
    for r in rd("stop_times.txt"):
        calls.setdefault(r["trip_id"], []).append(r)
    merged = {}
    for tid, st in calls.items():
        st.sort(key=lambda r: int(r["stop_sequence"]))
        codes = [uic(s["stop_id"]) for s in st]
        if FROM_UIC not in codes:
            continue
        i = codes.index(FROM_UIC)
        if TO_UIC not in codes[i + 1:]:
            continue
        j = codes.index(TO_UIC, i + 1)
        t = trips[tid]
        entry = {
            "train": t["trip_headsign"],
            "line": routes.get(t["route_id"], {}).get("route_short_name", ""),
            "mode": "car" if "Car" in st[i]["stop_id"] else "train",
            "dest": names.get(st[-1]["stop_id"], ""),
            "dep": st[i]["departure_time"],
            "arr": st[j]["arrival_time"],
            "stops": [names.get(s["stop_id"], "") for s in st[i + 1:j]],
        }
        key = json.dumps(entry, sort_keys=True)
        merged.setdefault(key, (entry, set()))[1].update(services.get(t["service_id"], ()))
    trains = []
    for entry, dates in merged.values():
        if dates:
            entry["dates"] = sorted(dates)
            trains.append(entry)
    trains.sort(key=lambda e: (e["dep"], e["train"]))
    doc = {"from_uic": FROM_UIC, "to_uic": TO_UIC,
           "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
           "valid_until": max(d for e in trains for d in e["dates"]) if trains else "",
           "trains": trains}
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    print("%d trains, jusqu'au %s, %d o" % (len(trains), doc["valid_until"], os.path.getsize(out)))


if __name__ == "__main__":
    main(sys.argv[1])
