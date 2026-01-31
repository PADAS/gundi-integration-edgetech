import json
import logging
from datetime import timezone
from typing import Any, Dict, List, Optional

import aiohttp

from .types import BuoyGear

logger = logging.getLogger(__name__)


class BuoyClient:
    def __init__(self, er_token: str, er_site: str):
        self.er_token = er_token
        self.er_site = er_site
        self.headers = {
            "Authorization": f"Bearer {self.er_token}",
        }

    async def get_er_gears(
        self,
        params: Optional[dict] = None,
    ) -> List[BuoyGear]:
        url = f"{self.er_site}api/v1.0/gear/?include_empty_location=true"
        items = []

        async with aiohttp.ClientSession() as session:
            while url:
                async with session.get(
                    url, headers=self.headers, params=params
                ) as response:
                    if response.status != 200:
                        body = await response.text()
                        raise RuntimeError(
                            f"Failed to fetch gear from Buoy Gear API. Status code: {response.status} Body: {body}"
                        )

                    data = await response.json()

                    if "data" not in data:
                        raise RuntimeError(
                            f"Unexpected response structure from Buoy Gear API: missing 'data' field. Response: {data}"
                        )

                    page_data = data["data"]

                    if "results" not in page_data:
                        raise RuntimeError(
                            f"Unexpected response structure from Buoy Gear API: missing 'results' field. Response: {page_data}"
                        )

                    results = page_data["results"]

                    items.extend(results)

                    url = page_data.get("next")

        if len(items) == 0:
            logger.warning("No gears found in Buoy API")

        gears = []
        for item in items:
            try:
                buoy = BuoyGear.parse_obj(item)
            except Exception as e:
                raise RuntimeError(f"Error parsing gear item: {e} (item: {json.dumps(item)})")
            if buoy.manufacturer.lower() != "edgetech":
                continue
            buoy.last_updated = buoy.last_updated.astimezone(timezone.utc)
            gears.append(buoy)

        return gears

    async def send_gear_to_buoy_api(self, gear_payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send gear payload to the Buoy API POST endpoint.

        Args:
            gear_payload: The gear payload in the format expected by /api/v1.0/gear/

        Returns:
            Dict containing the API response
        """
        url = f"{self.er_site}api/v1.0/gear/"
        headers = {
            "Authorization": f"Bearer {self.er_token}",
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=gear_payload, headers=headers) as response:
                    response_text = await response.text()
                    if response.status in [200, 201]:
                        logger.info(f"Successfully sent gear set to Buoy API: {response.status}")
                        return {"status": "success", "status_code": response.status, "response": response_text}
                    else:
                        logger.error(
                            f"Failed to send gear set to Buoy API. Status: {response.status}, Response: {response_text}"
                        )
                        return {"status": "error", "status_code": response.status, "response": response_text}
            except Exception as e:
                logger.exception(f"Exception while sending gear to Buoy API")
                return {"status": "error", "error": str(e)}

    async def get_existing_source_id_by_manufacturer_id(self, manufacturer_id: str) -> Optional[str]:
        """
        Check if a source with the given manufacturer device ID exists in Buoy.

        Args:
            manufacturer_id: The manufacturer device ID to check.

        Returns:
            The source ID if found, else None.
        """
        try:
            sources = await self.get_sources()
            
            for source in sources:
                if source.get("manufacturer_id") == manufacturer_id:
                    source_id = source.get("id")
                    logger.info(f"Found existing source with manufacturer_id '{manufacturer_id}': {source_id}")
                    return source_id
            
            logger.info(f"No source found with manufacturer_id '{manufacturer_id}'")
            return None
            
        except Exception as e:
            logger.exception(f"Error checking for existing source with manufacturer_id '{manufacturer_id}': {e}")
            return None
        
    async def get_sources(self, params: Optional[dict] = None) -> List[Dict[str, Any]]:
        """
        Get all sources from the Buoy API with pagination support.

        Args:
            params: Optional query parameters for the request.

        Returns:
            List of source dictionaries.
        """
        url = f"{self.er_site}api/v1.0/sources/"
        sources = []

        async with aiohttp.ClientSession() as session:
            while url:
                async with session.get(url, headers=self.headers, params=params) as response:
                    if response.status != 200:
                        logger.error(
                            f"Failed to fetch sources. Status code: {response.status} Body: {await response.text()}"
                        )
                        break

                    data = await response.json()

                    if "data" not in data:
                        logger.error("Unexpected response structure")
                        break

                    page_data = data["data"]

                    if "results" not in page_data:
                        logger.error("No results field in response")
                        break

                    results = page_data["results"]
                    sources.extend(results)

                    url = page_data.get("next")
                    params = None

        if len(sources) == 0:
            logger.warning("No sources found")

        return sources