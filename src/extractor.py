import asyncio
from typing import Optional

import httpx
from fastapi import HTTPException

from src.config import ANILIST_URL


JIKAN_URL = "https://api.jikan.moe/v4"

REQUEST_TIMEOUT = 20.0

MAX_JIKAN_PAGES = 50



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
            "[YUMEIRO API] AniList network error:",
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
            "[YUMEIRO API] AniList HTTP",
            response.status_code,
            response.text[:300],
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

    if payload.get("errors"):
        print(
            "[YUMEIRO API] AniList GraphQL errors:",
            payload.get("errors"),
        )

        raise HTTPException(
            status_code=502,
            detail="AniList GraphQL query failed",
        )

    return payload.get(
        "data",
        {},
    )



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

    media = data.get(
        "Media"
    )

    if not media:
        raise HTTPException(
            status_code=404,
            detail="Anime not found on AniList",
        )

    return media



def clean_string(
    value,
):
    if not isinstance(
        value,
        str,
    ):
        return None

    result = value.strip()

    return result or None


def positive_integer(
    value,
):
    try:
        number = int(
            value
        )

    except (
        TypeError,
        ValueError,
    ):
        return None

    if number <= 0:
        return None

    return number


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

    response = await client.get(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "YUMEIRO/1.0",
        },
    )

    if response.status_code == 404:
        return None

    if response.status_code == 429:
        print(
            "[YUMEIRO API]"
            " Jikan rate limited."
        )

        return {
            "rate_limited": True,
        }

    if response.status_code != 200:
        print(
            "[YUMEIRO API]"
            f" Jikan page {page}"
            f" returned {response.status_code}"
        )

        return None

    try:
        return response.json()

    except Exception:
        return None




async def get_jikan_episodes(
    mal_id: int,
):
    episodes = []

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT
    ) as client:

        page = 1

        while page <= MAX_JIKAN_PAGES:

            try:
                payload = await fetch_jikan_page(
                    client,
                    mal_id,
                    page,
                )

            except httpx.TimeoutException:
                print(
                    "[YUMEIRO API]"
                    f" Jikan timeout page {page}"
                )

                break

            except httpx.RequestError as exc:
                print(
                    "[YUMEIRO API]"
                    " Jikan network error:",
                    str(exc),
                )

                break

            if not payload:
                break

            if payload.get(
                "rate_limited"
            ):
                await asyncio.sleep(
                    1.2
                )

                continue

            page_data = payload.get(
                "data",
                [],
            )

            if not isinstance(
                page_data,
                list,
            ):
                break

            for item in page_data:

                number = positive_integer(
                    item.get(
                        "mal_id"
                    )
                )

                if not number:
                    continue

                title = (
                    clean_string(
                        item.get(
                            "title"
                        )
                    )
                    or clean_string(
                        item.get(
                            "title_romanji"
                        )
                    )
                    or clean_string(
                        item.get(
                            "title_japanese"
                        )
                    )
                )

                aired = item.get(
                    "aired"
                )

                filler = bool(
                    item.get(
                        "filler",
                        False,
                    )
                )

                recap = bool(
                    item.get(
                        "recap",
                        False,
                    )
                )

                forum_url = clean_string(
                    item.get(
                        "forum_url"
                    )
                )

                episodes.append(
                    {
                        "number": number,

                        "title": (
                            title
                            or
                            f"Episode {number}"
                        ),

                        "description": None,

                        "airedAt": aired,

                        "duration": None,

                        "image": None,

                        "thumbnail": None,

                        "filler": filler,

                        "recap": recap,

                        "forumUrl": forum_url,

                        "providerEpisodeId": None,

                        "available": True,
                    }
                )

            pagination = payload.get(
                "pagination",
                {},
            )

            has_next = bool(
                pagination.get(
                    "has_next_page"
                )
            )

            if not has_next:
                break

            page += 1

            # Jikan rate-limit friendly
            await asyncio.sleep(
                0.45
            )

    return episodes




def create_fallback_episodes(
    total_episodes: int,
    duration=None,
):
    total = positive_integer(
        total_episodes
    )

    if not total:
        return []

    result = []

    for number in range(
        1,
        total + 1,
    ):
        result.append(
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

    return result




def merge_episode_lists(
    base_list,
    detailed_list,
):
    merged = {}

    for episode in base_list:

        number = positive_integer(
            episode.get(
                "number"
            )
        )

        if not number:
            continue

        merged[number] = {
            **episode,
        }

    for episode in detailed_list:

        number = positive_integer(
            episode.get(
                "number"
            )
        )

        if not number:
            continue

        if number in merged:

            current = merged[
                number
            ]

            for (
                key,
                value,
            ) in episode.items():

                if value not in (
                    None,
                    "",
                    [],
                ):
                    current[
                        key
                    ] = value

        else:
            merged[number] = {
                **episode,
            }

    return [
        merged[number]
        for number in sorted(
            merged.keys()
        )
    ]



async def fetch_raw_episodes(
    anilist_id: int
) -> dict:

    if (
        not isinstance(
            anilist_id,
            int,
        )
        or
        anilist_id <= 0
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid AniList ID",
        )

    print(
        "\n[YUMEIRO API]"
        " loading episodes for AniList ID:",
        anilist_id,
    )

    

    media = await get_anilist_episode_info(
        anilist_id
    )

    mal_id = positive_integer(
        media.get(
            "idMal"
        )
    )

    total_episodes = positive_integer(
        media.get(
            "episodes"
        )
    )

    duration = positive_integer(
        media.get(
            "duration"
        )
    )

    title_data = media.get(
        "title"
    ) or {}

    title = (
        clean_string(
            title_data.get(
                "english"
            )
        )
        or clean_string(
            title_data.get(
                "romaji"
            )
        )
        or clean_string(
            title_data.get(
                "native"
            )
        )
        or (
            f"AniList {anilist_id}"
        )
    )

    

    fallback_episodes = (
        create_fallback_episodes(
            total_episodes,
            duration,
        )
        if total_episodes
        else []
    )

    

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
                "[YUMEIRO API]"
                " Jikan fallback error:",
                str(exc),
            )

            jikan_episodes = []

   

    episodes = merge_episode_lists(
        fallback_episodes,
        jikan_episodes,
    )

    

    if (
        not episodes
        and jikan_episodes
    ):
        episodes = jikan_episodes

   
    response = {
        "anilistId":
            anilist_id,

        "malId":
            mal_id,

        "title":
            title,

        "format":
            media.get(
                "format"
            ),

        "status":
            media.get(
                "status"
            ),

        "episodeCount":
            total_episodes,

        "duration":
            duration,

        "episodes":
            episodes,

        "source":
            (
                "anilist+jikan"
                if jikan_episodes
                else
                "anilist"
            ),

        "providers":
            {},
    }

    print(
        "[YUMEIRO API]"
        f" returned {len(episodes)} episodes"
        f" for {title}"
    )

    return response
