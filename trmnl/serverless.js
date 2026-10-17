// TRMNL Serverless (JavaScript) : trains Orléans → Paris avec l'info en gare SNCF.
// TRMNL appelle run(input) à chaque rechargement du plugin, après avoir lu l'URL de
// polling : input contient donc horaires.json (trains théoriques et dates) et trmnl.
// On y ajoute le temps réel, open data SNCF sans clé :
// - SIRI Lite Estimated Timetable (PIV : heure estimée, voie, suppression) ;
// - SIRI Lite Situation Exchange (messages voyageurs, en français).
// Les flux font 15 Mo et 2 Mo, et TRMNL n'accorde que 128 Mo : on les lit au fil de
// l'eau et on ne garde que les blocs qui citent la gare de départ ou nos trains.

const ET_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-siri-lite-estimated-timetable";
const SX_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-siri-lite-situation-exchange";
const MAX_DEPARTURES = 12;
const RT_STALE_MIN = 20;
const DAYS_FR = ["dim.", "lun.", "mar.", "mer.", "jeu.", "ven.", "sam."];

// Heure de Paris sans dépendre des fuseaux de l'environnement : UTC+2 du dernier dimanche
// de mars au dernier dimanche d'octobre (à 1 h UTC), UTC+1 le reste de l'année.
function lastSunday(y, m) {
  const d = Date.UTC(y, m + 1, 0, 1);
  return d - new Date(d).getUTCDay() * 86400000;
}
function offsetMs(ms) {
  const y = new Date(ms).getUTCFullYear();
  return ms >= lastSunday(y, 2) && ms < lastSunday(y, 9) ? 7200000 : 3600000;
}
const paris = (ms) => new Date(ms + offsetMs(ms));
const pad = (n) => (n < 10 ? "0" : "") + n;
const hm = (ms) => { if (!ms) return ""; const t = paris(ms); return pad(t.getUTCHours()) + ":" + pad(t.getUTCMinutes()); };
const ymd = (ms) => { const t = paris(ms); return t.getUTCFullYear() + pad(t.getUTCMonth() + 1) + pad(t.getUTCDate()); };
const weekday = (ms) => DAYS_FR[paris(ms).getUTCDay()];

// Heure GTFS (peut dépasser 24:00) d'un jour AAAAMMJJ, heure de Paris → instant (ms).
function gtfsTime(d, hms) {
  const [h, m, s] = hms.split(":").map(Number);
  const wall = Date.UTC(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6, 8), h, m, s);
  return wall - offsetMs(wall - offsetMs(wall));
}

const parseDt = (s) => (s ? Date.parse(s) : null);
const uic = (s) => { s = (s || "").trim(); return /\d{8}$/.test(s) ? s.slice(-8) : ""; };
const unxml = (s) => s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&amp;/g, "&");

function tag(block, name) {
  const m = block.match(new RegExp("<" + name + "(?:\\s[^>]*)?>([\\s\\S]*?)</" + name + ">"));
  return m ? unxml(m[1].trim()) : "";
}
function tags(block, name) {
  const re = new RegExp("<" + name + "(?:\\s[^>]*)?>([\\s\\S]*?)</" + name + ">", "g");
  const out = [];
  let m;
  while ((m = re.exec(block))) out.push(unxml(m[1].trim()));
  return out;
}

// Lit url par morceaux et garde les blocs open … close pour lesquels keep(bloc) est vrai.
// Renvoie aussi le début du document (horodatage du flux).
async function streamBlocks(url, open, close, keep, ms) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(url, { signal: ctl.signal, headers: { "User-Agent": "trmnl-orleans-trains/1.0" } });
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    const blocks = [];
    let buf = "", head = "";
    for (;;) {
      const { done, value } = await reader.read();
      const text = value ? dec.decode(value, { stream: true }) : "";
      if (head.length < 3000) head += text.slice(0, 3000 - head.length);
      buf += text;
      let a = buf.indexOf(open);
      while (a >= 0) {
        const b = buf.indexOf(close, a);
        if (b < 0) break;
        const block = buf.slice(a, b + close.length);
        if (keep(block)) blocks.push(block);
        a = buf.indexOf(open, b + close.length);
      }
      buf = a >= 0 ? buf.slice(a) : buf.slice(-open.length);
      if (done) break;
    }
    return { blocks, head };
  } finally {
    clearTimeout(timer);
  }
}

const CALL_RE = /<(RecordedCall|EstimatedCall)>([\s\S]*?)<\/\1>/g;

function loadEt(et, frm, to) {
  const m = et.head.match(/<ResponseTimestamp>([^<]+)<\/ResponseTimestamp>/);
  const stamp = m ? Date.parse(m[1]) : null;
  const found = {};
  for (const block of et.blocks) {
    const calls = [];
    let c;
    CALL_RE.lastIndex = 0;
    while ((c = CALL_RE.exec(block))) calls.push(c[2]);
    const head = block.replace(CALL_RE, "");
    const codes = calls.map((x) => uic(tag(x, "StopPointRef")));
    const i = codes.indexOf(frm);
    const j = i < 0 ? -1 : codes.indexOf(to, i + 1);
    if (i < 0 || j < 0) continue;
    const dep = calls[i], arr = calls[j];
    const aimed = parseDt(tag(dep, "AimedDepartureTime"));
    if (!aimed) continue;
    const num = tag(head, "TrainNumberRef");
    found[ymd(aimed) + "|" + num] = {
      aimed,
      expected: parseDt(tag(dep, "ExpectedDepartureTime")) || aimed,
      cancelled: tag(head, "Cancellation") === "true" || tag(dep, "Cancellation") === "true"
        || tag(dep, "DepartureStatus") === "cancelled" || tag(arr, "Cancellation") === "true"
        || tag(arr, "ArrivalStatus") === "cancelled",
      platform: tag(dep, "DeparturePlatformName"),
      arr_aimed: parseDt(tag(arr, "AimedArrivalTime")),
      arr_expected: parseDt(tag(arr, "ExpectedArrivalTime")),
      mode: ["bus", "coach"].includes(tag(head, "VehicleMode")) ? "car" : "train",
      dest: tag(head, "DestinationName"),
      stops: calls.slice(i + 1, j).map((x) => tag(x, "StopPointName")),
    };
  }
  return { found, stamp };
}

function frText(block, name) {
  const fr = block.match(new RegExp("<" + name + ' xml:lang="FR">([\\s\\S]*?)</' + name + ">"));
  return fr ? unxml(fr[1].trim()) : tag(block, name);
}

function loadSx(blocks, now, frm) {
  const byTrain = {};
  const station = [];
  for (const s of blocks) {
    const nums = tags(s, "TrainNumberRef");
    if (tag(s, "Progress") === "closed") continue;
    const live = tags(s, "ValidityPeriod").some((p) => {
      const a = parseDt(tag(p, "StartTime")), b = parseDt(tag(p, "EndTime"));
      return (a === null || a <= now) && (b === null || now <= b);
    });
    if (!live) continue;
    const msg = { id: tag(s, "SituationNumber"), title: frText(s, "Summary"), text: frText(s, "Description") };
    for (const n of nums) (byTrain[n] = byTrain[n] || []).push(msg);
    if (!nums.length && tags(s, "StopPointRef").map(uic).includes(frm)) station.push(msg);
  }
  return { byTrain, station };
}

function trainType(line, mode) {
  if (mode === "car") return "Car";
  if (line.startsWith("K")) return "Rémi Express";
  if (line.startsWith("P") || line.startsWith("C")) return "Rémi";
  return "TER";
}

async function run(input) {
  const t0 = Date.now();
  const now = Date.now();
  const frm = input.from_uic || "87543009";
  const to = input.to_uic || "87547000";
  const today = ymd(now);
  const tomorrow = ymd(now + 86400000);
  const errors = [];

  // Horaires théoriques du jour et du lendemain.
  const rows = {};
  for (const t of input.trains || []) {
    for (const d of [today, tomorrow]) {
      if (!t.dates.includes(d)) continue;
      rows[d + "|" + t.train] = {
        train: t.train, line: t.line, mode: t.mode, dest: t.dest, stops: t.stops,
        aimed: gtfsTime(d, t.dep), arr_aimed: gtfsTime(d, t.arr),
        expected: null, platform: "", cancelled: false, rt: false, arr_expected: null,
      };
    }
  }

  // Les deux flux en parallèle, lus au fil de l'eau.
  const known = new Set(Object.values(rows).map((r) => r.train));
  const sxKeep = (b) => b.indexOf(frm) >= 0 || tags(b, "TrainNumberRef").some((n) => known.has(n));
  const [et, sx] = await Promise.all([
    streamBlocks(ET_URL, "<EstimatedVehicleJourney>", "</EstimatedVehicleJourney>", (b) => b.indexOf(frm) >= 0, 3800)
      .catch((e) => { errors.push("temps réel : " + e); return null; }),
    streamBlocks(SX_URL, "<PtSituationElement>", "</PtSituationElement>", sxKeep, 3800)
      .catch((e) => { errors.push("messages : " + e); return null; }),
  ]);
  const { found, stamp } = et ? loadEt(et, frm, to) : { found: {}, stamp: null };
  const rtOk = stamp !== null && (now - stamp) / 60000 <= RT_STALE_MIN;

  if (rtOk) {
    for (const [key, r] of Object.entries(found)) {
      let row = rows[key];
      if (!row) {
        row = rows[key] = { train: key.split("|")[1], line: "", mode: r.mode, dest: r.dest, stops: r.stops,
          aimed: r.aimed, arr_aimed: r.arr_aimed };
      }
      Object.assign(row, { expected: r.expected, platform: r.platform, rt: true, cancelled: r.cancelled,
        arr_expected: r.arr_expected });
      if (r.dest) row.dest = r.dest;
      if (r.mode === "car") row.mode = "car";
    }
  }

  const { byTrain, station } = sx ? loadSx(sx.blocks, now, frm) : { byTrain: {}, station: [] };

  const out = [];
  for (const row of Object.values(rows).sort((a, b) => a.aimed - b.aimed)) {
    const eff = row.expected || row.aimed;
    if (Math.max(eff, row.aimed) < now - 60000) continue;
    const delay = row.expected && !row.cancelled ? Math.max(0, Math.round((row.expected - row.aimed) / 60000)) : 0;
    const n = row.stops.length;
    let via = n <= 1 ? "direct" : n + " arrêts";
    if (n > 1 && row.stops.some((x) => x.replace("É", "E").startsWith("Etampes"))) via = "par Étampes";
    const d = ymd(row.aimed);
    const msgs = byTrain[row.train] || [];
    out.push({
      train: row.train, type: trainType(row.line, row.mode), line: row.line, dest: row.dest, via,
      day: d === today ? "" : d === tomorrow ? "demain" : weekday(row.aimed),
      dep: hm(row.aimed), dep_ts: Math.floor(row.aimed / 1000),
      expected: row.expected ? hm(row.expected) : "", eff_ts: Math.floor(eff / 1000),
      delay, platform: row.platform,
      status: row.cancelled ? "supprime" : delay > 0 ? "retard" : row.rt ? "ok" : "theorique",
      rt: row.rt, arr: hm(row.arr_expected || row.arr_aimed),
      msg: msgs.length ? msgs[0].title : "", msg_ids: msgs.map((x) => x.id),
    });
    if (out.length >= MAX_DEPARTURES) break;
  }

  // Un message sur plusieurs trains affichés concerne la ligne ou la gare : bandeau.
  const count = {};
  for (const d of out) for (const id of d.msg_ids) count[id] = (count[id] || 0) + 1;
  const alerts = [];
  const seen = new Set();
  for (const d of out) {
    for (const id of d.msg_ids) {
      if (count[id] >= 2 && !seen.has(id)) {
        seen.add(id);
        const msg = byTrain[d.train].find((x) => x.id === id);
        alerts.push({ title: msg.title, text: msg.text });
      }
    }
    if (d.msg_ids.length && count[d.msg_ids[0]] >= 2) d.msg = "";
    delete d.msg_ids;
  }
  for (const msg of station) {
    if (!seen.has(msg.id)) { seen.add(msg.id); alerts.push({ title: msg.title, text: msg.text }); }
  }

  return {
    generated: hm(now), generated_ts: Math.floor(now / 1000), from: "Orléans", to: "Paris Austerlitz",
    rt_ok: rtOk, rt_time: hm(stamp), alerts, departures: out, errors,
    duration_ms: Date.now() - t0, source: "SNCF Voyageurs, open data (ODbL)",
  };
}
