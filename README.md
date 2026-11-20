# Jump Pilot — PC (clavier / souris)

Contrôleur **PC Windows** pour les Parrot Jumping Sumo / Jumping Night / Jumping Race.
Fonctionne comme un jeu : **ZQSD (ou WASD)** pour rouler, souris pour les actions,
sans manette. Le PC doit être connecté au **Wi-Fi du robot** (pas d'Internet nécessaire).

## Prérequis
- Windows 10/11, Python 3.8+
- PC connecté au Wi-Fi du robot (adresse par défaut `192.168.2.1`)
- Sideloadly n'est **pas** nécessaire pour cette version PC.

## Installation (sur un nouveau PC)
```powershell
git clone https://github.com/MonsieurHage/JumpPilot-PC.git
cd JumpPilot-PC
pip install -r requirements.txt
python server.py
```
Puis ouvrir http://127.0.0.1:8000 dans le navigateur.

## Commandes
- **Z / ↑** : avancer
- **S / ↓** : reculer
- **Q / ←** : tourner à gauche
- **D / →** : tourner à droite
- **Espace** : STOP (roues à zéro)
- **J** : saut haut
- **K** : saut long
- **1 / 2 / 3** : posture Jumper / Kicker / Standing
- **+ / -** : vitesse ±
- Relâcher une touche de déplacement envoie STOP.

## Hébergement sur GitHub Pages
L'interface seule (`static/index.html`) peut être hébergée sur GitHub Pages,
mais le pilotage réel nécessite le serveur Python local (pont vers le robot).
Le dépôt contient tout le nécessaire pour cloner et relancer sur n'importe quel PC.
"