# VTC-Bridge → SaaS : plan de conception validé

*Issu d'une revue structurée : Primary Designer → Skeptic → Constraint Guardian → User Advocate → Arbiter.
Version 4. Le Decision Log complet est en §8.*

---

## 0. Understanding Lock

**Problème verrouillé** : un outil personnel (userbot Telethon lisant des groupes VTC pour son seul propriétaire) doit devenir un service vendable à des chauffeurs VTC d'Île-de-France, sans violer les CGU Telegram ni le RGPD, et avec une valeur qui survive à la saturation des groupes.

**Contraintes gelées** — non renégociables sans reprise complète de la revue :

| # | Contrainte | Source |
|---|---|---|
| L1 | Aucune automatisation d'un compte utilisateur dans le SaaS | API ToS Telegram |
| L2 | Le bot n'écrit jamais à la place du chauffeur, ni dans un groupe | API ToS + acceptabilité admin |
| L3 | Aucun paiement de bien numérique dans le bot (sinon Telegram Stars obligatoires) | Bot Developer ToS |
| L4 | Le bot ne rejoint aucun groupe sans invitation de l'admin | Décision produit + ToS |
| L5 | Périmètre Île-de-France, société française existante, encaissement Stripe | Cadrage utilisateur |
| L6 | Une seule personne technique, qui conduit par ailleurs | Réalité opérationnelle |
| L7 | Axes de valeur retenus : rentabilité/décision, gestion/compta, réputation — **pas** la vitesse | Choix utilisateur |

**Hypothèses gelées, avec leur statut** :

| Hypothèse | Statut |
|---|---|
| Le partage de « live location » vers un chat privé de bot est possible | **Non vérifiée** — spike S0, bloquant |
| Le parser atteint ≥85 % sur les réservations réelles | **Non mesurée** — shadow mode, bloquant |
| Des chefs de groupe « entraide » acceptent le partenariat | **Non vérifiée** — 3 accords écrits exigés |
| Des chauffeurs paient ≥29 €/mois | **Non vérifiée** — 10 pré-commandes exigées |
| Marché : 10 000–20 000 chauffeurs actifs IDF, part active en groupes inconnue | **Non sourcée** — à falsifier en V0 |
| Churn mensuel 8 % | **Hypothèse de travail**, optimiste |
| Le géocodage se cache à >80 % ; le routing d'approche ne se cache pas | **Vérifiée par analyse** (origine différente par chauffeur) |
| Une AIPD est due | **Présumée due** — avis juridique avant la gate |

Aucune ligne de code produit ne s'écrit tant que les hypothèses bloquantes ne sont pas tranchées (§5.1).

---

## 1. Ce qu'on construit, en une phrase

**Un assistant Telegram qui dit au chauffeur VTC quelles réservations accepter et lesquelles refuser** — pas un outil pour répondre plus vite.

Ce recadrage n'est pas cosmétique. Sur une course **immédiate**, seul le premier à répondre gagne : équiper 40 chauffeurs du même groupe ne crée aucune valeur nette, ça déplace la loterie. Sur une **réservation**, la course reste disponible plusieurs minutes à plusieurs heures, la décision est économique et non réflexe, une erreur d'appréciation bloque un créneau entier, et personne aujourd'hui ne calcule le €/km net correctement. C'est le seul terrain où la valeur survit à la saturation du groupe.

L'alerte sur course immédiate existe, filtrée par zone, **sans aucune promesse de délai, écrite dans les CGV**.

---

## 2. Scénario A→Z

### Acte 1 — Le chef de groupe (semaines 1–3, avant tout code)

Tous les chefs de groupe ne se valent pas, et c'est la découverte la plus importante de la revue :

| Type de groupe | Comment il gagne sa vie | Attitude face au produit |
|---|---|---|
| **Dispatch commissionné** — il prend un % sur chaque course | Volume de courses couvertes | **Hostile.** Le produit apprend au chauffeur à refuser ses courses les moins payées, et rend sa marge visible. |
| **Entraide entre chauffeurs** — on repasse ce qu'on ne peut pas faire | Il conduit lui-même | **Aligné.** Il est son propre utilisateur. |
| Agence / société | Salaire ou marge | Neutre à positif si ça fiabilise ses sous-traitants. |

**Cible primaire du MVP : les groupes d'entraide.** Approcher un dispatcher commissionné en premier, c'est se faire retirer le bot au mois 2 quand il fera le lien entre l'outil et la baisse de son taux de prise.

Le chef reçoit un **kit de partenariat** — parce qu'il prend un risque social réel devant une communauté qu'il a mis des années à construire :
- un message d'annonce prêt à copier-coller, qui dit franchement que le bot lit les messages du groupe et pourquoi ;
- une page publique « qui est derrière, ce que le bot fait, ce qu'il ne fait pas », qu'il peut montrer ;
- une réponse prête à la question « et toi tu touches combien ? » ;
- **une alternative à la commission** : il peut être rémunéré en licences offertes plutôt qu'en cash, ce qui désamorce l'accusation de monétiser l'entraide.

Contrat d'apporteur d'affaires : 25 % à vie, non-contournement 12 mois, préavis de retrait du bot, mention explicite du privacy mode désactivé. **Il doit pouvoir facturer** (auto-entrepreneur suffit) ; sinon, rémunération en nature.

### Acte 2 — Le bot dans le groupe

Il est ajouté par l'admin, avec le **privacy mode désactivé** dans BotFather — condition technique pour lire le fil, et point de négociation à assumer, pas à cacher.

**Il ne poste jamais rien dans le groupe.** Ni alerte, ni pub, ni réponse. C'est ce qui le rend invisible pour l'anti-spam Telegram et non menaçant pour l'admin.

### Acte 3 — Le chauffeur s'inscrit (3 écrans, 90 secondes)

Il clique le lien épinglé → chat privé.

1. **Consentement** : CGU + politique de confidentialité, en français simple.
2. **Vérification automatique** qu'il est bien membre du groupe (`getChatMember`). Aucune saisie.
3. **Une seule question** : « Tu roules plutôt sur quel secteur ? » avec des choix cliquables.

**Aucun seuil €/km n'est demandé.** Un chauffeur raisonne « 60 balles pour Roissy, c'est correct », pas en ratio — lui demander un chiffre qu'il ne connaît pas, c'est garantir un mauvais réglage indiscernable d'un produit défaillant. À la place : les 3 premiers jours, il reçoit **tout ce qui passe dans son secteur**, et chaque alerte porte un `👍 / 👎`. Au bout de ~20 retours, le bot lui propose son seuil calculé : « d'après tes réponses, tu prends à partir de 1,80 €/km — je filtre là-dessus ? ». Le réglage devient un résultat, pas une question.

Ni carte bancaire, ni position, à ce stade.

### Acte 4 — Une réservation tombe

Message posté à 14h32. Le bot le parse **en mémoire**, géocode (cache d'abord), calcule le €/km net, et croise avec les abonnés **membres vérifiés de ce groupe**.

### Acte 5 — Le DM

```
💰 Réservation — demain 07h15
2,40 €/km net · 90 € · 25 km
CDG → Paris 8e

[texte original de la course, tel qu'écrit dans le groupe]

[📋 Copier « SP »]  [🔗 Ouvrir dans le groupe]
[👍]  [👎]  [⚠️ chiffre faux]
```

**Ce que les boutons peuvent et ne peuvent pas faire.** Aucune méthode de la Bot API ne permet à un bot d'envoyer un message au nom d'un utilisateur. La réponse automatique dans le groupe — ce que fait l'outil personnel actuel via `client.send_message` sur une session MTProto — **n'est pas transposable**, et c'est le seul élément du produit existant qui ne l'est pas. Trois mécanismes officiels restent disponibles :

| Mécanisme | Ce qu'il fait | Taps | Coût |
|---|---|---|---|
| **`copy_text`** (Bot API 7.11) — *défaut retenu* | Copie « SP » dans le presse-papier, un bouton URL ouvre le message dans le groupe, il colle et envoie | 3 | Aucun. Le groupe ne sait pas qu'il est équipé |
| **Mode inline** `switch_inline_query_chosen_chat` (Bot API 6.7) — *option activable* | Sélecteur de chat → requête pré-remplie → le message part **de son compte**, avec « via @bot » | 3 | Non cadrable techniquement — voir ci-dessous |
| **Callback** | 👍/👎 pour apprendre son seuil, « j'ai eu la course », « chiffre faux » — parlent au bot, jamais au groupe | 1 | Aucun |

Le lien ouvre le groupe **exactement sur le message** (`t.me/c/<groupe>/<message>`). En revanche aucun paramètre d'URL ne pré-arme le champ de saisie en mode réponse : le chauffeur fait le geste natif (swipe → répondre). La réponse citée n'est de toute façon pas obligatoire dans ces groupes.

**Pourquoi le mode inline n'est pas le défaut, et pourquoi ce n'est pas une question de stigmate.** Dans un groupe partenaire, la mention « via @bot » est un atout : le chef a introduit le bot et l'assume, la visibilité fait office de preuve sociale et de canal d'acquisition gratuit auprès des non-équipés. Le problème est technique : **une `InlineQuery` ne transporte pas l'identifiant du chat** — le bot reçoit `id`, `from`, `query`, `offset` et `chat_type` (« supergroup »), jamais lequel. Il en découle deux limites non contournables :

1. **Impossible de restreindre l'inline aux groupes partenaires**, ni même de détecter qu'il a servi ailleurs. Un abonné est membre de 4 à 6 groupes ; le bot n'en couvre que ceux qui sont signés. Rien ne l'empêche d'afficher « via @bot » sous les yeux d'un admin qui n'a jamais approuvé ce bot — le pire contexte de découverte possible pour une future négociation.
2. **Le sélecteur de chat ne peut pas être verrouillé sur un groupe** : `SwitchInlineQueryChosenChat` ne filtre que par type (user / bot / group / channel). Le chauffeur choisit dans une liste à chaque course, en conduisant, et une erreur de sélection poste « SP » dans un groupe où la course n'existe pas.

Le mode inline est donc **proposé, expliqué et activable par le chauffeur**, avec ces deux limites énoncées au moment de l'activation — pas imposé par défaut.

Trois décisions structurantes dans ce petit bloc :

Variante quand le prix est ambigu ou illisible :

```
💰 Réservation — demain 07h15
Prix à vérifier dans le message (« 90 client / 70 net »)
CDG → Paris 8e · 25 km
```

**Un chiffre affiché est un chiffre lu sans ambiguïté.** C'est la formulation unique de la règle : jamais d'estimation présentée comme un fait, jamais de valeur par défaut fabriquée. Tout ce qui n'est pas lu sans ambiguïté est remplacé par une phrase en français courant qui dit quoi vérifier — jamais par du jargon comme « rentabilité non calculable ». Les chiffres du premier exemple sont affichés **parce qu'ils sont sûrs** ; s'ils ne l'étaient pas, ils ne seraient pas là.

**Le texte original est conservé.** Le Constraint Guardian voulait le retirer (un DM est un stockage durable, hors de contrôle, chez Telegram). Le User Advocate a montré que sans lui, le chauffeur suit systématiquement le lien pour relire la course — le bot devient une notification de plus, et l'offre se vide. **Arbitrage retenu** : le texte est réémis, mais **uniquement vers un membre vérifié du groupe source**, qui a déjà cette donnée dans son propre client Telegram. On ne divulgue rien à personne de nouveau. Cette règle est absolue : la vérification d'appartenance conditionne la réémission.

**Chaque chiffre est vérifiable ou absent.** Jamais de valeur fabriquée. Quand le prix est ambigu (« 90 client / 70 net »), les deux sont affichés. Quand il est illisible, on écrit « prix à vérifier dans le message » — en français courant, jamais « rentabilité non calculable ».

**Un bouton « chiffre faux ».** Un chauffeur qui perd de l'argent sur une erreur de lecture ne fait plus jamais confiance à aucun chiffre affiché. Il lui faut un exutoire en 1 tap, et ce retour alimente directement la mesure du taux de parse.

### Acte 6 — Le bilan du soir

Chaque jour à 21h, un message de 3 lignes :

> Aujourd'hui : 47 courses vues dans tes groupes, 8 correspondaient à ton secteur, tu en as reçu 8.

Ça paraît anodin. C'est ce qui empêche le churn n°1 : **le silence ambigu**. Sans ce message, un chauffeur qui ne reçoit rien ne peut pas distinguer un marché calme d'un bot en panne, d'un abonnement expiré, d'un filtre trop serré. Il rouvre ses 6 groupes « pour vérifier » — et un outil d'alerte auquel on ne fait pas confiance en silence ne sert plus à rien. La commande `/status` donne la même information à la demande, et une panne détectée déclenche une notification automatique à tous les abonnés.

### Acte 7 — L'abonnement

**Essai 14 jours, sans carte, qui ne démarre qu'à la première alerte reçue** — une semaine creuse ne doit pas consumer l'essai. Rappel 48 h avant la fin. Paiement sur le web (Stripe), jamais dans le bot.

`/stop` dans Telegram propose « mettre en pause » ou « résilier », et **résilie réellement via l'API Stripe**. Un chauffeur qui a vécu 100 % de sa relation produit dans Telegram et qui est prélevé après avoir tapé `/stop` produit le pire récit possible dans un groupe fermé.

---

## 3. Architecture

```
Telegram webhook  (HTTPS + secret token + allowlist IP 149.154.160.0/20, 91.108.4.0/22)
   │
   ▼
Parser EN MÉMOIRE ──► objet Course + niveau de confiance par champ
   │
   ├─► Géocodage : API commerciale + cache Redis (clé = texte normalisé, TTL 90 j, hit > 80 %)
   │
   ├─► Routing : OSRM AUTO-HÉBERGÉ, extrait Île-de-France, service `table`
   │        1 appel = ETA de TOUS les abonnés éligibles. Coût marginal nul.
   │        ⚠️ CE BLOC N'EXISTE QUE SI LE SPIKE S0 RÉUSSIT (plan A).
   │           En plan B : pas d'ETA d'approche en V1, donc pas de routing,
   │           donc pas de VPS OSRM (voir §5.3).
   │
   ▼
Matching (abonnés du groupe source, membres vérifiés, secteur, seuil appris)
   │
   ▼
File d'émission : 25 msg/s global, ordre RANDOMISÉ, 429-aware
   │
   ▼
DM
```

**Pourquoi OSRM auto-hébergé** : le géocodage se cache (les mêmes 100 lieux reviennent en boucle), mais **le routing d'approche ne se cache pas** — son origine est la position de chaque chauffeur. En API payante, 1 course = N appels, et le coût devient superlinéaire en abonnés : à 150 abonnés, entre 113 € et 1 125 €/mois pour ce seul poste. OSRM auto-hébergé sur un extrait IDF ramène ce coût à zéro et fait les N chauffeurs en un appel via le service `table`. C'est la correction de conception la plus rentable du document.

### Ce qui est stocké, ce qui ne l'est pas

| Stocké (90 jours) | Jamais stocké |
|---|---|
| Codes postaux départ/arrivée | Texte brut du message |
| Prix, km, durée, catégorie | Adresses complètes avec n° de rue |
| Horodatage, id du groupe | Noms, téléphones, identités de passagers |
| Lien `t.me/c/…` (90 j) | **Identifiant de l'auteur, même haché** |
| Profil et historique de l'abonné | Historique de position |

Le texte brut n'est jamais stocké **en production**. Seule exception, encadrée et écrite : le corpus de test de 500 messages du §7, qui a sa propre base légale, son accord d'admin et sa durée.

L'identifiant d'auteur est supprimé parce qu'un hash salé est une **pseudonymisation**, pas une anonymisation, et que son usage produit n'est pas démontré. Cette suppression ne rend pas la base anonyme, et le document ne le prétend pas : le lien `t.me/c/…` reste un pointeur vers un contenu identifiant, résoluble par les seuls membres du groupe. On ne conserve donc **aucune donnée identifiante propre**, seulement un pointeur vers une donnée déjà détenue par Telegram et par les membres du groupe. Ce pointeur est soumis à la même rétention de 90 jours et doit être qualifié comme tel dans la LIA.

### Position du chauffeur

**Spike S0, 2 heures, avant toute autre ligne de code** : le partage de « live location » vers un chat privé **de bot** est-il possible sur iOS et Android ? La documentation Bot API expose bien `live_period` sur les `Location` reçus et les `edited_message`, mais aucune source officielle ne confirme que les clients offrent ce partage vers un bot. C'était une hypothèse tenue pour un fait dans la première version.

- **Plan A (si OK)** : position live, fraîcheur affichée dans chaque alerte.
- **Plan B (si KO)** : **on n'implémente pas l'ETA d'approche en V1.** Pas de relance toutes les 20 minutes — un chauffeur sur l'A86 ne re-tape pas sa position 30 fois par jour, et le harceler dégrade l'outil sans le casser franchement, ce qui est pire. Le produit tourne sur le €/km net et le secteur déclaré, ce qui suffit pour une réservation.

Dans les deux cas : position arrondie à une grille de 500 m avant tout appel externe.

### Limites dures

| Contrainte | Valeur |
|---|---|
| Appels routing externes par course | **0** (plan A) — sans objet en plan B |
| Appels géocodage par course | ≤2, cache d'abord |
| Timeout par appel externe | 800 ms → « ETA non disponible », jamais d'attente |
| p95 bout en bout, réservation | ≤5 s |
| Course immédiate | aucun engagement de délai |
| Émission | 25 msg/s global, ordre randomisé |
| Par abonné | ≤1 DM/10 s, ≤40/jour — **jamais par suppression** (voir ci-dessous) |
| Reprise après panne | immédiat abandonné au-delà de 3 min, réservation rejouée jusqu'à 30 min |
| Disponibilité visée | 99 %/mois, **aucun SLA contractuel supérieur** (pas d'astreinte) |
| Formats de groupe supportés en V1 | 6 maximum |

**Les plafonds ne suppriment jamais une course.** Un chauffeur qui découvre le soir une réservation à 280 € qu'il n'a pas reçue conclut que l'outil qu'il paie lui a fait rater 280 € — et le raconte au groupe. Quand le plafond est atteint, les courses sont **regroupées dans un digest** (« 3 courses pendant que tu étais occupé »), avec un compteur visible. Silencieusement jeter est interdit.

Fermeture du mécanisme : **le digest est un message, il compte donc dans les 40/jour.** Mais son contenu, lui, n'est pas plafonné — un digest peut porter 20 courses. Si le plafond de 40 messages est atteint, les courses restantes s'accumulent dans **un dernier digest de fin de journée**, envoyé quoi qu'il arrive et explicitement étiqueté « au-delà de ta limite quotidienne ». Aucune course éligible ne disparaît, quel que soit le volume.

### Sécurité

Token en secret manager, rotation documentée sous 2 h en cas de suspicion. Webhook avec secret token + allowlist IP, tout payload hors plage rejeté. **Isolation multi-tenant** : `getChatMember` à l'abonnement, revérification hebdomadaire ; une course n'est jamais délivrée hors de son groupe d'origine, et le regroupement de doublons ne franchit jamais cette frontière — envoyer une course du groupe A à un chauffeur du groupe B, c'est la fin du partenariat A. Destruction documentée de `vtc_session.session` et `vtc_bot.session` avant tout déploiement.

### Parsing hybride : regex d'abord, modèle en second rideau

Le parser regex seul plafonne le projet : D17 limite à 6 formats de groupe avec 2 à 6 h de maintenance hebdomadaire, ce qui est un **plafond de chiffre d'affaires**, pas seulement une contrainte technique. Et il ne voit pas du tout les courses postées en image ou en vocal.

```
message ──► regex (instantané, gratuit, déterministe)
              │
              ├── succès ──────────────────► objet Course
              │
              └── échec / image / vocal ──► caviardage (tel, noms)
                                                 │
                                                 ▼
                                        modèle rapide, sortie structurée
                                                 │
                                                 ▼
                                    VÉRIFICATION VERBATIM ──► objet Course
                                    (échec → champ INCONNU)
```

**La vérification verbatim est la pièce maîtresse.** Une regex qui échoue ne renvoie rien ; un modèle qui échoue renvoie une valeur plausible et fausse — exactement O3, et une violation frontale de D8. Parade : le modèle renvoie des **extraits littéraux**, et on vérifie que chaque chaîne extraite existe dans le message source. « Prix : 70 » n'est accepté que si « 70 » figure dans le texte. Sinon le champ passe en `INCONNU`. Déterministe, gratuit, et ça élimine l'essentiel du risque sur les deux champs qui coûtent de l'argent : prix et adresse.

**Économie.** ~422 messages de course/jour sur 6 groupes, soit ~12 650/mois. Tout passer au modèle : ~12 €/mois. Seulement les échecs (~15 %) : ~2 €/mois. Surtout, **le parsing est fait une fois par message quel que soit le nombre d'abonnés** — c'est un coût fixe, à l'inverse du routing (C3). À 20 groupes, ~40 €/mois face à 4 875 € de CA.

**Latence.** La regex reste sur le chemin rapide. Le chemin modèle a son propre budget (~5 s, au lieu des 800 ms de D32) : acceptable sur une réservation, qui est le cœur du produit ; sur l'immédiat, aucun délai n'est promis de toute façon.

**Contraintes de conformité, non négociables :**
- **Caviardage avant envoi** : téléphones et noms retirés par regex — c'est ce que les regex font bien. Le modèle n'a pas besoin du numéro du client pour extraire une adresse de départ.
- **Zéro rétention, zéro entraînement** chez le fournisseur. Les CGU Telegram interdisent explicitement d'entraîner un modèle sur les données de la plateforme.
- **Hébergement UE ou clauses contractuelles types**, DPA signé, sous-traitant nommé dans la LIA (§7).

**Effet secondaire précieux** : chaque passage par le modèle produit une paire (message → structure). C'est le corpus étiqueté qui permet de mesurer honnêtement le taux de parse et de durcir les regex sur les formats à haute fréquence — donc de faire baisser coût et latence avec le temps.

### Réutilisation du code existant

| Réutilisable après durcissement | À réécrire | À jeter |
|---|---|---|
| `expand_shortcuts`, `LANDMARKS`, `is_reservation`, `requires_van`, `extract_km`, logique €/km | `extract_price` (le `max()` renvoie systématiquement le prix client au lieu du net), `_clean`/`LEAD_JUNK` (mange les numéros de rue → géocodage faux et silencieux), `depart_time` (fabrique « Immédiat » par défaut), `course_fingerprint` (dédup destructrice en multi-tenant) | Telethon, relais Hermès, position par groupe, `escalate*` |

Stack : Python 3.11 / aiogram / Postgres / Redis / OSRM Docker / VPS français.

---

## 4. Plan de fonctionnalités

### V0 — Validation, zéro code produit (semaines 1–3)
Voir la gate en §5.

### V1 — MVP (semaines 4–13, ~10 semaines)
Bot lecteur sur ≤6 groupes, aucune écriture en groupe. Parser durci + mesure permanente du taux de parse + bouton « chiffre faux ». Alerte réservation avec €/km net, créneau, texte original, deep link. Alerte immédiate filtrée, sans promesse. Onboarding 3 écrans, seuil **appris** et non demandé. Bilan quotidien + `/status` + notification automatique de panne. Digest anti-plafond. Stripe web, essai 14 j sans carte démarrant à la première alerte, `/stop` qui résilie réellement. Base de connaissances publiée. Kit partenaire pour les chefs de groupe. Opérations manuelles assumées derrière.

### V2 — Rétention (semaines 14–24)
**Historique personnel** : « tu as fait 6 courses avec ce donneur d'ordre, 1 payée en retard » — sa donnée, son vécu, aucune notation de tiers. Carnet de courses : capture passive quand le fil du groupe permet d'inférer l'attribution, + confirmation groupée en fin de journée (un écran, tout cocher). Récap mensuel + export CSV compta. Dashboard chef de groupe **agrégé, jamais nominatif**.

La déclaration manuelle par course est écartée : un chauffeur qui ne tient pas de tableur ne tapera pas deux boutons quinze fois par jour. Un taux de déclaration réaliste est de 10–20 %, biaisé vers les mauvaises expériences.

### Repoussé, sans date
Heatmap personnelle, objectifs journaliers, détection d'arnaque, **réputation collective des donneurs d'ordre**. Aucune ligne de spec avant 100 abonnés payants **et** un avis juridique. Noter des personnes physiques sur leur solvabilité, sur la base de signalements anonymes non contradictoires, avec effet sur leur activité économique, c'est à la fois un traitement RGPD lourd et un risque de diffamation — et beaucoup de donneurs d'ordre sont précisément les chefs de groupe partenaires.

### Interdits permanents
Répondre à la place du chauffeur. Poster en groupe. Automatiser un compte utilisateur. Entraîner un modèle sur les messages. Réémettre du texte de tiers vers un non-membre du groupe source.

---

## 5. Plan économique

### 5.1 La gate — rien ne se code avant

| # | Critère | Seuil de passage |
|---|---|---|
| **1** | **Nature réelle des 6 groupes cibles : entraide ou dispatch commissionné ?** Lire 20 fils par groupe — qui poste, combien de posteurs distincts, part du volume des trois plus gros | **Tranché par groupe.** Si dispatch → D2 est faux et le modèle bascule (back-office vendu au dispatcher, chauffeur en canal gratuit) |
| 2 | Test d'attribution : un chef, un Google Form, 10 jours | **Taux de réponse mesuré à J1, J5, J10.** Sous 25 % → toute la V2 est replanifiée |
| 3 | Shadow mode sur corpus consenti, **regex + fallback modèle** (D48) | **≥ 95 % de parse combiné sur les réservations, erreur de prix < 3 % après vérification verbatim.** Mesurer séparément : taux regex seul (pilote le coût), part du flux en image/vocal, taux d'hallucination rejeté par la vérification verbatim |
| 4 | Entretiens chauffeurs (30 min) | 15 réalisés |
| 5 | Chefs de groupe du bon segment | 6 contactés, **3 accords écrits** |
| 6 | Test de prix (19 / 29 / 39 €) | **10 pré-commandes payées à ≥ 29 €** |
| 7 | Spike position S0 | tranché, plan A ou B acté |
| 8 | Avis juridique AIPD/LIA | rendu |

Le critère 1 est nouveau et il passe en tête : il conditionne D2, tout le back-office chef, et la source de données de la V2. Il coûte une demi-journée et aucun entretien n'y répondra mieux.

Coût : 3 semaines de ton temps + 1 500 à 4 000 € de juridique. **Si la gate échoue, le projet s'arrête ou change de forme.** C'est le meilleur rapport information/euro du plan : aujourd'hui, aucune hypothèse commerciale n'est vérifiée et le taux de parse réel n'a jamais été mesuré — le code actuel masque ses échecs en les escaladant vers un humain, toi.

### 5.2 Hypothèses, explicitement non sourcées
Chauffeurs VTC actifs en IDF : ordre de grandeur 10 000 à 20 000, **à vérifier sur registre public**. Part réellement active dans les groupes de dispatch : **inconnue**. Churn mensuel : hypothèse de travail à 8 %, déjà optimiste pour des indépendants à trésorerie tendue. Ces nombres sont à falsifier en V0, pas à présenter comme des prévisions.

### 5.3 P&L à 150 abonnés — en HT (39 € TTC = 32,50 € HT)

| Poste | Montant |
|---|---|
| CA HT (150 × 32,50 €) | **4 875 €** |
| Commission chefs de groupe (25 %) | −1 219 € |
| Stripe (~2,5 % du TTC) | −146 € |
| Infra fixe (VPS app + monitoring) | −60 € |
| VPS OSRM — **plan A uniquement** ; 0 € en plan B | −60 € |
| Géocodage (variable, ~3 000 appels/mois après cache) | −15 € |
| Parsing modèle (D48) — fixe, indépendant du nb d'abonnés | −12 € |
| Juridique / comptable amortis | −150 € |
| Support externalisé (17 h × 40 €) | −680 € |
| Maintenance du parser — **réduite par D48** (2 à 3 h/semaine au lieu de 2 à 6) | −250 € |
| **Marge avant ta rémunération** | **~2 283 €** |

- **Couverture des coûts fixes : ~34 abonnés.** Contribution marginale par abonné : ~19 € HT. Les coûts de parsing (D48) sont fixes et n'entrent pas dans la marge variable.
- **Avec 8 % de churn, tenir 150 abonnés impose d'en recruter 12 par mois, indéfiniment.** C'est du travail commercial, pas du développement, et c'est le vrai coût du modèle.
- À 150 abonnés, la marge avant rémunération (~2 145 €) est **inférieure à ce que rapporte une activité de conduite à plein temps**. Le projet ne remplace pas ton revenu avant plusieurs centaines d'abonnés. Il faut le savoir avant de commencer, pas au mois 8.

### 5.4 Support
SLA écrit dans les CGV, volontairement bas : réponse sous 24 h ouvrées, canal unique, **aucune assistance en temps réel**. Les pics de tickets tombent 6h–9h et 17h–21h, précisément quand tu conduis : c'est structurel. Une notification automatique en cas de panne supprime la majorité des tickets de week-end. La base de connaissances est publiée avant le premier euro encaissé.

---

## 6. Conformité Telegram

| Règle | Application |
|---|---|
| API ToS — « actions on behalf of users without consent » | Aucun userbot. Le bot n'écrit jamais à la place du chauffeur. |
| Bot ToS — anti-spam | Rien posté en groupe. DM aux seuls abonnés opt-in, plafonnés, `/stop` immédiat. |
| Bot ToS — biens numériques = Telegram Stars | **L'abonnement est vendu sur le web, jamais dans le bot.** Le bot n'affiche qu'un lien. Aucun paiement in-bot. |
| Bot ToS — politique de confidentialité obligatoire | Publiée, liée dans BotFather et à l'onboarding. |
| Content Licensing / AI Scraping Terms | Aucun entraînement de modèle sur les messages. Traitement transactionnel uniquement. |
| Privacy mode | Désactivé via BotFather, avec accord écrit de l'admin, mentionné au contrat. |
| Consentement de l'admin | Le bot ne rejoint aucun groupe sans invitation. |

## 7. Conformité RGPD

- **Le traitement en mémoire est un traitement** (Art. 4-2). L'absence de conservation réduit le risque, elle ne dispense d'aucune obligation : base légale, information, droits, registre, sécurité.
- **LIA écrite et datée** (intérêt légitime, Art. 6.1.f) avant mise en production.
- **AIPD présumée due** : collecte de données auprès de tiers sans contact avec eux + données de localisation + surveillance systématique d'un flux continu. Deux critères suffisent à déclencher la présomption. Tranchée par avis juridique **avant la gate**, et livrable bloquant si elle est due.
- **Aucun message brut persisté.** Réémission du texte original **conditionnée à l'appartenance vérifiée** au groupe source.
- Message d'information épinglé dans chaque groupe partenaire : présence du bot, finalité, contact.
- Rétention 90 jours sur l'objet normalisé. Position : mémoire seule. Aucune notation de personne physique.
- Registre des traitements, DPA avec les sous-traitants (Stripe, hébergeur, géocodeur), procédure d'effacement, mentions légales.

### Régime d'exception pour le corpus de test
Le shadow mode et la non-régression exigent des messages bruts — ce que le principe « aucun message persisté » interdit. Régime borné et écrit : volume plafonné à **500 messages figés**, **accord écrit des admins** des groupes concernés (pas de collecte silencieuse), anonymisation manuelle avant stockage (numéros, noms, adresses précises retirés), stockage séparé, accès nominatif, durée propre. En production, la mesure du taux de parse ne conserve que des compteurs, jamais de texte.

---

## 8. Decision Log

| # | Décision | Alternatives écartées | Rationale | Origine |
|---|---|---|---|---|
| D1 | Bot API officiel, zéro userbot dans le SaaS | Userbot multi-clients hébergé ; hybride dès le MVP | Bans en cascade + « actions on behalf of users » explicitement interdit par l'API ToS | Design v1 |
| D2 | Distribution via chefs de groupe, **segment entraide d'abord** | Toute catégorie de chef indifféremment | Le dispatcher commissionné est structurellement hostile : le produit apprend à refuser ses courses | Skeptic O5 |
| D3 | Le bot ne poste jamais en groupe | Alertes publiques ; auto-réponse | Anti-spam Telegram + acceptabilité par l'admin | Design v1 |
| D4 | Réponse par deep link, jamais par le bot | Auto-réponse | Conformité. Coût assumé : quelques secondes de latence | Design v1 |
| D5 | Valeur = rentabilité + gestion, pas vitesse | Vitesse pure, licences plafonnées par groupe | La vitesse s'annule à saturation du groupe | Design v1 |
| D6 | **La réservation est le cœur du produit** | La course immédiate | L'immédiat est une loterie à somme nulle ; la réservation est une décision économique | Skeptic O8 |
| D7 | **L'alerte immédiate est maintenue en V1, sans promesse de délai et sans coût de dev propre** | La retirer entièrement du périmètre V1 | Elle réutilise le même parser et le même pipeline : son coût marginal est nul. Elle sert la découverte du produit pendant l'essai, sur des groupes où les réservations sont rares. Aucune ressource, aucun engagement, aucune ligne marketing ne lui est allouée — arbitrage explicite face à D6 | Skeptic O13 |
| D8 | **Un chiffre affiché est un chiffre lu sans ambiguïté** ; tout le reste devient une phrase en français courant | Affichage direct des valeurs extraites ; vocabulaire SÛR/ESTIMÉ/INCONNU | Un chiffre faux fait perdre de l'argent au client et détruit la confiance dans tous les autres. Le jargon de confiance, lui, se lit comme « l'appli ne sait pas » | Skeptic O3 / Advocate U6 |
| D9 | Aucun message brut persisté en production ; réémission conditionnée à l'appartenance vérifiée | Rétention 30 j ; suppression totale du texte du DM | Arbitrage C2 vs U1 — voir §9, avec ses deux limites écrites | Guardian C2 / Advocate U1 |
| D10 | Historique personnel, **pas** de réputation collective au MVP | Score public des donneurs d'ordre | AIPD + risque de diffamation + attaque directe les partenaires | Skeptic O11 |
| D11 | **Gate de validation avant tout dev** | Construire puis vendre | Aucune hypothèse marché vérifiée, aucun taux de parse mesuré | Skeptic O6 |
| D12 | **Routing OSRM auto-hébergé (plan A) ; API payante pour le seul géocodage** | Tout en API payante ; tout auto-hébergé dès J1 | Le routing d'approche ne se cache pas → seul poste superlinéaire (jusqu'à 1 125 €/mois à 150 abonnés). Sans objet si le spike S0 échoue (D25) | Guardian C3 |
| D13 | Stripe web, hors du bot | Telegram Stars | Ponction ~30 % ; contrainte : rien vendu dans le bot | Design v1 |
| D14 | Prix = hypothèse, tranchée par la V0 | 39 € décidé d'avance | Le prix est un résultat de test, pas une décision de design | Skeptic O15 |
| D15 | Identifiant d'auteur supprimé du MVP ; le lien `t.me` est conservé 90 j et qualifié comme pointeur identifiant | Hash + sel conservé ; suppression du lien | Pseudonymisation ≠ anonymisation. On ne conserve aucune donnée identifiante propre, seulement un pointeur vers une donnée déjà détenue par Telegram et les membres du groupe | Guardian C8 |
| D16 | Corpus de test sous régime d'exception écrit | Interdiction totale ; collecte silencieuse | Sans corpus, ni mesure de parse ni non-régression possibles | Guardian C10 |
| D17 | Plafond de 6 formats de groupe en V1 ; gel d'onboarding si parse < 80 % | Croissance libre du nombre de groupes | Rupture de maintenabilité au-delà de ~10 formats pour une personne seule | Guardian C11 |
| D18 | **Le seuil €/km est appris, jamais demandé** | Saisie à l'onboarding | Le chauffeur ne connaît pas son ratio ; un mauvais réglage est indiscernable d'un produit défaillant | Advocate U2 |
| D19 | **Les plafonds regroupent, ne suppriment jamais** ; le digest compte dans les 40/jour mais son contenu n'est pas plafonné, et un dernier digest part quoi qu'il arrive | Throttling silencieux | Une course ratée à 280 € détruit la relation et devient un récit toxique dans le groupe | Advocate U3 |
| D20 | **Bilan quotidien + `/status` + notification automatique de panne** | Silence par défaut | Le silence ambigu (marché calme ? bot mort ?) est le premier moteur de churn | Advocate U4 |
| D21 | **Essai 14 j sans carte, démarrant à la première alerte** | Essai 7 j avec carte | La carte avant toute valeur tue la conversion sur une population méfiante et à trésorerie tendue ; la volonté de payer est déjà testée par les pré-commandes de la gate | Advocate U8 |
| D22 | **`/stop` résilie réellement dans Telegram** | Résiliation sur le web uniquement | Prélever après un `/stop` produit le pire récit possible en communauté fermée | Advocate U12 |
| D23 | **Kit partenaire pour le chef de groupe, rémunération en nature possible** | Contrat seul | Il porte tout le risque social devant sa communauté et doit répondre à « tu touches combien ? » | Advocate U11 |
| D24 | **Bouton « chiffre faux » sur chaque alerte** | Retour par le support | Une erreur non signalable détruit la confiance dans tous les chiffres et n'est jamais mesurée | Advocate U7 |
| D25 | **Plan B position = pas d'ETA d'approche en V1** (donc pas de routing, pas de VPS OSRM) | Relance de position toutes les 20 min | Le partage de live location vers un bot n'est pas vérifié. Harceler un chauffeur au volant dégrade l'outil sans le casser franchement, ce qui est pire | Skeptic O1 / Advocate U5 |
| D26 | **Onboarding en 3 écrans, 90 secondes** ; position et carte différées | 7 écrans avec seuil, zone, position et carte | Il s'inscrit entre deux courses ; il n'a pas de « plus tard » dans sa journée | Advocate U9 |
| D27 | Réécriture de `extract_price` (le `max()` renvoie le prix client), `_clean`/`LEAD_JUNK` (mange les n° de rue), `depart_time` (fabrique « Immédiat ») | Durcissement incrémental | Trois bugs qui produisent des erreurs **silencieuses** et coûteuses au client | Skeptic O3, O4 |
| D28 | Réécriture de `course_fingerprint` : la dédup **regroupe** et ne franchit jamais la frontière d'un groupe | Empreinte globale en mémoire | En multi-tenant, l'empreinte actuelle supprime de vraies courses pour tous les abonnés, et fusionne des groupes aux droits différents | Skeptic O14 / Guardian C7 |
| D29 | **REJET** de la déclaration manuelle par course → capture passive + confirmation groupée du soir | Déclaration en 2 taps par course | Taux de déclaration réaliste 10–20 %, biaisé vers les mauvaises expériences : le carnet et la réputation ne peuvent pas reposer dessus | Skeptic O10 |
| D30 | Dépendance partenaire : ≥6 groupes, aucun > 25 %, dashboard agrégé jamais nominatif, non-contournement 12 mois | Exclusivité avec un gros groupe | Le risque de défection du partenaire est réel et non éliminable : il se dilue, il ne se supprime pas | Skeptic O7 |
| D31 | File d'émission unique ≤25 msg/s, `429`/`retry_after`, **ordre de fan-out randomisé** | Envoi direct | Au-delà de ~30 msg/s le bot est limité ; et un ordre fixe avantagerait systématiquement les mêmes abonnés | Guardian C1, C4 |
| D32 | Timeout 800 ms par appel externe → dégradation ; p95 ≤5 s en réservation ; **aucun engagement de délai sur l'immédiat, écrit aux CGV** | Attente jusqu'à réponse | Mieux vaut une alerte incomplète à l'heure qu'une alerte complète en retard | Guardian C4 |
| D33 | Webhook ; état de dédup en Redis ; immédiat abandonné au-delà de 3 min, réservation rejouée jusqu'à 30 min ; 99 %/mois sans SLA supérieur | Polling ; rejeu intégral | Rejouer une course immédiate morte dégrade la confiance plus que ne rien envoyer. Pas d'astreinte = pas d'engagement | Guardian C5 |
| D34 | Token en secret manager (rotation < 2 h) ; webhook avec secret token + allowlist IP ; destruction documentée des `.session` | `.env` sur disque, webhook ouvert | Un token unique donne accès en lecture temps réel à tous les groupes partenaires | Guardian C6 |
| D35 | Isolation multi-tenant : `getChatMember` à l'abonnement + revérification hebdomadaire | Aucune vérification d'appartenance | Une course du groupe A délivrée à un chauffeur du groupe B met fin au partenariat A. Devient une mesure de conformité par D9 | Guardian C7 |
| D36 | AIPD et LIA tranchées par avis juridique **avant** la gate ; 1 500–4 000 € inscrits au budget | AIPD traitée en V2 | Collecte auprès de tiers + géolocalisation + surveillance systématique : deux critères suffisent à la présomption | Guardian C9 |
| D37 | P&L en HT ; SLA support 24 h ouvrées, aucune assistance temps réel ; base de connaissances avant le premier euro | 39 € traités comme du net ; support réactif | La TVA manquait (−975 €/mois à 150 abonnés) et les pics de tickets tombent pendant les heures de conduite | Guardian C12 / Advocate U10 |
| D48 | **Parsing hybride : regex d'abord, modèle sur les échecs + images + vocaux, avec vérification verbatim obligatoire** | Regex seule ; tout au modèle | Lève le plafond de D17 (6 formats = plafond de CA) et ouvre le flux image/vocal invisible aujourd'hui. Coût fixe ~12 €/mois, indépendant du nombre d'abonnés. La vérification verbatim est ce qui rend l'usage compatible avec D8 : un modèle qui échoue invente, une regex qui échoue se tait | Question utilisateur, post-conseil |
| D49 | **Le seuil de 85 % de parse cesse d'être un critère d'arrêt du projet** ; il devient un objectif de coût et de latence, le seuil combiné regex+modèle visant ≥ 95 % | Seuil regex à 85 % comme go/no-go | Conséquence directe de D48 : le risque d'échec du projet sur le parsing disparaît. C'est le dérisquage le plus important de toute la revue | Question utilisateur, post-conseil |
| D50 | **Caviardage des téléphones et noms avant tout envoi au modèle ; zéro rétention, zéro entraînement, sous-traitant UE nommé dans la LIA** | Envoi du message brut | Le modèle est un nouveau sous-traitant recevant des données personnelles de tiers. L'argument d'intérêt légitime repose sur l'absence de donnée identifiante conservée — il ne survit pas à une transmission non caviardée. Et les CGU Telegram interdisent l'entraînement sur les données de la plateforme | Question utilisateur, post-conseil |
| D40 | **Nature des groupes (entraide vs dispatch) = critère n°1 de la gate** | Supposer le segment | Sans acteur qui attribue, B1, C1–C5, F1, F2, A6, P2–P7 n'ont aucune source de données. D2 et le §1.2 de PLAN_FONCTIONNEL s'excluaient mutuellement sans que personne ne l'arbitre | Conseil, unanimité |
| D41 | **P1 (le chef poste via le bot) coupé** ; P2 survit en DM privé, testé à la main avant tout dev | Formulaire structuré publiant dans le groupe | Un formulaire perd contre un copier-coller de 5 s ; poster fonde l'autorité du chef. D3 reste intact | Conseil, unanimité |
| D42 | **P4 (score nominatif des chauffeurs) jamais construit** ; P6 le remplace | Score encadré par un DPA | Le droit était réglé, le sujet était ailleurs : vendre au chauffeur l'outil qui alimente le score dont son chef se sert pour l'écarter inverse le bouche-à-oreille | Conseil, 4/5 |
| D43 | **A2 (€/heure) reporté en V2** ; la V1 affiche €/km net + temps total mobilisé | A2 en titre de la V1 | D25 supprime la jambe d'approche en plan B et le temps de retour est modélisé : D8 interdit de mettre ça en titre | Conseil, 3/5 |
| D44 | **Dédup inter-groupes = fonction produit visible**, pas correctif de `course_fingerprint` | Traitement silencieux | 27,9 % du flux mesuré. « Déjà vue il y a 6 min ailleurs » est l'information la plus fréquemment utile du produit | Conseil + mesure |
| D45 | **F1 avec repli par inférence du fil**, indépendant du chef | F1 dépendant de P2 | Supprime le point de défaillance unique : si le chef cesse de répondre, F1 survit en probabilité — jamais en affirmation | Conseil, meilleure idée isolée |
| D46 | **6 réglages en V1, pas 35** ; périmètre V1 réduit à ~10 fonctions | Catalogue complet en V1 | Chaque réglage est une surface de support pendant les heures de conduite. La V1 précédente représentait ~1 an de travail, pas 10 semaines | Conseil, unanimité |
| D47 | **Retirés : §3.5 (vendre au chef B les données du groupe A), D4 exposé, repositionnement à vide** | Les conserver en V3 | Le premier trahit le canal, le deuxième classe tes distributeurs devant leurs chauffeurs, le troisième est autodestructeur et indéfendable sous D8 | Conseil |
| D39 | **Réponse assistée par `copy_text` + lien vers le message ; mode inline proposé et activable, jamais imposé** | Réponse automatique par le bot (impossible : aucune méthode Bot API n'écrit au nom d'un utilisateur) ; inline par défaut ; inline exclu | **Objection initiale corrigée** : dans un groupe partenaire, le « via @bot » est un atout de preuve sociale, pas un stigmate. La vraie limite est technique — une `InlineQuery` ne transporte pas l'id du chat, donc l'usage ne peut être ni restreint aux groupes partenaires ni détecté, et le sélecteur ne peut pas être verrouillé sur un groupe (risque d'envoi dans le mauvais groupe) | Question utilisateur, post-arbitrage |
| D38 | Heatmap, objectifs, détection d'arnaque et réputation collective **repoussés sans date** | Spécification dès la V3 | Features conçues avant le premier client payant, dont plusieurs exigent des données historiques inexistantes | Skeptic O13 |

---

## 9. Conflit arbitré : le texte original dans le DM

**Guardian (C2, REJET)** : le DM republie le texte brut d'un message de groupe. Un DM Telegram est un stockage durable, hors du contrôle de l'opérateur. L'argument de conformité de tout le design (« aucune donnée personnelle conservée ») tombe si un nom ou un téléphone de client transite dans un DM permanent.

**Advocate (U1, BLOQUANT)** : sans le texte original, le chauffeur suit systématiquement le lien pour relire la course — les métadonnées que le parser ne capte pas (bagages, langue du client, conditions de paiement, retour à vide) décident de son acceptation. Au bout de trois jours, le bot n'est plus qu'une notification supplémentaire et l'offre se vide.

**Arbitrage** : le texte est réémis, **conditionné à l'appartenance vérifiée au groupe source**. Le destinataire dispose déjà de ce message dans son propre client Telegram ; la réémission ne divulgue rien à personne de nouveau et ne crée pas d'audience supplémentaire. La règle d'isolation multi-tenant devient de ce fait une **mesure de conformité**, pas seulement une règle produit — si elle casse, la conformité casse avec elle.

Deux limites de cet arbitrage, à écrire noir sur blanc plutôt qu'à laisser dans l'implicite :

1. **La garantie d'appartenance est hebdomadaire, pas instantanée.** `getChatMember` est appelé à l'abonnement puis revérifié chaque semaine ; entre deux vérifications, un chauffeur exclu du groupe peut continuer à recevoir du texte de tiers pendant jusqu'à 7 jours. Le mot « absolu » ne s'applique donc pas à la fenêtre de vérification. Alternative si la LIA la juge insuffisante : vérification au moment de l'émission, au prix d'un appel API par alerte.
2. **Risque résiduel assumé : la copie en DM n'est pas effaçable.** Si l'auteur supprime son message dans le groupe, la copie réémise en message privé survit et échappe à la procédure d'effacement du §7. C'est le cœur de l'objection C2 et il n'est pas éliminable techniquement — le DM est chez Telegram, hors du contrôle de l'opérateur. Ce risque doit être **nommé et accepté explicitement dans la LIA**, avec sa mesure d'atténuation : rétention 90 jours côté serveur, et suppression du DM par le bot lorsqu'il détecte la suppression du message source dans le groupe, dans la limite des 48 h où l'API le permet.

---

## 10. Prochaine étape concrète

**Jour 1 — la demi-journée qui décide du reste (critère n°1).**
Ouvrir chacun des 6 groupes cibles, lire 20 fils : qui poste, combien de posteurs distincts, quelle part du volume vient des trois plus gros, et est-ce que quelqu'un dit publiquement qui a eu la course. Le résultat tranche entre deux produits différents — assistant du chauffeur dans un groupe d'entraide, ou back-office vendu au dispatcher. Tout le reste en dépend.

**Jour 1 aussi** : le spike position (2 h). Deux heures qui décident si l'ETA d'approche existe.

**Semaine 1** : demander à un chef de répondre « qui l'a eue ? » pendant 10 jours, sur un simple formulaire · devis avocat RGPD · prise de contact avec les 6 chefs.

**Semaines 2–3** : entretiens chauffeurs, corpus de parse, test de prix.

Rien d'autre. **Pas une ligne de code produit avant que la gate §5.1 soit franchie** — et le critère n°1 se répond avant tous les autres, parce qu'un « dispatch commissionné » invalide D2 et une bonne partie du plan fonctionnel.
