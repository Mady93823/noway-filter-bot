"""Quality token recognition (docs.md section 6, step 4).

Resolution and rip-source are recognised separately and combined into a
single label like "1080p WEB-DL". Bigram entries handle tokens that the
filename splitter breaks apart ("WEB-DL" -> "web", "dl").
"""

RESOLUTION_TOKENS: dict[str, str] = {
    "360p": "360p",
    "480p": "480p",
    "576p": "576p",
    "720p": "720p",
    "1080p": "1080p",
    "2160p": "2160p",
    "4k": "2160p",
    "uhd": "2160p",
}

SOURCE_TOKENS: dict[str, str] = {
    "hdrip": "HDRip",
    "webdl": "WEB-DL",
    "webrip": "WEBRip",
    "bluray": "BluRay",
    "brrip": "BRRip",
    "bdrip": "BDRip",
    "dvdrip": "DVDRip",
    "dvdscr": "DVDScr",
    "hdtv": "HDTV",
    "cam": "CAM",
    "camrip": "CAM",
    "hdcam": "HDCAM",
    "hdtc": "HDTC",
    "hdts": "HDTS",
    "predvd": "PreDVD",
}

# Pairs of consecutive tokens produced by splitting on '.'/'-'/'_'.
BIGRAM_SOURCE_TOKENS: dict[tuple[str, str], str] = {
    ("web", "dl"): "WEB-DL",
    ("web", "rip"): "WEBRip",
    ("blu", "ray"): "BluRay",
    ("hd", "rip"): "HDRip",
    ("br", "rip"): "BRRip",
    ("bd", "rip"): "BDRip",
    ("dvd", "rip"): "DVDRip",
    ("hd", "cam"): "HDCAM",
    ("hd", "tc"): "HDTC",
    ("hd", "ts"): "HDTS",
}


def resolution_label(token: str) -> str | None:
    return RESOLUTION_TOKENS.get(token)


def source_label(token: str) -> str | None:
    return SOURCE_TOKENS.get(token)
