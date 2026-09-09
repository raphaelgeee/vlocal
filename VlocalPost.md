# Vlocal : dix posts LinkedIn

Brouillons à relire avant publication. Un seul pronom par post (je + vous, ou je + tu). Zéro hashtag, zéro cadratin. Les chiffres viennent des logs et du dépôt, rien n'est inventé. Les titres en gras sont des repères, ils ne font pas partie du post.

---

**1. L'annonce**

Le 6 septembre 2026, j'ai rendu Vlocal gratuit et open source.

Vlocal, c'est l'app de dictée que j'ai construite pour moi : je maintiens Ctrl et Cmd, je parle, je relâche, le texte s'écrit là où est mon curseur. Dans Slack, dans un mail, dans Notion. Tout tourne sur le Mac, rien ne part sur un serveur.

Depuis juin, elle coûtait 3,99 € par mois. Je viens de retirer le paiement, les clés de licence, tout. Le code est sur GitHub, sous licence AGPL.

Pourquoi ? Trois raisons honnêtes.

Je n'avais pas d'abonné payant. Aucun. Le produit servait à moi et à quelques proches.

Je ne pouvais plus dire « rien ne sort de votre Mac » sans le prouver. Avec le code public, n'importe qui peut lire ce que l'app envoie. Il y a même un test qui échoue si quelqu'un ajoute un champ.

Et j'ai passé trois semaines à la remettre au niveau que j'exige d'un logiciel que je recommande en salle : un bug de vitesse traqué dans les logs, le paywall retiré proprement, un README qu'on peut lire sans moi.

Ce qu'elle envoie, si vous acceptez à l'installation : votre prénom, votre nom, et une fois par jour le temps qu'elle vous a fait gagner. C'est tout. Vous pouvez refuser.

Ce qu'elle ne sort jamais : l'audio, le texte, les réunions.

Mac Apple Silicon uniquement. Le lien est en commentaire.

---

**2. Le bug du 19 août**

Le 19 août, à 19 h 05, une de mes dictées a duré sept minutes.

Je ne m'en suis pas rendu compte tout de suite. Ce que j'ai vu, les jours suivants, c'est que Vlocal était devenu lent. Quatre secondes d'attente au relâchement au lieu d'une. Agaçant, pas bloquant. J'ai laissé traîner.

Hier, j'ai ouvert les logs. Trois lignes ont tout expliqué.

La dictée de sept minutes avait dépassé un délai fixe de 22 secondes que j'avais prévu pour détecter un plantage de la carte graphique. Pour l'app, dépasser ce délai voulait dire « GPU gelé ». Elle a donc basculé sur le processeur, pour toute la session. Et comme je ne redémarre jamais l'app, la session durait depuis 21 jours.

384 dictées transcrites sur le processeur, à 8 secondes de moyenne. Les 191 d'avant tournaient à 1 seconde.

Le correctif tient en deux idées. Le délai est maintenant proportionnel à la durée dictée. Et un dépassement suspend le GPU dix minutes au lieu de le condamner.

Ce que je retiens pour les PME que je forme : quand un outil devient « un peu lent », ce n'est presque jamais l'outil qui a changé. C'est un mécanisme de sécurité qui s'est déclenché et qui n'a jamais été rappelé. Il faut aller lire.

Vlocal est gratuit et open source depuis ce matin. Le correctif est dans la version 1.1.0.

---

**3. La télémétrie, en clair**

Voici la liste complète de ce que Vlocal envoie sur mes serveurs.

Votre prénom.
Votre nom.
Un identifiant aléatoire d'installation.
La version de l'app et de macOS.
Par jour : le nombre de dictées, le nombre de mots, le temps gagné estimé.

Et voici ce qu'elle n'envoie jamais.

L'audio.
Le texte dicté.
Le contenu des réunions.
Votre adresse email.
Le nom de votre Mac.

Pourquoi je vous demande votre nom, alors que l'app est gratuite ? Parce que je veux savoir qui l'utilise. Vlocal, c'est mon laboratoire. Si un directeur commercial dans l'industrie dicte deux heures par semaine avec, j'ai envie de le savoir, et peut-être de lui écrire.

Vous pouvez refuser à l'installation. Un lien « Continuer sans partager », l'app fonctionne exactement pareil.

Ce qui me tient à cœur, c'est que cette liste ne soit pas une promesse marketing. Le code est public. La fonction qui construit l'envoi fait quarante lignes. Et il y a un test qui échoue si un champ est ajouté.

Je forme des équipes à l'IA depuis deux ans. La question qui revient à chaque session, avant même « qu'est-ce que ça fait », c'est « où vont mes données ». Je voulais un outil où la réponse tient sur un écran.

Vlocal est disponible pour Mac Apple Silicon. Le lien est en commentaire.

---

**4. 96,9 %**

Une réunion d'une heure, quatre personnes autour de la table, et une question simple : qui a dit quoi ?

En juillet, Vlocal attribuait 92,3 % des mots à la bonne personne. J'ai passé trois jours en août à comprendre où partaient les 7,7 % restants.

Ce n'était pas le modèle de voix. C'était les frontières.

Quand deux personnes s'enchaînent vite, la coupure entre les deux phrases tombait parfois un mot trop tôt ou trop tard. Un « oui » attribué au mauvais interlocuteur, une fin de phrase héritée par le suivant. Multiplié par une heure de réunion, ça fait 7 %.

Le correctif : recaler chaque frontière sur la pause de parole la plus proche, à deux mots maximum. Résultat mesuré sur deux fenêtres annotées à la main : 96,9 %.

Puis un deuxième problème, découvert ce week-end. Sur cette même réunion, l'app comptait deux personnes au lieu de quatre. La méthode de comptage hésitait entre 2 et 4, avec un score de 0,296 contre 0,289, et tranchait pour 2. J'ai remplacé la méthode par une autre, fondée sur la structure du graphe des voix. Elle trouve 4. Sur les cinq autres réunions de test, à deux personnes, elle trouve 2.

Tout ça tourne en local, sur un MacBook, sans envoyer une seconde d'audio.

Vlocal est gratuit et open source depuis le 6 septembre. Si le sujet vous parle, le code de la séparation des voix est dans diarizer.py.

---

**5. Trois semaines**

J'ai passé trois semaines à préparer une mise à jour qui retire des fonctionnalités.

Le paiement. Les clés de licence. La vérification d'abonnement au démarrage. La page « Mon compte ». Les conditions de vente. Un bouton « Gérer mon abonnement » dans les réglages.

Retirer, c'est plus long qu'ajouter. Chaque garde-fou de licence était posé à un endroit précis, pour une raison précise, et il fallait comprendre la raison avant de le retirer. Huit endroits dans le code. Quatre méthodes d'API. Vingt-quatre chaînes de traduction, en français et en anglais.

Puis remplacer par quelque chose de propre. Un écran de premier lancement qui vous demande votre nom et dit exactement ce que l'app envoie. Un module de télémétrie de 150 lignes, testé. Une console d'administration qui montre trois choses : qui a installé, qui utilise, combien de temps gagné.

Puis rendre le dépôt lisible par quelqu'un d'autre que moi. Un README en deux langues. Une licence. Un guide de contribution. Une architecture écrite. Les documents internes, les enregistrements de réunions clients, les rapports d'audit : sortis du dépôt.

Vlocal est open source depuis ce matin.

Ce que j'ai appris : la qualité d'un logiciel ne se voit pas dans ce qu'il fait. Elle se voit dans ce qu'on peut en retirer sans rien casser.

---

**6. Le test qui protège la promesse**

Dans Vlocal, il y a un test qui échoue si quelqu'un ajoute un champ à ce que l'app envoie.

Il fait vingt lignes. Il construit l'envoi réel, comme l'app le ferait, avec un profil de test qui contient volontairement une adresse email, un chemin de fichier et un réglage de raccourci. Puis il vérifie deux choses : que l'envoi contient exactement les six champs déclarés, et qu'aucune des trois valeurs pièges n'apparaît nulle part dedans.

Je l'ai écrit avant la fonction d'envoi.

Pourquoi j'en parle ici, à des dirigeants de PME qui ne liront jamais ce test ?

Parce que c'est la différence entre une politique de confidentialité et une garantie. La politique de confidentialité, c'est un texte que j'écris et que vous croyez. Le test, c'est une contrainte que le code ne peut pas contourner sans que quelqu'un le voie.

Quand vous évaluez un outil d'IA pour votre équipe, posez cette question au fournisseur : qu'est-ce qui, techniquement, l'empêche d'envoyer plus que ce qu'il annonce ? La réponse est souvent « notre politique ». Ce n'est pas la même chose.

Vlocal est gratuit, open source, et tourne entièrement sur votre Mac. Le test s'appelle test_telemetry.py.

---

**7. Ce matin**

Ce matin, j'ai dicté ce post.

Pas écrit. Dicté. Ctrl et Cmd maintenus, trois minutes de parole en marchant dans mon bureau, relâché. Le texte est apparu dans LinkedIn, avec les paragraphes. J'ai relu, coupé deux phrases, corrigé un nom propre.

Je fais ça depuis juin pour tout ce que j'écris : mails, briefs, notes de formation, comptes rendus de rendez-vous. Le tableau de bord de l'app me dit que ce mois-ci j'ai dicté 41 000 mots et gagné 11 heures par rapport au clavier. La formule est simple : 40 mots par minute tapés contre 150 parlés.

Ce qui a changé ma pratique, ce n'est pas la vitesse. C'est que je parle mieux que je n'écris. Quand je dicte un mail à un client, je m'adresse à lui. Quand je le tape, je me relis en même temps et je me corrige avant d'avoir fini la phrase.

L'app s'appelle Vlocal. Je l'ai construite parce que je voulais une dictée qui reste sur mon Mac, sans compte, sans cloud. Depuis ce matin, elle est gratuite et open source.

Si vous voulez essayer : Mac Apple Silicon, un raccourci, et une première dictée de trente secondes dans une note. Le lien est en commentaire.

---

**8. Le dépôt, pour ceux qui codent**

Pour les développeurs qui me suivent : le code de Vlocal est public depuis ce matin.

17 000 lignes de Python, une interface dans un seul fichier HTML, macOS Apple Silicon.

Ce que vous y trouverez d'un peu inhabituel :

Un sous-process jetable pour le micro. PortAudio se fige quand un casque se débranche ou qu'une visio prend le périphérique. Plutôt que de gérer chaque cas, la capture tourne dans un process que l'app tue et relance en une seconde. Les trames sont déjà chez le parent, rien n'est perdu.

Un seul thread pour le GPU. MLX a ses streams Metal par thread ; charger le modèle dans un thread et l'utiliser dans un autre plante. Toutes les opérations GPU passent par une file avec priorité : la dictée passe devant un import de fichier d'une heure.

Une transcription pendant la parole. Passé 29 secondes, l'app coupe l'audio au premier vrai silence et transcrit la fenêtre en arrière-plan. Au relâchement, il ne reste que la fin. La latence ne dépend plus de la durée.

Des délais proportionnels. Toute borne de temps sur une inférence dépend de la durée d'audio qu'elle couvre. Le bug qui m'a coûté trois semaines de dictées lentes venait d'un délai fixe.

Licence AGPL, licence commerciale sur demande pour les entreprises. Le README explique comment lancer depuis les sources et quels modèles télécharger.

Les pull requests sont bienvenues, petites de préférence.

---

**9. Une seule version**

J'ai supprimé dix-neuf versions de Vlocal ce week-end.

Depuis le 17 juin, chaque mise à jour laissait son fichier d'installation sur mon stockage : vingt DMG de 280 Mo chacun, 5,6 Go au total. Personne ne téléchargeait les anciennes. L'app se met à jour toute seule vers la dernière.

Il en reste une. La 1.1.0, celle qui rend Vlocal gratuit et open source.

Je raconte ça parce que c'est le genre de détail que personne ne voit et qui dit quelque chose du soin porté à un outil. Un projet solo accumule vite : des rapports d'audit dans le dépôt, des enregistrements de test, des scripts d'expérience, des versions mortes sur un serveur. Rendre le code public m'a obligé à trier. Ce qui est personnel est sorti. Ce qui est lourd est ignoré par git. Ce qui reste, quelqu'un d'autre peut le lire.

Vingt versions en onze semaines, c'est aussi le rythme d'un logiciel qui apprend sur des usages réels. La 1.0.22 a rendu le micro incassable. La 1.0.24 a fait passer l'attribution des voix en réunion de 92 à 97 %. La 1.1.0 corrige un bug de vitesse que seuls les logs pouvaient révéler.

Si vous utilisez Vlocal, la mise à jour arrive dans l'onglet Mises à jour. Si vous ne l'utilisez pas encore : Mac Apple Silicon, gratuit, le lien est en commentaire.

---

**10. La question de la salle**

En formation, la question arrive toujours au même moment. Je montre la dictée, le texte s'écrit tout seul dans un mail, la salle sourit, et quelqu'un demande : « Et l'audio, il va où ? »

Nulle part. Il reste sur le Mac. Le modèle de reconnaissance vocale tourne sur la puce Apple, dans l'app, sans connexion. On peut couper le wifi et dicter.

Cette réponse, je la fais depuis juin. Ce qui change aujourd'hui, c'est que vous n'avez plus à me croire.

Le code de Vlocal est public. Le fichier qui gère les appels réseau tient sur un écran : une vérification de version, un bouton pour envoyer un diagnostic si vous le décidez, et un envoi quotidien de compteurs d'usage si vous l'acceptez à l'installation. Prénom, nom, nombre de dictées, temps gagné. Rien qui ressemble à du texte ou à du son.

Pour une PME, c'est le point qui débloque l'usage. La dictée, la transcription de réunion, ce sont des gains évidents. Le frein, c'est toujours la donnée. Un outil local lève le frein, et un outil local dont le code se lit le lève pour de bon.

Vlocal est gratuit et open source depuis le 6 septembre. Mac Apple Silicon. Le lien est en commentaire.
