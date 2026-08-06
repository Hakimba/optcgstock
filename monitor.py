#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Moniteur de stock / nouveaux produits pour shoptjeux.com (WooCommerce).

Ce que fait le script, à chaque passage (par défaut toutes les 5 minutes) :
  1. Récupère la liste des produits d'une catégorie via l'API publique
     WooCommerce Store (JSON propre, léger pour le site). Repli automatique
     sur un parsing HTML si l'API est indisponible.
  2. Compare l'état actuel à l'état du passage précédent (mémorisé sur disque).
  3. Envoie une notification Telegram quand :
        - un NOUVEAU produit apparaît sur la boutique  -> "Nouveau produit"
        - un produit connu passe de "épuisé" à "en stock" -> "Retour en stock"

Aucune donnée n'est achetée : le script se contente de t'alerter, à toi de
foncer acheter. Il respecte le site (une seule requête toutes les 5 min).

Usage :
    python monitor.py            # lance la surveillance en continu
    python monitor.py --test     # fait UN seul passage, affiche ce qu'il voit,
                                 #   envoie un message Telegram de test, puis s'arrête
    python monitor.py --once     # fait un seul passage réel (utile pour cron)
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
        log("Copie config.example.ini vers config.ini et remplis-le.")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    return cfg


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            log("État précédent illisible, on repart de zéro.")
    return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
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
#  Récupération des produits
# ---------------------------------------------------------------------------
def fetch_products_api(cfg, session):
    """
    Récupère les produits via l'API Store WooCommerce (JSON).
    Résout d'abord l'ID de catégorie depuis son slug (robuste si l'ID change).
    Renvoie une liste de dicts normalisés, ou lève une exception.
    """
    base = cfg.get("site", "base_url").rstrip("/")
    slug = cfg.get("site", "category_slug", fallback="").strip()
    cat_id = cfg.get("site", "category_id", fallback="").strip()

    # Résolution slug -> id (si un slug est fourni)
    if slug:
        try:
            resolved = resolve_category_id(base, slug, session)
            if resolved:
                cat_id = str(resolved)
        except requests.RequestException as e:
            log(f"Résolution du slug impossible ({e}), on garde category_id={cat_id}")

    if not cat_id:
        raise ValueError("Aucun category_id/category_slug utilisable.")

    products = []
    page = 1
    while True:
        url = (
            f"{base}/wp-json/wc/store/v1/products"
            f"?category={cat_id}&per_page=100&page={page}"
        )
        r = http_get(session, url)
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        for p in batch:
            products.append(normalize_api_product(p))
        # Pagination : on s'arrête quand on a tout récupéré
        total_pages = int(r.headers.get("X-WP-TotalPages", "1") or "1")
        if page >= total_pages:
            break
        page += 1
    return products


def resolve_category_id(base, slug, session):
    url = f"{base}/wp-json/wc/store/v1/products/categories?per_page=100"
    r = http_get(session, url)
    for cat in r.json():
        if cat.get("slug") == slug:
            return cat.get("id")
    return None


def normalize_api_product(p):
    prices = p.get("prices", {}) or {}
    minor = prices.get("currency_minor_unit", 2)
    raw = prices.get("price")
    price_txt = ""
    if raw not in (None, ""):
        try:
            val = int(raw) / (10 ** int(minor))
            sym = prices.get("currency_symbol", "€")
            price_txt = f"{val:.2f} {sym}"
        except (ValueError, TypeError):
            price_txt = str(raw)
    return {
        "id": str(p.get("id")),
        "name": (p.get("name") or "").strip(),
        "in_stock": bool(p.get("is_in_stock")),
        "price": price_txt,
        "url": p.get("permalink", ""),
    }


def fetch_products_html(cfg, session):
    """
    Repli : parsing HTML de la page catégorie (structure WooCommerce standard).
    Utilisé seulement si l'API Store est indisponible.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise RuntimeError(
            "Repli HTML indisponible : installe beautifulsoup4 "
            "(pip install beautifulsoup4)."
        )
    url = cfg.get("site", "page_url", fallback="").strip()
    if not url:
        raise ValueError("Aucune page_url configurée pour le repli HTML.")

    products = []
    page = 1
    while True:
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
                ".woocommerce-loop-product__title, h2, h3, .product-title"
            )
            link_el = li.select_one("a.woocommerce-LoopProduct-link, a[href]")
            price_el = li.select_one(".price")
            name = title_el.get_text(strip=True) if title_el else ""
            if not name:
                continue
            in_stock = "outofstock" not in classes  # WooCommerce ajoute cette classe
            # Sécurité : un badge "Épuisé"/"Rupture" confirme la rupture
            txt = li.get_text(" ", strip=True).lower()
            if "épuisé" in txt or "epuise" in txt or "rupture" in txt:
                in_stock = False
            pid = li.get("data-product-id") or (link_el["href"] if link_el else name)
            products.append({
                "id": str(pid),
                "name": name,
                "in_stock": in_stock,
                "price": price_el.get_text(" ", strip=True) if price_el else "",
                "url": link_el["href"] if link_el else url,
            })
        page += 1
        if page > 20:  # garde-fou anti-boucle
            break
    return products


def fetch_products(cfg, session):
    """Essaie l'API, sinon repli HTML."""
    prefer = cfg.get("site", "method", fallback="api").strip().lower()
    if prefer == "html":
        return fetch_products_html(cfg, session), "html"
    try:
        return fetch_products_api(cfg, session), "api"
    except Exception as e:
        log(f"API Store indisponible ({e}). Tentative de repli HTML…")
        return fetch_products_html(cfg, session), "html"


# ---------------------------------------------------------------------------
#  Comparaison et notifications
# ---------------------------------------------------------------------------
def esc(s):
    return html.escape(str(s))


def build_new_product_msg(p):
    stock = "✅ EN STOCK" if p["in_stock"] else "⛔ épuisé"
    price = f" — {esc(p['price'])}" if p["price"] else ""
    return (
        f"🆕 <b>Nouveau produit sur ShopTjeux</b>\n"
        f"<b>{esc(p['name'])}</b>{price}\n"
        f"Statut : {stock}\n"
        f"{esc(p['url'])}"
    )


def build_restock_msg(p):
    price = f" — {esc(p['price'])}" if p["price"] else ""
    return (
        f"🔔 <b>Retour en stock</b>\n"
        f"<b>{esc(p['name'])}</b>{price}\n"
        f"{esc(p['url'])}"
    )


def run_cycle(cfg, session, state, notify=True):
    """Un passage : récupère, compare, notifie, met à jour l'état."""
    products, method = fetch_products(cfg, session)
    log(f"{len(products)} produit(s) récupéré(s) via {method}.")

    first_run = len(state) == 0
    new_state = {}
    events = []

    for p in products:
        pid = p["id"]
        new_state[pid] = {
            "name": p["name"],
            "in_stock": p["in_stock"],
            "price": p["price"],
            "url": p["url"],
        }
        prev = state.get(pid)
        if prev is None:
            # Produit jamais vu
            if not first_run:
                events.append(("new", p))
        else:
            # Passage épuisé -> en stock
            if p["in_stock"] and not prev.get("in_stock", False):
                events.append(("restock", p))

    # Envoi des notifications
    if notify and not first_run:
        for kind, p in events:
            msg = build_new_product_msg(p) if kind == "new" else build_restock_msg(p)
            ok = send_telegram(cfg, msg)
            tag = "Nouveau produit" if kind == "new" else "Retour en stock"
            log(f"  -> {tag} : {p['name']} ({'envoyé' if ok else 'ÉCHEC envoi'})")

    if first_run:
        log("Premier passage : état de référence enregistré, aucune alerte "
            f"(baseline de {len(products)} produits).")

    save_state(new_state)
    return products, events, first_run


# ---------------------------------------------------------------------------
#  Programme principal
# ---------------------------------------------------------------------------
def make_session(cfg):
    global CONNECT_TIMEOUT, READ_TIMEOUT, MAX_RETRIES, RETRY_BACKOFF
    CONNECT_TIMEOUT = cfg.getint("monitor", "connect_timeout", fallback=CONNECT_TIMEOUT)
    READ_TIMEOUT = cfg.getint("monitor", "read_timeout", fallback=READ_TIMEOUT)
    MAX_RETRIES = cfg.getint("monitor", "max_retries", fallback=MAX_RETRIES)
    RETRY_BACKOFF = cfg.getint("monitor", "retry_backoff", fallback=RETRY_BACKOFF)

    s = requests.Session()
    ua = cfg.get("site", "user_agent", fallback=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ))
    s.headers.update({"User-Agent": ua, "Accept": "application/json, text/html"})
    # UNE SEULE couche de réessais : celle de http_get() (avec log + backoff).
    # On désactive les réessais internes de l'adaptateur pour ne PAS les cumuler
    # (sinon un site en panne fait durer un cycle plusieurs minutes inutilement).
    adapter = HTTPAdapter(max_retries=Retry(total=0, read=0, connect=0, redirect=0))
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def main():
    parser = argparse.ArgumentParser(description="Moniteur ShopTjeux")
    parser.add_argument("--test", action="store_true",
                        help="Un seul passage + message Telegram de test, puis stop")
    parser.add_argument("--once", action="store_true",
                        help="Un seul passage réel puis stop (pour cron)")
    args = parser.parse_args()

    cfg = load_config()
    session = make_session(cfg)
    interval = cfg.getint("monitor", "interval_seconds", fallback=300)

    if args.test:
        log("=== MODE TEST ===")
        ok = send_telegram(cfg, "✅ Test ShopTjeux : le bot Telegram fonctionne.")
        log(f"Message de test Telegram : {'OK' if ok else 'ÉCHEC (voir config)'}")
        state = load_state()
        try:
            products, events, first = run_cycle(cfg, session, dict(state), notify=False)
            log("--- Produits vus (aperçu) ---")
            for p in products:
                log(f"    [{'EN STOCK' if p['in_stock'] else 'épuisé  '}] "
                    f"{p['name']} {('- ' + p['price']) if p['price'] else ''}")
        except Exception:
            log("⚠️  Site injoignable pour l'instant (surchargé / 504 / hors ligne).")
            log("    Ce n'est PAS un souci du script : réessaie le test plus tard.")
            log("    Détail : " + traceback.format_exc().strip().splitlines()[-1])
        finally:
            # On restaure l'état d'origine : le mode test ne modifie jamais l'état.
            save_state(state)
        log("=== Fin du test (l'état n'a pas été modifié) ===")
        return

    log("=== Démarrage du moniteur ShopTjeux ===")
    log(f"Intervalle : {interval} s ({interval // 60} min). Ctrl+C pour arrêter.")

    startup_sent = False  # message "démarré" envoyé une seule fois, après 1er succès

    while True:
        try:
            state = load_state()
            was_baseline = (len(state) == 0)
            run_cycle(cfg, session, state, notify=True)
            # Message de démarrage seulement après une baseline RÉUSSIE
            if was_baseline and not startup_sent:
                send_telegram(cfg, "🟢 Moniteur ShopTjeux démarré. "
                              "Je te préviens dès qu'un produit arrive ou revient en stock.")
                startup_sent = True
        except KeyboardInterrupt:
            log("Arrêt demandé (Ctrl+C). À bientôt.")
            break
        except Exception:
            # Le site peut être momentanément lent/indisponible : on NE plante PAS,
            # on NE sauvegarde PAS d'état partiel, on réessaiera au prochain cycle.
            log("Cycle en échec (site lent/indispo ?). On réessaiera au prochain "
                "passage. Détail :")
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
