# La vie connectée

MUMU ne doit pas attendre qu'on lui transfère les choses. Avec l'accord de la personne, il lit ses services en arrière-plan, en tire ce qui compte et ne parle que si c'est utile.

Ce document décrit ce qui est construit, ce qui ne l'est pas, et les contraintes réelles des fournisseurs. État au 16 novembre 2026.

## Ce qui est construit

| Brique | Fichier | Rôle |
| --- | --- | --- |
| Formes communes | `app/connectors/base.py` | `SourceDocument` et `Event` : tout connecteur produit ces deux formes |
| Connecteur Google | `app/connectors/google.py` | OAuth, lecture Gmail, lecture Google Agenda, en lecture seule |
| Lecture en arrière-plan | `app/sync.py` | Premier passage sur 30 jours, puis nouveaux mails toutes les 5 minutes |
| Classement d'un mail | `app/brain.py`, `INGEST_PROMPT` | Compte ou non, éléments à suivre, achats, faits, faut-il le dire |
| Points serrés de l'agenda | `app/calendar_logic.py` | Chevauchements et enchaînements à moins de 30 minutes |
| Confidentialité | `app/service.py`, `visible_to` | Ce qui vient d'une boîte mail n'est visible que de son propriétaire |
| Chiffrement | `app/crypto.py` | Jetons chiffrés, liens de connexion signés et valables 15 minutes |

## Parcours

1. La personne écrit « connecte mon Gmail ». MUMU répond avec un lien signé.
2. Elle accepte chez Google. MUMU reçoit un jeton de rafraîchissement, le chiffre et le stocke.
3. Premier passage : 30 jours de mails, 100 au plus, sans commentaire mail par mail. Un seul résumé à la fin.
4. Ensuite, toutes les 5 minutes : agenda des 14 prochains jours, puis nouveaux mails.
5. Chaque mail utile devient des éléments à suivre, des achats et des faits, avec la référence du mail d'origine.
6. MUMU écrit de lui-même seulement si attendre poserait un problème, et deux fois par jour au plus.
7. « Déconnecte » révoque l'accès chez Google et efface tout ce qui en a été tiré.

## Règles de confidentialité appliquées dans le code

- Deux permissions seulement : `gmail.readonly` et `calendar.events.readonly`. Aucune écriture.
- Publicité, réseaux sociaux, forums, indésirables, brouillons et messages envoyés ne sont jamais lus.
- Le texte d'un mail est envoyé au modèle pour être classé, puis oublié. La base ne garde que les éléments structurés, l'identifiant du mail et un libellé court.
- Le modèle a pour consigne de ne rien tirer d'une conversation entre personnes.
- Tout ce qui vient d'un compte connecté est marqué `private` et rattaché à son propriétaire. Les autres membres du foyer ne le voient pas, et MUMU ne leur montre pas non plus les échanges des autres.
- Le jeton n'est jamais stocké en clair.

## Contraintes réelles à connaître

### Google : la lecture de Gmail est une permission « restreinte »

- Tant que l'application Google est en mode test : 100 comptes au plus, à déclarer un par un comme testeurs, et l'autorisation expire au bout de 7 jours. La personne doit alors reconnecter. Le code le détecte et la prévient.
- Pour sortir du mode test, il faut la vérification de l'application par Google et une évaluation de sécurité (CASA) par un cabinet agréé, à renouveler chaque année. C'est un coût et un délai à prévoir avant toute ouverture au public.
- Conséquence : la connexion Gmail convient à une bêta de quelques dizaines de personnes. Elle ne peut pas être le seul moyen d'entrer des informations.

Sources : [Google, audience d'une application OAuth](https://support.google.com/cloud/answer/15549945), [Nango, revue de sécurité Google](https://docs.nango.dev/integrations/google-security-review), [Nylas, vérification et CASA](https://developer.nylas.com/docs/dev-guide/provider-guides/google/google-verification-security-assessment-guide).

### WhatsApp : MUMU ne peut pas écrire quand il veut

- Un message libre n'est autorisé que dans les 24 heures qui suivent le dernier message de la personne. Au-delà, il faut un modèle de message approuvé par Meta.
- Le code respecte cette fenêtre : hors fenêtre, l'information est enregistrée et attend.
- À faire : faire approuver un modèle du type « MUMU a quelque chose pour toi » pour rouvrir la conversation, et le récap du dimanche.

### WhatsApp : les groupes existants sont hors de portée

- L'API officielle ne donne aucun accès aux conversations ou aux groupes personnels.
- Une API de groupes existe, mais elle ne concerne que des groupes créés par l'entreprise, limités à 8 participants, sur invitation, et réservés aux comptes officiels vérifiés.
- Les bibliothèques non officielles qui lisent un compte personnel font bannir les numéros et reviennent à surveiller des gens sans leur accord. À exclure.

Sources : [360dialog, API Groupes](https://docs.360dialog.com/docs/messaging/groups), [Kapso, état de l'API Groupes en 2026](https://kapso.ai/blog/whatsapp-groups-api-state-2026).

Recommandation pour les décisions partagées d'un foyer, par ordre de préférence :

1. **Le foyer MUMU** : chaque membre parle à MUMU en privé, et MUMU relaie entre eux. C'est le relais silencieux, à construire ensuite.
2. **L'export de conversation** : WhatsApp permet d'exporter un groupe en fichier texte. La personne le transfère à MUMU quand elle le veut. C'est explicite, autorisé et simple.
3. **Telegram** : un bot peut être ajouté à un groupe existant. Utile pour une coloc qui y est déjà.

## Ce qui n'est pas encore construit

| Sujet | État |
| --- | --- |
| Outlook | L'architecture le permet : un connecteur qui produit `SourceDocument` et `Event` |
| Pièces jointes des mails (factures en PDF) | Seul le texte du mail est lu |
| Notifications poussées par Google | Lecture toutes les 5 minutes pour l'instant |
| Montant inhabituel d'une facture | Le modèle compare avec les faits connus ; pas encore d'historique chiffré |
| Garanties et remplacement des produits | Les achats sont enregistrés ; rien n'en est déduit pour l'instant |
| Google Drive, Apple Agenda | Non commencés |
| Modèle de message approuvé, récap du dimanche | Non commencés |

## Ajouter un connecteur

1. Créer `app/connectors/<fournisseur>.py` avec l'autorisation et des fonctions qui renvoient des `SourceDocument` et des `Event`.
2. Lever `AccessRevoked` quand l'accès est retiré.
3. Brancher le fournisseur dans `app/sync.py`. Le classement, la confidentialité et les notifications sont déjà communs.
