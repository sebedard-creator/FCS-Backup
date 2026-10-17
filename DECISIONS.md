# FCS Backup — décisions validées — 17 octobre 2026

Le serveur de sauvegarde utilise Windows 7 ; viser Python 3.8 et vérifier les dépendances. Les essais ne doivent pas toucher à la copie USB en cours.

1. Corbeille serveur : conserver les fichiers supprimés pendant **48 heures**, puis les purger. Permettre leur restauration.
2. Comparer taille et date de modification ; calculer SHA-256 lorsque nécessaire, avec cache persistant. La première analyse constitue les empreintes ; les passages quotidiens ne relisent pas les contenus inchangés. Mesurer cette propriété.
3. Lier chaque transfert à son contenu ; reprendre seulement la même version. Vérifier le fichier assemblé avant remplacement atomique de l'ancienne copie.
4. Deux fichiers transférés en parallèle par défaut, nombre réglable. Réutiliser les connexions HTTP. Conserver le même accès Tailscale (adresse et port).
5. Inventaire SQLite local, mises à jour transactionnelles par fichier, incluant métadonnées, empreintes et corbeille.
6. **Refusé** : identifiants ou nouveaux emplacements par source. Les noms et l'arborescence restent inchangés ; l'utilisateur s'assure de noms de dossiers distincts.
7. Valider strictement les chemins, protéger les fichiers internes, nommer les transferts temporaires sans collision.
8. **Refusé** : rattrapage des sauvegardes manquées et nouvelles tentatives automatiques. Conserver la planification quotidienne actuelle.
9. Bouton « Réanalyser la sauvegarde » : inventaire réel du serveur, progression, erreurs, sans transfert ni suppression des médias pendant cette opération.
10. Interface utilisable pendant les opérations et après échec, états explicites, dates complètes dans le journal, bilan exact, arrêt propre conservant les transferts partiels.
11. Contrôler l'espace disque avant et pendant réception ; arrêter proprement si insuffisant. Conserver l'ancienne copie jusqu'à validation, respecter les 48 h de corbeille, afficher l'espace libre serveur.

La protection anti-déconnexion déjà corrigée reste impérative : annuler le cycle si une source est absente ou si son scan est incomplet ; aucune suppression après une erreur de transfert. Un dossier retiré de la configuration conserve sa copie serveur.
