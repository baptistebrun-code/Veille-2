#!/usr/bin/env python3
"""Briefing hebdomadaire : publicité, contenus de marque et clips musicaux.

Étapes : lecture des flux RSS -> tri par l'IA -> lecture des articles retenus ->
fiches (crédits + intérêt du projet) -> page HTML (docs/index.html + archive) -> e-mail optionnel.
"""
from __future__ import annotations

import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import feedparser

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
ARCHIVE = DOCS / "archive"
SEEN_FILE = ROOT / "seen.json"
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]
LIBELLES = {"publicité": "Publicité et contenus de marque", "clip": "Clips musicaux"}
ORDRE_ROLES = ["Réalisation", "Production", "Photographie", "Agence", "Annonceur", "Artiste"]


# --------------------------------------------------------------------------- #
# 1. Collecte
# --------------------------------------------------------------------------- #
def nettoyer(texte: str, limite: int = 300) -> str:
    texte = re.sub(r"<[^>]+>", " ", texte or "")
    texte = re.sub(r"\s+", " ", html.unescape(texte)).strip()
    return texte[:limite]


def charger_vus() -> set[str]:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def sauver_vus(vus: list[str]) -> None:
    SEEN_FILE.write_text(json.dumps(vus[-4000:], ensure_ascii=False), encoding="utf-8")


def collecter(config: dict, vus: set[str]) -> list[dict]:
    limite = datetime.now(timezone.utc) - timedelta(hours=config.get("fenetre_heures", 168))
    par_source: dict[str, list[dict]] = {}
    deja: set[str] = set()

    for flux in config["flux"]:
        try:
            parsed = feedparser.parse(flux["url"], agent="Mozilla/5.0 (compatible; veille-creative/1.0)")
        except Exception as exc:  # un flux en panne ne doit pas bloquer les autres
            print(f"[avertissement] {flux['nom']} : {exc}", file=sys.stderr)
            continue
        if not parsed.entries:
            print(f"[avertissement] {flux['nom']} : aucun article lu (URL à vérifier ?)", file=sys.stderr)
            continue

        items = []
        for entree in parsed.entries:
            lien = entree.get("link")
            if not lien or lien in vus or lien in deja:
                continue
            date_struct = entree.get("published_parsed") or entree.get("updated_parsed")
            date = (datetime.fromtimestamp(time.mktime(date_struct), tz=timezone.utc)
                    if date_struct else datetime.now(timezone.utc))
            if date < limite:
                continue
            deja.add(lien)
            items.append({
                "date": date,
                "titre": nettoyer(entree.get("title", ""), 200),
                "extrait": nettoyer(entree.get("summary", ""), 250),
                "lien": lien,
                "source": flux["nom"],
                "categorie": flux["categorie"],
            })
        items.sort(key=lambda i: i["date"], reverse=True)
        par_source[flux["nom"]] = items
        print(f"{flux['nom']} : {len(items)} articles récents")

    # Entrelace les sources pour qu'une source très prolixe ne noie pas les autres
    resultat: list[dict] = []
    files = list(par_source.values())
    while any(files) and len(resultat) < 200:
        for f in files:
            if f:
                resultat.append(f.pop(0))
    return resultat[:200]


def recuperer_texte(url: str, limite: int = 5000) -> str:
    """Texte de l'article (vide si le site refuse ou si le lien est une redirection Google)."""
    if "news.google.com" in url:
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; veille-creative/1.0)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            brut = r.read(400_000).decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"[info] lecture impossible ({exc}) : {url}", file=sys.stderr)
        return ""
    brut = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", brut)
    paragraphes = re.findall(r"(?is)<p[^>]*>(.*?)</p>", brut)
    texte = " ".join(nettoyer(p, 2000) for p in paragraphes)
    if len(texte) < 300:
        texte = nettoyer(brut, limite)
    return texte[:limite]


# --------------------------------------------------------------------------- #
# 2. Sélection : mode gratuit sans IA
# --------------------------------------------------------------------------- #
MOTS_CLIP = ["music video", "video clip", "clip", "visual", "video for", "new video", "directed by"]
MOTS_PUB = ["campaign", "commercial", "advert", "spot", "brand film", "branded", "film for", "ad for",
            "publicité", "campagne"]


def selection_simple(candidats: list[dict], config: dict) -> list[dict]:
    suivi = [n.lower() for n in config.get("liste_de_suivi", [])]
    notes = []
    for c in candidats:
        titre = c["titre"].lower()
        texte = f"{titre} {c['extrait'].lower()}"
        clip = sum(1 for m in MOTS_CLIP if m in titre)
        pub = sum(1 for m in MOTS_PUB if m in titre)
        est_suivi = any(n in texte for n in suivi)
        score = clip + pub + (5 if est_suivi else 0)
        if score >= 1:
            type_ = "clip" if clip >= pub else "publicité"
            notes.append((score, {
                "titre": c["titre"], "credits": [], "pourquoi": c["extrait"], "lien": c["lien"],
                "source": c["source"], "categorie": LIBELLES[type_], "suivi": est_suivi,
            }))
    notes.sort(key=lambda x: -x[0])
    return [e for _, e in notes[: config.get("max_elements", 15)]]


# --------------------------------------------------------------------------- #
# 3. Sélection : avec IA (Gemini gratuit ou Claude)
# --------------------------------------------------------------------------- #
CONSIGNE_TRI = """Tu fais la veille d'un professionnel de l'image. On te donne des articles des 7 derniers \
jours (JSON). Choisis ceux qui présentent un PROJET CONCRET dans l'un de ces deux domaines UNIQUEMENT :
- "publicité" : film publicitaire, campagne, contenu de marque (branded content, court métrage de marque, \
shooting de campagne), tout contenu créé pour un annonceur ;
- "clip" : clip musical, court métrage musical, film d'artiste.

Écarte : cinéma et séries sans lien avec un annonceur, actualités business des agences (nominations, \
fusions, résultats, classements), tests de matériel, tutoriels, palmarès sans projet précis, rumeurs, \
doublons (un même projet couvert plusieurs fois : garde la meilleure source).

Privilégie les projets ambitieux, les signatures notables, les idées ou techniques originales.
Liste de suivi (réalisateurs, sociétés, photographes à favoriser) : {suivi}

Retourne AU PLUS {max} éléments, sous forme d'un tableau JSON et rien d'autre (pas de Markdown) :
[{{"lien": "URL exacte fournie", "type": "publicité" ou "clip"}}]"""

CONSIGNE_FICHE = """Tu rédiges des fiches pour un briefing hebdomadaire en français destiné à un professionnel \
de l'image. On te donne, pour chaque projet, le titre, la source et le texte de l'article (ou son extrait).

Retourne un tableau JSON et rien d'autre (pas de Markdown), un objet par projet :
{{"lien": "URL exacte fournie",
  "titre": "Marque ou artiste, puis titre du projet (ex. : Nike, « Winner Stays »)",
  "credits": [{{"role": "Réalisation|Production|Photographie|Agence|Annonceur|Artiste", "nom": "..."}}],
  "pourquoi": "2 à 3 phrases : ce qui rend le projet intéressant (concept, particularité, technique, casting, \
fait notable de réalisation)",
  "suivi": true ou false}}

Règles strictes :
- Les crédits (réalisateur, société de production, photographe, agence, annonceur ou artiste) ne viennent QUE \
du texte fourni. N'invente jamais un nom. Si aucun crédit n'est donné, "credits" est une liste vide.
- "Réalisation" = réalisateur ou duo ; "Production" = société de production ; "Photographie" = photographe \
(shootings, campagnes photo) ; pas de rôle hors de cette liste.
- Pour "pourquoi", appuie-toi sur le texte. S'il n'apporte rien de notable, écris une seule phrase factuelle \
plutôt que de broder.
- "suivi": true si un nom de cette liste apparaît dans le projet : {suivi}"""


def _json_depuis_texte(texte: str) -> list[dict]:
    texte = re.sub(r"^```(?:json)?|```$", "", texte.strip(), flags=re.MULTILINE).strip()
    return json.loads(texte)


def appeler_gemini(consigne: str, donnees: list[dict]) -> list[dict]:
    """Google Gemini via l'API REST (offre gratuite). Réessaie en cas de surcharge (503, 429)."""
    principal = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
    modeles = [principal] + [m for m in ("gemini-3.1-flash-lite",) if m != principal]
    corps = json.dumps({
        "systemInstruction": {"parts": [{"text": consigne}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(donnees, ensure_ascii=False)}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }).encode()

    derniere_erreur: Exception = RuntimeError("aucun modèle essayé")
    for modele in modeles:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{modele}:generateContent"
               f"?key={os.environ['GEMINI_API_KEY']}")
        for essai in range(4):
            try:
                req = urllib.request.Request(url, corps, {"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = json.load(r)
                return _json_depuis_texte(data["candidates"][0]["content"]["parts"][0]["text"])
            except urllib.error.HTTPError as exc:
                derniere_erreur = exc
                print(f"[info] Gemini {modele} : HTTP {exc.code} (essai {essai + 1}/4)", file=sys.stderr)
                if exc.code in (429, 500, 502, 503, 504):
                    time.sleep(5 * 2 ** essai)  # 5 s, 10 s, 20 s, 40 s
                else:
                    break  # erreur définitive (404, 400, 403) : modèle suivant
    raise derniere_erreur


def appeler_claude(consigne: str, donnees: list[dict]) -> list[dict]:
    """Claude (payant). Nécessite `pip install anthropic` et ANTHROPIC_API_KEY."""
    import anthropic

    reponse = anthropic.Anthropic().messages.create(
        model=MODEL,
        max_tokens=8000,
        system=consigne,
        messages=[{"role": "user", "content": json.dumps(donnees, ensure_ascii=False)}],
    )
    return _json_depuis_texte("".join(b.text for b in reponse.content if b.type == "text"))


def nettoyer_credits(credits) -> list[dict]:
    propres = []
    for c in credits or []:
        if isinstance(c, dict) and c.get("role") in ORDRE_ROLES and str(c.get("nom", "")).strip():
            propres.append({"role": c["role"], "nom": str(c["nom"]).strip()})
    propres.sort(key=lambda c: ORDRE_ROLES.index(c["role"]))
    return propres


def selectionner(candidats: list[dict], config: dict) -> list[dict]:
    fournisseur = os.environ.get("LLM_PROVIDER", "none").lower()
    if fournisseur == "none":
        return selection_simple(candidats, config)

    appeler = appeler_gemini if fournisseur == "gemini" else appeler_claude
    suivi = ", ".join(config.get("liste_de_suivi", [])) or "aucune"
    maxi = config.get("max_elements", 15)
    par_lien = {c["lien"]: c for c in candidats}

    try:
        # Étape 1 : tri sur titres et extraits
        envoi = [{k: c[k] for k in ("titre", "extrait", "lien", "source")} for c in candidats]
        choix = appeler(CONSIGNE_TRI.format(suivi=suivi, max=maxi), envoi)
        choix = [x for x in choix if x.get("lien") in par_lien][:maxi]
        print(f"{len(choix)} projets retenus au tri")
        if not choix:
            return []

        # Étape 2 : lecture des articles retenus, puis fiches détaillées
        dossiers = []
        for x in choix:
            c = par_lien[x["lien"]]
            dossiers.append({"lien": c["lien"], "titre": c["titre"], "source": c["source"],
                             "texte": recuperer_texte(c["lien"]) or c["extrait"]})
        try:
            fiches = appeler(CONSIGNE_FICHE.format(suivi=suivi), dossiers)
        except Exception as exc:
            print(f"[avertissement] fiches indisponibles ({exc}), version courte", file=sys.stderr)
            fiches = [{"lien": d["lien"], "titre": d["titre"], "credits": [], "pourquoi": d["texte"][:300]}
                      for d in dossiers]
    except Exception as exc:  # quota dépassé, réponse invalide... on ne perd pas le briefing
        print(f"[avertissement] IA indisponible ({exc}), repli sur la sélection simple", file=sys.stderr)
        return selection_simple(candidats, config)

    types = {x["lien"]: x.get("type") for x in choix}
    elements = []
    for f in fiches:
        lien = f.get("lien")
        if lien not in par_lien:  # écarte toute URL inventée
            continue
        elements.append({
            "titre": f.get("titre") or par_lien[lien]["titre"],
            "credits": nettoyer_credits(f.get("credits")),
            "pourquoi": f.get("pourquoi", ""),
            "lien": lien,
            "source": par_lien[lien]["source"],
            "categorie": LIBELLES.get(types.get(lien), LIBELLES["publicité"]),
            "suivi": bool(f.get("suivi")),
        })
    return elements


# --------------------------------------------------------------------------- #
# 4. Page HTML
# --------------------------------------------------------------------------- #
CSS = """
:root{--bg:#E9ECEF;--ink:#1B1F2A;--muted:#566070;--rule:#C3C9D2;--accent:#2F4BDE}
:root:not([data-theme=light]){color-scheme:light dark}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#12151C;--ink:#E8EAF0;--muted:#98A1B1;--rule:#2A303C;--accent:#8CA0FF}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:1.0625rem/1.6 "Source Serif 4",Georgia,serif}
main{max-width:56rem;margin:0 auto;padding:clamp(1.5rem,5vw,4rem) clamp(1.1rem,4vw,2rem) 5rem}
h1{font:800 clamp(2.2rem,7.5vw,4.4rem)/1 "Bricolage Grotesque","Helvetica Neue",Arial,sans-serif;letter-spacing:-.03em;margin:0 0 .6rem}
.sous-titre{margin:0 0 3.5rem;color:var(--muted);font:500 1rem "Bricolage Grotesque","Helvetica Neue",Arial,sans-serif}
section{display:grid;grid-template-columns:10rem 1fr;gap:0 2rem;margin-bottom:3rem}
h2{margin:.35rem 0 0;font:700 1rem/1.3 "Bricolage Grotesque","Helvetica Neue",Arial,sans-serif;color:var(--muted)}
article{padding:1.25rem 0 1.4rem 1rem;border-top:1px solid var(--rule);border-left:3px solid transparent}
article:first-child{border-top-color:var(--ink)}
article.suivi{border-left-color:var(--accent)}
h3{margin:0 0 .55rem;font:700 1.35rem/1.25 "Bricolage Grotesque","Helvetica Neue",Arial,sans-serif;letter-spacing:-.01em}
h3 a{color:inherit;text-decoration:none;background:linear-gradient(var(--accent),var(--accent)) 0 100%/0 2px no-repeat;transition:background-size .2s}
h3 a:hover,h3 a:focus-visible{background-size:100% 2px}
a:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.credits{display:flex;flex-wrap:wrap;gap:.2rem 1.6rem;margin:0 0 .7rem;font-family:"Bricolage Grotesque","Helvetica Neue",Arial,sans-serif}
.credits div{display:flex;flex-direction:column}
.credits dt{font-size:.75rem;font-weight:500;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.credits dd{margin:0;font-size:1.05rem;font-weight:700}
article p{margin:0 0 .5rem;max-width:62ch}
.meta{font:500 .875rem "Bricolage Grotesque","Helvetica Neue",Arial,sans-serif;color:var(--muted)}
.meta b{color:var(--accent);font-weight:700;margin-right:.6rem}
.vide{color:var(--muted)}
footer{border-top:1px solid var(--rule);padding-top:1.5rem;font:500 .9rem "Bricolage Grotesque","Helvetica Neue",Arial,sans-serif;color:var(--muted)}
footer a{color:var(--ink)}
footer ul{list-style:none;padding:0;margin:.6rem 0 0;display:flex;flex-wrap:wrap;gap:.4rem 1.2rem}
@media (max-width:42rem){section{grid-template-columns:1fr}h2{margin:0 0 .6rem}}
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
         'family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800'
         '&family=Source+Serif+4:opsz,wght@8..60,400&display=swap">')


def jour_fr(d: datetime) -> str:
    return "1er" if d.day == 1 else str(d.day)


def periode_fr(fin: datetime) -> str:
    debut = fin - timedelta(days=6)
    if debut.month == fin.month:
        return f"Du {jour_fr(debut)} au {jour_fr(fin)} {MOIS[fin.month - 1]}"
    return f"Du {jour_fr(debut)} {MOIS[debut.month - 1]} au {jour_fr(fin)} {MOIS[fin.month - 1]}"


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def rendre_article(e: dict) -> str:
    credits = "".join(
        f'<div><dt>{esc(c["role"])}</dt><dd>{esc(c["nom"])}</dd></div>' for c in e.get("credits", [])
    )
    bloc_credits = f'<dl class="credits">{credits}</dl>' if credits else ""
    pourquoi = f'<p>{esc(e.get("pourquoi", ""))}</p>' if e.get("pourquoi") else ""
    marque = "<b>Dans votre liste</b>" if e.get("suivi") else ""
    return (
        f'<article class="{"suivi" if e.get("suivi") else ""}">'
        f'<h3><a href="{esc(e["lien"])}" rel="noopener">{esc(e["titre"])}</a></h3>'
        f"{bloc_credits}{pourquoi}"
        f'<div class="meta">{marque}{esc(e["source"])}</div></article>'
    )


def rendre_page(elements: list[dict], d: datetime, archives: list[str], racine: str = "") -> str:
    par_categorie: dict[str, list[dict]] = {}
    for e in elements:
        par_categorie.setdefault(e["categorie"], []).append(e)

    if not elements:
        corps = '<p class="vide">Rien de marquant cette semaine.</p>'
    else:
        ordre = [LIBELLES["publicité"], LIBELLES["clip"]]
        categories = sorted(par_categorie, key=lambda c: ordre.index(c) if c in ordre else 99)
        corps = "".join(
            f"<section><h2>{esc(c)}</h2><div>{''.join(rendre_article(e) for e in par_categorie[c])}</div></section>"
            for c in categories
        )

    liens_archives = "".join(
        f'<li><a href="{racine}archive/{a}.html">Semaine du {a}</a></li>' for a in archives[:12]
    )
    pied = (f"<footer>Sélection automatique à partir des flux des sources citées. "
            f"Vérifiez les détails sur l'article d'origine.<ul>{liens_archives}</ul></footer>")

    n = len(elements)
    sous_titre = f"{n} projet{'s' if n > 1 else ''} à retenir" if n else "Semaine calme"
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Veille créative, {periode_fr(d)}</title>{FONTS}<style>{CSS}</style></head><body><main>"
        f'<h1>{periode_fr(d)}</h1><p class="sous-titre">{sous_titre}</p>{corps}{pied}</main></body></html>'
    )


def ecrire_pages(elements: list[dict], d: datetime) -> None:
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    jour = d.strftime("%Y-%m-%d")
    archives = sorted({p.stem for p in ARCHIVE.glob("*.html")} | {jour}, reverse=True)
    (ARCHIVE / f"{jour}.html").write_text(rendre_page(elements, d, archives, "../"), encoding="utf-8")
    (DOCS / "index.html").write_text(rendre_page(elements, d, archives), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 5. E-mail optionnel (variables SMTP_* définies = envoi activé)
# --------------------------------------------------------------------------- #
def envoyer_email(elements: list[dict], d: datetime) -> None:
    hote = os.environ.get("SMTP_HOST")
    if not hote or not elements:
        return
    lignes = [f"<h2>Veille créative : {periode_fr(d)}</h2>"]
    for e in elements:
        credits = " / ".join(f'{esc(c["role"])} : <b>{esc(c["nom"])}</b>' for c in e.get("credits", []))
        lignes.append(
            f'<p><a href="{esc(e["lien"])}"><b>{esc(e["titre"])}</b></a><br>'
            f'{credits + "<br>" if credits else ""}{esc(e.get("pourquoi", ""))}'
            f'<br><small>{esc(e["source"])}, {esc(e["categorie"])}</small></p>'
        )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Veille créative : {periode_fr(d)}"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.attach(MIMEText("".join(lignes), "html", "utf-8"))
    with smtplib.SMTP_SSL(hote, int(os.environ.get("SMTP_PORT", "465"))) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)


# --------------------------------------------------------------------------- #
def main() -> None:
    config = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    maintenant = datetime.now()

    vus = charger_vus()
    candidats = collecter(config, vus)
    print(f"{len(candidats)} articles candidats")

    elements = selectionner(candidats, config) if candidats else []
    print(f"{len(elements)} fiches publiées")

    ecrire_pages(elements, maintenant)
    envoyer_email(elements, maintenant)
    sauver_vus(list(vus) + [c["lien"] for c in candidats])


if __name__ == "__main__":
    main()
