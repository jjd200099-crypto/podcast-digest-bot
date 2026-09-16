import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_officer.models import Episode, Transcript
from news_officer.transcript_view import clean_transcript, render_readable_transcript


def stored(text: str, *, source: str = "official transcript"):
    return SimpleNamespace(
        episode=Episode(
            "episode",
            "Readable/Test",
            "https://example.com/episode",
            "Example Show",
            duration_seconds=3600,
            published_at=datetime(2026, 9, 16, tzinfo=UTC),
        ),
        transcript=Transcript(
            text,
            source,
            "https://example.com/transcript",
            True,
        ),
        content_sha256="abcdef1234567890" * 4,
        stored_at=datetime(2026, 9, 16, tzinfo=UTC),
        reference="a1b2c3d4",
    )


class TranscriptCleaningTests(unittest.TestCase):
    def rendered_body(self, text, *, source="official transcript"):
        _filename, content = render_readable_transcript(
            stored(text, source=source)
        )
        return content.decode("utf-8")

    def test_srt_noise_stage_direction_and_filler_are_removed(self):
        text = """WEBVTT

1
00:00:01.000 --> 00:00:03.000 align:start
[Music]

2
00:00:03.000 --> 00:00:06.000
Host: Um.

3
00:00:06.000 --> 00:00:12.000
Guest: Revenue may reach $2.4B by 2027, but not before Q4.
"""

        rendered = self.rendered_body(text, source="YouTube captions")

        self.assertNotIn("WEBVTT", rendered)
        self.assertNotIn("00:00:01", rendered)
        self.assertNotIn("[Music]", rendered)
        self.assertNotIn("**Host**", rendered)
        self.assertIn("**Guest**", rendered)
        self.assertIn("$2.4B by 2027, but not before Q4", rendered)

    def test_right_now_and_substantive_repetition_are_preserved(self):
        text = (
            "Host: Right now revenue is $10M.\n"
            "Guest: Revenue doubled.\n"
            "Guest: Revenue doubled.\n"
            "Guest: Maybe this works only below 10%, not in production."
        )

        rendered = self.rendered_body(text, source="publisher RSS transcript")

        self.assertIn("Right now revenue is $10M", rendered)
        self.assertEqual(rendered.count("Revenue doubled."), 2)
        self.assertIn("only below 10%, not in production", rendered)

    def test_exact_caption_duplicate_is_removed_only_in_caption_mode(self):
        text = "Revenue doubled.\nRevenue doubled.\nMargins improved."

        caption = self.rendered_body(text, source="YouTube captions")
        publisher = self.rendered_body(text, source="publisher RSS transcript")

        self.assertEqual(caption.count("Revenue doubled."), 1)
        self.assertEqual(publisher.count("Revenue doubled."), 2)

    def test_paired_ad_and_outro_are_removed_without_losing_surrounding_text(self):
        text = """Ben: Did you build that door?
David: Yes.
INTRO
Ben: The company reached $20B, but only after changing its model.
David: Now is a great time to thank our presenting partner, ExampleCo.
David: ExampleCo helps every team move faster and save money.
David: Visit example.com/show and tell them we sent you. Okay, Ben, so how did the model change?
Ben: It changed because stores gained local autonomy.
David: I hope you enjoyed this episode. Please subscribe, rate and review.
"""

        rendered = self.rendered_body(text)

        self.assertIn("build that door", rendered)
        self.assertNotIn("ExampleCo", rendered)
        self.assertNotIn("example.com/show", rendered)
        self.assertNotIn("subscribe, rate and review", rendered)
        self.assertIn("$20B, but only after", rendered)
        self.assertIn("how did the model change", rendered)
        self.assertIn("local autonomy", rendered)

    def test_sponsor_as_a_substantive_fact_is_not_removed(self):
        rendered = self.rendered_body(
            "Guest: A sponsor funded the study, but did not influence results."
        )

        self.assertIn("did not influence results", rendered)

    def test_sponsor_disclosure_and_neighboring_financial_evidence_are_kept(self):
        rendered = self.rendered_body(
            "Guest: Our audit found this episode is sponsored by Acme, "
            "which may bias the claims.\n"
            "Host: The reported revenue was $40M, but cash receipts were only $12M.\n"
            "Guest: Visit acme.com/disclosures and compare the contract.\n"
            "Host: Separately, gross margin fell to 18%."
        )

        self.assertIn("may bias the claims", rendered)
        self.assertIn("$40M", rendered)
        self.assertIn("only $12M", rendered)
        self.assertIn("gross margin fell to 18%", rendered)

    def test_ad_cleanup_keeps_substantive_prefix_and_same_block_return(self):
        rendered = self.rendered_body(
            "Host: Revenue reached $20M. This episode is brought to you by Acme. "
            "Visit acme.com and use code SHOW. Now let's get back to why margins fell."
        )

        self.assertIn("Revenue reached $20M", rendered)
        self.assertIn("back to why margins fell", rendered)
        self.assertNotIn("use code SHOW", rendered)

    def test_inline_bracket_speakers_merge_adjacent_turn_fragments(self):
        rendered = self.rendered_body(
            "[Host] What changed? [Guest] Distribution improved. "
            "[Guest] The improvement was 20%. [Host] Why?"
        )

        self.assertEqual(rendered.count("**Guest**"), 1)
        self.assertIn("Distribution improved. The improvement was 20%", rendered)
        self.assertIn("**Host**", rendered)

    def test_bracketed_term_in_prose_is_not_invented_as_a_speaker(self):
        rendered = self.rendered_body(
            "The team uses [AI] models, but not for every decision."
        )

        self.assertIn("uses [AI] models, but not for every decision", rendered)
        self.assertNotIn("**AI**", rendered)

    def test_substantive_text_before_outro_call_to_action_is_kept(self):
        rendered = self.rendered_body(
            "Guest: My final advice is to protect downside before chasing growth. "
            "Thanks for listening and please subscribe."
        )

        self.assertIn("protect downside before chasing growth", rendered)
        self.assertNotIn("please subscribe", rendered)

    def test_short_and_chinese_facts_before_outro_are_kept(self):
        rendered = self.rendered_body(
            "Guest: Revenue was $10M. Thanks for listening.\n"
            "嘉宾：公司收入达到一亿美元，但尚未审计。感谢收听。"
        )

        self.assertIn("Revenue was $10M", rendered)
        self.assertIn("公司收入达到一亿美元，但尚未审计", rendered)

    def test_affirmative_answers_with_or_without_numbers_are_not_filler(self):
        rendered = self.rendered_body(
            "Host: Was retention exactly 100%?\n"
            "Guest: Yes, 100%.\n"
            "Host: Was that audited?\n"
            "Guest: No."
        )

        self.assertIn("Yes, 100%", rendered)
        self.assertIn("**Guest**", rendered)
        self.assertIn("No.", rendered)

    def test_affirmative_backchannels_are_preserved_as_answers(self):
        rendered = self.rendered_body(
            "Host: Did revenue reach $100M?\n"
            "Guest: Mm-hmm.\n"
            "Host: Was it audited?\n"
            "Guest: 嗯。"
        )

        self.assertIn("Mm-hmm.", rendered)
        self.assertIn("嗯。", rendered)
        self.assertEqual(rendered.count("**Guest**"), 2)

    def test_substantive_sentences_that_resemble_outro_ctas_are_preserved(self):
        examples = (
            (
                "The practical test is simple: if you like this episode's thesis, "
                "measure churn before scaling; it fell 20%."
            ),
            "Please review the audited statements; reported revenue was $40M.",
            "Please rate the three risks by probability before investing.",
            "Please subscribe to the data feed before comparing results.",
        )

        for value in examples:
            with self.subTest(value=value):
                rendered = self.rendered_body(f"Guest: {value}")
                self.assertIn(value, rendered)

    def test_outro_sentence_removal_keeps_a_later_caveat(self):
        rendered = self.rendered_body(
            "Guest: Thanks for listening. One final caveat: "
            "the $20M figure is unaudited."
        )

        self.assertNotIn("Thanks for listening", rendered)
        self.assertIn("One final caveat", rendered)
        self.assertIn("$20M figure is unaudited", rendered)

    def test_outro_cleanup_does_not_change_advice_or_quoted_evidence(self):
        examples = (
            "Please rate and review each risk before approving the investment.",
            'We tested “Thanks for listening.” It raised conversion by 20%.',
        )

        for value in examples:
            with self.subTest(value=value):
                rendered = self.rendered_body(f"Guest: {value}")
                self.assertIn(value, rendered)

    def test_ad_scan_stops_before_intervening_dialogue(self):
        rendered = self.rendered_body(
            "Host: This episode is brought to you by Acme.\n"
            "Guest: Our strategy relies on trust, governance and long-term "
            "customer relationships.\n"
            "Host: Visit acme.com/show and use code SHOW.\n"
            "Guest: The next point is about enterprise adoption."
        )

        self.assertIn("trust, governance and long-term customer", rendered)
        self.assertIn("The next point is about enterprise adoption", rendered)
        self.assertIn("This episode is brought to you by Acme", rendered)

    def test_ad_brand_tokens_never_swallow_financial_evidence(self):
        examples = (
            (
                "Host: This episode is sponsored by Acme, which helps engineers "
                "build reliable software.\n"
                "Guest: Revenue was $50M, which was 20% above plan.\n"
                "Host: Visit acme.com to learn more.\n"
                "Guest: Churn is next."
            ),
            (
                "Host: This episode is sponsored by Acme.\n"
                "Guest: Acme grew 50% and revenue reached $5M.\n"
                "Host: Visit acme.com to learn more."
            ),
        )

        for value in examples:
            with self.subTest(value=value):
                rendered = self.rendered_body(value)
                self.assertIn("revenue", rendered.casefold())
                self.assertRegex(rendered, r"\$(?:50|5)M")

    def test_same_block_ad_cleanup_never_removes_a_financial_fact(self):
        rendered = self.rendered_body(
            "Guest: Earlier evidence matters.\n"
            "Host: This episode is sponsored by Acme. Our guest reports that "
            "revenue was $50M last year. Visit acme.com to learn more. "
            "One later caveat survives."
        )

        self.assertIn("revenue was $50M last year", rendered)
        self.assertIn("One later caveat survives", rendered)

    def test_standalone_domain_research_instruction_is_preserved(self):
        value = (
            "Visit openai.com and anthropic.com to learn more at the original "
            "pages. Revenue then grew 40%, but the figure was unaudited."
        )

        rendered = self.rendered_body(f"Guest: {value}")

        self.assertIn(value, rendered)

    def test_ad_removal_preserves_evidence_before_and_after_cta_sentence(self):
        rendered = self.rendered_body(
            "Host: This episode is brought to you by Acme.\n"
            "Host: The audited baseline was $5B. Visit acme.com and use code "
            "SHOW. Gross margin later fell by four points."
        )

        self.assertIn("The audited baseline was $5B", rendered)
        self.assertIn("Gross margin later fell by four points", rendered)
        self.assertNotIn("use code SHOW", rendered)

    def test_show_housekeeping_and_partner_footer_are_removed(self):
        rendered = self.rendered_body(
            "Host: Revenue was $20M, but it was not audited.\n"
            "Host: Download our companion PDF at show.fm/pdf. Join the Slack "
            "at show.fm/slack. And we want to thank our brand new presenting "
            "partner, Acme.\n"
            "Guest: Acme helps teams work faster.\n"
            "Host: This show is not investment advice and is for informational "
            "and entertainment purposes only. Back to the interview.\n"
            "Guest: The durable conclusion is to protect downside.\n"
            "Host: A huge thank you to our partners this season. Acme at "
            "acme.com. Beta at beta.com."
        )

        self.assertIn("Revenue was $20M, but it was not audited", rendered)
        self.assertIn("This show is not investment advice", rendered)
        self.assertIn("protect downside", rendered)
        self.assertNotIn("companion PDF", rendered)
        self.assertNotIn("presenting partner", rendered)
        # A different speaker is an evidence boundary. Even sponsor-like words
        # are retained because the cleaner must not infer that the guest joined
        # the host's ad read.
        self.assertIn("Acme helps teams work faster", rendered)
        self.assertNotIn("huge thank you", rendered)

    def test_non_vtt_note_and_clock_answer_are_preserved(self):
        rendered = self.rendered_body(
            "NOTEWORTHY: Revenue was $12M, not $10M.\n"
            "Host: What was the exact clock reading?\n"
            "12:34\n"
            "Guest: That value was written into the contract."
        )

        self.assertIn("NOTEWORTHY", rendered)
        self.assertIn("$12M, not $10M", rendered)
        self.assertIn("12:34", rendered)

    def test_webvtt_control_words_inside_cue_payload_are_preserved(self):
        rendered = self.rendered_body(
            "WEBVTT\n\n"
            "NOTE this is an actual control block\n"
            "do not render this line\n\n"
            "cue-1\n"
            "00:00:01.000 --> 00:00:05.000\n"
            "NOTE Revenue was $12M, not $10M.\n"
            "Language: the source interview was bilingual.\n"
            "Kind: the reported number was unaudited.\n",
            source="YouTube captions",
        )

        self.assertNotIn("actual control block", rendered)
        self.assertNotIn("do not render this line", rendered)
        self.assertIn("NOTE Revenue was $12M, not $10M", rendered)
        self.assertIn("Language: the source interview was bilingual", rendered)
        self.assertIn("Kind: the reported number was unaudited", rendered)

    def test_three_plain_clock_values_are_not_guessed_to_be_timestamps(self):
        rendered = self.rendered_body(
            "Host: What readings were recorded?\n"
            "12:34\n"
            "13:45\n"
            "14:56\n"
            "Guest: Those are the three contract values."
        )

        self.assertIn("12:34", rendered)
        self.assertIn("13:45", rendered)
        self.assertIn("14:56", rendered)

    def test_standalone_and_fullwidth_speaker_labels_are_preserved(self):
        rendered = self.rendered_body(
            "Host:\nDid revenue grow?\nGuest:\nYes, by 30%.\n"
            "主持人：收入增长了吗？\n嘉宾：收入增长了30%，但尚未审计。"
        )

        self.assertIn("**Host**", rendered)
        self.assertIn("Did revenue grow?", rendered)
        self.assertIn("**Guest**", rendered)
        self.assertIn("Yes, by 30%", rendered)
        self.assertIn("**主持人**", rendered)
        self.assertIn("**嘉宾**", rendered)
        self.assertIn("收入增长了30%，但尚未审计", rendered)

    def test_inaudible_marker_is_kept_as_an_evidence_boundary(self):
        rendered = self.rendered_body(
            "Guest: Revenue increased.\n[inaudible 01:02]\n"
            "Guest: The increase was not audited."
        )

        self.assertIn("*[inaudible 01:02]*", rendered)
        self.assertEqual(rendered.count("**Guest**"), 2)

    def test_cleaning_is_deterministic_and_does_not_mutate_the_source(self):
        text = "Host: Um.\nGuest: The margin is 30%, not 40%."

        first = clean_transcript(text, source="official")
        second = clean_transcript(text, source="official")

        self.assertEqual(first, second)
        self.assertEqual(text, "Host: Um.\nGuest: The margin is 30%, not 40%.")
        self.assertEqual(first.removed_filler_turns, 1)


if __name__ == "__main__":
    unittest.main()
