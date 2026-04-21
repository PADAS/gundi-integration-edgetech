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
        state: Optional[str] = None,
    ) -> List[BuoyGear]:
        """Fetch gears from the Buoy API.

        The /gear/ endpoint defaults to state=deployed, so hauled gears are
        silently excluded unless the caller asks for them explicitly. Callers
        that need the full source-ID map (or that compare against hauled
        lifecycle data) must request both states.

        Args:
            params: Extra query parameters (e.g. page_size).
            state: Optional `state` filter value forwarded as-is — typically
                "deployed" or "hauled". When None, the server default
                (deployed-only) applies.
        """
        url = f"{self.er_site}api/v1.0/gear/"
        query_params = dict(params) if params else {}
        query_params["include_empty_location"] = "true"
        if state:
            query_params["state"] = state
        items = []

        async with aiohttp.ClientSession() as session:
            while url:
                async with session.get(
                    url, headers=self.headers, params=query_params
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
                    # Subsequent pagination URLs already encode the query
                    # string; re-sending `params` would double it up.
                    query_params = None

        if len(items) == 0:
            logger.warning("No gears found in Buoy API")

        gears = []
        for item in items:
            try:
                buoy = BuoyGear.parse_obj(item)
            except Exception as e:
                raise RuntimeError(
                    f"Error parsing gear item: {e} (item: {json.dumps(item)})"
                )
            if buoy.manufacturer.lower() != "edgetech":
                continue
            buoy.last_updated = buoy.last_updated.astimezone(timezone.utc)
            gears.append(buoy)

        return gears

    async def send_gear_to_buoy_api(
        self, gear_payload: Dict[str, Any]
    ) -> Dict[str, Any]:
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
                async with session.post(
                    url, json=gear_payload, headers=headers
                ) as response:
                    response_text = await response.text()
                    if response.status in [200, 201]:
                        logger.info(
                            f"Successfully sent gear set to Buoy API: {response.status}"
                        )
                        return {
                            "status": "success",
                            "status_code": response.status,
                            "response": response_text,
                        }
                    else:
                        logger.error(
                            f"Failed to send gear set to Buoy API. Status: {response.status}, Response: {response_text}"
                        )
                        return {
                            "status": "error",
                            "status_code": response.status,
                            "response": response_text,
                        }
            except Exception as e:
                logger.exception(f"Exception while sending gear to Buoy API")
                return {"status": "error", "error": str(e)}
