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

**Alertes actionnables.** Chaque alerte arrive en message privé avec deux boutons :
« Accepter » et « Refuser ». « Accepter » recalcule le temps de trajet jusqu'au départ depuis
ta position du moment et propose les délais à annoncer, le meilleur marqué ⭐ : « SP » sous
5 minutes, sinon le temps réel arrondi à la minute au-dessus, puis les paliers de 5 minutes
suivants. Un tap envoie la réponse dans le groupe. Sans réponse, l'alerte expire et les
boutons se neutralisent.

**Plusieurs chauffeurs.** Chaque chauffeur connecte son propre compte Telegram. Le pont lit
les groupes de ce compte, suit la position en direct de ce chauffeur et ne lui envoie que les
courses qui passent ses réglages. Deux chauffeurs dans le même groupe reçoivent le même
message et le jugent chacun depuis leur position. Les boutons, la comptabilité et les
bilans sont séparés : un chauffeur ne voit jamais les courses d'un autre, et sa réponse part
du groupe depuis son compte.

**Réglages par chauffeur.** `/reglages` dans le bot ouvre l'écran de réglages, avec des
boutons − / + ou une saisie libre. Effet immédiat, pour ce chauffeur seulement.

| Réglage | Effet | Défaut |
| --- | --- | --- |
| Temps max jusqu'au départ | Course immédiate : au-delà, rien n'est envoyé | 8 min |
| Distance max jusqu'au départ | Course immédiate : distance de route depuis sa position | sans limite |
| Prix minimum | Immédiates et réservations sous ce prix : ignorées | aucun |
| €/km minimum | Réservation : sous ce tarif au kilomètre, rien n'est envoyé | 1,5 €/km |
| Rayon des réservations | Réservation ignorée si son départ est trop loin de sa position | sans limite |
| Seuil « rentable » 🟢 | Couleur de la pastille de rentabilité | 2 €/km |
| Commission | Part retenue quand le message ne dit rien | aucune |
| Réponse type | Texte envoyé en acceptant une réservation | `ok` |
| Courses van | Les courses van ne sont plus écartées | désactivé |

L'écran « Avancé » règle le seuil « SP », le nombre de temps proposés et l'expiration des
alertes. Raccourcis : `/prixmin 30`, `/distancemax 12`, `/tempsmax 10`, `/eurkm 1.8`,
`/rayon 40`. Les variables `THRESHOLD_SECONDS`, `EURKM_MIN`, etc. du `.env` ne servent
plus que de valeurs par défaut.

**Comptabilité.** Les courses acceptées alimentent une base SQLite : chiffre d'affaires,
commission de l'apporteur, net. Bilans du jour, de la semaine et du mois par commande, plus
un relevé PDF paginé à envoyer au comptable.

## Architecture

Un seul processus Python, permanent. Telethon maintient une connexion Telegram ouverte par
chauffeur, plus un bot partagé pour les alertes. L'état vit sur disque : les sessions, la
base SQLite, la dernière position de chaque chauffeur. Pas de serveur web, pas de file
d'attente, pas de conteneur.

Le pont refuse une session qui n'appartient pas au chauffeur déclaré, pour qu'aucune alerte
ne parte vers le bot d'un autre. Le géocodage est mis en cache et partagé : la même course
tombe chez tous les chauffeurs d'un groupe, et Nominatim n'est interrogé qu'une fois.

Cette forme exclut les plateformes serverless : il faut une machine allumée en continu avec
un disque persistant. Le dossier [`deploy/`](deploy/) contient de quoi l'installer sur un
VPS — unité systemd durcie, scripts de transfert, sauvegarde quotidienne vérifiée avec
copie hors du serveur.

| Fichier | Rôle |
| --- | --- |
| `vtc_bridge.py` | Le pont : parsing, géocodage, décision, alertes, commandes du bot. |
| `drivers.py` | Profils chauffeur, définition des réglages, règles de décision. |
| `add_driver.py` | Ajouter, lister, couper un chauffeur ; régler ses paramètres. |
| `store.py` | Accès SQLite : chauffeurs, courses, statuts, agrégats (tout filtré par chauffeur). |
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

## Ajouter un chauffeur

Le compte de `.env` (`TG_SESSION`, `NOTIFY_USER_ID`, `VTC_GROUPS`) devient le chauffeur
propriétaire au premier lancement, avec tout son historique. Pour les autres :

```
.venv/bin/python add_driver.py add
```

Le script connecte le compte du chauffeur (numéro, code reçu dans son Telegram, mot de
passe 2FA éventuel), liste ses groupes et enregistre ceux qu'il faut surveiller. Fais-le
avec le chauffeur à côté de toi. Ensuite, il ouvre le bot, envoie `/start` et partage sa
position en direct. Le pont le prend en compte sous une minute, sans redémarrage.

```
.venv/bin/python add_driver.py list
.venv/bin/python add_driver.py groups ID -100123,-100456
.venv/bin/python add_driver.py set ID min_price 30
.venv/bin/python add_driver.py disable ID
```

Un inconnu qui écrit au bot reçoit « Bot privé » et son identifiant Telegram, à te
transmettre.

## Position en direct

Le calcul de proximité a besoin de savoir où est chaque chauffeur. Il partage sa position
**en direct** depuis Telegram, dans le message privé du bot. Le pont suit les mises à
jour, qui arrivent sous forme d'éditions du message. Le groupe relais reste possible pour le
chauffeur d'origine (`FEED_GROUP`).

Telegram coupe ce partage au bout de huit heures. Sans position fraîche, les courses
immédiates sont ignorées et le journal le dit : `Position périmée`. Les réservations, elles,
continuent d'être traitées.

## Sécurité

`.env` et les fichiers `*.session` ne sont pas versionnés, et ne doivent jamais l'être :
une session Telethon donne un accès complet au compte Telegram associé. Avec plusieurs
chauffeurs, le serveur détient donc l'accès complet à chacun de leurs comptes : chaque
chauffeur doit en être informé et d'accord, et `state/sessions/` ne sort jamais du serveur.
Même chose pour `state/` et `logs/`, qui contiennent des adresses, des tarifs et des
messages de tiers.

## Licence

[MIT](LICENSE). Réutilisation libre, y compris commerciale, à condition de conserver la
mention de copyright et le texte de la licence. Le logiciel est fourni sans garantie.
