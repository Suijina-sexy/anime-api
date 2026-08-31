import asyncio
from typing import Optional

import httpx
from fastapi import HTTPException

from src.config import ANILIST_URL


# ============================================================
# YUMEIRO EPISODE METADATA SERVICE
#
# Episode metadata only.
#
# Priority:
# 1. AniList total episode count
# 2. AniList next airing episode
# 3. AniList latest airing schedule
# 4. Jikan enrichment when available
#
# No dependency on Miruro for episode listings.
# ============================================================


JIKAN_URL = "https://api.jikan.moe/v4"

REQUEST_TIMEOUT = 20.0

MAX_JIKAN_PAGES = 60


# ============================================================
# GENERIC HELPERS
# ============================================================


def clean_string(value):
    if not isinstance(value, str):
        return None

    value = value.strip()

    return value or None


def positive_integer(value):
    try:
        number = int(value)

    except (TypeError, ValueError):
        return None

    if number <= 0:
        return None

    return number


# ============================================================
# ANILIST QUERY
# ============================================================


async def anilist_query(
    query: str,
    variables: Optional[dict] = None,
):
    body = {
        "query": query,
    }

    if variables:
        body["variables"] = variables

    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT
        ) as client:

            response = await client.post(
                ANILIST_URL,
                json=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "YUMEIRO/1.0",
                },
            )

    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="AniList request timed out",
        )

    except httpx.RequestError as exc:
        print(
            "[YUMEIRO API] AniList connection error:",
            str(exc),
        )

        raise HTTPException(
            status_code=502,
            detail="Could not connect to AniList",
        )

    if response.status_code == 429:
        raise HTTPException(
            status_code=429,
            detail="AniList rate limit reached",
        )

    if response.status_code != 200:
        print(
            "[YUMEIRO API]",
            "AniList HTTP error:",
            response.status_code,
            response.text[:500],
        )

        raise HTTPException(
            status_code=502,
            detail="AniList query failed",
        )

    try:
        payload = response.json()

    except Exception:
        raise HTTPException(
            status_code=502,
            detail="AniList returned invalid JSON",
        )

    errors = payload.get("errors")

    if errors:
        print(
            "[YUMEIRO API]",
            "AniList GraphQL errors:",
            errors,
        )

        raise HTTPException(
            status_code=502,
            detail="AniList GraphQL query failed",
        )

    return payload.get("data", {})


# ============================================================
# ANILIST ANIME INFO
# ============================================================


async def get_anilist_episode_info(
    anilist_id: int,
):
    query = """
    query ($id: Int!) {
        Media(id: $id, type: ANIME) {
            id
            idMal

            title {
                romaji
                english
                native
            }

            format
            status

            episodes
            duration

            startDate {
                year
                month
                day
            }

            endDate {
                year
                month
                day
            }

            nextAiringEpisode {
                episode
                airingAt
                timeUntilAiring
            }

            coverImage {
                extraLarge
                large
                medium
            }

            bannerImage
        }
    }
    """

    data = await anilist_query(
        query,
        {
            "id": anilist_id,
        },
    )

    media = data.get("Media")

    if not media:
        raise HTTPException(
            status_code=404,
            detail="Anime not found on AniList",
        )

    return media


# ============================================================
# ANILIST LATEST AIRING
#
# Used if:
# - Media.episodes == null
# - nextAiringEpisode == null
# ============================================================


async def get_latest_aired_episode(
    anilist_id: int,
):
    query = """
    query ($id: Int!) {
        Page(page: 1, perPage: 1) {
            airingSchedules(
                mediaId: $id
                sort: TIME_DESC
            ) {
                episode
                airingAt
            }
        }
    }
    """

    try:
        data = await anilist_query(
            query,
            {
                "id": anilist_id,
            },
        )

    except Exception as exc:
        print(
            "[YUMEIRO API]",
            "Latest airing lookup failed:",
            str(exc),
        )

        return None

    schedules = (
        data
        .get("Page", {})
        .get("airingSchedules", [])
    )

    if not schedules:
        return None

    first = schedules[0]

    return positive_integer(
        first.get("episode")
    )


# ============================================================
# DETERMINE EPISODE COUNT
# ============================================================


async def resolve_episode_count(
    media: dict,
):
    # --------------------------------------------------------
    # 1. AniList already knows final/full count
    # --------------------------------------------------------

    total = positive_integer(
        media.get("episodes")
    )

    if total:
        return (
            total,
            "anilist-total",
        )

    # --------------------------------------------------------
    # 2. Currently airing anime
    #
    # nextAiringEpisode = episode that has NOT aired yet.
    #
    # Example:
    # next = 1150
    # available = 1149
    # --------------------------------------------------------

    next_airing = (
        media.get("nextAiringEpisode")
        or {}
    )

    next_episode = positive_integer(
        next_airing.get("episode")
    )

    if next_episode and next_episode > 1:
        return (
            next_episode - 1,
            "anilist-next-airing",
        )

    # --------------------------------------------------------
    # 3. Look at latest known airing schedule
    # --------------------------------------------------------

    latest = await get_latest_aired_episode(
        media["id"]
    )

    if latest:
        return (
            latest,
            "anilist-airing-schedule",
        )

    return (
        None,
        "unknown",
    )


# ============================================================
# CREATE BASIC EPISODES
# ============================================================


def create_basic_episodes(
    total_episodes,
    duration=None,
):
    total = positive_integer(
        total_episodes
    )

    if not total:
        return []

    episodes = []

    for number in range(
        1,
        total + 1,
    ):
        episodes.append(
            {
                "number": number,

                "title": (
                    f"Episode {number}"
                ),

                "description": None,

                "airedAt": None,

                "duration": duration,

                "image": None,

                "thumbnail": None,

                "filler": False,

                "recap": False,

                "forumUrl": None,

                "providerEpisodeId": None,

                "available": True,
            }
        )

    return episodes


# ============================================================
# JIKAN PAGE
#
# Optional enrichment only.
#
# YUMEIRO does NOT depend on this succeeding.
# ============================================================


async def fetch_jikan_page(
    client: httpx.AsyncClient,
    mal_id: int,
    page: int,
):
    url = (
        f"{JIKAN_URL}"
        f"/anime/{mal_id}"
        f"/episodes"
        f"?page={page}"
    )

    try:
        response = await client.get(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "YUMEIRO/1.0",
            },
        )

    except (
        httpx.TimeoutException,
        httpx.RequestError,
    ) as exc:

        print(
            "[YUMEIRO API]",
            f"Jikan page {page} failed:",
            str(exc),
        )

        return None

    # --------------------------------------------------------
    # Jikan / MAL temporarily unavailable
    # --------------------------------------------------------

    if response.status_code in (
        500,
        502,
        503,
        504,
    ):
        print(
            "[YUMEIRO API]",
            f"Jikan unavailable ({response.status_code})",
        )

        return None

    if response.status_code == 404:
        return None

    if response.status_code == 429:
        return {
            "_rate_limited": True,
        }

    if response.status_code != 200:
        print(
            "[YUMEIRO API]",
            f"Jikan HTTP {response.status_code}",
        )

        return None

    try:
        return response.json()

    except Exception:
        return None


# ============================================================
# JIKAN EPISODES
# ============================================================


async def get_jikan_episodes(
    mal_id: int,
):
    if not mal_id:
        return []

    episodes = []

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT
    ) as client:

        page = 1

        consecutive_rate_limits = 0

        while page <= MAX_JIKAN_PAGES:

            payload = await fetch_jikan_page(
                client,
                mal_id,
                page,
            )

            if not payload:
                break

            if payload.get("_rate_limited"):
                consecutive_rate_limits += 1

                if consecutive_rate_limits >= 3:
                    print(
                        "[YUMEIRO API]",
                        "Stopping Jikan after repeated rate limits",
                    )

                    break

                await asyncio.sleep(1.5)

                continue

            consecutive_rate_limits = 0

            page_items = payload.get(
                "data",
                [],
            )

            if not isinstance(
                page_items,
                list,
            ):
                break

            if not page_items:
                break

            for item in page_items:

                number = positive_integer(
                    item.get("mal_id")
                )

                if not number:
                    continue

                title = (
                    clean_string(
                        item.get("title")
                    )
                    or clean_string(
                        item.get("title_romanji")
                    )
                    or clean_string(
                        item.get("title_japanese")
                    )
                    or f"Episode {number}"
                )

                episodes.append(
                    {
                        "number": number,

                        "title": title,

                        "description": None,

                        "airedAt":
                            item.get("aired"),

                        "duration": None,

                        "image": None,

                        "thumbnail": None,

                        "filler": bool(
                            item.get(
                                "filler",
                                False,
                            )
                        ),

                        "recap": bool(
                            item.get(
                                "recap",
                                False,
                            )
                        ),

                        "forumUrl":
                            clean_string(
                                item.get(
                                    "forum_url"
                                )
                            ),

                        "providerEpisodeId": None,

                        "available": True,
                    }
                )

            pagination = (
                payload.get("pagination")
                or {}
            )

            if not pagination.get(
                "has_next_page"
            ):
                break

            page += 1

            await asyncio.sleep(
                0.45
            )

    return episodes


# ============================================================
# MERGE BASIC + JIKAN
# ============================================================


def merge_episode_lists(
    basic_episodes,
    detailed_episodes,
):
    by_number = {}

    # --------------------------------------------------------
    # Basic AniList-derived entries
    # --------------------------------------------------------

    for episode in basic_episodes:

        number = positive_integer(
            episode.get("number")
        )

        if not number:
            continue

        by_number[number] = {
            **episode,
        }

    # --------------------------------------------------------
    # Optional Jikan metadata
    # --------------------------------------------------------

    for episode in detailed_episodes:

        number = positive_integer(
            episode.get("number")
        )

        if not number:
            continue

        existing = by_number.get(number)

        if not existing:
            by_number[number] = {
                **episode,
            }

            continue

        for key, value in episode.items():

            if value not in (
                None,
                "",
                [],
            ):
                existing[key] = value

    return [
        by_number[number]
        for number in sorted(
            by_number.keys()
        )
    ]


# ============================================================
# FETCH RAW EPISODES
#
# Kept with the old function name so endpoints.py
# remains compatible.
# ============================================================


async def fetch_raw_episodes(
    anilist_id: int,
) -> dict:

    if (
        not isinstance(anilist_id, int)
        or anilist_id <= 0
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid AniList ID",
        )

    print(
        "\n[YUMEIRO API]",
        "Loading episode metadata:",
        anilist_id,
    )

    # ========================================================
    # 1. ANILIST ANIME
    # ========================================================

    media = await get_anilist_episode_info(
        anilist_id
    )

    # ========================================================
    # 2. BASIC INFO
    # ========================================================

    mal_id = positive_integer(
        media.get("idMal")
    )

    duration = positive_integer(
        media.get("duration")
    )

    title_data = (
        media.get("title")
        or {}
    )

    title = (
        clean_string(
            title_data.get("english")
        )
        or clean_string(
            title_data.get("romaji")
        )
        or clean_string(
            title_data.get("native")
        )
        or f"AniList {anilist_id}"
    )

    # ========================================================
    # 3. RESOLVE NUMBER OF AVAILABLE EPISODES
    # ========================================================

    (
        resolved_episode_count,
        episode_count_source,
    ) = await resolve_episode_count(
        media
    )

    print(
        "[YUMEIRO API]",
        f"{title}: episode count =",
        resolved_episode_count,
        f"({episode_count_source})",
    )

    # ========================================================
    # 4. BUILD BASIC LIST
    # ========================================================

    basic_episodes = (
        create_basic_episodes(
            resolved_episode_count,
            duration,
        )
    )

    # ========================================================
    # 5. OPTIONAL JIKAN ENRICHMENT
    #
    # Failure here NEVER destroys AniList episode list.
    # ========================================================

    jikan_episodes = []

    if mal_id:

        try:
            jikan_episodes = (
                await get_jikan_episodes(
                    mal_id
                )
            )

        except Exception as exc:
            print(
                "[YUMEIRO API]",
                "Jikan enrichment failed:",
                str(exc),
            )

            jikan_episodes = []

    # ========================================================
    # 6. MERGE
    # ========================================================

    episodes = merge_episode_lists(
        basic_episodes,
        jikan_episodes,
    )

    # ========================================================
    # 7. EDGE CASE
    #
    # If AniList couldn't determine count but Jikan happened
    # to work, use Jikan alone.
    # ========================================================

    if (
        not episodes
        and jikan_episodes
    ):
        episodes = jikan_episodes

        resolved_episode_count = len(
            episodes
        )

        episode_count_source = "jikan"

    # ========================================================
    # 8. NEXT AIRING INFO
    # ========================================================

    next_airing = (
        media.get("nextAiringEpisode")
        or None
    )

    # ========================================================
    # RESPONSE
    # ========================================================

    response = {
        "anilistId":
            anilist_id,

        "malId":
            mal_id,

        "title":
            title,

        "format":
            media.get("format"),

        "status":
            media.get("status"),

        "episodeCount":
            resolved_episode_count,

        "episodeCountSource":
            episode_count_source,

        "duration":
            duration,

        "nextAiringEpisode":
            next_airing,

        "episodes":
            episodes,

        "source": (
            "anilist+jikan"
            if jikan_episodes
            else "anilist"
        ),

        # Kept for compatibility with any older YUMEIRO code.
        "providers": {},
    }

    print(
        "[YUMEIRO API]",
        f"Returned {len(episodes)} episodes",
        f"for {title}",
    )

    return response
