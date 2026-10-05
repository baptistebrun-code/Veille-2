#!/usr/bin/env python3
"""Briefing quotidien : les gros projets du cinéma, de la production et de la photo.

Étapes : lecture des flux RSS -> sélection et résumé par Claude -> page HTML
(docs/index.html + archive datée) -> e-mail optionnel.
"""
from __future__ import annotations

import html
import json
import os
import re
import smtplib
import sys
import time
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

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]


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
    SEEN_FILE.write_text(json.dumps(vus[-3000:], ensure_ascii=False), encoding="utf-8")


def collecter(config: dict, vus: set[str]) -> list[dict]:
    limite = datetime.now(timezone.utc) - timedelta(hours=config.get("fenetre_heures", 36))
    candidats: dict[str, dict] = {}

    for flux in config["flux"]:
        try:
            parsed = feedparser.parse(flux["url"], agent="veille-creative/1.0")
        except Exception as exc:  # un flux en panne ne doit pas bloquer les autres
            print(f"[avertissement] {flux['nom']} : {exc}", file=sys.stderr)
            continue
        if not parsed.entries:
            print(f"[avertissement] {flux['nom']} : aucun article lu (URL à vérifier ?)", file=sys.stderr)
            continue

        for entree in parsed.entries:
            lien = entree.get("link")
            if not lien or lien in vus or lien in candidats:
                continue
            date_struct = entree.get("published_parsed") or entree.get("updated_parsed")
            if date_struct:
                date = datetime.fromtimestamp(time.mktime(date_struct), tz=timezone.utc)
                if date < limite:
                    continue
            candidats[lien] = {
                "titre": nettoyer(entree.get("title", ""), 200),
                "extrait": nettoyer(entree.get("summary", ""), 300),
                "lien": lien,
                "source": flux["nom"],
                "categorie": flux["categorie"],
            }
    return list(candidats.values())[:150]


# --------------------------------------------------------------------------- #
# 2. Sélection et résumé par Claude
# --------------------------------------------------------------------------- #
CONSIGNE = """Tu prépares un briefing quotidien en français pour un professionnel de l'image \
(cinéma, production, publicité, photographie).

On te donne une liste d'articles récents. Choisis ceux qui annoncent ou documentent un GROS PROJET : \
sortie ou annonce de film, série, documentaire, clip, campagne publicitaire, série photo, exposition, \
livre photo, nouveau projet d'une société de production, d'un réalisateur ou d'un photographe.

Écarte : rumeurs sans substance, tests de matériel, bons plans, tutoriels, résultats d'audience, \
polémiques people, doublons (un même projet couvert par plusieurs sources : garde la meilleure source).

Liste de suivi (réalisateurs, sociétés, photographes à privilégier) : {suivi}
Si un élément retenu concerne un nom de cette liste, mets "suivi": true.

Retourne AU PLUS {max} éléments, du plus important au moins important, sous forme d'un tableau JSON \
et rien d'autre (pas de texte autour, pas de balises Markdown). Chaque élément :
{{"titre": "titre clair en français (le nom du projet en premier)",
  "resume": "deux phrases maximum : de quoi il s'agit, qui est derrière, quand ça sort si c'est connu",
  "lien": "URL exacte fournie",
  "source": "nom de la source fournie",
  "categorie": "catégorie fournie",
  "suivi": true ou false}}

Ne rien inventer : base-toi uniquement sur le titre et l'extrait fournis. Si une date de sortie n'y est \
pas, ne la mentionne pas."""


MOTS_CLES = [
    "trailer", "teaser", "first look", "premiere", "release date", "announces", "announced",
    "series", "documentary", "feature film", "campaign", "exhibition", "photobook", "music video",
    "greenlit", "acquires", "bande-annonce", "sortie", "nouveau film", "exposition",
]


def selection_simple(candidats: list[dict], config: dict) -> list[dict]:
    """Mode gratuit sans IA : mots-clés + liste de suivi, extrait d'origine (en anglais)."""
    suivi = [n.lower() for n in config.get("liste_de_suivi", [])]
    notes = []
    for c in candidats:
        texte = f"{c['titre']} {c['extrait']}".lower()
        est_suivi = any(n in texte for n in suivi)
        score = (5 if est_suivi else 0) + sum(1 for m in MOTS_CLES if m in c["titre"].lower())
        if score >= 1:
            notes.append((score, {
                "titre": c["titre"], "resume": c["extrait"], "lien": c["lien"],
                "source": c["source"], "categorie": c["categorie"], "suivi": est_suivi,
            }))
    notes.sort(key=lambda x: -x[0])
    return [e for _, e in notes[: config.get("max_elements", 18)]]


def _json_depuis_texte(texte: str) -> list[dict]:
    texte = re.sub(r"^```(?:json)?|```$", "", texte.strip(), flags=re.MULTILINE).strip()
    return json.loads(texte)


def appeler_gemini(consigne: str, candidats: list[dict]) -> list[dict]:
    """Google Gemini via l'API REST (offre gratuite, pas de dépendance supplémentaire)."""
    import urllib.request

    modele = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{modele}:generateContent"
           f"?key={os.environ['GEMINI_API_KEY']}")
    corps = {
        "systemInstruction": {"parts": [{"text": consigne}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(candidats, ensure_ascii=False)}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }
    req = urllib.request.Request(url, json.dumps(corps).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    return _json_depuis_texte(data["candidates"][0]["content"]["parts"][0]["text"])


def appeler_claude(consigne: str, candidats: list[dict]) -> list[dict]:
    """Claude (payant). Nécessite `pip install anthropic` et ANTHROPIC_API_KEY."""
    import anthropic

    reponse = anthropic.Anthropic().messages.create(
        model=os.environ.get("CLAUDE_MODEL", MODEL),
        max_tokens=6000,
        system=consigne,
        messages=[{"role": "user", "content": json.dumps(candidats, ensure_ascii=False)}],
    )
    return _json_depuis_texte("".join(b.text for b in reponse.content if b.type == "text"))


def selectionner(candidats: list[dict], config: dict) -> list[dict]:
    fournisseur = os.environ.get("LLM_PROVIDER", "none").lower()
    if fournisseur == "none":
        return selection_simple(candidats, config)

    consigne = CONSIGNE.format(
        suivi=", ".join(config.get("liste_de_suivi", [])) or "aucune",
        max=config.get("max_elements", 18),
    )
    try:
        elements = (appeler_gemini if fournisseur == "gemini" else appeler_claude)(consigne, candidats)
    except Exception as exc:  # quota dépassé, réponse invalide... on ne perd pas le briefing du jour
        print(f"[avertissement] IA indisponible ({exc}), repli sur la sélection simple", file=sys.stderr)
        return selection_simple(candidats, config)

    liens_valides = {c["lien"] for c in candidats}
    return [e for e in elements if e.get("lien") in liens_valides]  # écarte toute URL inventée


# --------------------------------------------------------------------------- #
# 3. Page HTML
# --------------------------------------------------------------------------- #
CSS = """
:root{--bg:#E9ECEF;--ink:#1B1F2A;--muted:#566070;--rule:#C3C9D2;--accent:#2F4BDE}
:root:not([data-theme=light]){color-scheme:light dark}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#12151C;--ink:#E8EAF0;--muted:#98A1B1;--rule:#2A303C;--accent:#8CA0FF}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:1.0625rem/1.6 "Source Serif 4",Georgia,serif}
main{max-width:56rem;margin:0 auto;padding:clamp(1.5rem,5vw,4rem) clamp(1.1rem,4vw,2rem) 5rem}
h1{font:800 clamp(2.6rem,9vw,5.5rem)/.95 "Bricolage Grotesque","Helvetica Neue",Arial,sans-serif;letter-spacing:-.03em;margin:0 0 .6rem}
.sous-titre{margin:0 0 3.5rem;color:var(--muted);font:500 1rem "Bricolage Grotesque","Helvetica Neue",Arial,sans-serif}
section{display:grid;grid-template-columns:10rem 1fr;gap:0 2rem;margin-bottom:3rem}
h2{margin:.35rem 0 0;font:700 1rem/1.3 "Bricolage Grotesque","Helvetica Neue",Arial,sans-serif;color:var(--muted)}
article{padding:1.25rem 0 1.4rem 1rem;border-top:1px solid var(--rule);border-left:3px solid transparent}
article:first-child{border-top-color:var(--ink)}
article.suivi{border-left-color:var(--accent)}
h3{margin:0 0 .4rem;font:700 1.35rem/1.25 "Bricolage Grotesque","Helvetica Neue",Arial,sans-serif;letter-spacing:-.01em}
h3 a{color:inherit;text-decoration:none;background:linear-gradient(var(--accent),var(--accent)) 0 100%/0 2px no-repeat;transition:background-size .2s}
h3 a:hover,h3 a:focus-visible{background-size:100% 2px}
a:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
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


def date_fr(d: datetime) -> str:
    jour = "1er" if d.day == 1 else str(d.day)
    return f"{JOURS[d.weekday()].capitalize()} {jour} {MOIS[d.month - 1]}"


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def rendre_page(elements: list[dict], d: datetime, archives: list[str], racine: str = "") -> str:
    par_categorie: dict[str, list[dict]] = {}
    for e in elements:
        par_categorie.setdefault(e["categorie"], []).append(e)

    if not elements:
        corps = '<p class="vide">Rien de marquant dans les dernières 36 heures.</p>'
    else:
        blocs = []
        for categorie, items in par_categorie.items():
            articles = "".join(
                f'<article class="{"suivi" if e.get("suivi") else ""}">'
                f'<h3><a href="{esc(e["lien"])}" rel="noopener">{esc(e["titre"])}</a></h3>'
                f'<p>{esc(e["resume"])}</p>'
                f'<div class="meta">{"<b>Dans votre liste</b>" if e.get("suivi") else ""}{esc(e["source"])}</div>'
                f"</article>"
                for e in items
            )
            blocs.append(f"<section><h2>{esc(categorie)}</h2><div>{articles}</div></section>")
        corps = "".join(blocs)

    liens_archives = "".join(
        f'<li><a href="{racine}archive/{a}.html">{a}</a></li>' for a in archives[:14]
    )
    pied = (f"<footer>Sélection automatique à partir des flux des sources citées. "
            f"Vérifiez les détails sur l'article d'origine.<ul>{liens_archives}</ul></footer>")

    n = len(elements)
    sous_titre = f"{n} projet{'s' if n > 1 else ''} à retenir" if n else "Journée calme"
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Veille créative, {date_fr(d)}</title>{FONTS}<style>{CSS}</style></head><body><main>"
        f"<h1>{date_fr(d)}</h1><p class=\"sous-titre\">{sous_titre}</p>{corps}{pied}</main></body></html>"
    )


def ecrire_pages(elements: list[dict], d: datetime) -> None:
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    jour = d.strftime("%Y-%m-%d")
    archives = sorted({p.stem for p in ARCHIVE.glob("*.html")} | {jour}, reverse=True)
    (ARCHIVE / f"{jour}.html").write_text(rendre_page(elements, d, archives, "../"), encoding="utf-8")
    (DOCS / "index.html").write_text(rendre_page(elements, d, archives), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 4. E-mail optionnel (variables SMTP_* définies = envoi activé)
# --------------------------------------------------------------------------- #
def envoyer_email(elements: list[dict], d: datetime) -> None:
    hote = os.environ.get("SMTP_HOST")
    if not hote or not elements:
        return
    lignes = [f"<h2>Veille créative, {date_fr(d)}</h2>"]
    for e in elements:
        lignes.append(
            f'<p><a href="{esc(e["lien"])}"><b>{esc(e["titre"])}</b></a><br>{esc(e["resume"])}'
            f'<br><small>{esc(e["source"])}, {esc(e["categorie"])}</small></p>'
        )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Veille créative du {date_fr(d)}"
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
    print(f"{len(elements)} éléments retenus")

    ecrire_pages(elements, maintenant)
    envoyer_email(elements, maintenant)
    sauver_vus(list(vus) + [c["lien"] for c in candidats])


if __name__ == "__main__":
    main()
