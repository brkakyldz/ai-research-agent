"""Interface strings, and the rule for which language a request is in.

The digest itself is written by the model in one language per run, chosen at the
press on `/runs`; this is the much smaller and entirely separate question of what
the buttons say. Until 2026-09-06 it was one value doing both jobs, so the switch
in the bar quietly decided what the next paid run would be written in and hid
every bulletin written in the other language (ADR 0017). Two dictionaries beat a
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

# The theme is resolved by the same three-step rule, so it lives beside it.
# "system" is not a colour, it is the absence of an override: it renders no
# `data-theme` at all and lets `color-scheme: light dark` follow the OS.
THEME_COOKIE: Final = "digest_theme"
THEMES: Final[tuple[str, ...]] = ("system", "light", "dark")

# Every label here is written in sentence case, and that is a rule rather than a
# habit: until 2026-09-06 every label in the interface was lower-cased - the day,
# the archive, both themes - which read as a placeholder set somebody had not got
# writing yet. A capital is what says a person put the word there. Units keep
# their lower case ("dk", "sa"), because they are notation and not labels, and a
# string that starts with a placeholder starts wherever the value starts.
STRINGS: Final[dict[str, dict[str, str]]] = {
    "tr": {
        "title": "AI Digest",
        "run_now": "Yenile",
        "run_start": "Çalıştır",
        # The question in front of the press, and it never restates the state
        # line above it: what this press will do, and what the last one cost.
        "confirm_what": "{n} kaynak taranacak ve yeni haberler özetlenecek.",
        "confirm_cost": "Son çalışma {c} tuttu.",
        # The one choice the press carries. It is asked here, next to the money,
        # because this is where the language is actually decided: the switch in
        # the bar translates the interface and nothing else (ADR 0017).
        "out_language": "Bülten dili",
        # The other two choices the press carries (ADR 0020). Named by the job
        # rather than by the node, because the reader is choosing what writes
        # the bulletin, not which function runs.
        "model_summarize": "Özet modeli",
        "model_rank": "Sıralama modeli",
        # Prices, not a projection: the tier's own number, in the figure face,
        # so a ten-fold jump is visible before the press rather than in the
        # billing dashboard afterwards.
        "model_price": "1M token: özet ${si:.2f}/${so:.2f} · sıralama ${ri:.2f}/${ro:.2f}",
        "bulletin_language": "Bu bülten {lang} yazıldı.",
        "confirm_yes": "Evet, çalıştır",
        "confirm_no": "Vazgeç",
        "running": "Çalışıyor",
        "note_label": "Bugünün notu",
        "tagline": "Günlük yapay zekâ bülteni",
        "nav_search": "Arama",
        # The rail used to read the scheduler's next fire time off APScheduler.
        # Nothing fires any more (ADR 0015), so the label is what the tool
        # advises rather than what it is about to do.
        "next_run": "Önerilen çalıştırma",
        "advice_now": "Şimdi",
        "advice_never": "Henüz yok",
        "state_due": "Çalıştırmaya hazır",
        "state_waiting": "Bugünün bülteni alındı",
        "state_first": "Henüz bülten alınmadı",
        "state_running": "Çalışma sürüyor",
        "state_blocked": "Anahtar yok",
        "why_due": "Son bültenin üzerinden {since} geçti · önerilen aralık {n} saat.",
        "why_waiting": (
            "Yine de çalıştırabilirsin: yalnızca son bültenden sonra gelen haberler "
            "özetlenir, aynı haber ikinci kez ücretlendirilmez."
        ),
        "why_first": "İlk çalıştırma kaynakları tarar, haberleri özetler ve günün notunu yazar.",
        "why_running": "Bittiğinde bu sayfa kendini yeniler.",
        "why_blocked": "OPENAI_API_KEY tanımlı değil; çalıştırma özetleme adımında durur.",
        "fact_collect": "Kaynak taraması",
        "fact_due": "Önerilen zaman",
        "left_in": "{t} sonra",
        "late_by": "{t} önce",
        "u_moment": "birkaç saniye",
        # The space between a number and its unit is a language's own habit, so
        # it is a string here rather than a rule in two formatters.
        "u_sep": " ",
        "u_min": "dk",
        "u_hour": "sa",
        "u_day": "gün",
        "theme": "Tema",
        "theme_system": "Sistem",
        "theme_light": "Açık",
        "theme_dark": "Koyu",
        # The rail is grouped rather than flat: five links read as one list of
        # five things to do; three groups say what kind of thing each one is.
        "group_daily": "Günlük",
        "group_discover": "Keşfet",
        "group_record": "Kayıt",
        "brief_label": "Bugünün özeti",
        "side_label": "Günün analizi",
        "side_themes": "Günün konuları",
        "side_impact": "Etki dağılımı",
        "side_status": "Makine",
        "topic_share": "{p}% · {n} haber",
        "rising": "Yükselişte",
        "rising_hint": "Geçen haftanın ortalamasının üstünde",
        "no_topics": "Konu yok",
        "impact_count": "{n}",
        "open_runs": "Çalışma kaydı",
        "side_activity": "Etkinlik",
        "side_week": "Son 7 gün",
        "side_cost": "Maliyet",
        "last_run": "Son çalışma",
        "cost_today": "Bugün",
        "cost_week": "7 gün",
        "cost_month": "30 gün",
        "sources_ok": "{n} kaynak açık",
        "sources_failing": "{n} kaynak hata veriyor",
        "no_runs_yet": "Henüz çalışma yok",
        "day_stories": "{d}: {n} haber",
        "brief_meta": "{n} haber · {s} kaynak · {g} konu",
        "why_label": "Neden önemli",
        "impact_high": "Yüksek etki",
        "impact_mid": "Orta etki",
        "impact_low": "Düşük etki",
        "open_source": "Kaynağa git",
        # The reader's verdict on a summary (PLAN-EVALS E2). Two words, no
        # question mark: they sit on the foot line beside the other actions.
        "verdict_ok": "Doğru",
        "verdict_wrong": "Yanlış",
        "verdict_note": "Neyi yanlış yaptı? (isteğe bağlı)",
        "verdict_save": "Kaydet",
        "verdict_saved": "Kaydedildi",
        "more_topics": "Daha fazla",
        "topics": "Konular",
        "skip_to_content": "İçeriğe geç",
        "refresh_hint": "Kaynakları tara ve yeni haberleri özetle",
        "digest_heading": "Bugünün bülteni",
        "nav_digest": "Bugün",
        "nav_archive": "Arşiv",
        "nav_sources": "Kaynaklar",
        "nav_runs": "Çalışmalar",
        "language": "Dil",
        "lang_tr": "Türkçe",
        "lang_en": "English",
        "search_placeholder": "Ara",
        "search": "Ara",
        "all_tags": "Hepsi",
        "show_others": "Diğer {n} haberi göster",
        "all_summarised": "Hepsi özetlendi · arşivde ve aramada",
        # The press is on `/runs` since ADR 0015; this sentence still pointed at a
        # refresh button "above" that the bar no longer carries.
        "empty_digest": (
            "Henüz bülten yok. Çalışmalar sayfasındaki Çalıştır düğmesi kaynakları tarar, "
            "yeni haberleri özetler ve günün notunu yazar."
        ),
        "empty_search": "Bu arama için sonuç yok.",
        "empty_archive": "Arşivde henüz çalışma yok.",
        "runs_heading": "Çalışmalar",
        "sources_heading": "Kaynaklar",
        "archive_heading": "Arşiv",
        "search_heading": "Arama",
        "col_time": "Zaman",
        "col_kind": "Tür",
        "col_lang": "Dil",
        "col_new": "Yeni",
        "col_summarised": "Özet",
        "col_cost": "Maliyet",
        "col_duration": "Süre",
        "col_status": "Durum",
        "col_source": "Kaynak",
        "col_weight": "Ağırlık",
        "col_last": "Son durum",
        "col_articles": "Haber",
        "col_toggle": "",
        "enable": "Aç",
        "disable": "Kapat",
        "add_feed": "Besleme ekle",
        "add": "Ekle",
        "status_ok": "Tamam",
        "status_partial": "Kısmi",
        "status_error": "Hata",
        "status_running": "Çalışıyor",
        "sources_count": "{n} kaynak",
        "queued": "Çalışma başladı",
        "busy": "Zaten bir çalışma sürüyor",
        "no_key": "OPENAI_API_KEY yok",
        "back_to_today": "Bugüne dön",
        "feed_added": "Eklendi",
        "feed_rejected": "Besleme okunamadı",
        "feed_blocked": "Bu kaynak kapsam dışı",
    },
    "en": {
        "title": "AI Digest",
        "run_now": "Refresh",
        "run_start": "Run now",
        "confirm_what": "{n} sources will be polled and what is new summarised.",
        "confirm_cost": "The last run cost {c}.",
        "out_language": "Bulletin language",
        "model_summarize": "Summary model",
        "model_rank": "Ranking model",
        "model_price": "per 1M tokens: summary ${si:.2f}/${so:.2f} · ranking ${ri:.2f}/${ro:.2f}",
        "bulletin_language": "This bulletin was written in {lang}.",
        "confirm_yes": "Yes, run it",
        "confirm_no": "Cancel",
        "running": "Working",
        "note_label": "Today's note",
        "tagline": "The daily AI brief",
        "nav_search": "Search",
        "next_run": "Suggested run",
        "advice_now": "Now",
        "advice_never": "Not yet",
        "state_due": "Ready to run",
        "state_waiting": "Today's bulletin is in",
        "state_first": "No bulletin yet",
        "state_running": "A run is going",
        "state_blocked": "No key",
        "why_due": "It has been {since} since the last bulletin · the suggested gap is {n} hours.",
        "why_waiting": (
            "You can run it anyway: only what arrived since the last bulletin is "
            "summarised, so no story is paid for twice."
        ),
        "why_first": (
            "The first run polls the feeds, summarises the news and writes the day's note."
        ),
        "why_running": "This page reloads itself when the run lands.",
        "why_blocked": "OPENAI_API_KEY is not set; a run would stop at the summarise step.",
        "fact_collect": "Feed poll",
        "fact_due": "Suggested time",
        "left_in": "in {t}",
        "late_by": "{t} ago",
        "u_moment": "under a minute",
        "u_sep": "",
        "u_min": "m",
        "u_hour": "h",
        "u_day": "d",
        "theme": "Theme",
        "theme_system": "System",
        "theme_light": "Light",
        "theme_dark": "Dark",
        "group_daily": "Daily",
        "group_discover": "Discover",
        "group_record": "Record",
        "brief_label": "Today's brief",
        "side_label": "The day, read sideways",
        "side_themes": "Today's themes",
        "side_impact": "Impact spread",
        "side_status": "Machine",
        "topic_share": "{p}% · {n} stories",
        "rising": "Rising",
        "rising_hint": "Above its average for the week behind it",
        "no_topics": "No topics",
        "impact_count": "{n}",
        "open_runs": "Run log",
        "side_activity": "Activity",
        "side_week": "Last 7 days",
        "side_cost": "Cost",
        "last_run": "Last run",
        "cost_today": "Today",
        "cost_week": "7 days",
        "cost_month": "30 days",
        "sources_ok": "{n} sources on",
        "sources_failing": "{n} sources failing",
        "no_runs_yet": "No runs yet",
        "day_stories": "{d}: {n} stories",
        "brief_meta": "{n} stories · {s} sources · {g} topics",
        "why_label": "Why it matters",
        "impact_high": "High impact",
        "impact_mid": "Medium impact",
        "impact_low": "Low impact",
        # "Open source" alone is the licensing term; the label is an instruction.
        "open_source": "Go to source",
        "verdict_ok": "Right",
        "verdict_wrong": "Wrong",
        "verdict_note": "What did it get wrong? (optional)",
        "verdict_save": "Save",
        "verdict_saved": "Saved",
        "more_topics": "More",
        "topics": "Topics",
        "skip_to_content": "Skip to content",
        "refresh_hint": "Poll the feeds and summarise what is new",
        "digest_heading": "Today's digest",
        "nav_digest": "Today",
        "nav_archive": "Archive",
        "nav_sources": "Sources",
        "nav_runs": "Runs",
        "language": "Language",
        "lang_tr": "Türkçe",
        "lang_en": "English",
        "search_placeholder": "Search",
        "search": "Search",
        "all_tags": "All",
        "show_others": "Show the other {n}",
        "all_summarised": "All summarised · in the archive and in search",
        "empty_digest": (
            "No digest yet. Run now, on the Runs page, polls the feeds, summarises what is "
            "new and writes the day's note."
        ),
        "empty_search": "Nothing matches that search.",
        "empty_archive": "No runs in the archive yet.",
        "runs_heading": "Runs",
        "sources_heading": "Sources",
        "archive_heading": "Archive",
        "search_heading": "Search",
        "col_time": "Time",
        "col_kind": "Kind",
        "col_lang": "Lang",
        "col_new": "New",
        "col_summarised": "Summarised",
        "col_cost": "Cost",
        "col_duration": "Took",
        "col_status": "Status",
        "col_source": "Source",
        "col_weight": "Weight",
        "col_last": "Last status",
        "col_articles": "Articles",
        "col_toggle": "",
        "enable": "Enable",
        "disable": "Disable",
        "add_feed": "Add a feed",
        "add": "Add",
        "status_ok": "Ok",
        "status_partial": "Partial",
        "status_error": "Error",
        "status_running": "Running",
        "sources_count": "{n} sources",
        "queued": "Run started",
        "busy": "A run is already going",
        "no_key": "OPENAI_API_KEY is not set",
        "back_to_today": "Back to today",
        "feed_added": "Added",
        "feed_rejected": "Could not read that feed",
        "feed_blocked": "That source is out of scope",
    },
}


def resolve_language(query: str | None, cookie: str | None) -> Language:
    """Which language the *interface* is drawn in. Never which one it reads.

    The fallback is `digest_language` because an operator who set the app to
    write Turkish bulletins wants Turkish buttons on first visit - a shared
    default, not a shared setting: from the first `?lang=` the two are apart.
    """
    for candidate in (query, cookie):
        if candidate in LANGUAGES:
            return candidate  # type: ignore[return-value]
    return get_settings().digest_language


def resolve_theme(query: str | None, cookie: str | None) -> str:
    for candidate in (query, cookie):
        if candidate in THEMES:
            return candidate
    return "system"


def strings(language: Language) -> dict[str, str]:
    return STRINGS[language]
