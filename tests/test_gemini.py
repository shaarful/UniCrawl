"""Try one article against the real LLM extraction, without saving anything.

Uses the same prompt, schema and model as the worker and the Process with LLM button.
Replace TITLE, ARTICLE and PUBLISHED_AT below, then run:

    pipenv run python tests/test_gemini.py
"""
import json
import os
import sys
import time
from pathlib import Path

TITLE = "Human anthrax in Bangladesh"

# ISO date, such as "2026-10-27". Leave empty if unknown; relative dates like
# "on Monday" then stay null.
PUBLISHED_AT = "2026-10-27"

ARTICLE = """Issue 2Volume 4
Anthrax Surveillance
written by iedcr_nbph
Anthrax is a zoonotic disease and endemic in the northern districts of Bangladesh. In 2019, IEDCR initiated an active anthrax surveillance in 9 upazilas of 5 districts (Sirajgonj, Pabna, Meherpur, Tangail, and Rajshahi) based on previous anthrax outbreaks. Since 2020, almost all the anthrax cases were reported from Meherpur district. Therefore, surveillance activities continue in three upazilas in Meherpur district.

The surveillance platform is also supporting disease prevention efforts through community engagement by distributing IEC (Information, Education and Communication) materials, such as posters and trifold leaflets. Anthrax management guidelines for community intervention and campaign programs are also developed. Awareness programs were conducted at the hospital premises to increase knowledge on caring for sick animals, disposal of carcasses, and vaccination of animals. For the diagnosis of cutaneous anthrax, IEDCR initially did staining and culture, and later diagnosis is being made by RT-PCR testing.

• In 2022, out of 464 suspected cases, 85 samples were tested of which 62 were positive (73%). Most of the cases could not be tested due to dry-wound (healed-lesion). One positive case was from Kamarkhond upazila of Sirajgonj district and the remaining were from Gangni upazila of Meherpur district.

• In 2023, out of 655 suspected cases, 142 samples could be tested of which 78 (57%) were positive. All of the cases were from Gangni upazila of Meherpur district."""


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # regional-language text in a Windows console
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "unicrawl.settings")

    import django

    django.setup()

    from llm.gemini import ArticleError, GeminiExtractor, ServiceError

    extractor = GeminiExtractor()
    started = time.time()
    try:
        result = extractor.extract(TITLE, ARTICLE, PUBLISHED_AT)
    except (ArticleError, ServiceError) as exc:
        sys.exit(f"{type(exc).__name__}: {exc}")
    elapsed = time.time() - started
    extraction = result.extraction

    print(f"\nModel:     {extractor.model}")
    print(f"Time:      {elapsed:.1f} s (limit: 30 s)")
    print(f"Tokens:    {result.prompt_tokens} in, {result.response_tokens} out")
    if result.input_truncated:
        print("Truncated: yes, the article exceeded LLM_MAX_INPUT_CHARS")

    print(f"\nRelevant: {extraction.is_disease_event}")

    print(f"\nSummary, {len(extraction.summary.split())} words:")
    print(extraction.summary)

    print("\nArticle fields:")
    print(json.dumps(
        {
            "reporter_author": extraction.reporter_author,
            "author_affiliation": extraction.author_affiliation,
            "publication_date": extraction.publication_date,
        },
        indent=2,
        ensure_ascii=False,
    ))

    print(f"\nEvents, {len(extraction.events)}:")
    print(json.dumps(
        [event.model_dump() for event in extraction.events], indent=2, ensure_ascii=False,
    ))

    if extraction.translated_body:
        print("\nTranslation:")
        print(extraction.translated_title or "")
        print(extraction.translated_body)
    else:
        print("\nTranslation: none, the article is already in English")


if __name__ == "__main__":
    main()
