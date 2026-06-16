#!/usr/bin/env python3
"""Build ``ivi_installer/resources/extras.json`` from a curated list of
RU/CIS apps using the live AppGallery API.

For each entry:
  1. Search AppGallery by ``keyword``.
  2. Find the result whose ``package`` matches ``expected_pkg``.
  3. Validate the resulting C-id via ``/appdl/<appid>`` — that redirect
     leaks the package name (``...<package>.<versionCode>.apk``), so
     a mismatch tells us the C-id is stale or pointing at the wrong
     app. (We caught the existing ``ru.yandex.music = C100315379``
     in the old extras.json this way — it now redirects to TikTok.)
  4. Convert to a CatalogEntry-shaped dict and emit.

Run from anywhere; the output is written to
``resources/extras.json``.

Usage:
    python3 scripts/build_extras.py [--dry-run] [--limit 5]
"""
from __future__ import annotations

import argparse
import json
import logging
import ssl
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ivi_installer import catalog as _catalog                # noqa: E402
from ivi_installer.sources import appgallery_index           # noqa: E402

log = logging.getLogger("build_extras")

# =============================================================================
# Curated catalog
#
# (display_name, search_keyword, expected_package, category,
#  tested, description_ru, source_kind, source_data)
#
# Where ``source_kind == "appgallery"``, ``source_data`` is None — the
# script discovers the C-id via the AppGallery API. Where
# ``source_kind == "direct"``, ``source_data`` is the static URL.
# =============================================================================

CURATED: list[dict] = [
    # ---------- navigation (8) ----------
    {"name": "Яндекс Карты", "kw": "Яндекс Карты", "pkg": "ru.yandex.yandexmaps",
     "cat": "navigation", "ru": "Карты, поиск мест и пробки от Яндекса",
     "tested": True},
    {"name": "Яндекс Навигатор", "kw": "Яндекс Навигатор",
     "pkg": "ru.yandex.yandexnavi",
     "cat": "navigation", "ru": "Навигатор с пробками и голосом Алисы",
     "tested": True},
    {"name": "2ГИС", "kw": "2ГИС", "pkg": "ru.dublgis.dgismobile",
     "cat": "navigation", "ru": "Карты городов СНГ с подробными данными",
     "tested": True},
    {"name": "Яндекс Метро", "kw": "Яндекс Метро", "pkg": "ru.yandex.metro",
     "cat": "navigation", "ru": "Карты метро с маршрутами"},
    {"name": "Яндекс Заправки", "kw": "Яндекс Заправки",
     "pkg": "ru.yandex.mobile.gasstations",
     "cat": "cars", "ru": "Оплата топлива на АЗС со смартфона"},
    {"name": "Яндекс Go", "kw": "Яндекс Go", "pkg": "ru.yandex.taxi",
     "cat": "cars", "ru": "Такси, доставка, каршеринг и самокаты"},
    {"name": "Petal Maps", "kw": "Petal Maps", "pkg": "com.huawei.maps.app",
     "cat": "navigation", "ru": "Карты от Huawei с пробками и POI"},
    {"name": "HERE WeGo", "kw": "HERE WeGo", "pkg": "com.here.app.maps",
     "cat": "navigation", "ru": "Офлайн-карты с пешеходной и авто-навигацией"},

    # ---------- video / streaming (10) ----------
    {"name": "Кинопоиск", "kw": "Кинопоиск", "pkg": "ru.kinopoisk",
     "cat": "entertainment", "ru": "Стриминговый сервис с фильмами и сериалами",
     "tested": True},
    {"name": "Okko", "kw": "Okko фильмы", "pkg": "ru.okko.tv",
     "cat": "entertainment", "ru": "Онлайн-кинотеатр Okko: фильмы, сериалы, спорт"},
    {"name": "IVI", "kw": "ivi фильмы", "pkg": "ru.ivi.client",
     "cat": "entertainment", "ru": "Онлайн-кинотеатр IVI"},
    {"name": "Wink", "kw": "Wink Ростелеком", "pkg": "ru.rt.video.app.mobile",
     "cat": "entertainment", "ru": "Онлайн-кинотеатр Ростелекома: фильмы и ТВ"},
    {"name": "START", "kw": "START кинотеатр", "pkg": "ru.start.androidmobile",
     "cat": "entertainment", "ru": "Онлайн-кинотеатр START — оригинальные сериалы"},
    {"name": "KION", "kw": "KION МТС", "pkg": "ru.mts.mtstv",
     "cat": "entertainment", "ru": "Онлайн-кинотеатр МТС с ТВ-каналами и кино"},
    {"name": "Premier", "kw": "Premier", "pkg": "ru.gpm.premier.tv",
     "cat": "entertainment", "ru": "Онлайн-кинотеатр Premier"},
    {"name": "MEGOGO", "kw": "MEGOGO", "pkg": "com.megogo.application",
     "cat": "entertainment", "ru": "Онлайн-кинотеатр MEGOGO с ТВ и аудиокнигами"},
    {"name": "VK Видео", "kw": "VK Видео", "pkg": "com.vk.vkvideo",
     "cat": "entertainment", "ru": "VK Видео: кино, шоу, сериалы и блогеры"},
    {"name": "Plex", "kw": "Plex", "pkg": "com.plexapp.android",
     "cat": "entertainment", "ru": "Клиент личного медиасервера Plex"},

    # ---------- music / audio (8) ----------
    {"name": "Яндекс Музыка", "kw": "Яндекс Музыка", "pkg": "ru.yandex.music",
     "cat": "entertainment", "ru": "Стриминговый аудио-сервис с подборками и подкастами",
     "tested": True},
    {"name": "VK Музыка", "kw": "VK Музыка", "pkg": "com.vk.music",
     "cat": "entertainment", "ru": "Стриминг музыки от ВКонтакте"},
    {"name": "Звук", "kw": "Звук Sber", "pkg": "com.zvuk.mobile",
     "cat": "entertainment", "ru": "Музыкальный стриминг от Sber"},
    {"name": "BOOM", "kw": "BOOM плеер", "pkg": "com.uma.musicvk",
     "cat": "entertainment", "ru": "Музыкальный плеер с интеграцией VK"},
    {"name": "Boom: 3D-звук", "kw": "Boom 3D эквалайзер",
     "pkg": "com.globaldelight.boom",
     "cat": "entertainment", "ru": "Плеер с 3D-звуком и эквалайзером"},
    {"name": "Deezer", "kw": "Deezer", "pkg": "deezer.android.app",
     "cat": "entertainment", "ru": "Музыкальный стриминг Deezer"},
    {"name": "SoundHound", "kw": "SoundHound",
     "pkg": "com.melodis.midomiMusicIdentifier.freemium",
     "cat": "entertainment", "ru": "Распознавание музыки и плеер"},
    {"name": "Shazam", "kw": "Shazam", "pkg": "com.shazam.android",
     "cat": "entertainment", "ru": "Распознавание звучащей музыки"},

    # ---------- radio / podcasts (5) ----------
    {"name": "Радиопоток", "kw": "Радиопоток",
     "pkg": "ru.unofonte.radiopotok",
     "cat": "entertainment", "ru": "Онлайн-радио с тематическими подборками",
     "tested": True},
    {"name": "Radio Record", "kw": "Radio Record", "pkg": "ru.radiorecord.app",
     "cat": "entertainment", "ru": "Радио Record: онлайн-станции и подкасты"},
    {"name": "Europa Plus", "kw": "Europa Plus", "pkg": "ru.europaplus.app",
     "cat": "entertainment", "ru": "Радио Europa Plus онлайн"},
    {"name": "TuneIn Radio", "kw": "TuneIn Radio", "pkg": "tunein.player",
     "cat": "entertainment", "ru": "Тысячи радиостанций со всего мира"},
    {"name": "Castbox", "kw": "Castbox подкасты",
     "pkg": "fm.castbox.audiobook.radio.podcast",
     "cat": "entertainment", "ru": "Подкастный плеер с библиотекой и поиском"},

    # ---------- audiobooks (3) ----------
    {"name": "ЛитРес", "kw": "ЛитРес читай",
     "pkg": "ru.litres.android.readfree",
     "cat": "entertainment", "ru": "Аудиокниги и электронные книги от ЛитРес"},
    {"name": "MyBook", "kw": "MyBook книги", "pkg": "ru.mybook",
     "cat": "entertainment", "ru": "Электронные и аудиокниги по подписке"},
    {"name": "Storytel", "kw": "Storytel", "pkg": "com.storytel.base",
     "cat": "entertainment", "ru": "Аудиокниги по подписке"},

    # ---------- messaging / social (6) ----------
    {"name": "Telegram", "kw": None, "pkg": "org.telegram.messenger",
     "cat": "social", "ru": "Мессенджер, сборка с telegram.org",
     "tested": True,
     "source_kind": "direct",
     "source_data": {"url": "https://telegram.org/dl/android/apk",
                     "version": "11.2.3"},
     "version": "11.2.3", "size_mb": 67.8, "min_api": 21,
     "homepage": "https://telegram.org/",
     "notes_ru": "Прямая ссылка с telegram.org — версия меняется при каждом релизе."},
    {"name": "ВКонтакте", "kw": "ВКонтакте", "pkg": "com.vkontakte.android",
     "cat": "social", "ru": "Соцсеть ВКонтакте: лента, сообщения, музыка"},
    {"name": "VK Мессенджер", "kw": "VK Мессенджер", "pkg": "com.vk.im",
     "cat": "social", "ru": "Мессенджер VK: чаты и звонки"},
    {"name": "VK Звонки", "kw": "VK Звонки", "pkg": "com.vk.calls",
     "cat": "social", "ru": "VK Звонки: безлимитное общение"},
    {"name": "Одноклассники", "kw": "Одноклассники", "pkg": "ru.ok.android",
     "cat": "social", "ru": "Одноклассники: соцсеть"},
    {"name": "Viber", "kw": "Viber", "pkg": "com.viber.voip",
     "cat": "social", "ru": "Мессенджер Viber: чаты и звонки"},

    # ---------- weather / utilities (5) ----------
    {"name": "Яндекс Погода", "kw": "Яндекс Погода",
     "pkg": "ru.yandex.weatherplugin",
     "cat": "tools", "ru": "Прогноз погоды от Яндекса"},
    {"name": "Gismeteo lite", "kw": "Gismeteo", "pkg": "com.gismeteo.client",
     "cat": "tools", "ru": "Прогноз погоды от Гисметео (lite-версия)"},
    {"name": "Яндекс с Алисой", "kw": "Яндекс с Алисой",
     "pkg": "com.yandex.searchapp",
     "cat": "tools", "ru": "Поисковик и ассистент Алиса от Яндекса"},
    {"name": "Яндекс Старт", "kw": "Яндекс Старт",
     "pkg": "ru.yandex.searchplugin",
     "cat": "tools", "ru": "Браузер и поисковик Яндекс Старт"},
    {"name": "Яндекс Браузер", "kw": "Яндекс Браузер",
     "pkg": "com.yandex.browser",
     "cat": "tools", "ru": "Полнофункциональный Яндекс Браузер"},

    # ---------- auto-specific (5) ----------
    {"name": "Дром", "kw": "Дром авто", "pkg": "ru.farpost.dromfilter",
     "cat": "cars", "ru": "Дром: цены на машины, объявления"},
    {"name": "Авто.ру", "kw": "Авто.ру", "pkg": "ru.auto.ara",
     "cat": "cars", "ru": "Авто.ру: купить и продать автомобиль"},
    {"name": "Штрафы ГИБДД", "kw": "Штрафы ГИБДД",
     "pkg": "ru.gibdd_pay.app",
     "cat": "cars", "ru": "Проверка и оплата штрафов ГИБДД"},
    {"name": "Парковки России", "kw": "Парковки России",
     "pkg": "ru.maximaster.parkingrus",
     "cat": "cars", "ru": "Парковки городов России"},
    {"name": "OBDeleven", "kw": "OBDeleven",
     "pkg": "com.voltasit.obdeleven",
     "cat": "cars", "ru": "Диагностика автомобиля по OBD2"},

    # ---------- maps / nav alternatives (3) ----------
    {"name": "OsmAnd Карты", "kw": "OsmAnd", "pkg": "net.osmand.huawei",
     "cat": "navigation", "ru": "OpenStreetMap-карты с офлайн-навигацией"},
    {"name": "Sygic GPS", "kw": "Sygic",
     "pkg": "com.sygic.navigation.offline.maps.route.navigator",
     "cat": "navigation", "ru": "Офлайн GPS-навигация Sygic"},
    {"name": "Radarbot", "kw": "Radarbot",
     "pkg": "com.vialsoft.radarbot_free",
     "cat": "cars", "ru": "Детектор камер и радаров на дороге"},

    # ---------- file managers / utility (4) ----------
    {"name": "OZON", "kw": "OZON", "pkg": "ru.ozon.app.android",
     "cat": "tools", "ru": "Маркетплейс OZON: товары, билеты, доставка"},
    {"name": "Wildberries", "kw": "Wildberries",
     "pkg": "com.wildberries.ru",
     "cat": "tools", "ru": "Маркетплейс Wildberries"},
    {"name": "Яндекс Диск", "kw": "Яндекс Диск", "pkg": "ru.yandex.disk",
     "cat": "tools", "ru": "Облачное хранилище Яндекс Диск"},
    {"name": "VK Cloud", "kw": "VK Cloud облако", "pkg": "ru.mail.cloud",
     "cat": "tools", "ru": "Облачное хранилище от Mail.ru / VK"},

    # ---------- video players (3) ----------
    {"name": "MX Player", "kw": "MX Player", "pkg": "com.mxtech.videoplayer.ad",
     "cat": "entertainment", "ru": "Видеоплеер с поддержкой большинства кодеков"},
    {"name": "VLC", "kw": "VLC", "pkg": "org.videolan.vlc",
     "cat": "entertainment", "ru": "Универсальный медиаплеер VLC"},
    {"name": "Just Player", "kw": "Just Video Player",
     "pkg": "com.brouken.player",
     "cat": "entertainment", "ru": "Минималистичный видеоплеер на ExoPlayer"},
]


# =============================================================================
# Validation: probe /appdl/<appid> to confirm the C-id maps to the
# expected package. The redirect URL on AppGallery's CDN contains
# ``<package>.<versionCode>.apk`` which is a reliable ground truth.
# =============================================================================


_APPDL_URL = "https://appgallery.cloud.huawei.com/appdl/{id}"


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def probe_appgallery_cid(appid: str) -> str | None:
    """Return the package name the redirect URL exposes for ``appid``,
    or None if the C-id resolves to AppGallery's homepage (= invalid).
    """
    req = urllib.request.Request(
        _APPDL_URL.format(id=appid),
        headers={"User-Agent": appgallery_index._USER_AGENT},
    )
    # Don't follow redirects — read the Location header instead.
    class _NoFollow(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_a, **_kw):
            return None
    opener = urllib.request.build_opener(
        _NoFollow(),
        urllib.request.HTTPSHandler(context=_ssl_ctx()),
    )
    try:
        opener.open(req, timeout=10)
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location") or e.headers.get("location") or ""
    except Exception:
        return None
    else:
        return None
    if "appdl/application/apk" not in location:
        return None
    # `.../<hash>/<package>.<versionCode>.apk`
    import re
    m = re.search(r"/([A-Za-z0-9_.-]+)\.([0-9]+)\.apk", location)
    if not m:
        return None
    return m.group(1)


# =============================================================================
# Bulk-find driver
# =============================================================================


def _category_uri_for_search(keyword: str) -> str:
    return f"searchapp|{keyword}"


def _find_match(items: list[dict], expected_pkg: str) -> dict | None:
    """Pick the search hit whose `package` matches the expected pkg.

    Scoring: exact match > prefix match > the first hit. The first hit
    fallback is intentional — when AppGallery has only one app even
    vaguely related to the keyword, it's almost always the one we
    want, and the extras-driven entry usually has a stricter expected
    package than what AppGallery's region returns (e.g. ru.kinopoisk
    vs ru.kinopoisk.huawei).
    """
    for it in items:
        if it.get("package") == expected_pkg:
            return it
    for it in items:
        pkg = it.get("package") or ""
        if pkg.startswith(expected_pkg) or expected_pkg.startswith(pkg):
            return it
    return None


def find_appgallery(session: appgallery_index.AppGallerySession,
                    keyword: str, expected_pkg: str) -> dict | None:
    items = appgallery_index.search(session, keyword, max_results=10)
    return _find_match(items, expected_pkg)


def build_entry(spec: dict, session: appgallery_index.AppGallerySession,
                ) -> tuple[dict | None, str]:
    """Resolve one curated spec into an extras.json entry dict.

    Returns ``(entry, status)`` — ``entry`` is None on hard failure.
    ``status`` is a short human-readable diagnostic.
    """
    name = spec["name"]
    expected_pkg = spec["pkg"]
    if spec.get("source_kind") == "direct":
        # Hand-coded direct-download entry; no AppGallery roundtrip.
        return ({
            "id": expected_pkg,
            "name": name,
            "category": spec["cat"],
            "version": spec.get("version"),
            "size_mb": spec.get("size_mb"),
            "min_api": spec.get("min_api"),
            "tested": bool(spec.get("tested", False)),
            "initials": _catalog.derive_initials(name),
            "description_ru": spec.get("ru"),
            "description_en": spec.get("en"),
            "homepage": spec.get("homepage"),
            "sources": [
                {"kind": "direct",
                 "url": spec["source_data"]["url"],
                 **{k: v for k, v in spec["source_data"].items()
                    if k != "url"}},
            ],
            **({"notes_ru": spec["notes_ru"]} if spec.get("notes_ru") else {}),
        }, "direct")

    keyword = spec.get("kw") or name
    item = find_appgallery(session, keyword, expected_pkg)
    if item is None:
        return None, f"no AppGallery hit for {keyword!r}/{expected_pkg}"
    appid = item.get("appid") or ""
    actual_pkg = item.get("package") or ""

    # Validate the C-id via the appdl redirect.
    redirected = probe_appgallery_cid(appid)
    if redirected is None:
        return None, f"C-id {appid} ({actual_pkg}) resolved to homepage"
    if redirected != actual_pkg:
        return None, (
            f"C-id {appid} mismatch: AppGallery says {actual_pkg}, "
            f"CDN serves {redirected}")
    if redirected != expected_pkg:
        log.warning(
            "%s: AppGallery returned %s instead of %s — accepting",
            name, redirected, expected_pkg)

    return ({
        "id": actual_pkg,
        "name": item.get("name") or name,
        "category": spec["cat"],
        "version": item.get("appVersionName") or item.get("versionName"),
        "size_mb": (round(int(item["size"]) / (1024 * 1024), 1)
                    if item.get("size") else None),
        "min_api": None,
        "tested": bool(spec.get("tested", False)),
        "initials": _catalog.derive_initials(name),
        "description_ru": spec.get("ru") or item.get("memo") or "",
        "description_en": spec.get("en") or item.get("memo") or "",
        "icon_url": item.get("icon") or None,
        "sources": [{"kind": "appgallery", "id": appid}],
        **({"notes_ru": spec["notes_ru"]} if spec.get("notes_ru") else {}),
    }, f"AppGallery {appid}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0,
                        help="Process only the first N entries (debug).")
    parser.add_argument("--out",
                        default=str(REPO_ROOT
                                    / "ivi_installer/resources/extras.json"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    session = appgallery_index.AppGallerySession()

    apps: list[dict] = []
    todo: list[str] = []

    work = CURATED if not args.limit else CURATED[:args.limit]
    for i, spec in enumerate(work):
        name = spec["name"]
        try:
            entry, status = build_entry(spec, session)
        except Exception as e:  # noqa: BLE001
            log.exception("%s crashed", name)
            todo.append(f"  - {name}: exception {e}")
            continue
        if entry is None:
            log.warning("%s: %s", name, status)
            todo.append(f"  - {name} ({spec['pkg']}): {status}")
            continue
        log.info("%s -> %s (%s)", name, entry["id"], status)
        apps.append(entry)
        # rate-limit ourselves a bit so AppGallery doesn't WAF-block.
        time.sleep(0.25)

    out_doc = {
        "schema_version": 2,
        "comment": ("Curated catalog of RU/CIS-friendly apps for in-car "
                    "head units. AppGallery C-ids are resolved live via "
                    "the AppGallery API and validated by probing the "
                    "/appdl/<id> redirect for the expected package "
                    "name. Re-run scripts/build_extras.py to refresh."),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "apps": apps,
    }

    if args.dry_run:
        json.dump(out_doc, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        out_path = Path(args.out)
        out_path.write_text(
            json.dumps(out_doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        log.info("wrote %d entries to %s", len(apps), out_path)

    if todo:
        log.warning("=== %d entries need manual attention: ===", len(todo))
        for line in todo:
            log.warning(line)

    return 0 if apps else 1


if __name__ == "__main__":
    raise SystemExit(main())
