"""Géométrie de la marque Vlocal : le mot « vlocal » et son symbole, la bulle.

Source UNIQUE des tracés. Les SVG de assets/ et les images de la barre des
menus et de l'icône (tools/make_icon.py) en sont dérivés ; un test vérifie que
les SVG livrés correspondent toujours à cette construction.

Le dessin est construit, pas vectorisé : chaque lettre est faite de droites,
d'arcs d'ellipse et de deux courbes libres (la queue de la bulle, le coin de la
contre-forme du a), cotés sur le logo de référence (702 x 260 px) dont on garde
le repère, puis ajustés au dixième de pixel par recouvrement avec ce logo. Cela donne des fûts
d'épaisseur constante (l, fût du a : 26,8 px ; panses : 27 px de flanc pour
24 px en haut et en bas, compensation optique classique) et des dépassements
ronds d'environ 3 px de part et d'autre de la hauteur d'x et de la ligne de base
(la bulle s'arrête sur la ligne de base, puisqu'elle s'y pose).
"""
import math

# Repères verticaux (px du logo de référence).
ASCENDER = 47.0       # haut des l
X_HEIGHT = 92.25      # haut plat du v
BASELINE = 204.18
STEM = 26.84          # épaisseur des fûts droits

# --------------------------------------------------------------------------- #
# Primitives : un tracé est une liste de sous-tracés, chacun une liste de
# segments ("M", p) / ("L", p) / ("C", c1, c2, p), fermé implicitement. Le
# remplissage est pair-impair : un sous-tracé intérieur est un trou.
# --------------------------------------------------------------------------- #
def _ell(c, rx, ry, t):
    return (c[0] + rx * math.cos(t), c[1] + ry * math.sin(t))


def _arc(c, rx, ry, t0, t1):
    """Arc d'ellipse de t0 à t1 (radians, repère écran y vers le bas) en
    cubiques de 90° au plus (erreur radiale < 0,03 %)."""
    n = max(1, math.ceil(abs(t1 - t0) / (math.pi / 2) - 1e-9))
    d = (t1 - t0) / n
    k = 4.0 / 3.0 * math.tan(d / 4.0)
    segs = []
    for i in range(n):
        a, b = t0 + i * d, t0 + (i + 1) * d
        p0, p3 = _ell(c, rx, ry, a), _ell(c, rx, ry, b)
        c1 = (p0[0] - k * rx * math.sin(a), p0[1] + k * ry * math.cos(a))
        c2 = (p3[0] + k * rx * math.sin(b), p3[1] - k * ry * math.cos(b))
        segs.append(("C", c1, c2, p3))
    return segs


def _line_ellipse_t(c, rx, ry, line, t_lo, t_hi):
    """Angle t dans [t_lo, t_hi] où l'ellipse coupe la droite y = m x + b
    (bissection : la construction garantit un seul croisement dans l'intervalle)."""
    m, b = line

    def f(t):
        x, y = _ell(c, rx, ry, t)
        return y - (m * x + b)
    lo, hi = t_lo, t_hi
    flo = f(lo)
    for _ in range(80):
        mid = (lo + hi) / 2.0
        fm = f(mid)
        if (fm < 0) == (flo < 0):
            lo, flo = mid, fm
        else:
            hi = mid
    return (lo + hi) / 2.0


def _rect(x0, y0, x1, y1):
    return [("M", (x0, y0)), ("L", (x1, y0)), ("L", (x1, y1)), ("L", (x0, y1))]


# --------------------------------------------------------------------------- #
# Lettres
# --------------------------------------------------------------------------- #
def glyph_v():
    cx = 125.64
    top_out, top_in = 59.5, 32.0           # demi-largeurs au sommet
    slope_out, slope_in = 0.4114, 0.3932   # dx/dy des flancs : l'intérieur, moins
                                           # incliné, épaissit les bras vers la pointe
    apex_y = X_HEIGHT + top_in / slope_in
    half_bottom = top_out - slope_out * (BASELINE - X_HEIGHT)
    return [[("M", (cx - top_out, X_HEIGHT)), ("L", (cx - top_in, X_HEIGHT)),
             ("L", (cx, apex_y)), ("L", (cx + top_in, X_HEIGHT)),
             ("L", (cx + top_out, X_HEIGHT)), ("L", (cx + half_bottom, BASELINE)),
             ("L", (cx - half_bottom, BASELINE))]]


def glyph_l(left):
    return [_rect(left, ASCENDER, left + STEM, BASELINE)]


# La bulle : cercle posé sur la ligne de base, contre-forme ovale, et une queue
# qui prolonge le flanc gauche puis file vers le bas à gauche.
BUBBLE_C = (307.27, 146.46)
BUBBLE_R = BASELINE - 146.46          # 57,7 : le cercle tangente la ligne de base
BUBBLE_IN = ((307.11, 146.75), 30.39, 33.55)
TAIL_START = 159.5                    # fin de la partie droite du flanc gauche
TAIL_TIP = (228.7, BASELINE)
TAIL_C1 = 22.2                        # poignée verticale au départ
TAIL_C2 = (9.6, 2.25)                 # poignée d'arrivée (dx, dy) depuis la pointe


def glyph_bubble():
    c, r = BUBBLE_C, BUBBLE_R
    left_x = c[0] - r
    outer = [("M", (c[0], BASELINE))]
    outer += _arc(c, r, r, math.pi / 2, -math.pi)          # bas -> droite -> haut -> gauche
    outer += [("L", (left_x, TAIL_START)),
              ("C", (left_x, TAIL_START + TAIL_C1),
               (TAIL_TIP[0] + TAIL_C2[0], TAIL_TIP[1] - TAIL_C2[1]), TAIL_TIP)]
    ic, irx, iry = BUBBLE_IN
    inner = [("M", _ell(ic, irx, iry, 0))] + _arc(ic, irx, iry, 0, 2 * math.pi)
    return [outer, inner]


def glyph_c():
    c = (433.88, 148.0)
    orx, ory, irx, iry = 58.19, 59.28, 31.02, 33.92
    slope, gap, x_ref = 0.382, 21.23, 470.0
    top = (-slope, c[1] - gap + slope * x_ref)             # coupe de la terminaison haute
    bot = (slope, c[1] + gap - slope * x_ref)              # coupe basse (symétrique)
    to_ = _line_ellipse_t(c, orx, ory, top, -math.pi / 2, 0)
    bo_ = _line_ellipse_t(c, orx, ory, bot, 0, math.pi / 2)
    ti_ = _line_ellipse_t(c, irx, iry, top, -math.pi / 2, 0)
    bi_ = _line_ellipse_t(c, irx, iry, bot, 0, math.pi / 2)
    path = [("M", _ell(c, orx, ory, to_))]
    path += _arc(c, orx, ory, to_, bo_ - 2 * math.pi)       # par la gauche
    path += [("L", _ell(c, irx, iry, bi_ - 2 * math.pi))]
    path += _arc(c, irx, iry, bi_ - 2 * math.pi, ti_)
    return [path]


def glyph_a():
    stem_l, stem_r = 593.5 - STEM, 593.5
    arch_c, arch_rx, arch_ry = (stem_r - 49.82, 135.7), 49.82, 47.18
    in_c, in_rx, in_ry = (stem_l - 22.65, 135.61), 22.65, 23.08
    term = (0.323, 120.08 - 0.323 * 502.5)                  # coupe de la terminaison
    bowl_c, bowl_rx, bowl_ry = (533.61, 172.23), 39.8, 35.44
    bowl_top = bowl_c[1] - bowl_ry
    t_to = _line_ellipse_t(arch_c, arch_rx, arch_ry, term, -math.pi, -math.pi / 2)
    t_ti = _line_ellipse_t(in_c, in_rx, in_ry, term, -math.pi, -math.pi / 2)
    # Pied de la panse : où l'ellipse rejoint le fût.
    t_n = math.acos((stem_l - bowl_c[0]) / bowl_rx)
    path = [("M", _ell(arch_c, arch_rx, arch_ry, t_to))]
    path += _arc(arch_c, arch_rx, arch_ry, t_to, 0)
    path += [("L", (stem_r, BASELINE)), ("L", (stem_l, BASELINE)),
             ("L", _ell(bowl_c, bowl_rx, bowl_ry, t_n))]
    path += _arc(bowl_c, bowl_rx, bowl_ry, t_n, 1.5 * math.pi)
    path += [("L", (stem_l, bowl_top)), ("L", (stem_l, in_c[1]))]
    path += _arc(in_c, in_rx, in_ry, 0, t_ti)
    # Contre-forme de la panse : ovale à gauche, coin franc contre le fût.
    k_c, k_rx, k_ry = (542.34, 170.43), 21.01, 13.2
    k_top, k_bot = k_c[1] - k_ry, k_c[1] + k_ry
    counter = [("M", (k_c[0], k_top)), ("L", (stem_l, k_top)), ("L", (stem_l, k_top + 9.25)),
               ("C", (stem_l, k_bot - 2.78), (stem_l - 10.8, k_bot), (k_c[0], k_bot))]
    counter += _arc(k_c, k_rx, k_ry, math.pi / 2, 1.5 * math.pi)
    return [path, counter]


def wordmark():
    """Sous-tracés du mot entier, dans le repère de référence."""
    return (glyph_v() + glyph_l(195.16) + glyph_bubble() + glyph_c()
            + glyph_a() + glyph_l(642.0 - STEM))


def symbol():
    return glyph_bubble()


def bounds(subpaths):
    """Cadre d'encre (sur le tracé aplati : les poignées des courbes débordent
    de la courbe et fausseraient le cadre)."""
    pts = [p for poly in flatten(subpaths) for p in poly]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


# --------------------------------------------------------------------------- #
# Sorties
# --------------------------------------------------------------------------- #
def _fmt(v):
    s = "%.2f" % v
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def svg_d(subpaths, dx=0.0, dy=0.0):
    out = []
    for sp in subpaths:
        for seg in sp:
            pts = " ".join("%s %s" % (_fmt(p[0] - dx), _fmt(p[1] - dy)) for p in seg[1:])
            out.append(seg[0] + pts)
        out.append("Z")
    return "".join(out)


def svg_document(subpaths, title):
    """SVG autonome en currentColor, cadré au plus juste sur l'encre."""
    x0, y0, x1, y1 = bounds(subpaths)
    w, h = x1 - x0, y1 - y0
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %s %s" '
            'role="img" aria-label="%s"><path fill="currentColor" '
            'fill-rule="evenodd" d="%s"/></svg>\n'
            % (_fmt(w), _fmt(h), title, svg_d(subpaths, x0, y0)))




def flatten(subpaths, steps=24):
    """Polygones (une liste de points par sous-tracé) pour un rendu sans moteur
    vectoriel (contrôle de fidélité, tests)."""
    polys = []
    for sp in subpaths:
        pts, cur = [], None
        for seg in sp:
            if seg[0] in ("M", "L"):
                cur = seg[1]
                pts.append(cur)
            else:
                c1, c2, p = seg[1], seg[2], seg[3]
                for i in range(1, steps + 1):
                    t = i / steps
                    u = 1 - t
                    pts.append((u ** 3 * cur[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t ** 3 * p[0],
                                u ** 3 * cur[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t ** 3 * p[1]))
                cur = p
        polys.append(pts)
    return polys
