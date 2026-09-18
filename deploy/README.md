# Déploiement du pont VTC sur un VPS Hetzner

Le pont est un processus permanent : il maintient une connexion Telegram ouverte
(`client.run_until_disconnected()`) et garde son état sur disque (session Telethon,
base SQLite `state/courses.db`, `state/position.json`). Il lui faut donc une machine
allumée en continu avec un disque persistant — pas une plateforme serverless.

## Serveur

Un **CX23** (2 vCPU, 4 Go, 40 Go SSD, 7,19 €/mois TTC avec l'IPv4) suffit largement :
le pont est limité par les entrées-sorties réseau, pas par le calcul. La gamme CX est
en disponibilité limitée ; au 16 septembre 2026 seule la région **Nuremberg** en
proposait encore. Image : **Ubuntu 26.04 LTS** (Python 3.14, Telethon 1.45 validé).
Ajoute ta clé SSH à la création du serveur ; ne laisse pas l'authentification par mot
de passe active.

Vérifie que la clé proposée est bien celle de la machine qui déploiera. Compare les
empreintes : `ssh-keygen -E md5 -lf ~/.ssh/id_ed25519.pub` d'un côté, la colonne
*Fingerprint* de la console Hetzner de l'autre. Une clé dont tu n'as pas la partie
privée donne un serveur inaccessible, qu'il faut alors supprimer et recréer.

**Serveur en service : `vtc-bridge`, 2.28.121.51, Nuremberg.**

## Fichiers

| Fichier | Rôle |
| --- | --- |
| `server-setup.sh` | Préparation du serveur, à lancer une fois en root sur le VPS. |
| `first-sync.sh` | Transfert unique des secrets et de l'état depuis le Mac. |
| `deploy.sh` | Déploiement du code, à relancer à chaque modification. |
| `vtc-bridge.service` | Unité systemd : redémarrage automatique, logs dans journald, durcissement. |
| `backup.py`, `vtc-backup.service`, `vtc-backup.timer` | Sauvegarde quotidienne de la base, voir plus bas. |

## Séquence complète

Les trois scripts se lancent dans cet ordre, une seule fois pour les deux premiers.

### 1. Préparer le serveur

```
scp deploy/server-setup.sh root@VPS:/tmp/
ssh root@VPS bash /tmp/server-setup.sh
```

Crée l'utilisateur système `vtc`, l'arborescence `/opt/vtc-bridge`, l'environnement
virtuel Python, plafonne les journaux à 500 Mo et ferme tout sauf SSH en entrée.

### 2. Envoyer le code

```
VPS=root@1.2.3.4 ./deploy/deploy.sh
```

### 3. Transférer les sessions et l'état

Arrête d'abord le pont sur ton Mac : le script refuse de continuer s'il le voit tourner,
car copier une base SQLite en cours d'écriture donne un fichier incohérent.

```
VPS=root@1.2.3.4 ./deploy/first-sync.sh
```

> **Avertissement.** `vtc_session.session` donne un accès complet à ton compte Telegram.
> Ce fichier ne transite que par `scp`, jamais par mail, messagerie, stockage en ligne ou
> dépôt Git. Sur le serveur il est en mode 600, lisible par le seul utilisateur `vtc`.

### 4. Première connexion, à la main

Telegram voit une adresse IP inconnue et peut réclamer un code de confirmation, saisi
au clavier. Sous systemd il n'y a pas de clavier : le service tomberait en boucle de
redémarrage. Lance donc le pont une fois en SSH interactif.

```
ssh root@VPS
cd /opt/vtc-bridge
sudo -u vtc .venv/bin/python vtc_bridge.py
```

Garde ton téléphone à portée pour le code. Quand le log affiche `Pont VTC démarré`
et que les groupes remontent, arrête avec Ctrl-C.

### 5. Activer le service

```
VPS=root@1.2.3.4 ./deploy/deploy.sh
```

Le script installe l'unité systemd, l'active au démarrage et lance le pont. À partir
de là, chaque modification du code se déploie avec cette seule commande. `deploy.sh`
ne touche ni aux sessions, ni à `state/` : ces fichiers ne vivent que sur le serveur.

## Secrets : Infisical

Les variables d'environnement vivent dans Infisical, projet `vtc-bridge`, environnement
`prod`. C'est la source de vérité. Le `.env` du serveur n'en est qu'une copie, régénérée
à chaque `deploy.sh` :

```
infisical export --env=prod --format=dotenv   # sur le Mac, puis scp vers le serveur
```

Le pont lit un fichier local, pas Infisical directement. Ce choix est délibéré : si
Infisical est injoignable au démarrage, le pont démarre quand même. Seul un déploiement
a besoin du réseau vers Infisical.

Deux garde-fous dans `deploy.sh` avant d'écraser le `.env` de production : l'export doit
contenir au moins vingt variables et la clé `TG_API_HASH`. En dessous, rien n'est envoyé
et le déploiement s'arrête. `SKIP_SECRETS=1` saute l'étape pour déployer du code seul.

Pour changer un secret : le modifier dans Infisical, puis relancer `deploy.sh`. Le
`.env` local du Mac n'est plus utilisé pour la production ; il ne sert qu'à lancer le
pont en local.

Le fichier `.infisical.json` versionné ne contient que l'identifiant du projet, aucun
secret. Il évite de répéter `--projectId` à chaque commande.

## Exploitation

```
ssh root@VPS journalctl -u vtc-bridge -f      # logs en direct
ssh root@VPS systemctl status vtc-bridge      # état du service
ssh root@VPS systemctl restart vtc-bridge     # redémarrage
```

Le service redémarre seul en cas de plantage, dans la limite de 5 tentatives par
tranche de 5 minutes. Au-delà, systemd le laisse arrêté : c'est le signe d'un problème
de configuration, pas d'un incident réseau. Va lire le journal.

## Sauvegarde de la base

`state/courses.db` est la seule donnée irremplaçable. Un timer systemd en fait une
copie tous les jours à 4 h 30, installé par `deploy.sh` en même temps que le service.

| Fichier | Rôle |
| --- | --- |
| `backup.py` | Copie à chaud par l'API de sauvegarde SQLite, contrôle d'intégrité, gzip, rotation. |
| `vtc-backup.service` | Unité oneshot qui lance le script sous l'utilisateur `vtc`. |
| `vtc-backup.timer` | Déclenchement quotidien à 4 h 30, avec rattrapage si le serveur était éteint. |

Les archives vivent dans `/var/lib/vtc-bridge/backups/`, sous le nom
`courses-AAAA-MM-JJ.db.gz`, et les quatorze dernières sont conservées. Ce répertoire
vient de `StateDirectory=` : systemd le crée et l'attribue à `vtc` tout seul, et il est
hors de `/opt/vtc-bridge`, donc le `rsync --delete` de `deploy.sh` ne l'efface jamais.

Le pont n'a pas besoin d'être arrêté. La copie passe par l'API de sauvegarde SQLite,
qui produit un instantané cohérent pendant que le pont écrit, là où un `cp` donnerait
un fichier tronqué. La copie est ensuite relue avec `PRAGMA integrity_check` : au
moindre doute le service échoue, ce qui laisse une trace rouge dans le journal plutôt
qu'une archive muette et inutilisable.

```
ssh root@VPS systemctl list-timers vtc-backup    # prochaine exécution
ssh root@VPS journalctl -u vtc-backup -n 20      # comptes rendus
ssh root@VPS systemctl start vtc-backup.service  # sauvegarde immédiate
```

Pour restaurer, arrête le pont, décompresse par-dessus la base, redémarre :

```
systemctl stop vtc-bridge
gunzip -c /var/lib/vtc-bridge/backups/courses-2026-09-16.db.gz > /opt/vtc-bridge/state/courses.db
chown vtc:vtc /opt/vtc-bridge/state/courses.db
rm -f /opt/vtc-bridge/state/courses.db-wal /opt/vtc-bridge/state/courses.db-shm
systemctl start vtc-bridge
```

## Copie hors du serveur : Storage Box

Les archives locales sont sur le même disque que la base : elles couvrent la fausse
manœuvre, la corruption et la régression de code, pas la perte du serveur. Chaque
sauvegarde est donc recopiée dans la foulée sur un Storage Box Hetzner.

| | |
| --- | --- |
| Storage Box | `vtc-backups`, BX11, 1 To, Falkenstein — le serveur est à Nuremberg |
| Hôte | `u671222.your-storagebox.de`, utilisateur `u671222`, SSH sur le port **23** |
| Chemin | `vtc-bridge/` |
| Clé | `/var/lib/vtc-bridge/ssh/id_ed25519_storagebox`, née sur le serveur |
| Coût | 3,84 €/mois TTC |

La clé privée a été générée sur le serveur et n'en est jamais sortie. Le Storage Box
n'accepte que cette clé : ni mot de passe, ni SMB, ni WebDAV. *External Reachability*
est désactivé, donc il n'est joignable que depuis le réseau Hetzner — pas depuis
l'internet public, pas depuis le Mac. Si tu as besoin d'y accéder de l'extérieur un
jour, l'option se bascule dans la console.

L'envoi se fait en `rsync` **sans `--delete`**, volontairement : la rotation locale
efface au bout de quatorze jours, le Storage Box garde tout l'historique. À 131 Ko par
jour, cela fait moins de 50 Mo par an sur un téraoctet.

Le shell du Storage Box est restreint et refuse les commandes composées. D'où l'appel
`ssh … mkdir -p` séparé dans `backup.py` : la formule habituelle
`rsync --rsync-path="mkdir -p … && rsync"` échoue ici sur
`connection unexpectedly closed (0 bytes received so far)`.

Restauration depuis le Storage Box, sur le serveur, sous l'utilisateur `vtc` :

```
rsync -a -e "ssh -p 23 -i /var/lib/vtc-bridge/ssh/id_ed25519_storagebox" \
  u671222@u671222.your-storagebox.de:vtc-bridge/courses-2026-09-16.db.gz /tmp/
```

Puis la même séquence d'arrêt, décompression et redémarrage que ci-dessus.

## Agent launchd du Mac

Le pont tournait sur le Mac sous un agent launchd, `~/Library/LaunchAgents/com.vtc.bridge.plist`,
avec `KeepAlive` et `RunAtLoad` : il redémarrait tout seul, y compris après un `kill`.
Il a été renommé en `com.vtc.bridge.plist.disabled` au moment de la bascule.

Ne le réactive pas tant que le serveur tourne. Les deux processus partagent le même
fichier de session Telegram : les faire tourner en parallèle donne des alertes en double
et des déconnexions à répétition.

## Ce qui ne change pas

Ta position vient du partage de position en direct depuis ton téléphone, pas de
l'ordinateur : le déplacement vers le serveur n'y touche pas. Ce partage expire au
bout de 8 heures au maximum et reste à relancer à chaque journée de travail.

## Agenda : ce que Linux ne sait pas faire

L'ajout automatique des réservations dans l'app Calendrier passe par `osascript`, qui
n'existe que sur macOS. Sur le serveur, le self-test au démarrage écrit donc :

```
Agenda : self-test écriture bloqué (timeout/permission) : FileNotFoundError(2, 'No such file or directory')
```

C'est attendu, pas une panne. Le repli est déjà codé : l'alerte de réservation affiche
un bouton **📅 Fichier agenda** qui envoie un `.ics` avec les rappels 24 h et 1 h, à
ouvrir depuis l'iPhone. Un geste de plus, même résultat dans l'agenda.

## Limites des services de géocodage

Si `OSRM_URL` et `NOMINATIM_URL` pointent vers les instances publiques, elles imposent
un quota et exigent un `USER_AGENT` identifiable. Au-delà de quelques centaines de
requêtes par jour, héberge OSRM sur le même VPS avec l'extrait Île-de-France (environ
2 Go de mémoire) ou passe sur un fournisseur payant.
