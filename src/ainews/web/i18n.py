"""Interface strings, and the rule for which language a request is in.

The digest itself is written by the model in one language per run; this is the
much smaller question of what the buttons say. Two dictionaries beat a
translation framework for thirty strings, and a missing key is a KeyError in a
template render rather than a silent English fallback nobody notices.

Language resolution has one rule with three steps: an explicit `?lang=` wins,
then the cookie it set, then the configured default. The query parameter is what
makes a link shareable and a screenshot reproducible.
"""

from __future__ import annotations

from typing import Final

from ainews.config import Language, get_settings

LANGUAGE_COOKIE: Final = "digest_lang"
LANGUAGES: Final[tuple[Language, ...]] = ("tr", "en")

STRINGS: Final[dict[str, dict[str, str]]] = {
    "tr": {
        "title": "AI Digest",
        "run_now": "yenile",
        "running": "çalışıyor",
        "note_label": "bugünün notu",
        "nav_digest": "bugün",
        "nav_archive": "arşiv",
        "nav_sources": "kaynaklar",
        "nav_runs": "çalışmalar",
        "other_language": "english",
        "search_placeholder": "ara",
        "search": "ara",
        "all_tags": "hepsi",
        "show_others": "diğer {n} haberi göster",
        "all_summarised": "hepsi özetlendi · arşivde ve aramada",
        "empty_digest": (
            "Henüz digest yok. Üstteki yenile düğmesi kaynakları tarar, yeni haberleri "
            "özetler ve günün notunu yazar."
        ),
        "empty_search": "Bu arama için sonuç yok.",
        "empty_archive": "Arşivde henüz çalışma yok.",
        "runs_heading": "Çalışmalar",
        "sources_heading": "Kaynaklar",
        "archive_heading": "Arşiv",
        "search_heading": "Arama",
        "col_time": "zaman",
        "col_kind": "tür",
        "col_lang": "dil",
        "col_new": "yeni",
        "col_summarised": "özet",
        "col_cost": "maliyet",
        "col_duration": "süre",
        "col_status": "durum",
        "col_source": "kaynak",
        "col_weight": "ağırlık",
        "col_last": "son durum",
        "col_articles": "haber",
        "col_toggle": "",
        "enable": "aç",
        "disable": "kapat",
        "add_feed": "besleme ekle",
        "add": "ekle",
        "status_ok": "tamam",
        "status_partial": "kısmi",
        "status_error": "hata",
        "status_running": "çalışıyor",
        "sources_count": "{n} kaynak",
        "queued": "çalışma başladı",
        "busy": "zaten bir çalışma sürüyor",
        "no_key": "OPENAI_API_KEY yok",
        "back_to_today": "bugüne dön",
        "feed_added": "eklendi",
        "feed_rejected": "besleme okunamadı",
    },
    "en": {
        "title": "AI Digest",
        "run_now": "refresh",
        "running": "working",
        "note_label": "today's note",
        "nav_digest": "today",
        "nav_archive": "archive",
        "nav_sources": "sources",
        "nav_runs": "runs",
        "other_language": "türkçe",
        "search_placeholder": "search",
        "search": "search",
        "all_tags": "all",
        "show_others": "show the other {n}",
        "all_summarised": "all summarised · in the archive and in search",
        "empty_digest": (
            "No digest yet. The refresh button above polls the feeds, summarises what is "
            "new and writes the day's note."
        ),
        "empty_search": "Nothing matches that search.",
        "empty_archive": "No runs in the archive yet.",
        "runs_heading": "Runs",
        "sources_heading": "Sources",
        "archive_heading": "Archive",
        "search_heading": "Search",
        "col_time": "time",
        "col_kind": "kind",
        "col_lang": "lang",
        "col_new": "new",
        "col_summarised": "summarised",
        "col_cost": "cost",
        "col_duration": "took",
        "col_status": "status",
        "col_source": "source",
        "col_weight": "weight",
        "col_last": "last status",
        "col_articles": "articles",
        "col_toggle": "",
        "enable": "enable",
        "disable": "disable",
        "add_feed": "add a feed",
        "add": "add",
        "status_ok": "ok",
        "status_partial": "partial",
        "status_error": "error",
        "status_running": "running",
        "sources_count": "{n} sources",
        "queued": "run started",
        "busy": "a run is already going",
        "no_key": "OPENAI_API_KEY is not set",
        "back_to_today": "back to today",
        "feed_added": "added",
        "feed_rejected": "could not read that feed",
    },
}


def resolve_language(query: str | None, cookie: str | None) -> Language:
    for candidate in (query, cookie):
        if candidate in LANGUAGES:
            return candidate  # type: ignore[return-value]
    return get_settings().digest_language


def other_language(language: Language) -> Language:
    return "en" if language == "tr" else "tr"


def strings(language: Language) -> dict[str, str]:
    return STRINGS[language]
