# Plan fonctionnel — ce que le bot doit faire pour vraiment augmenter les revenus

*Complément à [PLAN_SAAS.md](PLAN_SAAS.md), qui reste le design validé (architecture, économie, conformité).
Révisé après passage en conseil (5 avis indépendants + relecture croisée à l'aveugle). Les arbitrages du conseil sont intégrés ; ce qui reste à trancher est en §8.*

---

## 0. Ce que le flux réel dit — mesuré, pas supposé

140 922 lignes de log sur 6 groupes, plusieurs mois. **C'est la première fois que ce projet raisonne sur des chiffres observés plutôt que sur des hypothèses.**

| Mesure | Valeur | Ce que ça change |
|---|---|---|
| Messages de course traités | 5 483 | |
| **Doublons inter-groupes** | **1 532 → 27,9 %** | Plus d'un message sur quatre est la même course reposée ailleurs. La dédup n'est pas un détail d'implémentation, **c'est une fonction produit** — et elle n'est dans aucun catalogue |
| Immédiates | 3 073 (56 %) | |
| Réservations | 789 (14 %) | |
| **Immédiates écartées : trop loin** | **2 610 sur 3 073 → 85 %** | Le flux brut est dominé par l'immédiat, mais l'écrasante majorité est hors de portée |
| **Alertes réellement envoyées** | **1 164** — dont **701 réservations (60 %)** et 463 immédiates | **Dans ce que le chauffeur reçoit, la réservation est majoritaire.** D6 (la résa au cœur du produit) est confirmé, pas infirmé |
| Réponses effectivement envoyées | 75 sur 1 164 alertes → **6,4 %** | À creuser en entretien : est-ce du filtrage sain, ou des alertes qui n'aboutissent pas ? |
| Forme de ces réponses | 44 en minutes · 21 « ok » · 10 « SP » | **Attention : ce sont les suggestions du bot actuel**, `reply = "SP" if eta_s < SP_UNDER_SECONDS else str(prox)`. Pas une observation du groupe. À vérifier en lisant 20 fils réels |
| Échecs de parse assumés | 89 | Le taux de parse réel reste non mesuré — la gate le mesurera |

**Deux conclusions directes :**

1. **La dédup inter-groupes entre au MVP comme fonction visible** (« déjà vue il y a 6 min dans un autre groupe »), pas comme correctif de `course_fingerprint`.
2. **Le tri par distance est déjà l'essentiel de la valeur** : 85 % des immédiates sont écartées avant même de parler de rentabilité. Le filtrage (A1), classé « sans valeur défendable », est ce qui fait le gros du travail — non-défendable stratégiquement, indispensable commercialement.

---

## 1. Quatre constats structurels

### 1.1 Le €/km est la mauvaise métrique, et il fait perdre de l'argent

C'est la métrique que tout le monde utilise dans ces groupes, y compris ton code actuel. Elle est trompeuse parce qu'elle ignore le temps.

| Course | Prix | Distance | €/km | Temps mobilisé (approche + course + retour) | **€/heure** |
|---|---|---|---|---|---|
| A — Levallois → Paris 8e | 24 € | 8 km | **3,00 €/km** 🟢 | 15 + 20 + 10 = 45 min | **32 €/h** |
| B — Paris 15e → Deauville | 190 € | 200 km | **0,95 €/km** 🔴 | 20 + 120 + 120 à vide = 4h20 | **44 €/h** |
| C — CDG → Paris 8e | 65 € | 30 km | **2,17 €/km** 🟠 | 25 + 45 + 15 = 1h25 | **46 €/h** |

Au €/km, l'ordre est A > C > B. Au €/heure, c'est C > B > A — **exactement l'inverse pour A**. Un chauffeur qui optimise le €/km enchaîne des petites courses « rentables » et finit sa journée à 32 €/h, en croyant bien faire.

Le €/heure mobilisée, c'est la métrique que le bot peut calculer et que personne ne calcule. C'est ça, le produit. Et c'est structurellement non-concurrentiel : que 5 ou 500 chauffeurs du groupe l'aient, la course B reste meilleure que la course A pour chacun d'eux.

**Ce qu'il faut pour la calculer** : le prix (déjà), la distance course (déjà), la durée avec trafic (déjà), le temps d'approche (spike S0), et le **temps de retour vers une zone où il peut recharger** — c'est le morceau manquant, et il est calculable : distance du point de dépose vers la zone d'activité déclarée du chauffeur.

Le raffinement suivant, c'est de déduire les coûts réels : carburant/recharge, péage, et son coût de revient kilométrique. Paris → Deauville, c'est ~25 € de péage et ~35 € de carburant aller-retour. Les 190 € deviennent 130 €, et le 44 €/h devient 30 €/h. La course B redevient médiocre — mais pour la bonne raison, pas parce qu'un ratio arbitraire l'a classée rouge.

> **Arbitrage du conseil — A2 ne peut pas être le titre de la V1.** Le raisonnement ci-dessus est juste, la mise en œuvre immédiate ne l'est pas, pour une raison interne au plan : **D25** supprime le temps d'approche en plan B, et le temps de retour n'est pas une mesure mais **un modèle** reposant sur une hypothèse comportementale non validée (qu'il rentre à vide vers sa zone). Or **D8 interdit d'afficher un chiffre qui n'est pas lu sans ambiguïté**. On ne peut pas invoquer D8 pour masquer un prix ambigu et mettre en gros titre un chiffre à deux jambes modélisées.
>
> **V1 affiche donc : le €/km net réel (A4 + A3) et le temps total mobilisé en minutes.** Deux valeurs vérifiables, dans le vocabulaire du métier. Le temps en minutes fait déjà une partie du travail pédagogique — « 24 € / 45 min » contre « 65 € / 1h25 » se compare sans qu'on ait à imposer une métrique nouvelle.
>
> **A2 devient le titre du produit en V2**, quand S0 est tranché et que le €/h réel constaté (D2) permet de calibrer l'estimation sur du vécu au lieu d'un modèle.

### 1.2 Le chef de groupe n'est pas un canal de distribution, c'est la source de données

La revue avait acté deux impasses :
- **O10** : la déclaration manuelle par le chauffeur plafonne à 10–20 %, donc le carnet, les commissions et la réputation reposent sur du vide.
- **O2 / C11** : le parser regex sur du langage humain est un puits de maintenance, avec une gate à 85 % qui peut échouer.

Les deux se résolvent au même endroit : **quelqu'un sait qui a eu la course.** Reste à savoir qui, et c'est là que le conseil a trouvé le défaut le plus grave du plan.

> ### ⚠️ Contradiction non arbitrée — le point le plus important de ce document
>
> **D2** (PLAN_SAAS) fait des groupes d'**entraide** la cible primaire, *précisément parce que* le dispatcher commissionné est structurellement hostile au produit.
>
> Le raisonnement ci-dessus décrit un **dispatcher** : quelqu'un qui reçoit les courses, les poste, les attribue, et prend une commission. Dans un groupe d'entraide, celui qui poste est un chauffeur quelconque qui repasse une course qu'il ne peut pas faire, et **personne n'attribue** — chacun se débrouille.
>
> **Les deux ne peuvent pas être vrais.** Et sans un acteur qui attribue, **B1, C1–C5, F1, F2, A6, D1, D2 et P3–P7 n'ont aucune source de données** : toute la V2 est suspendue à cette question.
>
> **Elle devient le critère n°1 de la gate** (§8). Elle ne se résout pas par trois semaines d'entretiens mais par une demi-journée d'observation : dans chacun des 6 groupes cibles, *qui poste, combien de posteurs distincts, quelle part du volume vient des trois plus gros*.

**P1 (le chef poste via le bot) est coupé** — à l'unanimité du conseil. Un formulaire à 6 champs perd systématiquement contre un copier-coller de 5 secondes, et poster est l'acte qui fonde l'autorité du chef dans son groupe : l'uniformiser, c'est le dépersonnaliser devant sa communauté. En prime, P1 faisait sauter D3 et rouvrait le risque anti-spam. Bénéfice attendu : incertain. Coût : certain.

**P2 survit sous une forme qui ne touche à rien : un DM au chef.** Le bot lit déjà le fil, il voit qui a répondu. Vingt minutes après le post, il envoie en privé : *« CDG → Paris 8e de 14h32 — c'est parti à qui ? [Karim] [Sofiane] [Mehdi] [hors groupe] [personne] »*. Un tap, en privé, aucun changement d'habitude, D3 intact.

**Mais ça se teste à la main avant d'être codé.** Un chef, un Google Form, dix jours. Son taux de réponse à J1, J5 et J10 décide de toute la V2 — un chef qui répond au premier tap et plus au vingtième ramène le projet exactement sur O10. C'est deux semaines de dev économisées sur une hypothèse invérifiée.

### 1.3 Le back-office du chef est la vraie défense contre « le partenaire devient concurrent »

La revue avait classé O7 comme « réel et non éliminable », avec pour seule parade une clause de non-contournement à 12 mois — c'est-à-dire rien.

Un chef de groupe qui gère ses commissions, ses impayés et la fiabilité de ses 40 chauffeurs dans ton outil ne peut plus partir : **son historique est dedans**. Le coût de sortie change de camp. Et ça règle aussi le problème d'alignement (O5) : tu ne lui vends plus une commission de 10 €/mois par filleul face à une menace sur son activité, tu lui donnes l'outil de gestion qu'il n'a pas, et qui lui fait gagner de l'argent directement.

### 1.4 ~~Le repositionnement à vide~~ — constat gardé, fonction retirée

Tout ce qui précède l'aide à **choisir entre des courses**. Mais le vrai problème d'une journée n'est pas de choisir entre deux courses : c'est le trou de 10h20 à 13h où rien de correct ne passe. Sur ce créneau — celui qui plombe son €/heure de la journée — le produit tel que conçu ne dit rien. Il attend.

Or le bot est le seul à voir le **flux complet de tous les groupes partenaires**, sur des mois. Aucun chauffeur ne voit ça : il ne lit que ce qui passe pendant qu'il regarde, dans les groupes où il est, quand il n'est pas en course.

Ce qui manque, c'est **le repositionnement à vide** :

> Il est 10h20, tu es à Levallois.
> Sur ce créneau, 40 % des courses partent de La Défense / Neuilly — à 12 min de toi.
> Les 3 dernières y sont parties en moins de 2 minutes.

> **Retiré du plan après passage en conseil — pas repoussé.** Deux membres ont démontré que cette fonction ne tient pas : elle est **autodestructrice** (elle sature exactement ce qu'elle recommande, dès que plusieurs abonnés d'un même groupe la suivent) et **statistiquement indéfendable** sur 6 groupes — « 40 % des courses partent de La Défense » est un numérateur sans dénominateur d'offre, et donne un chiffre plausible et faux, le pire cas sous D8.
>
> Le constat de départ reste vrai : le trou de 10h20 à 13h est le vrai problème de la journée, et le bot est le seul à voir le flux complet. Mais on n'a pas de réponse honnête à lui donner, et une réponse malhonnête coûte plus cher que le silence. Ce qu'on peut faire à la place, sans rien inventer : lui montrer **ses propres** créneaux et zones rentables une fois qu'il a trois mois d'historique (D3, V3).

---

## 2. Ce dont un chauffeur VTC a réellement besoin

Marquage : 🟢 non-concurrentiel (garde sa valeur même si tout le groupe l'a) · 🔴 somme nulle · **Faisabilité** : ✅ données disponibles · ⚠️ dépend du spike S0 ou du chef · ❌ pas de source fiable.

### A — Décider quelle course prendre

| # | Fonction | Type | Faisab. | Note |
|---|---|---|---|---|
| A1 | Filtrage secteur / véhicule / horaire / seuil — proposé par apprentissage, réglable à tout moment (§3) | 🔴 | ✅ | La base. Sans valeur défendable seule. |
| A2 | **€/heure mobilisée** (approche + course + retour + attente) | 🟢 | ⚠️ | **Le cœur.** Voir §1.1. |
| A3 | Coût de revient personnalisé (leasing, assurance, conso, entretien) → seuil de rentabilité en €/h | 🟢 | ✅ | 4 champs dans `/reglages`, jamais à l'inscription. Transforme tous les autres chiffres. |
| A4 | Péage + carburant/recharge déduits du prix | 🟢 | ✅ | Péages IDF↔province connus ; conso × prix carburant. Change radicalement les longues distances. |
| A5 | Probabilité de recharger au point de dépose | 🟢 | ⚠️ | Calculable sur l'historique des courses vues par le bot dans ce secteur. Mûrit avec le temps. |
| A6 | **Conflit d'agenda sur les réservations** | 🟢 | ✅ | Accepter deux résas incompatibles est la faute la plus coûteuse du métier. Détection triviale une fois le carnet en place. |
| A7 | Coût d'opportunité (« cette résa bloque ton créneau 17h–20h, le plus rentable de ta semaine ») | 🟢 | ⚠️ | Demande son historique. Très fort en V2+. |
| A8 | Conditions de paiement lues et affichées (net / PAB / fin de mois / lien) | 🟢 | ✅ | Souvent noyé dans le texte. Décisif pour la trésorerie. |

### B — Ne pas se faire avoir

| # | Fonction | Type | Faisab. | Note |
|---|---|---|---|---|
| B1 | Historique personnel avec chaque donneur d'ordre (payé / retard / impayé) | 🟢 | ⚠️→✅ | Passe de ⚠️ à ✅ dès que le chef attribue via le bot (§1.2). |
| B2 | Signal collectif anonymisé sur les donneurs d'ordre | 🟢 | ⚠️ | **Reste le point juridique.** Voir §5. |
| B3 | Détection d'anomalie (prix aberrant, donneur inconnu, adresse floue, écart au marché) | 🟢 | ✅ | Le bot voit tout le flux : il connaît le prix normal d'un CDG→Paris mieux que quiconque. |
| B4 | Trace horodatée de la course acceptée (capture du message + heure) | 🟢 | ✅ | Preuve en cas de litige. Coût de dev ~nul, valeur perçue forte. |

### C — Se faire payer

| # | Fonction | Type | Faisab. | Note |
|---|---|---|---|---|
| C1 | Carnet de courses automatique | 🟢 | ⚠️→✅ | Idem B1 : dépend de l'attribution par le chef. |
| C2 | Commissions dues / réglées, par donneur d'ordre | 🟢 | ⚠️→✅ | Le vrai point de douleur : personne ne sait où il en est. |
| C3 | Échéancier des encaissements attendus | 🟢 | ⚠️ | « 1 240 € attendus dont 380 € en retard ». |
| C4 | Relance d'impayé rédigée par le bot, envoyée par le chauffeur | 🟢 | ⚠️ | Le bot rédige, le chauffeur valide et envoie. Jamais d'envoi automatique. |
| C5 | Export compta / récap mensuel (CA, commissions, charges, km) | 🟢 | ⚠️ | Ancre de rétention : ses données de l'année sont dedans. |

### D — Piloter son activité

| # | Fonction | Type | Faisab. | Note |
|---|---|---|---|---|
| D1 | CA jour / semaine / mois vs objectif | 🟢 | ⚠️ | |
| D2 | **€/heure réel constaté** (vs estimé) | 🟢 | ⚠️ | Il découvre ce qui le paie vraiment. Souvent contre-intuitif. |
| D3 | Ses meilleurs créneaux et zones, sur ses propres données | 🟢 | ⚠️ | Ne vaut rien avant ~3 mois d'historique. |
| D4 | **Comparatif entre groupes** : lequel te rapporte le plus | 🟢 | ✅ | Politiquement sensible côté chefs. À garder privé au chauffeur. |
| D5 | Suivi des charges et du coût de revient | 🟢 | ✅ | |

### E — Répondre vite (assumé secondaire)

| # | Fonction | Type | Faisab. | Note |
|---|---|---|---|---|
| E1 | Notification filtrée + lien direct au message | 🔴 | ✅ | |
| E2 | Bouton copier la réponse / mode inline | 🔴 | ✅ | Voir PLAN_SAAS §2 Acte 5 et D39. |
| E3 | Réponse automatique | — | ❌ | **Impossible et interdit.** Aucune méthode Bot API n'écrit au nom d'un utilisateur. |

### F — Exécuter la course, une fois obtenue *(la zone morte du plan actuel)*

Entre « je réponds SP » et « je suis payé », il se passe deux heures pendant lesquelles le produit, tel que conçu jusqu'ici, ne sert à rien. C'est pourtant là que le chauffeur perd le plus d'argent.

| # | Fonction | Type | Faisab. | Note |
|---|---|---|---|---|
| F1 | **Notification d'attribution — et de non-attribution** | 🟢 | ⚠️→✅ | « Cette course a été attribuée à un autre. » Il arrête d'attendre et se remet en chasse. Aujourd'hui il reste en suspens sur 4 courses à la fois. Immédiat dès que P2 existe. |
| F2 | **Fiche course structurée** après attribution : nom, téléphone, adresse exacte, n° de vol, terminal, consignes | 🟢 | ⚠️ | Ce transfert se fait aujourd'hui en DM, en vrac. P1 étant coupé, la fiche se construit à partir de ce que le chauffeur reçoit et complète — pas d'une saisie du chef. |
| F3 | Bouton **« ouvrir dans Waze / Google Maps »** | 🟢 | ✅ | Recopier une adresse au volant est absurde et dangereux. Coût de dev : une URL. |
| F4 | **Suivi du vol / du train en temps réel** | 🟢 | ✅ | Un vol retardé de 2 h à CDG, c'est une demi-journée perdue s'il ne le sait pas. API vol ~0,001 €/appel. **Très forte valeur perçue, très faible coût.** |
| F5 | **Chrono d'attente + seuil facturable** par donneur d'ordre | 🟢 | ✅ | « Tu attends depuis 38 min ; chez ce donneur d'ordre l'attente est facturable au-delà de 30. » De l'argent que personne ne réclame. |
| F6 | **Assistant no-show** : chrono, rappel de la procédure, trace horodatée, photo | 🟢 | ✅ | L'indemnité de no-show est due mais rarement obtenue, faute de preuve constituée sur le moment. |
| F7 | Frais annexes rattachés à la course (parking aéroport, péage avancé, siège enfant) | 🟢 | ✅ | Systématiquement oubliés à la facturation. |

### G — Rester en règle

| # | Fonction | Type | Faisab. | Note |
|---|---|---|---|---|
| G1 | **Rappels d'échéances** : carte VTC, visite médicale, assurance, contrôle technique, macaron | 🟢 | ✅ | Une date oubliée = amende ou interdiction d'exercer. Une date + un rappel : coût de dev quasi nul, zéro risque de données, valeur perçue très élevée. |
| G2 | Justificatifs, kilométrage et charges pour la compta / l'URSSAF | 🟢 | ⚠️ | Prolonge C5. |

---

## 3. Configuration : rien à l'inscription, tout réglable ensuite

Deux contraintes validées semblent s'opposer. **U9/D26** : l'onboarding tient en 3 écrans, il abandonne au 4e. **U2/D18** : lui demander son seuil €/km à l'inscription garantit un mauvais réglage, indiscernable d'un produit défaillant. Et pourtant un chauffeur qui utilise l'outil depuis deux semaines sait exactement ce qu'il veut filtrer — le brider est paternaliste, et ça frustre les utilisateurs les plus engagés, ceux qui paient et qui restent.

**Résolution — D18 amendée, pas annulée** : « jamais demandé à l'inscription » reste. « Jamais demandé » devient « toujours accessible ».

### 3.1 Trois chemins vers le même réglage

1. **Proposé par le bot** *(le chemin principal)*. Il refuse 4 courses sous 30 € → « je filtre sous 30 € ? » [Oui] [Non] [Plus tard]. Le réglage naît d'un comportement observé : il est juste par construction, et confirmé par lui. C'est ça qui remplace la question à l'inscription.
2. **Ajusté depuis l'alerte**. Sous chaque alerte : « trop loin » · « trop peu payé » · « pas ce secteur ». Un tap, dans le contexte, au moment où l'irritation est vive.
3. **Menu complet `/reglages`**. Tout, directement, pour ceux qui veulent piloter. Ce sont tes meilleurs clients.

### 3.2 Catalogue des réglages

| Catégorie | Réglages |
|---|---|
| **Seuils économiques** | **€/km minimum — séparément pour les réservations et pour l'immédiat** · prix plancher absolu (« jamais sous 25 € ») · €/heure minimum (une fois qu'il l'a adopté) · temps d'approche maximum, en minutes · distance de course min et max |
| **Véhicule** | Éco / berline / van / green / VIP · nombre de passagers · bagages · sièges enfant disponibles · animaux · PMR |
| **Géographie** | Départements et arrondissements de prise en charge acceptés · **zones refusées** · province oui/non · aéroports et gares oui/non · rayon maximum autour de sa position |
| **Temps** | Jours et plages horaires travaillés · délai minimum avant une réservation (« pas de résa à moins de 2 h ») · pause / congés avec date de reprise |
| **Notifications** | Voir §3.4 |
| **Groupes** | Activation par groupe · priorité (voir §3.5) |
| **Conditions de paiement** | Refuser fin de mois / 30 jours · refuser un donneur d'ordre sans historique · seuil d'historique minimum |
| **Coût de revient** | Mensualité véhicule · assurance · consommation · prix du carburant ou de la recharge · entretien → alimente le €/km net et le €/heure (A3, A4) |

Le €/km séparé résa / immédiat que tu demandes est juste, et pas seulement par confort : l'économie des deux n'a rien à voir. Une réservation bloque un créneau, donc elle doit être plus exigeante ; une course immédiate est opportuniste, elle remplit un trou qui ne vaut rien autrement.

### 3.3 Le €/km, puisque c'est sa langue

Le §1.1 démontre que le €/km est trompeur. Ce n'est pas une raison pour lui imposer une métrique qu'il ne parle pas : le vocabulaire du métier, c'est le €/km, et un produit qui commence par corriger le vocabulaire de son client ne se fait pas adopter.

Donc : **il règle en €/km, et chaque alerte affiche les deux** — `2,17 €/km · 46 €/h`. Après une trentaine de courses, le bot lui montre son propre écart :

> Tes courses au-dessus de 3 €/km te rapportent 34 €/h en moyenne.
> Celles entre 1 et 1,5 €/km t'en rapportent 47.
> Tu veux que je filtre au €/heure plutôt qu'au €/km ?

Le passage est proposé, avec ses chiffres à lui, jamais imposé. On ne gagne pas une bataille de vocabulaire ; on fait une démonstration.

### 3.4 Le silence en un clic

- **🔕 sur chaque alerte** → 1 h · ce soir · demain matin · jusqu'à nouvel ordre.
- **`/pause`** depuis n'importe où, avec date de reprise pour les congés.
- **Silence automatique pendant une course**, dès qu'il a signalé l'avoir prise.
- **Heures de nuit** proposées après observation de ses habitudes, jamais imposées.
- **Mode digest seul** : aucune alerte en temps réel, un récap à heure fixe. Pour ceux que les notifications rendent fous — et il y en a.
- Le **plafond quotidien est relevable** par lui jusqu'à la limite technique, mais jamais désactivable ni silencieux.

**Le silence ne supprime jamais rien** (D19). À la sortie : « 9 courses pendant ton silence, dont 2 au-dessus de ton seuil », avec les liens. Sans ça, on recrée mot pour mot le récit « l'outil que je paie m'a fait rater 280 € ».

### 3.5 Réglage par groupe

Il est membre de 4 groupes partenaires et n'en veut que 2 en alerte. Activation et priorité par groupe.

> **Ligne retirée après passage en conseil.** Une version antérieure proposait d'utiliser « quels groupes les chauffeurs coupent en premier » comme argument de vente auprès des chefs. C'est vendre au chef du groupe B ce que les chauffeurs du groupe A ont confié au produit. Si ça fuite une seule fois — et dans un milieu qui vit par captures d'écran, ça fuite — le modèle de distribution est terminé. **Métrique interne de priorisation, jamais argument commercial.**

### 3.6 Ce qui n'est pas configurable

Les fenêtres de fraîcheur (D33), le fait que rien ne soit jamais supprimé silencieusement (D19), et l'obligation d'afficher un chiffre seulement s'il est sûr (D8). Ce sont des garde-fous, pas des préférences : un réglage qui permet au client de se tirer une balle dans le pied n'est pas de la liberté, c'est une négligence déguisée en option.

---

## 4. Le chef de groupe comme second utilisateur

Ce n'est plus un apporteur d'affaires à qui on verse 25 %. C'est un utilisateur avec ses propres douleurs, et il en a autant que le chauffeur.

| # | Fonction | Ce que ça lui apporte | Faisab. |
|---|---|---|---|
| ~~P1~~ | ~~Poster une course via le bot (formulaire structuré)~~ | **COUPÉ** — perd contre le copier-coller, dépersonnalise le chef, fait sauter D3 | ⛔ |
| P2 | **Dire qui a eu la course, en 1 tap, par DM** (pas dans le groupe) | Fin des « c'est à qui ? » et des doubles attributions | ⚠️ *à tester à la main avant de coder* |
| P3 | **Commissions dues par chauffeur**, avec échéancier | Son problème n°1 : il court après son argent, sur un cahier ou de mémoire | ✅ |
| ~~P4~~ | ~~Fiabilité de ses chauffeurs (score nominatif)~~ | **JAMAIS CONSTRUIT** — voir §5 | ⛔ |
| P5 | Relance des commissions impayées | | ✅ |
| P6 | **Santé de son groupe** : taux de couverture, délai moyen d'attribution, courses jamais prises | Il découvre que ses courses à 1,1 €/km ne partent jamais → il ajuste ses prix, et il gagne plus | ✅ |
| P7 | Carnet clients et récurrence | | ✅ |

**P6 est le levier de revenus le plus direct du produit**, et personne ne l'a. Un chef qui apprend que 30 % de ses courses ne trouvent pas preneur et lesquelles, c'est du chiffre d'affaires qu'il récupère immédiatement. **C'est aussi la contrepartie qui fait répondre le chef aux demandes d'attribution (P2)** : sans bénéfice visible pour lui, P2 meurt en deux semaines.

**P4 est retiré du produit** — pas repoussé. Le §5 avait raison sur le droit et se trompait de sujet : voir l'arbitrage en §5.

### Conséquence sur le modèle économique

Le back-office du chef vaut assez cher pour être vendu. Deux options :

| Option | Chef | Chauffeur | Effet |
|---|---|---|---|
| **Gratuit contre exclusivité** | 0 €, back-office offert, plus de commission cash | 39 € | Supprime les 1 219 €/mois de commission à 150 abonnés (**+56 % de marge**), crée un coût de sortie énorme, et évite le problème des chefs non immatriculés qui ne peuvent pas facturer |
| **Mixte** | back-office offert + 10–15 % | 39 € | Moins agressif à vendre, marge intermédiaire |

L'option gratuite est probablement la bonne : elle règle d'un coup la commission (coût), la facturation par un particulier (blocage juridique), et la défection du partenaire (O7). **À trancher en V0, avec les 6 chefs démarchés — c'est exactement la question à leur poser.**

---

## 5. Le système de réputation, reformulé

La revue avait bloqué la réputation pour une raison précise : noter publiquement des personnes physiques sur leur solvabilité, à partir de signalements anonymes non contradictoires, avec effet sur leur activité économique. Ça reste vrai — **mais ce n'est qu'une des trois formes possibles**, et c'est la seule vraiment problématique.

| Forme | Qui note qui | Statut juridique | Verdict |
|---|---|---|---|
| **Historique personnel** — le chauffeur voit son propre vécu avec un donneur d'ordre | Lui-même, sur ses propres courses | Ses données, sa relation contractuelle | ✅ **V2, sans obstacle** |
| **Fiabilité interne au groupe (P4)** — le chef suit ses propres chauffeurs | Le chef, sur des gens avec qui il a une relation d'affaires | Juridiquement propre : le chef est responsable de traitement, tu es sous-traitant, un DPA suffit | ⛔ **JAMAIS — pour une raison qui n'est pas juridique** |
| **Score public collectif** des donneurs d'ordre, agrégé entre chauffeurs | Tout le monde, sur des tiers | AIPD, base légale fragile, contradictoire absent, risque diffamatoire, et beaucoup de donneurs d'ordre sont tes partenaires | ⛔ **Jamais** |

> **Arbitrage du conseil sur P4 — quatre membres sur cinq, indépendamment.** L'analyse juridique de ce document était correcte et hors sujet. Le problème n'est pas le RGPD, c'est ceci : **tu vendrais au chauffeur, 39 €/mois, l'outil qui alimente le score que son chef utilise pour lui refuser des courses.** L'arbitraire du chef est déjà le grief numéro un du milieu. Le jour où un abonné comprend ça, l'information fait le tour des groupes en 48 h et ton canal d'acquisition — le bouche-à-oreille entre chauffeurs (§7.1) — s'inverse et se retourne contre toi.
>
> Tu ne peux pas être à la fois l'assistant du chauffeur et l'outil de surveillance de son donneur d'ordre. **P6 (santé du groupe) donne au chef le même bénéfice de pilotage sans trahir personne** : des agrégats sur son groupe, aucune note nominative.
>
> Si un jour un signal de fiabilité devient indispensable, la seule forme acceptable est **factuelle et visible d'abord par le chauffeur lui-même** (ses propres no-show, ses propres annulations tardives), jamais un score que son chef consulte à son insu.

Autrement dit : **le système de réputation que tu veux pour aider le chef à gérer ses chauffeurs et ses commissions est le plus facile des trois**, pas le plus dur. Il devient un traitement pour le compte du chef, encadré par un contrat, avec des personnes qui ont une relation d'affaires établie avec lui — situation banale, comme n'importe quel logiciel RH ou CRM.

Ce qui reste interdit sans avis juridique, c'est le score public sur les donneurs d'ordre — celui qui vise, entre autres, tes propres partenaires.

---

## 6. Phasage révisé

Le conseil a été unanime sur un point : **la V1 précédente faisait environ 15 fonctions plus 35 réglages, pour une personne seule qui conduit — c'est un an de travail, pas dix semaines.** Le périmètre ci-dessous est calibré pour être réellement livrable.

### V0 — Validation, zéro code produit (3 semaines)
Voir la gate en §8. Trois ajouts par rapport à la version précédente : la question **entraide vs dispatch** (critère n°1), le **test de P2 à la main** pendant 10 jours, et la mesure de la **part du flux en image ou en vocal** — que le parser ne voit pas, et qui rendrait la gate à 85 % mensongère sur le périmètre réel.

Aux 6 chefs démarchés : *combien de commissions impayées tu traînes ?*, *comment tu suis qui te doit quoi ?*, *tu préfères 25 % ou le back-office gratuit ?*, *tes chauffeurs sauraient-ils que tu les notes ?* (la réaction à cette dernière dit si le back-office est une douve ou une bombe).

### V1 — MVP (~10 semaines)

**Le produit vendu : moins de bruit, et de l'argent qu'il ne voyait pas.**

| Fonction | Pourquoi elle est là |
|---|---|
| **A1** filtrage — **6 réglages, pas 35** | 85 % des immédiates sont écartées par la distance (§0). C'est l'essentiel du travail. Réglages retenus : secteur, véhicule, horaires, plancher €, €/km résa, €/km immédiat, plus le 🔕 |
| **Dédup inter-groupes, visible** | 27,9 % du flux mesuré. « Déjà vue il y a 6 min dans un autre groupe » — sans jamais nommer l'autre groupe (D35) |
| **A8** conditions de paiement | **Les cinq membres du conseil l'ont désignée comme sous-évaluée.** Net / PAB / fin de mois décide de l'acceptation plus souvent que le €/km, c'est noyé dans le texte, et ça coûte quelques regex |
| **A4 + A3** péage, carburant, coût de revient | Des soustractions vérifiables. Aucune dépendance à S0. Inverse la décision sur les longues distances, là où on se fait avoir |
| **B4** trace horodatée | Quelques heures de dev, culture de litiges verbaux, valeur perçue sans rapport avec le coût |
| **F1 + son repli** | Voir ci-dessous — la meilleure idée du conseil |
| **E1 / E2** notification + `copy_text` | Le canal (D39) |
| **F3** bouton GPS · **G1** rappels d'échéances | Une URL et un cron. G1 donne une raison d'ouvrir le bot les jours creux |
| **Parrainage** (§7.1) | Le P&L impose 12 recrutements/mois indéfiniment. Ne pas le câbler en V1 est une faute arithmétique |
| **D19 / D20** digest, bilan du soir, `/status`, notif de panne | Non négociables : c'est ce qui empêche le churn du premier mois |
| **P2 par DM** — *seulement si la V0 le valide* | Un tap, en privé. Testé à la main avant d'être codé |

**F1 et son repli — la meilleure contribution du conseil.** Aujourd'hui un chauffeur reste en suspens sur 3 ou 4 courses à la fois, et refuse pendant ce temps. Personne ne dit jamais « c'est pris ». F1 lui rend ce temps.

Mais F1 ne doit pas dépendre du chef. **Repli à construire dès la V1** : si personne ne dit qui a eu la course, le bot l'infère du fil — *« 4 réponses depuis, plus de 12 min »* — une probabilité, **jamais une affirmation**. Ça vaut l'essentiel de la valeur de F1 et ça ne dépend de personne. C'est aussi ce qui protège tout le produit si P2 s'effondre.

**Sortis de la V1** : A2 (€/heure — voir §1.1), B3 (anomalie de prix : il faut une distribution de référence qui n'existe pas au jour 1), A6 (conflit d'agenda : un détecteur sur agenda partiel se tait exactement sur le conflit qui compte — il attend C1), P1 (coupé), et 29 des 35 réglages. **Chaque réglage est une surface de support pendant tes heures de conduite** : ils attendent qu'un client les réclame.

### V2 — Gestion et exécution (~10 semaines), conditionnée
**Condition de démarrage : taux d'attribution connu ≥ 50 %.** S'il est à 20 %, on ne construit pas le carnet — on annoncerait une fonction vide.

Carnet (C1), commissions des deux côtés (C2, P3), échéancier et relances (C3, C4 en rédaction seule), historique personnel (B1), **P6 santé du groupe — offert au chef, jamais facturé : c'est le loyer du partenariat et la contrepartie qui le fait répondre à P2**, export compta (C5), €/heure réel (D2), **A2 en titre** une fois S0 tranché et D2 disponible pour calibrer. Exécution : F4 suivi de vol et F7 frais annexes d'abord (fort ratio valeur/coût, cash récupéré immédiatement), puis F2 fiche course, puis F5 et F6 — **en chrono et constitution de preuve uniquement**, sans jamais affirmer ce qui est dû : tu n'as pas les conditions contractuelles du donneur d'ordre. Récap mensuel de preuve de valeur (§7.1), B3, A6.

### V3 — Pilotage
Créneaux et zones personnels (D3), coût d'opportunité (A7), probabilité de recharge (A5). Tous exigent 3 à 6 mois de flux.

### Jamais construit
**B2** score public des donneurs d'ordre · **P4** score nominatif des chauffeurs (§5) · **D4** comparatif entre groupes exposé au chauffeur — « privé » est une fiction dans un milieu qui vit par captures d'écran, et construire l'outil qui classe tes propres distributeurs est un suicide commercial · **C4 en version envoi automatique** — une relance entre deux personnes qui se croisent tous les jours est un acte relationnel, le bot rédige, le chauffeur envoie · **E3** réponse automatique · **module comptable complet** — export CSV et rien de plus, tu ne peux pas maintenir de la conformité fiscale seul · **multi-véhicules** — autre acheteur, autre produit.

**Le repositionnement à vide (§1.4) descend en « à ne pas construire en l'état »** : deux membres du conseil ont montré qu'il est autodestructeur (il sature ce qu'il recommande dès quelques abonnés du même groupe) et statistiquement indéfendable sur 6 groupes — « 40 % des courses partent de La Défense » est un numérateur sans dénominateur d'offre. Sous D8, ce chiffre ne devrait jamais s'afficher.

---

## 7. Ce que ça change au P&L

Deux effets qui vont dans le bon sens, un qui coûte.

- **La commission chef peut tomber à 0** si le back-office la remplace : +1 219 €/mois à 150 abonnés, la marge avant rémunération passe de ~2 145 € à ~3 364 €.
- **Le seuil de couverture des coûts fixes** descend de ~36 à ~22 abonnés.
- **En face** : P1 étant coupé et la V1 réduite à ~10 fonctions, le périmètre initial s'allège nettement — mais toute la V2 devient **conditionnelle** au taux d'attribution mesuré. Le prix de 39 € sera plus facile à défendre quand la compta et les commissions seront dedans ; en V1, il repose sur la réduction du bruit et le €/km net. C'est ce que la gate doit tester, pas supposer.

Le risque de dépendance change aussi de nature : moins de chefs, mais chacun beaucoup plus attaché. La règle « aucun groupe > 25 % des abonnés » devient **plus** importante, pas moins.

### 7.1 Trois manques qui coûtent directement de l'argent

| Manque | Pourquoi ça coûte | Traitement |
|---|---|---|
| **Aucun mécanisme de parrainage chauffeur → chauffeur** | Le P&L établit qu'il faut recruter 12 abonnés/mois indéfiniment pour tenir 150 avec 8 % de churn. C'est le vrai coût du modèle, et aujourd'hui il repose entièrement sur ton temps de démarchage. Un chauffeur satisfait dans un groupe de 400 est le canal le moins cher qui existe | Un mois offert de chaque côté, suivi par lien de parrainage. À câbler en V1, pas après |
| **Aucune preuve de valeur mensuelle** | Le churn est le risque n°1 identifié, et rien ne rappelle au chauffeur, chiffres en main, ce que l'outil lui a rapporté. Le bilan quotidien (D20) traite le silence, pas le renouvellement | Récap mensuel comparatif : « 14 courses prises via une alerte, à 47 €/h en moyenne — contre 38 €/h sur celles que tu as prises hors alerte. » C'est l'argument de reconduction, et il est calculable |
| **Le segment multi-véhicules n'est pas couvert** | Un « chauffeur » avec 3 voitures et des salariés a les mêmes besoins × 3, un budget bien supérieur, et un besoin en plus : répartir les courses entre ses véhicules. C'est un palier tarifaire naturel | À sonder pendant la V0 : combien y en a-t-il dans les groupes visés ? Ne rien construire avant |

---

## 8. Ce qui doit repasser en revue

### 8.1 Tranché par le conseil

| Point | Décision touchée | Verdict |
|---|---|---|
| **P1 — le chef poste via le bot** | D3 | **Coupé, unanimité.** D3 reste intact : le bot n'écrit jamais dans un groupe |
| **P4 — score nominatif des chauffeurs** | D10 | **Jamais construit** (4 membres sur 5). Le DPA règle le droit, pas la politique. P6 donne le même bénéfice sans la trahison |
| **A2 — €/heure en titre de la V1** | D8, D25 | **Reporté en V2.** Incohérence interne : on ne peut pas invoquer D8 contre un prix ambigu et afficher un chiffre à deux jambes modélisées |
| **§3 — configuration accessible** | D18 | **Amendée et confirmée** : « jamais demandé à l'inscription » tient ; « toujours accessible ensuite » est acquis. Mais **6 réglages en V1, pas 35** |
| **§3.5 — vendre au chef B les données du groupe A** | — | **Retiré.** Trahison du canal de distribution |
| **§1.4 — repositionnement à vide** | D8 | **Retiré du plan**, pas repoussé : autodestructeur et statistiquement indéfendable |
| **D4 — comparatif entre groupes** | — | **Jamais exposé.** Analytique interne uniquement |

### 8.2 Reste à trancher — et l'ordre compte

| # | Question | Comment y répondre | Coût |
|---|---|---|---|
| **1** | **Tes 6 groupes cibles sont-ils de l'entraide ou du dispatch commissionné ?** Si c'est du dispatch, D2 est faux et il faut vendre le back-office au dispatcher comme produit principal, en traitant le chauffeur comme canal gratuit. Si c'est de l'entraide, P2–P7 n'ont pas d'acteur pour les alimenter et toute la V2 change | Lire 20 fils par groupe : qui poste, combien de posteurs distincts, quelle part du volume vient des trois plus gros | **une demi-journée** |
| **2** | **Le chef répond-il aux demandes d'attribution, et pendant combien de temps ?** Décide de toute la V2 | Un chef, un Google Form, 10 jours. Mesurer J1, J5, J10 | 10 jours, zéro dev |
| **3** | **Quel est le taux de parse réel, et quelle part du flux échappe au parser** (image, vocal) ? Au-delà de ~20 % d'inparsable, la gate à 85 % ment sur le périmètre | Corpus consenti, §7 de PLAN_SAAS | inclus dans la gate |
| **4** | **Le spike S0** — live location vers un bot | 2 heures | 2 h |
| **5** | **Back-office gratuit contre 0 % de commission ?** Touche D2 et D30 | Question directe aux 6 chefs | inclus |

**La question 1 est le nouveau critère n°1 de la gate.** Elle ne figurait dans aucune version précédente, et elle conditionne tout ce qui suit : une demi-journée d'observation contre trois semaines d'entretiens qui n'y répondront pas mieux.
