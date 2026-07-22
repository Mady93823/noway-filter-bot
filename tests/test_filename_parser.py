"""Parser behavior on real-world-shaped (synthetic) filenames."""

from shared.parsing.filename import parse_media


def test_clean_filename():
    parsed = parse_media("Swati.1997.Tamil.1080p.BluRay.x264.mkv")
    assert parsed.title_guess == "swati"
    assert parsed.year == 1997
    assert parsed.languages == ("tamil",)
    assert parsed.quality == "1080p BluRay"


def test_abbreviated_filename():
    parsed = parse_media("swat.1997.tam.480p.mkv")
    assert parsed.title_guess == "swat"
    assert parsed.year == 1997
    assert parsed.languages == ("tamil",)
    assert parsed.quality == "480p"


def test_bigram_quality_and_multiple_languages():
    parsed = parse_media("Movie.Name.2023.720p.WEB-DL.Hindi-English.mkv")
    assert parsed.title_guess == "movie name"
    assert parsed.year == 2023
    assert parsed.languages == ("hindi", "english")
    assert parsed.quality == "720p WEB-DL"


def test_language_only_in_caption():
    parsed = parse_media(
        "KGF.Chapter.2.2022.1080p.mkv",
        caption="KGF 2 [Kannada + Hindi] full HD",
    )
    assert parsed.title_guess == "kgf chapter 2"
    assert parsed.year == 2022
    assert "kannada" in parsed.languages
    assert "hindi" in parsed.languages


def test_year_like_title_with_release_year():
    parsed = parse_media("1917.2019.1080p.mkv")
    assert parsed.title_guess == "1917"
    assert parsed.year == 2019


def test_year_like_title_alone():
    parsed = parse_media("1917.mkv")
    assert parsed.title_guess == "1917"
    assert parsed.year is None


def test_junk_and_channel_tag_stripped():
    parsed = parse_media("@MovieChannel Dasara 2023 Telugu 720p HEVC x265 ESub.mkv")
    assert parsed.title_guess == "dasara"
    assert parsed.year == 2023
    assert parsed.languages == ("telugu",)
    assert parsed.quality == "720p"


def test_caption_fallback_when_no_filename():
    parsed = parse_media(None, caption="Ponniyin Selvan 2022 Tamil 1080p")
    assert parsed.title_guess == "ponniyin selvan"
    assert parsed.year == 2022
    assert parsed.languages == ("tamil",)


def test_empty_input():
    parsed = parse_media(None, None)
    assert parsed.title_guess == ""
    assert parsed.year is None
    assert parsed.languages == ()
    assert parsed.quality is None


def test_subtitle_emoji_line_not_indexed_as_language():
    # Real caption shape: 🔊 = audio, 💬 = subtitles.
    parsed = parse_media(
        "Michael.2026.1080p.10bit.WEBRip.6CH.x265.HEVC-PSA.mkv",
        caption=(
            "Michael 2026 1080p 10bit WEBRip 6CH x265 HEVC PSA mkv\n\n"
            "🎞 1080p HEVC 10bit | ⏳ 02:08:44\n"
            "🔊 English\n"
            "💬 Chinese, Danish, Dutch, English, Finnish, French, German, "
            "Indonesian, Italian, Malay, Norwegian, Portuguese, Spanish, "
            "Swedish, Thai"
        ),
    )
    assert parsed.title_guess == "michael psa" or parsed.title_guess == "michael"
    assert parsed.languages == ("english",)


def test_subtitle_line_dropped_but_audio_line_kept():
    parsed = parse_media(
        "Peter.2026.HQ.HDRip.720p.x264.Tamil.Malayalam.AAC.mkv",
        caption=(
            "Peter 2026 HQ HDRip - 720p - x264 - Tamil + Malayalam - "
            "AAC 2.0 - 1.4GB - ESub\n\n"
            "🔊 Malayalam, Tamil\n"
            "💬 English"
        ),
    )
    assert set(parsed.languages) == {"tamil", "malayalam"}


def test_esub_keyword_tail_dropped():
    parsed = parse_media(
        None, caption="Dasara 2023 Telugu 720p - Subs: English, Spanish"
    )
    assert parsed.languages == ("telugu",)


def test_language_immediately_before_sub_marker_dropped():
    parsed = parse_media(None, caption="Vikram 2022 Tamil 1080p English ESub")
    assert parsed.languages == ("tamil",)


# --- regressions from the real channel dump (2026-07-22) ---


def test_codec_and_release_group_debris_dropped():
    parsed = parse_media("Parimala.And.Co.2026.Tamil.1080p.Z5.WEB-DL.DD+5.1.H.265-JeRi.mkv")
    assert parsed.title_guess == "parimala and co"
    assert parsed.quality == "1080p WEB-DL"

    parsed = parse_media("Seven.Snipers.2026.1080p.WEBRip.x264.AAC5.1-YTS.mp4")
    assert parsed.title_guess == "seven snipers"

    parsed = parse_media("The.Sheep.Detectives.2026.1080p.WEBRip.x265-KONTRAST.mkv")
    assert parsed.title_guess == "the sheep detectives"


def test_number_debris_dropped_but_numeric_titles_kept():
    parsed = parse_media("Kumki 2 (2025) Tamil HQ HDRip - 1080p - x264 - (DD+5.1 - 640.mkv")
    assert parsed.title_guess == "kumki 2"

    parsed = parse_media("23 000 Lives (2026) 720p x264 WEB DL (DD+ 5 1 192 @X.mkv")
    assert parsed.title_guess == "23 000 lives"


def test_multipart_files_unify_to_one_title():
    part1 = parse_media(
        "Raja Pokkisham [Thota Bavi] (2025) HQ HDRip - 10.part001.mkv",
        caption="Raja Pokkisham [Thota Bavi] (2025) HQ HDRip - 1080p - x264 - "
        "[Tamil + Telugu] - AAC - 2.1GB.part001.mkv",
    )
    part2 = parse_media(
        "Raja Pokkisham [Thota Bavi] (2025) HQ HDRip - 10.part002.mkv",
        caption="Raja Pokkisham [Thota Bavi] (2025) HQ HDRip - 1080p - x264 - "
        "[Tamil + Telugu] - AAC - 2.1GB.part002.mkv",
    )
    assert part1.title_guess == part2.title_guess == "raja pokkisham thota bavi"
    # resolution recovered from the caption despite the truncated filename
    assert part1.quality == "1080p HDRip"


def test_url_encoded_filename_decoded():
    parsed = parse_media("The%20Dark%20Knight.2008.720p.WEB-DL.EAC3.H265-LioN.mkv")
    assert parsed.title_guess == "the dark knight"
    assert parsed.year == 2008


def test_br_rip_bigram_recognized():
    parsed = parse_media("Attack on Titan (2022) BR Rip x264 Tamil Dub AAC @X.mkv")
    assert parsed.title_guess == "attack on titan"
    assert parsed.quality == "BRRip"
    assert parsed.languages == ("tamil",)


def test_caption_resolution_fallback():
    parsed = parse_media(
        "Project Hail Mary (2026) IMAX WEBRip x264 [Tam + @X.mkv",
        caption="Project Hail Mary (2026) IMAX WEBRip x264 [Tam + Tel + Hin] AAC\n\n"
        "⁍🎬 Quality : 360p\n⁍🔉 Audio : Tamil, Telugu, Hindi",
    )
    assert parsed.title_guess == "project hail mary"
    assert parsed.quality == "360p WEBRip"


# --- series identity (real Wednesday / Dhoolpet files from the dump) ---


def test_season_and_episode_range_split_out_of_title():
    parsed = parse_media("Wednesday S01 [E05-08] COMBINED 1080p 10bit WEB-DL HEVC .mkv")
    assert parsed.title_guess == "wednesday"
    assert parsed.season == 1
    assert parsed.episodes == "E05-E08"
    assert parsed.quality == "1080p WEB-DL"


def test_seasons_of_one_show_are_different_identities():
    s01 = parse_media("Wednesday S01 COMBINED 480p WEB-DL x264 [Hindi + English.mkv")
    s02 = parse_media("Wednesday (2025) S02 PART 1 [E01 - 04] COMBINED 720p NF WEB-.mkv")
    # same canonical name, but the season keeps them apart
    assert s01.title_guess == s02.title_guess == "wednesday"
    assert (s01.season, s02.season) == (1, 2)
    assert s02.episodes == "E01-E04"


def test_season_part_without_episode_range():
    parsed = parse_media("Wednesday S02 PART 1 COMBINED 720p NF WEB-DL x264 [Hindi.mkv")
    assert parsed.title_guess == "wednesday"
    assert parsed.season == 2
    assert parsed.episodes == "Part 1"


def test_fused_season_episode_drops_episode_name():
    parsed = parse_media("Dhoolpet Police Station S01E12 A New Knot.mkv")
    assert parsed.title_guess == "dhoolpet police station"
    assert parsed.season == 1
    assert parsed.episodes == "E12"


def test_ep_prefix_episode():
    parsed = parse_media("Second Love (2026) S01 EP01 1080p WEB DL x264 [Ta @MoviiWrld.mkv")
    assert parsed.title_guess == "second love"
    assert parsed.year == 2026
    assert (parsed.season, parsed.episodes) == (1, "E01")


def test_multipart_split_is_not_a_season_part():
    # part001/part002 are one movie cut into two uploads - not a series.
    parsed = parse_media("Spider-Man: No.Way.Home.2021.1080p.SonyLiv.WEB-D.part001.mkv")
    assert parsed.season is None
    assert parsed.episodes is None
    assert parsed.title_guess == "spider man no way home"


def test_movies_never_get_a_season():
    parsed = parse_media("Swati.1997.Tamil.1080p.BluRay.x264.mkv")
    assert parsed.season is None and parsed.episodes is None
