"""
Génère le schéma du réseau électrique du vaisseau (pilier EnergyTech & SmartGrid).

Le réseau est modélisé comme un graphe orienté et pondéré (networkx) :
- Producteur  : Réacteur Nucléaire
- Routeurs    : Sous-stations de distribution
- Consommateurs : Cockpit, Serveurs, Commerce, Habitations, Hôpital

Chaque arête porte un poids = capacité max de la ligne (en Watts).
Le script exporte une image PNG utilisable directement dans le dossier technique.
"""

import networkx as nx
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

# ---------------------------------------------------------------------------
# 1. Définition des nœuds (catégorie = producteur / sous-station / consommateur)
# ---------------------------------------------------------------------------
NODES = {
    "Réacteur Nucléaire":        "producteur",
    "SS Cockpit":                "sous-station",
    "SS Serveurs":               "sous-station",
    "SS Quartiers":              "sous-station",
    "Secours Cockpit":           "sous-station",
    "Secours Serveurs":          "sous-station",
    "Secours Quartiers":         "sous-station",
    "Cockpit":                   "sous-station",
    "Serveurs":                  "sous-station",
    "Quartiers":                 "sous-station",
    "Poste de Commandement":     "consommateur",
    "Srv-Graphana":              "consommateur",
    "Srv-bdd":                   "consommateur",
    "Srv-backup":                "consommateur",
    "Commerce":                  "consommateur",
    "Habitations":               "consommateur",
    "Hôpital":                   "consommateur",
}

# ---------------------------------------------------------------------------
# 2. Définition des arêtes : (source, cible, capacité_max_W)
# ---------------------------------------------------------------------------
EDGES = [
    ("Réacteur Nucléaire", "SS Cockpit",    2000),
    ("Réacteur Nucléaire", "SS Serveurs",   2500),
    ("Réacteur Nucléaire", "SS Quartiers",  3000),
    ("Réacteur Nucléaire", "Secours Cockpit",    1000),
    ("Réacteur Nucléaire", "Secours Serveurs",   1200),
    ("Réacteur Nucléaire", "Secours Quartiers",  1500),
    ("SS Cockpit",         "Cockpit",       1800),
    ("SS Serveurs",        "Serveurs",      2200),
    ("SS Quartiers",       "Quartiers",     2800),
    ("Secours Cockpit",    "Cockpit",       800),
    ("Secours Serveurs",   "Serveurs",      1000),
    ("Secours Quartiers",  "Quartiers",     1200),
    ("Cockpit",            "Poste de Commandement", 1500),
    ("Serveurs",           "Srv-Graphana",          1200),
    ("Serveurs",           "Srv-bdd",               1000),
    ("Serveurs",           "Srv-backup",            800),
    ("Quartiers",          "Commerce",              2000),
    ("Quartiers",          "Habitations",           2500),
    ("Quartiers",          "Hôpital",               1800),
]

# ---------------------------------------------------------------------------
# 3. Construction du graphe
# ---------------------------------------------------------------------------
G = nx.DiGraph()
for node, category in NODES.items():
    G.add_node(node, category=category)
for src, dst, capacity in EDGES:
    G.add_edge(src, dst, capacity=capacity)

# ---------------------------------------------------------------------------
# 4. Positionnement manuel (mise en page hiérarchique lisible)
# ---------------------------------------------------------------------------
pos = {
    "Réacteur Nucléaire": (5, 4),

    "SS Cockpit":         (3.5, 3),
    "SS Serveurs":        (4.5, 3),
    "SS Quartiers":       (7.5, 3),
    
    "Secours Cockpit":    (2.5, 3),
    "Secours Serveurs":   (5.5, 3),
    "Secours Quartiers":  (6.5, 3),
    
    "Cockpit":            (3, 1),
    "Serveurs":           (5, 1),
    "Quartiers":          (7, 1),
    
    "Poste de Commandement": (3, 0),
    "Srv-Graphana":          (5, 0),
    "Srv-bdd":               (5.5, 0),
    "Srv-backup":            (4.5, 0),
    "Commerce":              (6.5, 0),
    "Habitations":           (7, 0),
    "Hôpital":               (7.5, 0),
}

# ---------------------------------------------------------------------------
# 5. Style visuel par catégorie
# ---------------------------------------------------------------------------
COLORS = {
    "producteur":    "#2ecc71",  # vert
    "sous-station":       "#3498db",  # bleu
    "consommateur":  "#c2e622",  # vert citron
}
SHAPES = {
    "producteur":   "s",   # carré
    "sous-station":      "o",   # rond
    "consommateur": "o",
}

fig, ax = plt.subplots(figsize=(12, 7))

for category, color in COLORS.items():
    nodelist = [n for n, d in G.nodes(data=True) if d["category"] == category]
    nx.draw_networkx_nodes(
        G, pos, nodelist=nodelist, node_color=color,
        node_shape=SHAPES[category], node_size=2600,
        edgecolors="black", linewidths=1.2, ax=ax,
    )

nx.draw_networkx_labels(G, pos, font_size=8, font_weight="bold", ax=ax)

nx.draw_networkx_edges(
    G, pos, arrowstyle="-|>", arrowsize=18, width=2,
    edge_color="#555555", connectionstyle="arc3,rad=0.05", ax=ax,
)

edge_labels = {(u, v): f'{d["capacity"]} W' for u, v, d in G.edges(data=True)}
nx.draw_networkx_edge_labels(G, pos, edge_labels=edge_labels, font_size=7, ax=ax)

legend_handles = [
    mpatches.Patch(color=COLORS["producteur"], label="Producteur (Réacteur)"),
    mpatches.Patch(color=COLORS["sous-station"], label="Sous-station"),
    mpatches.Patch(color=COLORS["consommateur"], label="Consommateur"),
]
ax.legend(handles=legend_handles, loc="upper left", fontsize=9, frameon=True)

ax.set_title("Schéma du réseau électrique — Vaisseau Interstellaire", fontsize=14, fontweight="bold")
ax.axis("off")

plt.tight_layout()
plt.savefig("schema_reseau_electrique.png", dpi=300, bbox_inches="tight")
print("Schéma exporté : schema_reseau_electrique.png")