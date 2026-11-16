# Feuille de route produit et technique

État au 16 novembre 2026. Le code couvre une grande partie de la vision, mais **rien n'a encore servi à une vraie personne**. La priorité absolue n'est donc pas d'ajouter des fonctionnalités : c'est de vérifier que ce qui existe marche avec de vrais messages, de vrais mails et le vrai modèle.

Chaque phase a une condition de passage. On ne passe pas à la suivante sans l'avoir remplie.

## Phase 0 : faire marcher pour de vrai (semaine 1)

Objectif : MUMU répond sur ton téléphone et lit ta propre boîte.

1. Déployer (Supabase, Railway ou Render, Twilio en bac à sable) avec `alembic upgrade head`.
2. Lancer les tests live : `MUMU_LIVE=1 python -m pytest -m live tests/live`.
3. Connecter ta boîte Gmail et lire le résumé du premier passage.
4. Constituer un jeu d'évaluation : 30 vrais mails et photos (factures, courriers, commandes, retours, attestations), anonymisés, avec la bonne réponse attendue. Mesurer ce que le modèle en tire.
5. Corriger `persona.py` et les schémas de `brain.py` jusqu'à atteindre les seuils ci-dessous.

**Condition de passage** :
- Sur le jeu d'évaluation, 90 % des montants et des dates sont justes et 95 % des mails sans intérêt sont ignorés.
- Aucune information privée n'apparaît chez un autre membre.
- Aucun message spontané n'est inutile sur une semaine d'usage personnel.

## Phase 1 : bêta privée, étudiants (semaines 2 à 6)

Objectif : 10 à 15 étudiants l'utilisent chaque semaine sans qu'on le leur rappelle.

Produit :
- Accueil adapté au type de foyer (seul, coloc).
- Playbook « premier logement » mis en avant : assurance, électricité, CAF, état des lieux.
- Récap hebdomadaire, qui demande un modèle de message WhatsApp approuvé par Meta.
- Lecture de l'export d'une conversation WhatsApp (fichier `.txt` transféré), pour les décisions de coloc.

Technique :
- Page d'administration privée : lire les conversations, corriger un processus, voir le journal.
- File de traitement durable (table Postgres) à la place des tâches de fond en mémoire : un redémarrage ne perd plus de message.
- Suivi des coûts par foyer (appels au modèle, transcriptions, messages WhatsApp).
- Alerte en cas d'erreurs répétées sur un connecteur.

**Mesures** : transferts ou connexions par personne et par semaine ; part des processus menés jusqu'au bout ; part des messages spontanés jugés utiles (pouce levé ou réponse) ; personnes actives en semaine 4.

**Condition de passage** : au moins 60 % des personnes actives en semaine 4, au moins la moitié des processus ouverts résolus ou fermés volontairement, et au moins 3 personnes sur 10 qui accepteraient de payer.

## Phase 2 : parents et couples (mois 2 à 3)

Objectif : prouver la coordination à deux adultes.

Produit :
- Parcours parent : mails de l'école, autorisations, activités, rendez-vous des enfants, partage entre parents. Le moteur existe déjà (événement scolaire, propositions, acceptation).
- Fiche enfant consultable par les adultes du foyer seulement.
- Départ d'un foyer, changement de propriétaire d'un processus.
- Partage des dépenses de coloc ou de couple, à partir de montants réels.

Technique :
- Connecteur Outlook (Microsoft Graph), d'abord pour les comptes personnels. Tester avec une ou deux universités avant de compter sur les comptes scolaires.
- Lecture des pièces jointes `.txt` et `.ics`.
- Notifications poussées Gmail (Pub/Sub) à la place de la lecture toutes les 5 minutes.

**Condition de passage** : dans au moins la moitié des foyers à deux adultes, le second adulte accepte et termine au moins une tâche par semaine.

## Phase 3 : MUMU s'en occupe (mois 3 à 6)

Objectif : passer de « je prépare » à « c'est fait », avec accord.

Produit :
- Appels téléphoniques pour prendre ou déplacer un rendez-vous (voix temps réel sur une ligne téléphonique, annonce qu'il s'agit d'une IA dès la première phrase).
- Envoi de mails au nom de la personne, avec accord explicite à chaque fois (permission `gmail.send`, plus légère que la lecture).
- Permissions apprises : « je paie seul les sorties de moins de 15 € ? ». Jamais de paiement sans règle explicite acceptée.
- Alerte avant la fin d'une garantie ; achats récurrents proposés.

Technique :
- Connexion bancaire en lecture seule via un agrégateur agréé (voir `integrations.md`), après analyse d'impact RGPD.
- Google Drive avec la permission `drive.file`.
- Vérification Google et évaluation de sécurité CASA pour sortir du plafond de 100 comptes.

**Condition de passage** : un palier payant (environ 9 à 15 € par foyer) gardé par au moins 40 % des foyers actifs après deux mois.

## Dette technique, par ordre d'importance

| Sujet | Risque | Quand |
| --- | --- | --- |
| Compréhension du vrai modèle jamais mesurée | Tout le reste repose dessus | Phase 0 |
| Migrations jamais exécutées sur un vrai Postgres | Échec au premier déploiement | Phase 0 |
| Traitement des messages en tâche de fond en mémoire | Message perdu si le serveur redémarre | Phase 1 |
| Boucle d'arrière-plan dans le processus web | Doublons avec plusieurs exemplaires du serveur ; utiliser alors `RUN_BACKGROUND=false` et `python -m app.worker` | Phase 1 |
| Message spontané envoyé avant l'écriture en base | En cas de panne entre les deux, il peut être renvoyé une fois | Phase 1 |
| Facture mensuelle sans échéance ni référence, au même montant | La nouvelle facture peut être confondue avec l’ancienne (clé de déduplication) | Phase 1 |
| Pas de limite de débit par numéro | Abus possible du webhook | Phase 1 |
| Chiffrement au repos limité à celui de l'hébergeur | Données sensibles en clair dans la base, hors jetons | Avant la banque |
| Dépôt public | Risque de fuite le jour où de vraies données y entrent | Maintenant |

## Ce qu'on ne fera pas

- Recréer 26 modules de gestion domestique.
- Lire des conversations ou des portails sans accord ni API officielle.
- Payer, envoyer ou appeler sans accord explicite.
- Conseiller sur des investissements ou décider de l'éligibilité à une aide.
