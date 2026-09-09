"""Interface strings, and the rule for which language a request is in.

The digest itself is written by the model in one language per run, chosen at the
press on `/runs`; this is the much smaller and entirely separate question of what
the buttons say. One value doing both jobs makes the switch in the bar quietly
decide what the next paid run will be written in and hide every bulletin written
in the other language, which is why they are two (ADR 0017). Two dictionaries beat a
translation framework for thirty strings, and a missing key is a KeyError in a
template render rather than a silent English fallback nobody notices.

Language resolution has one rule with three steps: an explicit `?lang=` wins,
then the cookie it set, then the configured default. The query parameter is what
makes a link shareable and a screenshot reproducible.
"""

from __future__ import annotations

import json
from typing import Final

from jinja2 import UndefinedError

from ainews.config import Language, get_settings

LANGUAGE_COOKIE: Final = "digest_lang"
LANGUAGES: Final[tuple[Language, ...]] = ("tr", "en")

# The theme is resolved by the same three-step rule, so it lives beside it.
# "system" is not a colour, it is the absence of an override: it renders no
# `data-theme` at all and lets `color-scheme: light dark` follow the OS.
THEME_COOKIE: Final = "digest_theme"
THEMES: Final[tuple[str, ...]] = ("system", "light", "dark")

# Every label here is written in sentence case, and that is a rule rather than a
# habit. An interface of lower-cased labels - the day, the archive, both themes -
# reads as a placeholder set somebody had not got to writing yet; a capital is
# what says a person put the word there. Units keep their lower case ("dk",
# "sa"), because they are notation and not labels, and a string that starts with
# a placeholder starts wherever the value starts.
STRINGS: Final[dict[str, dict[str, str]]] = {
    "tr": {
        "title": "AI Digest",
        "run_now": "Yenile",
        "run_start": "Çalıştır",
        # The question in front of the press, and it never restates the state
        # line above it: what this press will do, and what the last one cost.
        "confirm_what": "{n} kaynak taranacak ve yeni haberler özetlenecek.",
        # How big the delta is, before it is paid for. "Bekleyen", not "will
        # be": the poll can add to it and dedupe can take from it.
        "confirm_waiting": (
            "Şu an özetlenmeyi bekleyen {c} haber var; tarama yenilerini ekleyebilir."
        ),
        "confirm_none": (
            "Şu an özetlenmeyi bekleyen haber yok; tarama yenisini bulmazsa bu baskı boş kalır."
        ),
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
        # Thousands, for the same reason and with more at stake: Turkish groups
        # with "." and English with ",", and the two are each other's decimal
        # point - so the wrong one does not merely look foreign, it reads as a
        # different number. "50,521 token" is fifty-and-a-half to a Turkish eye.
        "n_sep": ".",
        # And the decimal point, which is the same two characters swapped. A
        # step that took 24.6 seconds is "24,6 sn" here and "24.6s" in English;
        # printing one form in both would be a different number in one of them.
        "d_sep": ",",
        "u_sec": "sn",
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
        "impact_spread": "Etki dağılımı",
        "week_stories": "Son 7 günün haberleri",
        "last_run": "Son çalışma",
        "cost_today": "Bugün",
        "cost_week": "7 gün",
        "cost_month": "30 gün",
        "sources_ok": "{n} kaynak açık",
        "no_runs_yet": "Henüz çalışma yok",
        "day_stories": "{d}: {n} haber",
        "brief_meta": "{n} haber · {s} kaynak · {g} konu",
        "why_label": "Neden önemli",
        # The three band names, and the spread's label above. The Turkish is
        # *etki* because what the bands report is the story's weight in the
        # field; *ilgi* names the reader's own curiosity, which nothing here
        # measures. The keys, the CSS classes and `impact_band()` stay English.
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
        # The four things a story block claims, so a "wrong" says which one broke
        # (PLAN-V2 5.1). Written as what the reader saw rather than as the field
        # it grades: nobody reading a bulletin thinks in `relevant` or `tier`.
        "verdict_why": "Yanlış olan ne?",
        "reason_wrong_fact": "Bilgi yanlış",
        "reason_not_news": "Yapay zekâ haberi değil",
        "reason_duplicate": "Aynı haber tekrar",
        "reason_wrong_place": "Sırası yanlış",
        # How many summaries carry one, on `/runs` beside the spend. A count and
        # not a call: the label names the thing, and the figure is a fraction
        # because "3" on its own says nothing without the 118 under it.
        # What the press measured about the day it just published (PLAN-V2 5.5).
        # Written as claims about the writing rather than as function names: the
        # person reading this page is the one who has to decide whether the
        # bulletin is any good, and "key_fact_kept" is not a sentence.
        # The demo band, on every page. It names the day rather than saying
        # "demo": a reviewer needs to know what they are looking at, and a date
        # says it without a word of jargon.
        "demo_band": (
            "Bu bir kayıt: gerçekten çalışmış baskılar, en yenisi {day} "
            "tarihinde. Anahtarsız açılsın diye tarihleri bugüne taşındı; "
            "çalıştır düğmesi bir anahtar olmadan çalışmaz."
        ),
        "quality_heading": "Bu bültenin ölçümleri",
        "quality_lead": "Hepsi ücretsiz, baskının kendisi hesapladı.",
        "quality_key_fact": "Haberi haber yapan şeyi adlandırdı",
        "quality_key_fact_kept": "Ve bunu özete taşıdı",
        "quality_numeral_recall": "Metindeki bir rakamı özete taşıdı",
        "quality_budget": "Özet kelime sınırında kaldı",
        "quality_vocabulary": "Etiketler ortak sözlükten",
        "quality_ungrounded": "Metinde olmayan rakam taşıyan haber",
        "quality_na": "yok",
        "verdicts_labelled": "Karar verilen",
        # `/runs/verdicts`: the labels themselves, under the count. The summary
        # line states the two figures the count states and no third one - what
        # counts as "enough" labels is a constant in `evals/judge.py` and this
        # layer does not import it (ADR 0019 §2).
        "verdicts_heading": "Okurun kararları",
        "verdicts_summary": "{n} / {total} habere karar verildi · {w} tanesine “yanlış” dendi",
        "verdicts_empty": (
            "Henüz bir habere karar verilmedi. Bir özetin altındaki Doğru · Yanlış "
            "bu listeyi doldurur."
        ),
        "col_story": "Haber",
        "col_verdict": "Karar",
        # The judge's findings on the same page, above the labels. The sentence
        # states the count and how many the reader has answered.
        "findings_heading": "Yargıcın desteksiz bulduğu cümleler",
        "findings_summary": "{n} bulgu · {a} tanesine karar verildi",
        "findings_empty": (
            "Yargıç henüz bir cümleyi desteksiz bulmadı. `ainews eval judge` bu listeyi doldurur."
        ),
        "labels_heading": "Bütün kararlar",
        "col_claim": "Desteksiz cümle",
        "col_judged": "Yargılandı",
        "more_topics": "Daha fazla",
        "topics": "Konular",
        "skip_to_content": "İçeriğe geç",
        # One label for a control with two states. "Daralt/genişlet" would have
        # to be swapped in JavaScript on every press, and a button whose name
        # the reader hears only when it is already halfway moved is worse than
        # one that is named after the thing it acts on.
        "rail_toggle": "Menüyü daralt veya genişlet",
        "refresh_hint": "Kaynakları tara ve yeni haberleri özetle",
        "digest_heading": "Bugünün bülteni",
        "nav_digest": "Bugün",
        "nav_archive": "Arşiv",
        "nav_sources": "Kaynaklar",
        "nav_runs": "Çalışmalar",
        "language": "Dil",
        "lang_tr": "Türkçe",
        "lang_en": "English",
        "search_placeholder": "Haber, konu veya kaynak ara",
        "search": "Ara",
        "all_tags": "Hepsi",
        "show_others": "Diğer {n} haberi göster",
        # The heading over the day's unpublished summaries. It exists because
        # those draw at the summariser's own 1-5 and the bulletin above draws at
        # the editor's tier; without a line between them the reader compares two
        # scales by eye (ADR 0030).
        "others_label": "Editörün seçmedikleri",
        "superseded": "Bu bültenin daha yeni bir sürümü var.",
        "superseded_go": "Güncel sürüme git",
        "all_summarised": "Hepsi özetlendi · arşivde ve aramada",
        # The press is on `/runs` since ADR 0015; this sentence still pointed at a
        # refresh button "above" that the bar no longer carries.
        "empty_digest": (
            "Henüz bülten yok. Çalışmalar sayfasındaki Çalıştır düğmesi kaynakları tarar, "
            "yeni haberleri özetler ve günün notunu yazar."
        ),
        "empty_search": "Bu arama için sonuç yok.",
        "empty_archive": "Arşivde henüz bülten yok.",
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
        # The run detail page (ADR 0022). The two counts mean something
        # different at every node, so each node names its own pair rather than
        # the table carrying one "in" and one "out" heading that is right for
        # none of them.
        "run_heading": "Çalışma",
        "back_to_runs": "Çalışmalara dön",
        "run_missing": "Böyle bir çalışma yok.",
        "no_steps": "Bu çalışma adım kaydından önce yapıldı; kırılımı yok.",
        "col_step": "Adım",
        "col_flow": "Akış",
        "col_note": "Not",
        "fact_tokens": "Token",
        "fact_models": "Modeller",
        "fact_stories": "Haber",
        "step_collect": "Toplama",
        "step_dedupe": "Ayıklama",
        "step_enrich": "Zenginleştirme",
        "step_summarize": "Özetleme",
        "step_rank": "Sıralama",
        "step_persist": "Yazma",
        "flow_collect": "{a} görülen → {b} yeni",
        "flow_dedupe": "{a} aday → {b} kalan",
        "flow_enrich": "{a} makale → {b} gövdeli",
        "flow_summarize": "{a} aday → {b} özet",
        "flow_rank": "{a} özet → {b} seçilen",
        "flow_persist": "{a} özet → {b} satır",
        # A step's note. Recorded by the node as a key and its numbers
        # (`StepRecord.note_key`) so that the one column of the run detail page
        # that used to be English is written here like every other label.
        "note_collect": "{sources} kaynak, {unchanged} değişmemiş",
        "note_dedupe": "{dropped} tekrar ayıklandı",
        "note_enrich": "{fetched} indirildi, {tavily} Tavily ile",
        "note_summarize_silent": "{n}/{of} dal özet üretmedi",
        "col_source": "Kaynak",
        "col_weight": "Ağırlık",
        "col_last": "Son durum",
        "col_articles": "Haber",
        "col_toggle": "",
        "enable": "Aç",
        "disable": "Kapat",
        "add_feed": "Besleme ekle",
        "add": "Ekle",
        "save": "Kaydet",
        "showing_of": "{n}/{total} gösteriliyor",
        "show_more": "Daha fazla",
        "weights_saved": "{n} ağırlık kaydedildi",
        "weights_unchanged": "Ağırlıklar değişmedi",
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
        "confirm_waiting": "{c} stories are waiting to be summarised; the poll may add more.",
        "confirm_none": (
            "No story is waiting to be summarised; unless the poll finds one, this press "
            "produces an empty bulletin."
        ),
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
        "n_sep": ",",
        "d_sep": ".",
        "u_sec": "s",
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
        "impact_spread": "Impact spread",
        "week_stories": "Stories, last 7 days",
        "last_run": "Last run",
        "cost_today": "Today",
        "cost_week": "7 days",
        "cost_month": "30 days",
        "sources_ok": "{n} sources on",
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
        "verdict_why": "What was wrong?",
        "reason_wrong_fact": "A fact is wrong",
        "reason_not_news": "Not AI news",
        "reason_duplicate": "Same story again",
        "reason_wrong_place": "In the wrong place",
        "demo_band": (
            "This is a recording: presses that really ran, the newest on "
            "{day}. The dates were moved to today so it opens without a key; "
            "the run button does nothing without one."
        ),
        "quality_heading": "What this bulletin measured",
        "quality_lead": "All of it free, computed by the press itself.",
        "quality_key_fact": "Named the thing that made it news",
        "quality_key_fact_kept": "And carried it into the summary",
        "quality_numeral_recall": "Carried a figure from the article",
        "quality_budget": "Summary kept the word budget",
        "quality_vocabulary": "Tags from the shared vocabulary",
        "quality_ungrounded": "Stories with a figure the text lacks",
        "quality_na": "none",
        "verdicts_labelled": "With a verdict",
        # See the note on the Turkish block.
        "verdicts_heading": "The reader's verdicts",
        "verdicts_summary": "{n} of {total} stories have a verdict · {w} called wrong",
        "verdicts_empty": (
            "No story carries a verdict yet. The Right · Wrong under a summary fills this list."
        ),
        "col_story": "Story",
        "col_verdict": "Verdict",
        "findings_heading": "Sentences the judge could not support",
        "findings_summary": "{n} finding(s) · {a} answered",
        "findings_empty": (
            "The judge has not failed a sentence yet. `ainews eval judge` fills this list."
        ),
        "labels_heading": "Every verdict",
        "col_claim": "Unsupported sentence",
        "col_judged": "Judged",
        "more_topics": "More",
        "topics": "Topics",
        "skip_to_content": "Skip to content",
        "rail_toggle": "Collapse or expand the menu",
        "refresh_hint": "Poll the feeds and summarise what is new",
        "digest_heading": "Today's digest",
        "nav_digest": "Today",
        "nav_archive": "Archive",
        "nav_sources": "Sources",
        "nav_runs": "Runs",
        "language": "Language",
        "lang_tr": "Türkçe",
        "lang_en": "English",
        "search_placeholder": "Search news, topics or sources",
        "search": "Search",
        "all_tags": "All",
        "show_others": "Show the other {n}",
        "others_label": "Not in the bulletin",
        "superseded": "A newer version of this bulletin exists.",
        "superseded_go": "Go to the current version",
        "all_summarised": "All summarised · in the archive and in search",
        "empty_digest": (
            "No digest yet. Run now, on the Runs page, polls the feeds, summarises what is "
            "new and writes the day's note."
        ),
        "empty_search": "Nothing matches that search.",
        "empty_archive": "No bulletins in the archive yet.",
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
        # The run detail page (ADR 0022). See the note on the Turkish block.
        "run_heading": "Run",
        "back_to_runs": "Back to runs",
        "run_missing": "There is no such run.",
        "no_steps": "This run predates step recording; there is no breakdown.",
        "col_step": "Step",
        "col_flow": "Flow",
        "col_note": "Note",
        "fact_tokens": "Tokens",
        "fact_models": "Models",
        "fact_stories": "Stories",
        "step_collect": "Collect",
        "step_dedupe": "Dedupe",
        "step_enrich": "Enrich",
        "step_summarize": "Summarize",
        "step_rank": "Rank",
        "step_persist": "Persist",
        "flow_collect": "{a} seen → {b} new",
        "flow_dedupe": "{a} candidates → {b} kept",
        "flow_enrich": "{a} articles → {b} with a body",
        "flow_summarize": "{a} candidates → {b} summaries",
        "flow_rank": "{a} summaries → {b} chosen",
        "flow_persist": "{a} summaries → {b} rows",
        "note_collect": "{sources} source(s), {unchanged} unchanged",
        "note_dedupe": "{dropped} restatement(s) dropped",
        "note_enrich": "{fetched} fetched, {tavily} via Tavily",
        "note_summarize_silent": "{n} of {of} branches produced no summary",
        "col_source": "Source",
        "col_weight": "Weight",
        "col_last": "Last status",
        "col_articles": "Articles",
        "col_toggle": "",
        "enable": "Enable",
        "disable": "Disable",
        "add_feed": "Add a feed",
        "add": "Add",
        "save": "Save",
        "showing_of": "showing {n} of {total}",
        "show_more": "Show more",
        "weights_saved": "{n} weight(s) saved",
        "weights_unchanged": "Weights unchanged",
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


class Strings(dict[str, str]):
    """The page's dictionary, which refuses to be missing a word quietly.

    The paragraph at the top of this file says a missing key is loud. It was
    not: Jinja catches `LookupError` on both `t.foo` and `t["foo"]` and renders
    `Undefined` as the empty string, so a key dropped from one dictionary showed
    up as a blank label in that language only - no traceback, no test failure,
    nothing in the log. That is worse than the silent English fallback the two
    dictionaries were chosen over, because an English word at least tells you it
    is there.

    `UndefinedError` is what makes it loud: it is neither `TypeError` nor
    `LookupError`, so Jinja lets it through instead of swallowing it, and it
    names the key. The lookups that *want* a quiet miss say so by asking rather
    than catching - `in` for the run detail page's fallback to a bare node name,
    `.get()` in `note_text` - and neither goes through `__missing__`.
    """

    def __missing__(self, key: str) -> str:
        raise UndefinedError(f"no interface string named {key!r}")


def strings(language: Language) -> Strings:
    return Strings(STRINGS[language])


def note_text(t: dict[str, str], detail: str | None) -> str:
    """One step's note, written in the reader's language.

    `run_steps.detail` carries two kinds of thing and the shape tells them
    apart. A JSON object is a sentence the pipeline recorded as its parts -
    `{"k": "dedupe", "dropped": 7}` - and it is written out here from
    `note_<k>`. Anything else is machine output that was never in a language:
    a feed's error text, an exception's repr. Those are printed verbatim.

    Every failure falls back to printing what was stored. A note is the least
    important cell on the page, and a run detail that 500s because a key was
    renamed would be worse than a run detail with one row of JSON in it.
    """
    if not detail:
        return ""
    if not detail.startswith("{"):
        return detail
    try:
        parts = json.loads(detail)
        # `.get`, not a subscript: a miss on `t` is now a loud `UndefinedError`
        # (see `Strings`), and this is the one lookup in the app that wants a
        # miss to be quiet. Asking rather than catching says so on the line.
        sentence = t.get("note_" + parts.pop("k"))
        return sentence.format(**parts) if sentence is not None else detail
    # `LookupError` covers both halves of "numbers that do not fit the sentence":
    # a named field the object does not carry raises `KeyError`, a positional
    # `{}` left in the string raises `IndexError`. Only the first used to be
    # caught, so one `{}` written into a dictionary would have taken the page
    # down over its least important cell.
    except (ValueError, LookupError, TypeError, AttributeError):
        return detail
