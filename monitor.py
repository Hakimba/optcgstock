#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Moniteur de stock / nouveaux produits multi-boutiques.

Ce que fait le script, à chaque passage (par défaut toutes les 5 minutes) :
  1. Pour chaque "source" déclarée dans config.ini (une source = une boutique
     + une collection/catégorie), récupère la liste des produits via l'API
     publique de la plateforme :
        - Shopify      -> /collections/<handle>/products.json
        - WooCommerce  -> /wp-json/wc/store/v1/products  (repli HTML possible)
  2. Compare l'état actuel à celui du passage précédent (mémorisé sur disque).
  3. Envoie une notification Telegram quand :
        - un NOUVEAU produit apparaît dans la collection -> "Nouveau produit"
        - un produit connu passe de "épuisé" à "en stock" -> "Retour en stock"

Chaque source est isolée : si une boutique est en panne, les autres continuent
d'être surveillées et l'état de la boutique en panne est conservé intact.

Aucun achat n'est effectué : le script se contente de t'alerter.

Usage :
    python monitor.py              # surveillance en continu
    python monitor.py --once       # un seul passage réel (utilisé par le cron)
    python monitor.py --test       # message Telegram de test + aperçu, sans
                                   #   jamais modifier l'état
    python monitor.py --heartbeat  # récapitulatif Telegram depuis l'état,
                                   #   sans interroger les boutiques
"""

import argparse
import configparser
import html
import json
import os
import sys
import time
import traceback
from datetime import datetime

try:
    import requests
    from requests.adapters import HTTPAdapter
    try:
        from urllib3.util.retry import Retry
    except ImportError:  # très vieilles versions
        from requests.packages.urllib3.util.retry import Retry
except ImportError:
    print("Le module 'requests' est manquant. Installe-le avec :")
    print("    pip install requests")
    sys.exit(1)

# Timeout (connexion, lecture) en secondes, et nombre de tentatives réseau.
# Surchargés depuis config.ini au lancement.
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_BACKOFF = 4  # secondes entre deux tentatives (croissant)

# Version du format de state.json. Sert à repérer (et ignorer proprement) un
# état écrit par l'ancienne version mono-boutique du script.
STATE_VERSION = 2


def http_get(session, url):
    """
    GET robuste : réessaie sur timeout / erreur réseau passagère, avec
    temporisation croissante. Lève la dernière exception si tout échoue.
    """
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_err = e
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF * attempt
                log(f"  tentative {attempt}/{MAX_RETRIES} échouée ({type(e).__name__}), "
                    f"nouvel essai dans {wait}s…")
                time.sleep(wait)
    raise last_err

# --- Emplacement des fichiers (à côté du script) ---------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.ini")
STATE_PATH = os.path.join(HERE, "state.json")
LOG_PATH = os.path.join(HERE, "monitor.log")


# ---------------------------------------------------------------------------
#  Utilitaires
# ---------------------------------------------------------------------------
def log(msg):
    """Écrit dans la console ET dans monitor.log, avec horodatage."""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config():
    if not os.path.exists(CONFIG_PATH):
        log(f"ERREUR : fichier de config introuvable : {CONFIG_PATH}")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    return cfg


def load_sources(cfg):
    """
    Lit toutes les sections [source:xxx] de config.ini.
    Une source = une boutique + une collection/catégorie à surveiller.
    """
    sources = []
    for section in cfg.sections():
        if not section.startswith("source:"):
            continue
        key = section.split(":", 1)[1].strip()
        get = lambda o, d="": cfg.get(section, o, fallback=d).strip()  # noqa: E731
        if not cfg.getboolean(section, "enabled", fallback=True):
            log(f"Source '{key}' désactivée (enabled = false), ignorée.")
            continue
        sources.append({
            "key": key,
            "label": get("label", key),
            "platform": get("platform", "shopify").lower(),
            "base_url": get("base_url").rstrip("/"),
            "collection": get("collection"),          # Shopify
            "category_slug": get("category_slug"),    # WooCommerce
            "category_id": get("category_id"),        # WooCommerce
            "page_url": get("page_url"),              # WooCommerce, repli HTML
            "method": get("method", "api").lower(),   # WooCommerce
            "currency": get("currency", "€"),
        })
    return sources


def load_state():
    """
    Renvoie un dict {clé_source: {id_produit: {...}}}.
    Un état écrit par l'ancienne version (dict plat de produits) est ignoré :
    chaque source repartira sur une baseline, donc sans alerte parasite.
    """
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        log("État précédent illisible, on repart de zéro.")
        return {}

    if isinstance(data, dict) and data.get("version") == STATE_VERSION:
        return data.get("sources", {})

    log("État précédent au format mono-boutique : ignoré, chaque source "
        "repart sur une baseline (aucune alerte ne sera envoyée pour ce "
        "premier passage).")
    return {}


def save_state(sources_state):
    tmp = STATE_PATH + ".tmp"
    payload = {"version": STATE_VERSION, "sources": sources_state}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)  # écriture atomique


# ---------------------------------------------------------------------------
#  Notification Telegram
# ---------------------------------------------------------------------------
def send_telegram(cfg, text):
    # Priorité aux variables d'environnement (utilisées par GitHub Actions via
    # les "Secrets"), sinon on retombe sur config.ini (usage local).
    token = (os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
             or cfg.get("telegram", "bot_token", fallback="").strip())
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID", "").strip()
               or cfg.get("telegram", "chat_id", fallback="").strip())
    if not token or not chat_id or token.startswith("METS_"):
        log("Telegram non configuré (bot_token / chat_id manquants) — message non envoyé :")
        log("    " + text.replace("\n", " | "))
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "false",
            },
            timeout=20,
        )
        if r.status_code == 200 and r.json().get("ok"):
            return True
        log(f"Telegram a répondu {r.status_code} : {r.text[:300]}")
        return False
    except requests.RequestException as e:
        log(f"Échec envoi Telegram : {e}")
        return False


# ---------------------------------------------------------------------------
#  Récupération des produits — Shopify
# ---------------------------------------------------------------------------
def format_price(raw, currency):
    """
    Met en forme un prix Shopify (chaîne type '89.90').
    Un prix à 0 signifie "pas encore tarifé" (préco) : on n'affiche rien
    plutôt qu'un trompeur « 0.00 € ».
    """
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return ""
    if val <= 0:
        return ""
    return f"{val:.2f} {currency}"


def normalize_shopify_product(p, src):
    """
    Un produit est "en stock" dès qu'au moins une de ses variantes l'est.
    Le prix affiché est celui d'une variante disponible si possible, sinon
    le plus bas parmi toutes les variantes.
    """
    variants = p.get("variants") or []
    dispo = [v for v in variants if v.get("available")]
    in_stock = bool(dispo)

    if dispo:
        prix_src = dispo[0].get("price")
    else:
        prix = []
        for v in variants:
            try:
                val = float(v.get("price"))
            except (TypeError, ValueError):
                continue
            if val > 0:
                prix.append(val)
        prix_src = min(prix) if prix else None

    handle = p.get("handle", "")
    return {
        "id": str(p.get("id")),
        "name": (p.get("title") or "").strip(),
        "in_stock": in_stock,
        "price": format_price(prix_src, src["currency"]),
        "url": f"{src['base_url']}/products/{handle}" if handle else src["base_url"],
    }


def fetch_products_shopify(src, session):
    """
    Récupère les produits d'une collection Shopify via l'endpoint public
    /collections/<handle>/products.json (JSON stable, léger pour la boutique).
    """
    handle = src["collection"]
    if not handle:
        raise ValueError(f"Source '{src['key']}' : 'collection' manquant.")

    produits = []
    page = 1
    while page <= 20:  # garde-fou anti-boucle
        url = (f"{src['base_url']}/collections/{handle}"
               f"/products.json?limit=250&page={page}")
        lot = http_get(session, url).json().get("products", [])
        if not lot:
            break
        produits.extend(normalize_shopify_product(p, src) for p in lot)
        if len(lot) < 250:
            break
        page += 1
    return produits


# ---------------------------------------------------------------------------
#  Récupération des produits — WooCommerce
# ---------------------------------------------------------------------------
def resolve_category_id(base, slug, session):
    url = f"{base}/wp-json/wc/store/v1/products/categories?per_page=100"
    r = http_get(session, url)
    for cat in r.json():
        if cat.get("slug") == slug:
            return cat.get("id")
    return None


def normalize_woo_product(p):
    prices = p.get("prices", {}) or {}
    minor = prices.get("currency_minor_unit", 2)
    raw = prices.get("price")
    price_txt = ""
    if raw not in (None, ""):
        try:
            val = int(raw) / (10 ** int(minor))
            sym = prices.get("currency_symbol", "€")
            price_txt = f"{val:.2f} {sym}" if val > 0 else ""
        except (ValueError, TypeError):
            price_txt = str(raw)
    return {
        # WooCommerce renvoie les noms encodés ("&#8211;" pour un tiret long).
        # On les décode ici, sinon esc() les ré-échapperait et Telegram
        # afficherait l'entité brute au lieu du caractère.
        "id": str(p.get("id")),
        "name": html.unescape((p.get("name") or "").strip()),
        "in_stock": bool(p.get("is_in_stock")),
        "price": price_txt,
        "url": p.get("permalink", ""),
    }


def fetch_products_woo_api(src, session):
    """Récupère les produits via l'API Store WooCommerce (JSON)."""
    base = src["base_url"]
    slug = src["category_slug"]
    cat_id = src["category_id"]

    if slug:
        try:
            resolved = resolve_category_id(base, slug, session)
            if resolved:
                cat_id = str(resolved)
        except Exception as e:
            log(f"Résolution du slug impossible ({e}), on garde category_id={cat_id}")

    if not cat_id:
        raise ValueError("Aucun category_id/category_slug utilisable.")

    produits = []
    page = 1
    while True:
        url = (f"{base}/wp-json/wc/store/v1/products"
               f"?category={cat_id}&per_page=100&page={page}")
        r = http_get(session, url)
        lot = r.json()
        if not isinstance(lot, list) or not lot:
            break
        produits.extend(normalize_woo_product(p) for p in lot)
        total_pages = int(r.headers.get("X-WP-TotalPages", "1") or "1")
        if page >= total_pages:
            break
        page += 1
    return produits


def fetch_products_woo_html(src, session):
    """Repli : parsing HTML de la page catégorie (thème WooCommerce standard)."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise RuntimeError("Repli HTML indisponible : pip install beautifulsoup4")
    url = src["page_url"]
    if not url:
        raise ValueError("Aucune page_url configurée pour le repli HTML.")

    produits = []
    page = 1
    while page <= 20:  # garde-fou anti-boucle
        page_url = url if page == 1 else f"{url.rstrip('/')}/page/{page}/"
        try:
            r = http_get(session, page_url)
        except requests.HTTPError as e:
            # Une 404 = plus de pages à parcourir (fin normale de pagination)
            if e.response is not None and e.response.status_code == 404:
                break
            raise
        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select("li.product")
        if not items:
            break
        for li in items:
            classes = li.get("class", [])
            title_el = li.select_one(
                ".woocommerce-loop-product__title, h2, h3, .product-title")
            link_el = li.select_one("a.woocommerce-LoopProduct-link, a[href]")
            price_el = li.select_one(".price")
            name = title_el.get_text(strip=True) if title_el else ""
            if not name:
                continue
            in_stock = "outofstock" not in classes  # classe ajoutée par WooCommerce
            txt = li.get_text(" ", strip=True).lower()
            if "épuisé" in txt or "epuise" in txt or "rupture" in txt:
                in_stock = False
            pid = li.get("data-product-id") or (link_el["href"] if link_el else name)
            produits.append({
                "id": str(pid),
                "name": name,
                "in_stock": in_stock,
                "price": price_el.get_text(" ", strip=True) if price_el else "",
                "url": link_el["href"] if link_el else url,
            })
        page += 1
    return produits


def fetch_products(src, session):
    """Aiguillage vers la bonne plateforme. Renvoie (produits, méthode)."""
    plateforme = src["platform"]
    if plateforme == "shopify":
        return fetch_products_shopify(src, session), "shopify"
    if plateforme == "woocommerce":
        if src["method"] == "html":
            return fetch_products_woo_html(src, session), "html"
        try:
            return fetch_products_woo_api(src, session), "api"
        except Exception as e:
            log(f"  API Store indisponible ({e}). Tentative de repli HTML…")
            return fetch_products_woo_html(src, session), "html"
    raise ValueError(f"Plateforme inconnue : '{plateforme}' "
                     f"(attendu : shopify ou woocommerce)")


# ---------------------------------------------------------------------------
#  Comparaison et notifications
# ---------------------------------------------------------------------------
def esc(s):
    return html.escape(str(s))


def build_new_product_msg(p, src):
    stock = "✅ EN STOCK" if p["in_stock"] else "⛔ épuisé"
    price = f" — {esc(p['price'])}" if p["price"] else ""
    return (
        f"🆕 <b>Nouveau produit</b> — {esc(src['label'])}\n"
        f"<b>{esc(p['name'])}</b>{price}\n"
        f"Statut : {stock}\n"
        f"{esc(p['url'])}"
    )


def build_restock_msg(p, src):
    price = f" — {esc(p['price'])}" if p["price"] else ""
    return (
        f"🔔 <b>Retour en stock</b> — {esc(src['label'])}\n"
        f"<b>{esc(p['name'])}</b>{price}\n"
        f"{esc(p['url'])}"
    )


def run_source(cfg, src, session, state, notify=True):
    """
    Traite UNE source. Renvoie un dict de compte-rendu.
    En cas d'échec réseau, l'état précédent de la source est laissé intact :
    sans cela, ses produits sembleraient avoir disparu puis réapparaîtraient
    tous comme "nouveaux" au passage suivant.
    """
    cr = {"key": src["key"], "label": src["label"], "ok": False,
          "total": 0, "en_stock": 0, "events": [], "baseline": False,
          "erreur": None, "produits": []}
    try:
        produits, methode = fetch_products(src, session)
    except Exception as e:
        cr["erreur"] = f"{type(e).__name__}: {e}"
        log(f"  [{src['label']}] ÉCHEC : {cr['erreur']}")
        log(f"  [{src['label']}] état précédent conservé "
            f"({len(state.get(src['key'], {}))} produit(s) mémorisé(s)).")
        return cr

    ancien = state.get(src["key"])
    premier_passage = ancien is None
    ancien = ancien or {}

    nouvel_etat = {}
    events = []
    for p in produits:
        pid = p["id"]
        nouvel_etat[pid] = {
            "name": p["name"],
            "in_stock": p["in_stock"],
            "price": p["price"],
            "url": p["url"],
        }
        prec = ancien.get(pid)
        if prec is None:
            if not premier_passage:
                events.append(("new", p))
        elif p["in_stock"] and not prec.get("in_stock", False):
            events.append(("restock", p))

    if notify and not premier_passage:
        for genre, p in events:
            msg = (build_new_product_msg(p, src) if genre == "new"
                   else build_restock_msg(p, src))
            envoye = send_telegram(cfg, msg)
            tag = "Nouveau produit" if genre == "new" else "Retour en stock"
            log(f"  -> {tag} : {p['name']} ({'envoyé' if envoye else 'ÉCHEC envoi'})")

    # On ne met à jour l'état que si la source a répondu (on est ici, donc OK).
    state[src["key"]] = nouvel_etat

    cr.update({
        "ok": True,
        "total": len(produits),
        "en_stock": sum(1 for p in produits if p["in_stock"]),
        "events": events,
        "baseline": premier_passage,
        "produits": produits,
    })
    etiquette = (f"baseline de {len(produits)} produits, aucune alerte"
                 if premier_passage
                 else f"{len(events)} événement(s)")
    log(f"  [{src['label']}] {len(produits)} produit(s) via {methode}, "
        f"{cr['en_stock']} en stock — {etiquette}")
    return cr


def run_all(cfg, sources, session, state, notify=True, persist=True):
    """Traite toutes les sources, en isolant les échecs les uns des autres."""
    rapports = []
    for src in sources:
        rapports.append(run_source(cfg, src, session, state, notify=notify))
    if persist:
        save_state(state)

    ok = sum(1 for r in rapports if r["ok"])
    log(f"Cycle terminé : {ok}/{len(rapports)} source(s) OK, "
        f"{sum(len(r['events']) for r in rapports)} événement(s).")
    return rapports


# ---------------------------------------------------------------------------
#  Session HTTP
# ---------------------------------------------------------------------------
def make_session(cfg):
    global CONNECT_TIMEOUT, READ_TIMEOUT, MAX_RETRIES, RETRY_BACKOFF
    CONNECT_TIMEOUT = cfg.getint("monitor", "connect_timeout", fallback=CONNECT_TIMEOUT)
    READ_TIMEOUT = cfg.getint("monitor", "read_timeout", fallback=READ_TIMEOUT)
    MAX_RETRIES = cfg.getint("monitor", "max_retries", fallback=MAX_RETRIES)
    RETRY_BACKOFF = cfg.getint("monitor", "retry_backoff", fallback=RETRY_BACKOFF)

    s = requests.Session()
    ua = cfg.get("monitor", "user_agent", fallback=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ))
    s.headers.update({"User-Agent": ua, "Accept": "application/json, text/html"})
    return s


# ---------------------------------------------------------------------------
#  Programme principal
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Moniteur de stock multi-boutiques")
    parser.add_argument("--test", action="store_true",
                        help="Un seul passage + message Telegram de test, puis stop")
    parser.add_argument("--once", action="store_true",
                        help="Un seul passage réel puis stop (pour cron)")
    parser.add_argument("--heartbeat", action="store_true",
                        help="Envoie un récapitulatif Telegram depuis l'état "
                             "enregistré, sans interroger les boutiques")
    args = parser.parse_args()

    cfg = load_config()
    session = make_session(cfg)
    interval = cfg.getint("monitor", "interval_seconds", fallback=300)

    if args.heartbeat:
        # Signe de vie périodique : on lit UNIQUEMENT l'état déjà enregistré,
        # sans toucher aux boutiques ni modifier state.json. L'absence de ce
        # message est en soi le signal qu'il y a un problème.
        log("=== BATTEMENT DE CŒUR ===")
        state = load_state()
        sources = load_sources(cfg)
        total = sum(len(v) for v in state.values())
        en_stock = sum(1 for v in state.values() for p in v.values()
                       if p.get("in_stock"))
        maj = os.environ.get("ETAT_MAJ_LE", "").strip()

        if total == 0:
            msg = ("🟡 Moniteur : aucun état de référence.\n"
                   "Le moniteur n'a encore jamais réussi à lire les boutiques. "
                   "Aucune alerte ne peut partir tant que ce n'est pas résolu.")
        else:
            lignes = [f"💓 <b>Moniteur OK</b>",
                      f"{total} produit(s) suivi(s), dont {en_stock} en stock.",
                      ""]
            for src in sources:
                produits = state.get(src["key"], {})
                if produits:
                    dispo = sum(1 for p in produits.values() if p.get("in_stock"))
                    lignes.append(f"• {esc(src['label'])} : {len(produits)} "
                                  f"({dispo} en stock)")
                else:
                    lignes.append(f"• {esc(src['label'])} : ⚠️ aucun état")
            if maj:
                lignes += ["", f"Dernière mise à jour : {esc(maj)}"]
            msg = "\n".join(lignes)

        ok = send_telegram(cfg, msg)
        log(f"Battement de cœur : {'envoyé' if ok else 'ÉCHEC envoi'}")
        # Sortie non nulle si l'envoi rate : le run passe en rouge et GitHub
        # t'envoie un e-mail — seul moyen d'être prévenu quand Telegram casse.
        sys.exit(0 if ok else 1)

    sources = load_sources(cfg)
    if not sources:
        log("ERREUR : aucune source active dans config.ini (section [source:…]).")
        sys.exit(1)
    log(f"{len(sources)} source(s) à surveiller : "
        + ", ".join(s["label"] for s in sources))

    if args.test:
        log("=== MODE TEST ===")
        ok = send_telegram(cfg, "✅ Test : le bot Telegram fonctionne.")
        log(f"Message de test Telegram : {'OK' if ok else 'ÉCHEC (voir config)'}")
        state = load_state()
        # persist=False + copie de l'état : le mode test ne modifie jamais rien.
        rapports = run_all(cfg, sources, session, json.loads(json.dumps(state)),
                           notify=False, persist=False)
        for r in rapports:
            if not r["ok"]:
                log(f"--- {r['label']} : INJOIGNABLE ({r['erreur']}) ---")
                continue
            log(f"--- {r['label']} : {r['total']} produit(s) ---")
            for p in r["produits"]:
                log(f"    [{'EN STOCK' if p['in_stock'] else 'épuisé  '}] "
                    f"{p['name']} {('- ' + p['price']) if p['price'] else ''}")
        log("=== Fin du test (l'état n'a pas été modifié) ===")
        return

    log("=== Démarrage du moniteur ===")
    if not args.once:
        log(f"Intervalle : {interval} s ({interval // 60} min). Ctrl+C pour arrêter.")

    demarrage_envoye = False

    while True:
        try:
            state = load_state()
            etait_vide = (len(state) == 0)
            rapports = run_all(cfg, sources, session, state, notify=True)
            # Message de démarrage seulement après une baseline RÉUSSIE
            if etait_vide and not demarrage_envoye and any(r["ok"] for r in rapports):
                lignes = ["🟢 <b>Moniteur démarré.</b>",
                          "Je te préviens dès qu'un produit arrive ou revient en stock.",
                          ""]
                for r in rapports:
                    lignes.append(f"• {esc(r['label'])} : "
                                  + (f"{r['total']} produit(s), {r['en_stock']} en stock"
                                     if r["ok"] else "⚠️ injoignable"))
                send_telegram(cfg, "\n".join(lignes))
                demarrage_envoye = True
        except KeyboardInterrupt:
            log("Arrêt demandé (Ctrl+C). À bientôt.")
            break
        except Exception:
            # Filet de sécurité : une erreur inattendue ne doit pas tuer la
            # boucle. Les pannes par source sont déjà gérées dans run_source.
            log("Cycle en échec inattendu. On réessaiera au prochain passage. Détail :")
            log("    " + " / ".join(traceback.format_exc().strip().splitlines()[-1:]))

        if args.once:
            break

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log("Arrêt demandé (Ctrl+C). À bientôt.")
            break


if __name__ == "__main__":
    main()
