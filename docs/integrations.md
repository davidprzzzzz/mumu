# Intégrations : ce qui est possible, ce qui est construit

MUMU se branche aux services existants au lieu de demander à la personne de tout ressaisir. Ce document dit, pour chaque intégration, ce que permettent les API officielles, ce qu'elles exigent, et où en est le code. État au 16 novembre 2026.

Règle commune : aucune extraction non autorisée de sites, aucun identifiant bancaire ou de messagerie demandé à la personne, aucune intégration présentée comme existante si elle ne l'est pas.

## Synthèse

| Intégration | API officielle | État dans le code | Priorité |
| --- | --- | --- | --- |
| Gmail | Oui (OAuth, lecture seule) | Construit, testé avec doublures, jamais contre Google | 1 |
| Google Agenda | Oui | Construit, testé avec doublures | 1 |
| WhatsApp (Twilio) | Oui, avec restrictions | Construit, testé avec doublures | 1 |
| Vocaux (transcription) | Oui | Construit, testé avec doublures | 2 |
| Outlook et agenda Microsoft | Oui (Microsoft Graph) | Interface prête, connecteur non écrit | 2 |
| Banque (agrégateur agréé) | Oui, sous contrat | Non construit | 3 |
| Google Drive | Oui | Non construit | 4 |
| OneDrive | Oui (Microsoft Graph) | Non construit | 4 |
| Telegram | Oui | Non construit | 4 |
| Achats via les mails de confirmation | Passe par Gmail | Construit | 1 |
| Sites des marchands, portails scolaires, Doctolib, CAF | Pas d'API ouverte aux particuliers | Rien, volontairement | — |

## Gmail et Google Agenda

**Construit.** Connexion OAuth, jeton chiffré, lecture des nouveaux mails toutes les 5 minutes, premier passage sur 30 jours, pièces jointes PDF et images, agenda des 14 prochains jours, déconnexion avec effacement. Code : `app/connectors/google.py`, `app/sync.py`.

| Point | Détail |
| --- | --- |
| Permissions demandées | `gmail.readonly` et `calendar.events.readonly`, rien d'autre |
| Vérification | La lecture de Gmail est une permission « restreinte » : vérification de l'application par Google et évaluation de sécurité (CASA) par un cabinet agréé, à renouveler chaque année |
| Mode test | 100 comptes au plus, à déclarer un par un ; l'autorisation expire 7 jours après le consentement |
| Coût | API gratuite ; l'évaluation de sécurité a un coût à chiffrer auprès d'un cabinet agréé |
| Conservation | Le texte des mails n'est pas stocké ; seuls restent les éléments structurés, l'identifiant du mail et un libellé |
| Viabilité | Suffisant pour une bêta de quelques dizaines de personnes, à condition de reconnecter chaque semaine. L'ouverture au public passe par la vérification |

Ce qui n'est pas fait : notifications poussées par Google (Pub/Sub) au lieu de la lecture périodique ; test contre un vrai compte (`tests/live/`).

Sources : [Google, audience d'une application OAuth](https://support.google.com/cloud/answer/15549945), [Nango, revue de sécurité Google](https://docs.nango.dev/integrations/google-security-review), [Nylas, vérification et CASA](https://developer.nylas.com/docs/dev-guide/provider-guides/google/google-verification-security-assessment-guide).

## WhatsApp

**Construit** avec Twilio : réception, réponse, pièces jointes, vérification de la signature Twilio, idempotence sur `MessageSid`.

| Point | Détail |
| --- | --- |
| Écrire en premier | Un message libre n'est permis que dans les 24 h qui suivent le dernier message de la personne. Au-delà, il faut un modèle de message approuvé par Meta. Le code respecte cette fenêtre |
| Assistants IA généralistes | Meta a interdit ces assistants sur l'API Business en janvier 2026, puis les a réautorisés en Europe pour 12 mois à partir de mars 2026, avec des frais par message. Risque réel au printemps 2027 |
| Groupes | L'API ne donne aucun accès aux groupes ou conversations personnels. L'API Groupes ne concerne que des groupes créés par l'entreprise, 8 participants au plus, sur invitation, réservée aux comptes officiels vérifiés |
| Coût | Frais Twilio plus frais Meta par message |
| Viabilité | Bon canal pour la bêta. Garder l'architecture indépendante du canal |

À faire : faire approuver un modèle de message (« MUMU a quelque chose pour toi ») pour rouvrir la conversation et envoyer un récap hebdomadaire.

**Décisions partagées d'un foyer**, par ordre de préférence :

1. **Le foyer MUMU** : chacun parle à MUMU en privé et MUMU relaie. Construit (invitations, propositions, acceptation).
2. **L'export de conversation** : WhatsApp exporte un groupe en fichier texte ; la personne le transfère quand elle le veut. Non construit : il faudrait lire un fichier `.txt` joint.
3. **Telegram** : un bot peut être ajouté à un groupe existant. Voir plus bas.

Les bibliothèques non officielles qui pilotent un compte WhatsApp personnel font bannir les numéros et reviennent à lire les messages de gens qui n'ont rien accepté. Exclues.

Sources : [eWeek, Meta et les assistants IA sur WhatsApp en Europe](https://www.eweek.com/news/meta-allows-rival-ai-chatbots-whatsapp-europe/), [360dialog, API Groupes](https://docs.360dialog.com/docs/messaging/groups), [Kapso, état de l'API Groupes en 2026](https://kapso.ai/blog/whatsapp-groups-api-state-2026).

## Vocaux

**Construit.** Les vocaux WhatsApp sont téléchargés depuis Twilio, transcrits par l'API OpenAI, puis traités comme un message écrit. Code : `app/voice.py`.

| Point | Détail |
| --- | --- |
| Identifiants | `OPENAI_API_KEY` |
| Formats | OGG/Opus (format WhatsApp), MP3, M4A, WAV, WebM ; AMR refusé ; 25 Mo au plus |
| Conservation | L'audio n'est pas gardé ; seule la transcription entre dans la conversation |
| Non vérifié | Le nom du modèle `gpt-4o-transcribe` et son prix n'ont pas été vérifiés contre l'API réelle. Le modèle est réglable par `MUMU_TRANSCRIPTION_MODEL` |

## Outlook et agenda Microsoft

**Non construit.** L'interface `Provider` (`app/connectors/base.py`) définit ce qu'un connecteur doit faire, et `sync_all` aiguille chaque connexion vers son fournisseur. Un connecteur Microsoft Graph n'a qu'à implémenter ces six méthodes.

| Point | Détail |
| --- | --- |
| API | Microsoft Graph : `Mail.Read`, `Calendars.Read`, `offline_access` en délégué |
| Comptes | Une seule inscription d'application peut servir les comptes personnels (Outlook.com) et professionnels ou scolaires |
| Consentement | Les réglages par défaut de Microsoft Entra excluent `Mail.Read` et `Calendars.Read` du consentement par l'utilisateur seul. Dans une université ou une entreprise, un administrateur devra souvent approuver MUMU. Les comptes Outlook.com personnels ne sont pas concernés |
| Vérification | Éditeur vérifié recommandé ; sans lui, les comptes professionnels voient un avertissement « éditeur non vérifié » |
| Viabilité | Bonne pour les comptes personnels. Incertaine pour les messageries universitaires, à tester auprès d'une ou deux universités avant de construire pour les étudiants |

Sources : [Unipile, OAuth Microsoft Graph pour les mails](https://www.unipile.com/microsoft-graph-oauth-email/), [Unipile, éditeur vérifié](https://www.unipile.com/microsoft-graph-api-email/), [XenForo, réglage de consentement par défaut de Microsoft](https://xenforo.com/community/threads/microsoft-oauth-email-type-is-not-documented.236008).

## Banque : intelligence financière en lecture seule

**Non construit.** Aujourd'hui, les montants viennent uniquement des factures et reçus lus dans les mails (`app/money.py`). La bonne voie est un agrégateur agréé, jamais des identifiants bancaires.

| Point | Détail |
| --- | --- |
| Cadre | DSP2 : service d'information sur les comptes (AIS), avec consentement de la personne, renouvelé environ tous les 180 jours |
| Acteurs en France | Powens (ex-Budget Insight, établissement de paiement agréé par l'ACPR), Bridge, Tink, Linxo Connect, TrueLayer |
| Option gratuite d'hier | GoCardless n'accepte plus de nouveaux clients sur son offre Bank Account Data (ex-Nordigen) |
| Licence | Passer par la licence de l'agrégateur évite d'en demander une à l'ACPR, selon le contrat proposé |
| Coût | Sur devis, contrat commercial ; à chiffrer avant de s'engager |
| Données | Très sensibles : chiffrement, durée de conservation courte, effacement à la déconnexion, analyse d'impact RGPD avant lancement |
| Viabilité | Forte valeur pour les étudiants (abonnements, loyer, prélèvements), mais pas avant d'avoir prouvé l'usage avec les mails |

Le code est prêt à recevoir ces données : `record_charge` accepte n'importe quelle source, et la détection des hausses et des prélèvements récurrents ne dépend pas de l'origine.

Sources : [Open Banking Tracker, Powens](https://www.openbankingtracker.com/api-aggregators/budget-insight), [Crassula, open banking en France en 2026](https://crassula.io/fr/solutions/digital-banking/guides/open-banking.md), [Open Banking Tracker, fournisseurs en 2026](https://openbankingtracker.com/blog/best-open-banking-api-providers-developers-2026), [Powens, renouvellement du consentement](https://www.powens.com/blog/ebics-alternatives-automating-reconciliation-france).

## Google Drive et OneDrive

**Non construits.** Utiles pour retrouver une attestation ou un contrat déjà rangé.

- **Drive** : la lecture de tout le Drive (`drive.readonly`) est une permission restreinte, avec la même vérification que Gmail. La permission `drive.file`, limitée aux fichiers que la personne choisit elle-même via le sélecteur Google, est beaucoup plus légère. C'est l'option à privilégier. Classement des permissions à revérifier au moment de construire.
- **OneDrive** : Microsoft Graph, `Files.Read` en délégué, mêmes questions de consentement que pour Outlook.
- **Code** : un connecteur qui produit des `SourceDocument` avec des pièces jointes ; `store_document` et le reste sont communs.

## Telegram

**Non construit.** Option pour une colocation déjà organisée sur Telegram.

- Un bot peut être ajouté à un groupe existant par ses membres, ce que WhatsApp ne permet pas.
- En mode confidentialité, activé par défaut, un bot de groupe ne reçoit que les messages qui lui sont adressés. C'est le bon réglage pour MUMU : il ne lit pas tout le groupe. Ce point vient de la documentation Telegram telle que je la connais, et n'a pas été revérifié aujourd'hui.
- API gratuite, pas de fenêtre de 24 h.
- Il faudrait un adaptateur de canal à côté de `whatsapp.py` ; le reste est commun.

## Ce qui ne sera pas fait

- Lire les portails scolaires (Pronote, ENT), Doctolib, la CAF ou les sites marchands en se connectant à la place de la personne.
- Demander des identifiants bancaires.
- Lire des conversations WhatsApp personnelles.

Quand un de ces services n'a pas d'API, MUMU prépare le texte ou le dossier, et la personne fait l'envoi.
