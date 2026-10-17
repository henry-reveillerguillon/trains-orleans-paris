"""Relais trains Orléans → Paris pour TRMNL.

Fusionne trois sources ouvertes de SNCF Voyageurs (licence ODbL, sans clé) :
- les horaires théoriques (GTFS, rechargés une fois par jour) ;
- l'info en gare en temps réel (SIRI Lite Estimated Timetable, alimenté par la PIV) :
  heure estimée, voie, suppression ;
- les messages d'information voyageur (SIRI Lite Situation Exchange), en français.

Écrit un petit JSON (quelques Ko) que TRMNL interroge, car les flux bruts pèsent
plusieurs Mo, au-delà des 100 Ko qu'accepte TRMNL.

Usage : python relais.py --out site/trains.json [--cache cache/] [--et fichier] [--sx fichier]
Uniquement la bibliothèque standard de Python (3.9 ou plus).
"""
import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")
UA = "trmnl-orleans-relais/1.0 (+https://github.com/henry-reveillerguillon)"
GTFS_URL = "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"
ET_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-siri-lite-estimated-timetable"
SX_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-siri-lite-situation-exchange"
NS = "{http://www.siri.org.uk/siri}"
XL = "{http://www.w3.org/XML/1998/namespace}lang"

# Codes UIC des gares.
FROM_UIC = os.environ.get("FROM_UIC", "87543009")   # Orléans
TO_UIC = os.environ.get("TO_UIC", "87547000")       # Paris Austerlitz
MAX_DEPARTURES = 12
# Au-delà, le flux temps réel est jugé en panne : l'écran le signale.
RT_STALE_MIN = 20

# Lignes Rémi : K = directs (Rémi Express), P = omnibus (Rémi).
def train_type(line, mode):
    if mode == "car":
        return "Car"
    if line.startswith("K"):
        return "Rémi Express"
    if line.startswith("P") or line.startswith("C"):
        return "Rémi"
    return "TER"


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def uic(stop_ref):
    """« StopPoint:OCETrain TER-87543009 » ou « FR:ScheduledStopPoint::87543009 » → « 87543009 »."""
    s = (stop_ref or "").strip()
    return s[-8:] if len(s) >= 8 and s[-8:].isdigit() else ""


# ---------- Horaires théoriques ----------

def build_static(days):
    """Départs théoriques FROM → TO pour les dates données (objets date)."""
    z = zipfile.ZipFile(io.BytesIO(fetch(GTFS_URL, timeout=180)))
    rd = lambda n: csv.DictReader(io.TextIOWrapper(z.open(n), encoding="utf-8-sig"))
    wanted = {d.strftime("%Y%m%d") for d in days}
    services = {}
    for r in rd("calendar_dates.txt"):
        if r["exception_type"] == "1" and r["date"] in wanted:
            services.setdefault(r["service_id"], set()).add(r["date"])
    routes = {r["route_id"]: r for r in rd("routes.txt")}
    trips = {r["trip_id"]: r for r in rd("trips.txt") if r["service_id"] in services}
    names = {r["stop_id"]: r["stop_name"] for r in rd("stops.txt")}
    calls = {}
    for r in rd("stop_times.txt"):
        if r["trip_id"] in trips:
            calls.setdefault(r["trip_id"], []).append(r)
    out = []
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
        route = routes.get(t["route_id"], {})
        mode = "car" if "Car" in st[i]["stop_id"] else "train"
        stops_between = [names.get(s["stop_id"], "") for s in st[i + 1:j]]
        for d in sorted(services[t["service_id"]]):
            out.append({
                "date": d,
                "train": t["trip_headsign"],
                "line": route.get("route_short_name", ""),
                "line_name": route.get("route_long_name", ""),
                "mode": mode,
                "dest": names.get(st[-1]["stop_id"], ""),
                "dep": st[i]["departure_time"],
                "arr": st[j]["arrival_time"],
                "stops": stops_between,
            })
    return out


def gtfs_time(date_s, hms):
    d = datetime.strptime(date_s, "%Y%m%d")
    h, m, s = (int(x) for x in hms.split(":"))
    return datetime(d.year, d.month, d.day, tzinfo=TZ) + timedelta(hours=h, minutes=m, seconds=s)


def load_static(cache_dir, today):
    days = [today, today + timedelta(days=1)]
    path = os.path.join(cache_dir, "static-%s.json" % today.strftime("%Y%m%d"))
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    data = build_static(days)
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


# ---------- Temps réel (PIV) ----------

def txt(el, tag):
    v = el.find(NS + tag)
    return (v.text or "").strip() if v is not None and v.text else ""


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def load_et(raw):
    """Trajets temps réel qui desservent FROM puis TO, clés (date AAAAMMJJ, numéro)."""
    found, stamp = {}, None
    for ev, el in ET.iterparse(io.BytesIO(raw), events=("end",)):
        if el.tag == NS + "ResponseTimestamp" and stamp is None:
            stamp = parse_dt(el.text)
        if el.tag != NS + "EstimatedVehicleJourney":
            continue
        calls = [c for c in el.iter() if c.tag in (NS + "RecordedCall", NS + "EstimatedCall")]
        codes = [uic(txt(c, "StopPointRef")) for c in calls]
        if FROM_UIC in codes and TO_UIC in codes[codes.index(FROM_UIC) + 1:]:
            i = codes.index(FROM_UIC)
            j = codes.index(TO_UIC, i + 1)
            c, a = calls[i], calls[j]
            num = el.findtext(NS + "TrainNumbers/" + NS + "TrainNumberRef") or ""
            aimed = parse_dt(txt(c, "AimedDepartureTime"))
            expected = parse_dt(txt(c, "ExpectedDepartureTime")) or aimed
            cancelled = (txt(el, "Cancellation") == "true" or txt(c, "Cancellation") == "true"
                         or txt(c, "DepartureStatus") == "cancelled")
            if aimed:
                key = (aimed.astimezone(TZ).strftime("%Y%m%d"), num.strip())
                found[key] = {
                    "aimed": aimed, "expected": expected, "cancelled": cancelled,
                    "platform": txt(c, "DeparturePlatformName"),
                    "arr_aimed": parse_dt(txt(a, "AimedArrivalTime")),
                    "arr_expected": parse_dt(txt(a, "ExpectedArrivalTime")),
                    "arr_cancelled": txt(a, "Cancellation") == "true" or txt(a, "ArrivalStatus") == "cancelled",
                    "mode": "car" if txt(el, "VehicleMode") in ("bus", "coach") else "train",
                    "line_name": txt(el, "PublishedLineName"),
                    "dest": txt(el, "DestinationName"),
                    "stops": [txt(x, "StopPointName") for x in calls[i + 1:j]],
                }
        el.clear()
    return found, stamp


# ---------- Messages voyageurs ----------

def fr_text(el, tag):
    best = ""
    for e in el.findall(NS + tag):
        if (e.get(XL) or "").upper() == "FR":
            return (e.text or "").strip()
        best = best or (e.text or "").strip()
    return best


def load_sx(raw, now):
    """Messages en cours : {numéro de train: [msg]} et messages de la gare de départ."""
    by_train, station = {}, []
    root = ET.fromstring(raw)
    for s in root.iter(NS + "PtSituationElement"):
        if txt(s, "Progress") == "closed":
            continue
        live = False
        for p in s.findall(NS + "ValidityPeriod"):
            a, b = parse_dt(txt(p, "StartTime")), parse_dt(txt(p, "EndTime"))
            if (a is None or a <= now) and (b is None or now <= b):
                live = True
        if not live:
            continue
        msg = {"id": txt(s, "SituationNumber"), "title": fr_text(s, "Summary"),
               "text": fr_text(s, "Description"), "severity": txt(s, "Severity")}
        trains = {(e.text or "").strip() for e in s.iter(NS + "TrainNumberRef")}
        for t in trains:
            by_train.setdefault(t, []).append(msg)
        stops = {uic(e.text) for e in s.iter(NS + "StopPointRef")}
        if not trains and FROM_UIC in stops:
            station.append(msg)
    return by_train, station


# ---------- Fusion ----------

def hm(dt):
    return dt.astimezone(TZ).strftime("%H:%M") if dt else ""


def hm_day(dt):
    return ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")[dt.astimezone(TZ).weekday()]


def ts(dt):
    return int(dt.timestamp()) if dt else 0


def merge(static, rt, by_train, station_msgs, now, rt_ok):
    rows = {}
    for s in static:
        aimed = gtfs_time(s["date"], s["dep"])
        rows[(s["date"], s["train"])] = {
            "train": s["train"], "line": s["line"], "mode": s["mode"], "dest": s["dest"],
            "stops": s["stops"], "aimed": aimed, "expected": None, "platform": "",
            "cancelled": False, "rt": False,
            "arr_aimed": gtfs_time(s["date"], s["arr"]), "arr_expected": None,
        }
    for key, r in rt.items():
        row = rows.get(key)
        if row is None:
            # Trajet absent des horaires théoriques : car de substitution, train ajouté…
            row = rows[key] = {"train": key[1], "line": "", "mode": r["mode"], "dest": r["dest"],
                               "stops": r["stops"], "aimed": r["aimed"], "arr_aimed": r["arr_aimed"]}
        row.update({"expected": r["expected"], "platform": r["platform"], "rt": True,
                    "cancelled": r["cancelled"] or r["arr_cancelled"],
                    "arr_expected": r["arr_expected"]})
        if r["dest"]:
            row["dest"] = r["dest"]
        if r["mode"] == "car":
            row["mode"] = "car"

    out = []
    for row in sorted(rows.values(), key=lambda r: r["aimed"]):
        eff = row["expected"] or row["aimed"]
        # On garde un train supprimé jusqu'à son heure prévue, les autres jusqu'au départ.
        if max(eff, row["aimed"]) < now - timedelta(minutes=1):
            continue
        delay = 0
        if row["expected"] and not row["cancelled"]:
            delay = max(0, int(round((row["expected"] - row["aimed"]).total_seconds() / 60)))
        msgs = by_train.get(row["train"], [])
        status = "supprime" if row["cancelled"] else "retard" if delay > 0 else "ok" if row["rt"] else "theorique"
        n_stops = len(row["stops"])
        via = "direct" if n_stops <= 1 else "%d arrêts" % n_stops
        if n_stops > 1 and any(x.replace("É", "E").startswith("Etampes") for x in row["stops"]):
            via = "par Étampes"
        day = row["aimed"].astimezone(TZ).date() - now.astimezone(TZ).date()
        out.append({
            "train": row["train"],
            "type": train_type(row["line"], row["mode"]),
            "line": row["line"],
            "dest": row["dest"],
            "via": via,
            "day": "" if day.days == 0 else "demain" if day.days == 1 else hm_day(row["aimed"]),
            "dep": hm(row["aimed"]),
            "dep_ts": ts(row["aimed"]),
            "expected": hm(row["expected"]) if row["expected"] else "",
            "eff_ts": ts(eff),
            "delay": delay,
            "platform": row["platform"],
            "status": status,
            "rt": row["rt"],
            "arr": hm(row["arr_expected"] or row["arr_aimed"]),
            "msg": msgs[0]["title"] if msgs else "",
            "msg_ids": [m["id"] for m in msgs],
        })
        if len(out) >= MAX_DEPARTURES:
            break

    # Un même message sur plusieurs trains affichés concerne la ligne ou la gare :
    # on le remonte en bandeau, une seule fois.
    count = {}
    for d in out:
        for i in d["msg_ids"]:
            count[i] = count.get(i, 0) + 1
    alerts, seen = [], set()
    for d in out:
        for i in d["msg_ids"]:
            if count[i] >= 2 and i not in seen:
                seen.add(i)
                m = next(m for m in by_train[d["train"]] if m["id"] == i)
                alerts.append({"title": m["title"], "text": m["text"]})
        if d["msg_ids"] and count[d["msg_ids"][0]] >= 2:
            d["msg"] = ""
        del d["msg_ids"]
    for m in station_msgs:
        if m["id"] not in seen:
            seen.add(m["id"])
            alerts.append({"title": m["title"], "text": m["text"]})
    return out, alerts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", default="cache")
    ap.add_argument("--et", help="fichier SIRI ET local (tests)")
    ap.add_argument("--sx", help="fichier SIRI SX local (tests)")
    ap.add_argument("--now", help="heure ISO (tests)")
    a = ap.parse_args()

    now = datetime.fromisoformat(a.now) if a.now else datetime.now(timezone.utc)
    today = now.astimezone(TZ).date()
    static = load_static(a.cache, today)

    rt, stamp, by_train, station_msgs, errors = {}, None, {}, [], []
    try:
        raw = open(a.et, "rb").read() if a.et else fetch(ET_URL)
        rt, stamp = load_et(raw)
    except Exception as e:  # flux temps réel en panne : on garde le théorique
        errors.append("temps réel : %s" % e)
    try:
        raw = open(a.sx, "rb").read() if a.sx else fetch(SX_URL)
        by_train, station_msgs = load_sx(raw, now)
    except Exception as e:
        errors.append("messages : %s" % e)

    rt_age = int((now - stamp).total_seconds() // 60) if stamp else None
    rt_ok = stamp is not None and rt_age <= RT_STALE_MIN
    departures, alerts = merge(static, rt if rt_ok else {}, by_train, station_msgs, now, rt_ok)

    doc = {
        "generated": hm(now),
        "generated_ts": ts(now),
        "from": "Orléans",
        "to": "Paris Austerlitz",
        "rt_ok": rt_ok,
        "rt_time": hm(stamp),
        "alerts": alerts,
        "departures": departures,
        "errors": errors,
        "source": "SNCF Voyageurs, open data (ODbL)",
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    print("%d départs, %d alertes, temps réel %s (%s), %d o" % (
        len(departures), len(alerts), "ok" if rt_ok else "INDISPONIBLE", hm(stamp), os.path.getsize(a.out)))
    for e in errors:
        print("!", e, file=sys.stderr)


if __name__ == "__main__":
    main()
