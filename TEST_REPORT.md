# Validation FCS Backup v2 — 17 octobre 2026

L'identité FCS Backup est appliquée à l'interface, au lanceur, au dossier serveur, à la documentation et au paquet de mise à jour. Les libellés d'affichage préservent les chemins sources existants ; le test d'interface vérifie aussi que retirer une source affichée sous un libellé retire la bonne entrée de configuration. Les tests ci-dessous ont été repassés après ce changement.

## Résultats

- **Python 3.11.9 : 55 tests réussis**, y compris Tkinter et le transport HTTP réel. Flask 3.1.3 du poste de développement ; Waitress 3.0.0 chargé depuis un répertoire temporaire isolé.
- **Python 3.8.10 : 50 tests réussis, 5 tests d'interface ignorés** (Tkinter absent du runtime embarqué). Profil serveur figé de `requirements-win7.txt`, Requests 2.32.4 pour le client de test.
- Syntaxe des fichiers applicatifs contrôlée pour Python 3.8.
- Le runtime 3.8 et ses dépendances ont été placés uniquement dans un répertoire temporaire. Aucune installation Python ou dépendance de production n'a été remplacée.

Les deux séries ont été exécutées sur le poste de développement Windows, pas sur le PC Windows 7. La compatibilité de l'interpréteur et des dépendances est vérifiée ; la validation sur le matériel et le système Windows 7 réels reste à faire lors du déploiement.

## Scénarios couverts

- Source absente, liste vide, source remplacée par un fichier, sous-dossier illisible, échec de `stat`, déconnexion pendant scan ou transfert : aucune suppression.
- Suppression réelle dans une source accessible ; conservation des dossiers retirés de la configuration et des autres dossiers serveur.
- Modifications à taille constante ; changement de date sans changement de contenu ; second passage sans lecture de contenu ni transfert.
- Adoption des copies manuelles après réanalyse, sans envoi des médias identiques.
- Reprise après interruption et après redémarrage ; nouveau contenu repartant à zéro ; morceau dupliqué idempotent ; suppression d'une fin de partiel non confirmée.
- Empreinte de morceau incorrecte, fichier final corrompu, remplacement impossible : ancienne copie conservée.
- Corbeille exactement 48 h, purge refusée avant échéance ; restauration vérifiée sans écrasement.
- Coupures simulées immédiatement après remplacement, déplacement en corbeille et restauration : réparation de l'inventaire au redémarrage.
- Chemins absolus, traversées, chemins internes, noms Windows réservés et points de réanalyse rejetés ; noms temporaires distincts pour les chemins anciennement en collision.
- Manque d'espace avant ou pendant transfert ; réservations concurrentes ne comptant pas les mêmes octets libres deux fois.
- Réanalyse annulée ou échouée bloquant l'emploi d'un inventaire périmé ; mutations bloquées pendant la réanalyse.
- Deux transferts réellement concurrents, réglage à un seul transfert, authentification et refus des anciennes versions.
- Requêtes HTTP réelles via Waitress sur `127.0.0.1` : connexions TCP réutilisées et octets reçus identiques.
- Construction de l'interface masquée, file de messages, progression, espace disque, sélection multiple de corbeille ; nettoyage de l'interface après une erreur de travail ; dates complètes dans le journal.

## Mesure du cache

Commande : `python -B tools/benchmark_cache.py`. Lot temporaire de **1 000 fichiers de 4 Kio**, sans transfert réseau.

| Passage | Temps mesuré | Contenus relus | Fichiers à envoyer |
| --- | ---: | ---: | ---: |
| Constitution initiale du cache | 2,714 s | 1 000 | 0 |
| Sources inchangées | 0,0411 s | 0 | 0 |
| Un contenu modifié, même taille | 0,0467 s | 1 | 1 |

Ces mesures valident le fonctionnement du cache sur ce lot ; elles ne prédisent pas les débits sur les disques médias ou via Tailscale. Les tests ne substituent pas un essai sur le serveur réel.

## Isolation

Les tests ont utilisé des dossiers temporaires, des sessions Flask simulées ou une écoute locale éphémère. Le programme de production n'a pas été démarré ; les configurations personnelles, journaux existants et médias de la copie USB n'ont pas été modifiés par ces vérifications.

## Nettoyage du 17 octobre 2026

- Retrait de six imports inutilisés, de la méthode intermédiaire `FCSBackupApp._run_backup`, de l'attribut `backup_thread` et des paramètres sans effet `kind` et `status` dans les helpers d'interface. Le test d'erreur du fil de travail appelle maintenant directement `_run_operation('backup')`.
- Ancien `manifest.json` et cache `client.cpython-311.pyc` archivés hors du projet. Configurations personnelles, journaux, exemples, tests, outils et fichiers serveur conservés ; le paquet de mise à jour est reconstruit avec les sources nettoyées.
- Ruff : aucune erreur avec les règles `F` sur tous les fichiers Python, et `F,ARG` sur l'application et les outils. Vulture : aucun signalement avec un seuil de confiance de 80 %. Les signalements à 60 % restants concernent les routes et gestionnaires Flask, ainsi que les attributs utilisés par les bibliothèques ; ils ont été vérifiés manuellement.
- Après nettoyage : **55 tests réussis sous Python 3.11.9** ; **50 réussis et 5 ignorés sous Python 3.8.10** (Tkinter absent de ce runtime). Syntaxe des six fichiers applicatifs et outils valide pour Python 3.8.
- Mesure du cache : 1 000 contenus lus au premier passage (2,2169 s), aucun contenu relu et aucun transfert au second (0,049 s), un seul contenu relu et un fichier à transférer après modification à taille constante (0,0559 s).

## Organisation Client/Server du 17 octobre 2026

- Application, moteur, lanceur, dépendances, configuration et journal client regroupés dans `Client/`. API, stockage, dépendances, configuration et journaux serveur regroupés dans `Server/`.
- Imports des tests, benchmark, guide d'installation, exclusions Git et archive de mise à jour adaptés à cette structure. Les configurations et journaux déplacés sont identiques octet pour octet aux originaux.
- **55 tests réussis sous Python 3.11.9** ; **50 réussis et 5 ignorés sous Python 3.8.10** (Tkinter absent). Ruff et syntaxe Python 3.8 vérifiés. Le benchmark confirme toujours zéro lecture de contenu et zéro transfert au second passage.
- Le dossier parent peut être renommé : les chemins applicatifs sont résolus depuis les fichiers. Le lanceur du client reste dans son dossier, à côté de `client.py`.
