# Relais trains Orléans → Paris pour TRMNL

Prochains départs d'Orléans vers Paris Austerlitz pour un écran e-ink [TRMNL](https://trmnl.com), avec l'**info en gare** de la SNCF : heure estimée, voie, suppressions et messages voyageurs. Aucune clé d'API n'est nécessaire.

## Pourquoi un relais

Les flux temps réel de la SNCF couvrent toute la France et pèsent plusieurs mégaoctets, alors que TRMNL n'accepte que 100 Ko par réponse. Toutes les 5 minutes environ, une tâche GitHub Actions lit ces flux, garde les trains Orléans → Paris et publie un petit fichier [`trains.json`](https://henry-reveillerguillon.github.io/trains-orleans-paris/trains.json) (environ 3 Ko) sur GitHub Pages. TRMNL interroge ce fichier.

## Sources

Données ouvertes de SNCF Voyageurs, sous licence ODbL, diffusées par [transport.data.gouv.fr](https://transport.data.gouv.fr/datasets/horaires-sncf) :

| Donnée | Flux | Usage |
|---|---|---|
| Horaires théoriques | GTFS, rechargé une fois par jour | liste des trains du jour et du lendemain |
| Info en gare | SIRI Lite Estimated Timetable, alimenté par la PIV (les écrans des gares) | heure estimée, voie, suppression, sur environ les 2 heures à venir |
| Messages voyageurs | SIRI Lite Situation Exchange, texte en français | message sur un train, ou bandeau quand il concerne plusieurs trains ou la gare |

Si le flux temps réel ne répond plus ou date de plus de 20 minutes, `rt_ok` passe à `false` : l'écran l'annonce et affiche les horaires théoriques.

## Fichiers

- `relais.py` : le relais (Python 3.9 ou plus, bibliothèque standard uniquement).
- `.github/workflows/relais.yml` : la tâche planifiée qui le lance et publie le résultat.
- `trmnl/` : le plugin privé TRMNL (`settings.yml` et les gabarits Liquid). `trains.liquid` est la source : `python3 trmnl/build.py` en fait le gabarit commun `shared.liquid`, que TRMNL ajoute en tête des 4 formats.

Pour une autre gare ou une autre destination, changer les variables `FROM_UIC` et `TO_UIC` (codes UIC à 8 chiffres) dans la tâche planifiée.

## Essai en local

```bash
python3 relais.py --out site/trains.json --cache cache
```

## Le JSON

```json
{
  "generated": "17:12", "generated_ts": 1792249930, "rt_ok": true, "rt_time": "17:11",
  "alerts": [{"title": "…", "text": "…"}],
  "departures": [{
    "train": "860526", "type": "Rémi Express", "line": "K8+", "dest": "Paris Austerlitz",
    "via": "direct", "day": "", "dep": "17:34", "dep_ts": 1792251240,
    "expected": "17:34", "eff_ts": 1792251240, "delay": 0, "platform": "2",
    "status": "ok", "rt": true, "arr": "18:39", "msg": ""
  }]
}
```

`status` vaut `ok`, `retard`, `supprime` ou `theorique` (train pas encore suivi en temps réel).
