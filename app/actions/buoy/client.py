import logging
from typing import List, Optional

import aiohttp

from .types import ObservationSubject

logger = logging.getLogger(__name__)


class BuoyClient:
    def __init__(self, er_token: str, er_site: str):
        self.er_token = er_token
        self.er_site = er_site
        self.headers = {
            "Authorization": f"Bearer {self.er_token}",
        }

    async def get_er_subjects(self, start_datetime: Optional[str] = None) -> List:
        query_params = {
            "include_details": "true",
            "include_inactive": "true",
        }
        if start_datetime:
            query_params["updated_since"] = start_datetime

        url = f"{self.er_site}api/v1.0/subjects/"

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=self.headers, params=query_params
            ) as response:
                if response.status != 200:
                    logger.error(
                        f"Failed to make request. Status code: {response.status}"
                    )
                    return []
                data = await response.json()
                if len(data["data"]) == 0:
                    logger.error("No subjects found")
                    return []
                data = data["data"]
                return [ObservationSubject.parse_obj(item) for item in data]
