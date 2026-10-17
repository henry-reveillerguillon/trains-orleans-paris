# Trains Orléans → Paris pour TRMNL

Prochains départs d'Orléans vers Paris Austerlitz sur un écran e-ink [TRMNL](https://trmnl.com), avec l'**info en gare** de la SNCF : heure estimée, voie, suppressions et messages voyageurs. Aucune clé d'API n'est nécessaire.

## Fonctionnement

1. **Horaires théoriques** : `horaires.py` lit le GTFS SNCF et publie [`horaires.json`](https://henry-reveillerguillon.github.io/trains-orleans-paris/horaires.json) sur GitHub Pages : les trains Orléans → Paris et leurs dates de circulation (environ 30 Ko). La tâche GitHub Actions le régénère à chaque envoi de code et chaque lundi.
2. **Temps réel** : le plugin TRMNL interroge `horaires.json`, puis sa fonction Serverless (`trmnl/serverless.py`, Python) lit à chaque rechargement l'info en gare et les messages voyageurs, et produit les variables de l'écran. Elle tourne en moins d'une seconde, dans la limite de 5 s de TRMNL.

Les flux temps réel couvrent toute la France et pèsent 15 Mo : la fonction ne décode que les trajets qui citent la gare d'Orléans, repérés par une recherche de texte.

## Sources

Données ouvertes de SNCF Voyageurs, sous licence ODbL, diffusées par [transport.data.gouv.fr](https://transport.data.gouv.fr/datasets/horaires-sncf) :

| Donnée | Flux | Usage |
|---|---|---|
| Horaires théoriques | GTFS | trains du jour et du lendemain |
| Info en gare | SIRI Lite Estimated Timetable, alimenté par la PIV (les écrans des gares) | heure estimée, voie, suppression, sur environ les 2 heures à venir |
| Messages voyageurs | SIRI Lite Situation Exchange, texte en français | message sur un train, ou bandeau quand il concerne plusieurs trains ou la gare |

Si le flux temps réel ne répond pas ou date de plus de 20 minutes, `rt_ok` passe à `false` : l'écran l'annonce et affiche les horaires théoriques.

## Fichiers

- `horaires.py` : fabrique `horaires.json` (Python 3.9 ou plus, bibliothèque standard).
- `.github/workflows/relais.yml` : publie `horaires.json` sur GitHub Pages.
- `trmnl/` : le plugin privé TRMNL : `settings.yml`, la fonction `serverless.py` et les gabarits Liquid. `trains.liquid` est la source : `python3 trmnl/build.py` en fait le gabarit commun `shared.liquid`.

Pour une autre gare ou une autre destination, changer `FROM_UIC` et `TO_UIC` (codes UIC à 8 chiffres) dans la tâche GitHub.
