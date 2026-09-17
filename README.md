# vtc-bridge

Pont Telegram pour chauffeur VTC. Il lit les groupes de mise en relation, reconnaît les
courses dans des messages écrits à la main, calcule le temps de trajet réel depuis la
position en direct du chauffeur, et n'alerte que sur ce qui vaut le déplacement.

Le problème qu'il résout : une douzaine de groupes déversent des centaines de messages
par jour, la même course est repostée partout, et une course intéressante se noie dans
le bruit. Lire tout ça en conduisant est impossible.

## Ce qu'il fait

**Courses immédiates.** Le départ est géocodé, le temps de trajet est calculé depuis la
position en direct (HERE avec trafic, repli OSRM). Au-delà du seuil — 15 minutes par
défaut — la course est ignorée sans notification.

**Réservations.** Pas de logique de proximité : le tarif au kilomètre décide. En dessous
du plancher configuré, rien n'est notifié. Au-dessus, l'alerte arrive avec un fichier
`.ics` prêt à poser dans l'agenda, rappels 24 h et 1 h avant.

**Déduplication inter-groupes.** Une empreinte floue — prix plus jeu de codes postaux sur
le texte expansé, pour que `P10`, `Paris 10` et `75010` convergent — reconnaît la même
course repostée ailleurs. Quand le message n'offre pas assez de signal géographique, un
repli sur le texte normalisé couvre le cas, avec une fenêtre plus courte.

**Alertes actionnables.** Chaque alerte arrive en message privé avec des boutons : prendre
la course, répondre « SP », proposer un délai. Sans réponse, l'alerte expire et les boutons
se neutralisent.

**Comptabilité.** Les courses acceptées alimentent une base SQLite : chiffre d'affaires,
commission de l'apporteur, net. Bilans du jour, de la semaine et du mois par commande, plus
un relevé PDF paginé à envoyer au comptable.

## Architecture

Un seul processus Python, permanent. Telethon maintient la connexion Telegram ouverte et
l'état vit sur disque : la session, la base SQLite, la dernière position connue. Pas de
serveur web, pas de file d'attente, pas de conteneur.

Cette forme exclut les plateformes serverless : il faut une machine allumée en continu avec
un disque persistant. Le dossier [`deploy/`](deploy/) contient de quoi l'installer sur un
VPS — unité systemd durcie, scripts de transfert, sauvegarde quotidienne vérifiée avec
copie hors du serveur.

| Fichier | Rôle |
| --- | --- |
| `vtc_bridge.py` | Le pont : parsing, géocodage, décision, alertes, commandes du bot. |
| `store.py` | Accès SQLite : courses, statuts, agrégats. |
| `eta.py` | Calcul d'itinéraire et de temps de trajet. |
| `deploy/` | Installation serveur, service systemd, sauvegardes. |
| `test_*.py` | Tests du parseur, des réservations, de la déduplication. |

Les scripts à la racine (`scan_*.py`, `dm_*.py`, `find_*.py`, `list_*.py`) sont des outils
d'analyse ponctuels écrits pour reconstituer l'historique des commissions. Ils ne font pas
partie du pont.

## Installation

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Renseigne `.env` : identifiants API Telegram obtenus sur https://my.telegram.org,
identifiants des groupes à surveiller, seuils. `list_ids.py` affiche les identifiants des
groupes dont tu es membre.

```
.venv/bin/python vtc_bridge.py
```

Au premier lancement, Telegram demande un code de confirmation au clavier.

Pour un déploiement serveur, suis [`deploy/README.md`](deploy/README.md).

## Position en direct

Le calcul de proximité a besoin de savoir où tu es. Partage ta position **en direct** depuis
Telegram, dans le message privé du bot ou dans le groupe relais. Le pont suit les mises à
jour, qui arrivent sous forme d'éditions du message.

Telegram coupe ce partage au bout de huit heures. Sans position fraîche, les courses
immédiates sont ignorées et le journal le dit : `Position périmée`. Les réservations, elles,
continuent d'être traitées.

## Sécurité

`.env` et les fichiers `*.session` ne sont pas versionnés, et ne doivent jamais l'être :
une session Telethon donne un accès complet au compte Telegram associé. Même chose pour
`state/` et `logs/`, qui contiennent des adresses, des tarifs et des messages de tiers.

## Licence

Aucune licence déclarée : tous droits réservés.
