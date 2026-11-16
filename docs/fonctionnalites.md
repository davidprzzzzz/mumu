# Fonctionnalités : ce qui existe vraiment

État au 16 novembre 2026. Une seule règle de lecture : **« testé » veut dire testé avec des doublures** (faux WhatsApp, faux Google, faux modèle). Rien n'a encore été vérifié contre un vrai service. La qualité de compréhension du vrai modèle sur de vrais mails est l'inconnue principale.

Légende de la colonne « État » :
- **Construit et testé** : le code existe et des tests le couvrent.
- **Construit, peu testé** : le code existe, la couverture est partielle.
- **Non construit** : décrit dans la feuille de route seulement.

## Vue d'ensemble

| Fonctionnalité | État | Identifiants nécessaires |
| --- | --- | --- |
| Capture WhatsApp (texte, photo, PDF) | Construit et testé | Twilio, Anthropic |
| Vocaux | Construit et testé | OpenAI (transcription) |
| Gmail en lecture seule, avec pièces jointes | Construit et testé | Google Cloud, compte testeur |
| Google Agenda en lecture seule | Construit et testé | Idem |
| Moteur de processus | Construit et testé | Aucun |
| Facture à payer | Construit et testé | Aucun |
| Courrier administratif | Construit et testé | Aucun |
| Document à fournir | Construit et testé | Aucun |
| Événement scolaire | Construit et testé | Aucun |
| Retour et remboursement | Construit et testé | Aucun |
| Renouvellement de contrat | Construit, peu testé | Aucun |
| Premier logement (playbook) | Construit et testé | Aucun |
| Documents : doublons, validité, récupération | Construit et testé | `MUMU_SECRET_KEY`, `PUBLIC_URL` |
| Argent : hausses de prix, prélèvements attendus | Construit et testé | Aucun (données lues dans les mails) |
| Achats, délais de retour, garanties | Construit et testé | Aucun |
| Foyer : invitation, partage, responsabilités | Construit et testé | Aucun |
| Moteur proactif | Construit et testé | Aucun |
| Vue de la semaine | Construit et testé | Aucun |
| Conflits d'agenda et créneau libre | Construit et testé | Agenda connecté |
| Journal, effacement, conservation limitée | Construit et testé | Aucun |
| Outlook, Drive, OneDrive, banque, Telegram | Non construit | Voir `integrations.md` |

## Détail

### Capture WhatsApp

- **Problème** : l'information arrive partout (papier, mail, message) et il faut la ressaisir.
- **Expérience** : on transfère, on photographie ou on écrit. MUMU répond en trois lignes : compris, noté, à faire.
- **Code** : `main.py` (webhook), `service.py`, `brain.py`, `persona.py`.
- **Ce qui marche** : texte, une photo ou un PDF par message ; accueil ; prénom et type de foyer ; enfants nommés ; idempotence sur `MessageSid`.
- **Testé** : `test_pipeline.py`, `test_hardening.py`.
- **Manques** : une seule pièce jointe par message WhatsApp ; traitement en tâche de fond dans le processus web (un redémarrage pendant le traitement perd le message) ; pas de page d'admin pour relire les conversations.

### Vocaux

- **Problème** : les étudiants et les parents parlent plus qu'ils n'écrivent.
- **Expérience** : un vocal est transcrit et traité comme un message écrit. Sans transcription, MUMU dit qu'il ne sait pas écouter, sans deviner.
- **Code** : `voice.py`, `main.py`.
- **Testé** : requête de transcription (doublure HTTP), vocal de bout en bout, absence de clé (`test_engine.py`, `test_coverage_gaps.py`).
- **Identifiants** : `OPENAI_API_KEY`. Le nom du modèle de transcription (`gpt-4o-transcribe`) n'a pas été vérifié contre l'API réelle.
- **Manques** : réponses vocales ; format AMR refusé.

### Gmail et Google Agenda

- **Problème** : la personne ne doit pas penser à transférer.
- **Expérience** : « connecte mon Gmail », un lien, un consentement. Premier passage sur 30 jours, un seul message de résumé, puis lecture toutes les 5 minutes.
- **Code** : `connectors/google.py`, `connectors/base.py`, `sync.py`, `main.py`.
- **Ce qui marche** : publicité, réseaux sociaux, brouillons et envoyés ignorés ; PDF et images joints (3 au plus, 5 Mo au plus) lus par le modèle ; texte des mails jamais stocké ; un mail jamais relu ; accès retiré détecté ; « déconnecte » révoque et efface.
- **Testé** : `test_connected.py`, `test_acceptance.py`. Test live écrit, jamais lancé.
- **Identifiants** : projet Google Cloud, écran de consentement en mode test, testeurs déclarés.
- **Manques** : vérification Google et évaluation de sécurité avant toute ouverture publique ; notifications poussées (aujourd'hui, lecture périodique) ; l'autorisation expire tous les 7 jours en mode test.

### Moteur de processus

- **Problème** : les assistants créent un rappel puis abandonnent la personne. Le problème, lui, n'est pas réglé.
- **Expérience** : chaque situation devient un processus avec des étapes, un responsable, une échéance, un blocage éventuel et une prochaine action. MUMU fait ce qu'il peut faire seul, prépare les textes, et attend l'accord avant tout ce qui engage la personne.
- **Code** : `workflows/engine.py` (commun), `workflows/templates.py` (types), `signals.py` (création à partir de ce qui a été compris).
- **Règles** : résolu seulement quand toutes les étapes sont faites ou que la personne ferme ; une étape qui demande un accord ne passe jamais seule ; un texte approuvé n'est pas « envoyé » tant que la personne ne le confirme pas ; une preuve (mail de paiement, de remboursement) peut valider une étape ; un même sujet vu dans deux sources reste un seul processus.
- **Testé** : `test_engine.py`, `test_acceptance.py`.
- **Ajouter un type** : un `Template` dans `templates.py` et une branche dans `signals.py`.

| Type | Déclencheur | Étapes | Fin |
| --- | --- | --- | --- |
| Facture à payer | Facture avec montant ou échéance | Payer | Confirmation de paiement ou « c'est payé » |
| Courrier administratif | Lettre de la CAF, de la CPAM, des impôts, de la fac… | Réunir chaque pièce (cochée seule si MUMU l'a déjà), actions, préparer la réponse (accord), l'envoyer, attendre la réponse | Réponse reçue |
| Document à fournir | Un organisme réclame une attestation | Retrouver le document (bloqué et expliqué s'il est périmé ou absent), l'envoyer (accord) | Envoi confirmé |
| Événement scolaire | Sortie, autorisation, inscription | Actions du courrier, paiement s'il y en a un | Toutes les actions faites |
| Retour et remboursement | Confirmation de retour | Colis renvoyé, remboursement reçu ; relance (accord) seulement si le remboursement est en retard | Mail de remboursement |
| Renouvellement | Échéance de contrat | Vérifier couverture et prix, garder ou changer | Décision de la personne |
| Premier logement | « J'emménage » | Assurance, électricité, CAF, état des lieux, changement d'adresse ; ce qui est déjà connu est coché seul | Toutes les étapes |

Les dates légales utilisées par défaut (14 jours de rétractation, remboursement 14 jours après le retour, 2 ans de garantie légale de conformité) sont marquées comme estimations dans ce que MUMU dit. Elles ne remplacent pas les conditions du marchand.

### Documents

- **Problème** : on ne retrouve jamais l'attestation au moment où on la demande.
- **Expérience** : une attestation envoyée en photo ou reçue en pièce jointe est reconnue et datée. Quand on la redemande, MUMU la retrouve, vérifie qu'elle est encore valable et donne un lien temporaire.
- **Code** : `documents.py`, `main.py` (`/documents/{jeton}`).
- **Ce qui marche** : le fichier n'est pas copié, seule sa référence est gardée (pièce jointe Gmail, média Twilio) ; doublons écartés par empreinte ; lien d'une heure, réservé au propriétaire.
- **Testé** : `test_acceptance.py` (scénarios 2, 2b, 5, 8).
- **Manques** : si la source disparaît (mail supprimé, média Twilio effacé), le document n'est plus récupérable ; pas de recherche plein texte ; pas d'import depuis Drive.

### Argent

- **Problème** : les hausses discrètes et les prélèvements oubliés.
- **Expérience** : « Spotify est passé de 10,99 € à 12,99 €, soit 2 € de plus (+18 %). » Une seule fois. Dans la vue de la semaine : les prélèvements récurrents attendus d'ici la fin du mois, présentés comme une estimation.
- **Code** : `money.py`.
- **Ce qui marche** : seulement des montants lus dans de vrais documents ; une hausse compte à partir de 2 % et 0,50 € ; un marchand est récurrent après deux montants espacés de 25 à 35 jours.
- **Testé** : `test_engine.py`, `test_acceptance.py` (scénario 3).
- **Manques** : pas de connexion bancaire (voir `integrations.md`) ; doublons de prélèvement non détectés ; aucune comparaison d'offres.

### Achats et garanties

- **Problème** : « elle est encore sous garantie ? où est la facture ? »
- **Expérience** : chaque commande lue dans un mail est gardée avec sa date limite de retour et sa garantie ; un retour est relié à la commande d'origine.
- **Code** : `signals.py`, modèle `Purchase`.
- **Testé** : `test_engine.py`, `test_acceptance.py` (scénario 4).
- **Manques** : pas d'alerte avant la fin d'une garantie ; pas d'achat récurrent prédit ; suivi de livraison limité à ce que disent les mails.

### Foyer et coordination

- **Problème** : une seule personne pense à tout et doit relancer les autres.
- **Expérience** : un lien d'invitation (7 jours) fait rejoindre le foyer. Une tâche se propose (« Claire te propose : signer l'autorisation »), on répond oui ou non, on dit « fait ». Le proposant est prévenu une fois. Une seule relance la veille, jamais de reproche.
- **Code** : `household.py`, `service.py`, `proactive.py`.
- **Ce qui marche** : un mail privé peut suggérer un responsable, mais rien n'est transmis sans l'accord du propriétaire ; seul le propriétaire peut partager ; les conversations restent individuelles.
- **Testé** : `test_acceptance.py` (scénarios 1, 6, 8), `test_engine.py`.
- **Manques** : WhatsApp interdit d'écrire en premier à quelqu'un qui n'a pas écrit depuis 24 h, donc une proposition attend que la personne écrive. Il faut un modèle de message approuvé par Meta. Pas de départ d'un foyer, pas de partage de dépenses.

### Moteur proactif

- **Problème** : un assistant qui parle trop devient une charge de plus.
- **Expérience** : peu de messages, chacun utile : échéance proche, retard, remboursement en retard avec relance prête, hausse de prix, conflit d'agenda, proposition de partage.
- **Code** : `proactive.py`, `messaging.py`.
- **Règles** : note importance × urgence, seuil ; aucune intervention à confiance inférieure à 0,6 ; une clé par intervention, jamais envoyée deux fois ; pas de rappel de ce que la personne vient de dire ; 21 h à 8 h silencieux ; deux messages spontanés par jour au plus.
- **Testé** : `test_acceptance.py` (scénarios 3, 4, 7, 9), `test_engine.py`.
- **Manques** : pas de récap du dimanche ; pas de réglage par personne des heures calmes ; pas d'apprentissage de ce qui est utile.

### Vue de la semaine

- **Problème** : « qu'est-ce que j'oublie ? »
- **Expérience** : une liste classée : en retard, accords attendus, propositions, échéances proches, blocages, attentes, rendez-vous, hausses.
- **Code** : `agenda.py`, transmise au modèle dans le contexte.
- **Testé** : `test_acceptance.py` (scénario 10).
- **Manques** : la formulation finale dépend du modèle.

### Agenda

- **Problème** : deux rendez-vous qui s'enchaînent sans marge.
- **Expérience** : « La réunion et le dentiste s'enchaînent sans marge. Un créneau est libre de 8 h à 8 h 30. »
- **Code** : `calendar_logic.py`.
- **Testé** : `test_connected.py`, `test_acceptance.py` (scénario 7).
- **Manques** : pas de temps de trajet ; créneau cherché le même jour seulement, entre 8 h et 20 h.

### Confiance

- **Code** : `audit.py`, `retention.py`, `crypto.py`, `sync.py` (`erase_connected_data`), migrations Alembic.
- **Testé** : `test_hardening.py`, `test_coverage_gaps.py`, `test_connected.py`.
- **Manques** : la personne ne peut pas encore consulter son journal ni exporter ses données depuis WhatsApp ; pas de suppression complète de compte par message ; pas de chiffrement des données au repos au-delà de ce que fournit l'hébergeur.
