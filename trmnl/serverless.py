# TRMNL Serverless (Python) : trains Orléans → Paris avec l'info en gare SNCF.
# TRMNL appelle run(input) à chaque rechargement du plugin (128 Mo, 5 s).
# - Horaires théoriques : horaires.json, publié par le dépôt trains-orleans-paris
#   (URL de polling du plugin, donc déjà dans input ; sinon téléchargé ici).
# - Temps réel : SIRI Lite Estimated Timetable (PIV : heure estimée, voie, suppression)
#   et Situation Exchange (messages voyageurs), open data SNCF sans clé.
# Pour tenir dans le temps imparti, on ne parse que les trajets qui citent la gare de
# départ, repérés par recherche de texte dans le flux brut (15 Mo).
import re
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

ET_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-siri-lite-estimated-timetable"
SX_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-siri-lite-situation-exchange"
STATIC_URL = "https://henry-reveillerguillon.github.io/trains-orleans-paris/horaires.json"
NS = "{http://www.siri.org.uk/siri}"
XL = "{http://www.w3.org/XML/1998/namespace}lang"
UA = {"User-Agent": "trmnl-orleans-trains/1.0"}
MAX_DEPARTURES = 12
RT_STALE_MIN = 20
DAYS_FR = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")


def paris_offset(dt):
    # Heure d'été : du dernier dimanche de mars 1 h UTC au dernier dimanche d'octobre 1 h UTC.
    y = dt.year
    def last_sunday(m):
        d = datetime(y, m, 31, 1, tzinfo=timezone.utc)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    return 2 if last_sunday(3) <= dt < last_sunday(10) else 1


def local(dt):
    return dt + timedelta(hours=paris_offset(dt))


def hm(dt):
    return local(dt).strftime("%H:%M") if dt else ""


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc) if s else None


def uic(s):
    s = (s or "").strip()
    return s[-8:] if len(s) >= 8 and s[-8:].isdigit() else ""


def txt(el, tag):
    v = el.find(NS + tag)
    return (v.text or "").strip() if v is not None and v.text else ""


def journeys_with(raw, code):
    """Blocs <EstimatedVehicleJourney> qui contiennent le code UIC, sans parser tout le flux."""
    out, seen, start = [], set(), 0
    while True:
        i = raw.find(code, start)
        if i < 0:
            break
        a = raw.rfind("<EstimatedVehicleJourney>", 0, i)
        b = raw.find("</EstimatedVehicleJourney>", i)
        if a < 0 or b < 0:
            break
        if a not in seen:
            seen.add(a)
            out.append(raw[a:b + len("</EstimatedVehicleJourney>")])
        start = b
    return out


def load_et(raw, frm, to):
    m = re.search(r"<ResponseTimestamp>([^<]+)</ResponseTimestamp>", raw[:3000])
    stamp = parse_dt(m.group(1)) if m else None
    found = {}
    for block in journeys_with(raw, frm):
        el = ET.fromstring(block.replace("<EstimatedVehicleJourney>",
                                         '<EstimatedVehicleJourney xmlns="http://www.siri.org.uk/siri">', 1))
        calls = [c for c in el.iter() if c.tag in (NS + "RecordedCall", NS + "EstimatedCall")]
        codes = [uic(txt(c, "StopPointRef")) for c in calls]
        if frm not in codes or to not in codes[codes.index(frm) + 1:]:
            continue
        i = codes.index(frm)
        j = codes.index(to, i + 1)
        c, a = calls[i], calls[j]
        aimed = parse_dt(txt(c, "AimedDepartureTime"))
        if not aimed:
            continue
        num = (el.findtext(NS + "TrainNumbers/" + NS + "TrainNumberRef") or "").strip()
        found[(local(aimed).strftime("%Y%m%d"), num)] = {
            "aimed": aimed,
            "expected": parse_dt(txt(c, "ExpectedDepartureTime")) or aimed,
            "cancelled": (txt(el, "Cancellation") == "true" or txt(c, "Cancellation") == "true"
                          or txt(c, "DepartureStatus") == "cancelled"
                          or txt(a, "Cancellation") == "true" or txt(a, "ArrivalStatus") == "cancelled"),
            "platform": txt(c, "DeparturePlatformName"),
            "arr_aimed": parse_dt(txt(a, "AimedArrivalTime")),
            "arr_expected": parse_dt(txt(a, "ExpectedArrivalTime")),
            "mode": "car" if txt(el, "VehicleMode") in ("bus", "coach") else "train",
            "dest": txt(el, "DestinationName"),
            "stops": [txt(x, "StopPointName") for x in calls[i + 1:j]],
        }
    return found, stamp


def fr_text(el, tag):
    best = ""
    for e in el.findall(NS + tag):
        if (e.get(XL) or "").upper() == "FR":
            return (e.text or "").strip()
        best = best or (e.text or "").strip()
    return best


def load_sx(raw, now, trains, frm):
    """Messages en cours pour nos trains, et ceux de la gare de départ."""
    by_train, station = {}, []
    wanted = set(trains)
    for block in re.findall(r"<PtSituationElement>.*?</PtSituationElement>", raw, re.S):
        nums = set(re.findall(r"<TrainNumberRef>\s*(\d+)\s*</TrainNumberRef>", block))
        if not (nums & wanted) and frm not in block:
            continue
        s = ET.fromstring(block.replace("<PtSituationElement>",
                                        '<PtSituationElement xmlns="http://www.siri.org.uk/siri">', 1))
        if txt(s, "Progress") == "closed":
            continue
        live = False
        for p in s.findall(NS + "ValidityPeriod"):
            a, b = parse_dt(txt(p, "StartTime")), parse_dt(txt(p, "EndTime"))
            if (a is None or a <= now) and (b is None or now <= b):
                live = True
        if not live:
            continue
        msg = {"id": txt(s, "SituationNumber"), "title": fr_text(s, "Summary"), "text": fr_text(s, "Description")}
        for t in nums:
            by_train.setdefault(t, []).append(msg)
        stops = {uic(e.text) for e in s.iter(NS + "StopPointRef")}
        if not nums and frm in stops:
            station.append(msg)
    return by_train, station


def train_type(line, mode):
    if mode == "car":
        return "Car"
    if line.startswith("K"):
        return "Rémi Express"
    if line.startswith("P") or line.startswith("C"):
        return "Rémi"
    return "TER"


def static_rows(static, days):
    """Horaires théoriques des jours voulus (AAAAMMJJ) à partir de horaires.json."""
    rows = {}
    for t in static.get("trains", []):
        for d in days:
            if d not in t["dates"]:
                continue
            base = datetime.strptime(d, "%Y%m%d").replace(tzinfo=timezone.utc)
            def at(hms):
                h, m, s = (int(x) for x in hms.split(":"))
                loc = base + timedelta(hours=h, minutes=m, seconds=s)
                return loc - timedelta(hours=paris_offset(loc - timedelta(hours=2)))
            rows[(d, t["train"])] = {
                "train": t["train"], "line": t["line"], "mode": t["mode"], "dest": t["dest"],
                "stops": t["stops"], "aimed": at(t["dep"]), "arr_aimed": at(t["arr"]),
                "expected": None, "platform": "", "cancelled": False, "rt": False, "arr_expected": None,
            }
    return rows


def run(input):
    t0 = time.time()
    now = datetime.now(timezone.utc)
    static = input if isinstance(input, dict) and input.get("trains") else None
    errors = []
    if static is None:
        try:
            static = requests.get(STATIC_URL, headers=UA, timeout=3).json()
        except Exception as e:
            static = {"trains": []}
            errors.append("horaires : %s" % e)
    frm = static.get("from_uic", "87543009")
    to = static.get("to_uic", "87547000")
    today = local(now).date()
    days = [today.strftime("%Y%m%d"), (today + timedelta(days=1)).strftime("%Y%m%d")]
    rows = static_rows(static, days)

    rt, stamp = {}, None
    try:
        raw = requests.get(ET_URL, headers=UA, timeout=3.5).content.decode("utf-8", "replace")
        rt, stamp = load_et(raw, frm, to)
    except Exception as e:
        errors.append("temps réel : %s" % e)
    rt_age = (now - stamp).total_seconds() / 60 if stamp else None
    rt_ok = stamp is not None and rt_age <= RT_STALE_MIN

    trains = {k[1] for k in rows} | {k[1] for k in rt}
    by_train, station_msgs = {}, []
    if time.time() - t0 < 3.8:
        try:
            raw = requests.get(SX_URL, headers=UA, timeout=max(0.5, 4.3 - (time.time() - t0))).content.decode("utf-8", "replace")
            by_train, station_msgs = load_sx(raw, now, trains, frm)
        except Exception as e:
            errors.append("messages : %s" % e)

    if rt_ok:
        for key, r in rt.items():
            row = rows.get(key)
            if row is None:
                row = rows[key] = {"train": key[1], "line": "", "mode": r["mode"], "dest": r["dest"],
                                   "stops": r["stops"], "aimed": r["aimed"], "arr_aimed": r["arr_aimed"]}
            row.update({"expected": r["expected"], "platform": r["platform"], "rt": True,
                        "cancelled": r["cancelled"], "arr_expected": r["arr_expected"]})
            if r["dest"]:
                row["dest"] = r["dest"]
            if r["mode"] == "car":
                row["mode"] = "car"

    out = []
    for row in sorted(rows.values(), key=lambda r: r["aimed"]):
        eff = row["expected"] or row["aimed"]
        if max(eff, row["aimed"]) < now - timedelta(minutes=1):
            continue
        delay = 0
        if row["expected"] and not row["cancelled"]:
            delay = max(0, int(round((row["expected"] - row["aimed"]).total_seconds() / 60)))
        n_stops = len(row["stops"])
        via = "direct" if n_stops <= 1 else "%d arrêts" % n_stops
        if n_stops > 1 and any(x.replace("É", "E").startswith("Etampes") for x in row["stops"]):
            via = "par Étampes"
        day = (local(row["aimed"]).date() - today).days
        msgs = by_train.get(row["train"], [])
        out.append({
            "train": row["train"], "type": train_type(row["line"], row["mode"]), "line": row["line"],
            "dest": row["dest"], "via": via,
            "day": "" if day == 0 else "demain" if day == 1 else DAYS_FR[local(row["aimed"]).weekday()],
            "dep": hm(row["aimed"]), "dep_ts": int(row["aimed"].timestamp()),
            "expected": hm(row["expected"]) if row["expected"] else "", "eff_ts": int(eff.timestamp()),
            "delay": delay, "platform": row["platform"],
            "status": "supprime" if row["cancelled"] else "retard" if delay > 0 else "ok" if row["rt"] else "theorique",
            "rt": row["rt"], "arr": hm(row["arr_expected"] or row["arr_aimed"]),
            "msg": msgs[0]["title"] if msgs else "", "msg_ids": [m["id"] for m in msgs],
        })
        if len(out) >= MAX_DEPARTURES:
            break

    # Un message sur plusieurs trains affichés concerne la ligne ou la gare : bandeau.
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

    return {
        "generated": hm(now), "generated_ts": int(now.timestamp()),
        "from": "Orléans", "to": "Paris Austerlitz",
        "rt_ok": rt_ok, "rt_time": hm(stamp), "alerts": alerts, "departures": out,
        "errors": errors, "duration_ms": int((time.time() - t0) * 1000),
        "source": "SNCF Voyageurs, open data (ODbL)",
    }
