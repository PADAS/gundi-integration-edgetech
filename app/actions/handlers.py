import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from gundi_client_v2 import GundiClient
from gundi_core import schemas
from gundi_core.events import LogLevel
from gundi_core.schemas.v2 import ConnectionIntegration, Integration

from app.actions.configurations import EdgeTechAuthConfiguration, EdgeTechConfiguration
from app.actions.edgetech import EdgeTechClient
from app.actions.edgetech.exceptions import InvalidCredentials
from app.actions.edgetech.processor import EdgeTechProcessor
from app.services.action_scheduler import crontab_schedule
from app.services.activity_logger import activity_logger, log_action_activity
from app.services.utils import find_config_for_action

logger = logging.getLogger(__name__)
LOAD_BATCH_SIZE = 100


# --- Helper Functions ---


def generate_batches(iterable: List, n: int = LOAD_BATCH_SIZE):
    """Yield successive n-sized batches from iterable."""
    for i in range(0, len(iterable), n):
        yield iterable[i : i + n]


async def get_destination_credentials(
    gundi_client: GundiClient,
    destination: ConnectionIntegration,
) -> Tuple[str, str]:
    """
    Retrieve the ER destination token and URL for a given destination.

    :param gundi_client: Instance of GundiClient.
    :param destination: Destination object from connection_details.
    :return: Tuple of (token, base_url).
    """
    destination_details = await gundi_client.get_integration_details(destination.id)
    auth_config_data = find_config_for_action(
        configurations=destination_details.configurations, action_id="auth"
    )
    # Parse using the ER auth schema.
    er_auth_config = schemas.v2.ERAuthActionConfig.parse_obj(auth_config_data.data)
    if not er_auth_config:
        raise ValueError(f"Missing auth configuration for destination {destination.id}")
    return er_auth_config.token, destination_details.base_url


async def process_destination(
    gundi_client: GundiClient,
    integration: Integration,
    data: List[dict],
    destination: ConnectionIntegration,
    auth_config: EdgeTechAuthConfiguration,
    start_datetime: Optional[datetime] = None,
) -> Dict:
    """
    Process a single destination: retrieve credentials, run the EdgeTech processor,
    convert observations to gear format, and send to Buoy API.

    :param gundi_client: Instance of GundiClient.
    :param integration: Integration object.
    :param data: Raw data from EdgeTechClient.
    :param destination: Destination object.
    :param auth_config: EdgeTech auth configuration containing er_token.
    :param start_datetime: Optional datetime to filter observations from.
    :return: Dictionary with processing results including total, success count, failure count, and failed payloads.
    """
    logger.info(
        f"Executing pull action for integration {integration} and destination {destination}..."
    )
    # Get only the destination URL, use er_token from auth_config
    _, er_destination_url = await get_destination_credentials(
        gundi_client, destination
    )
    
    filters = None
    if start_datetime:
        filters = {"start_datetime": start_datetime}
        
    processor = EdgeTechProcessor(data, auth_config.er_token.get_secret_value(), er_destination_url, filters=filters)
    gear_payloads = await processor.process()

    # Send gear payloads directly to Buoy API and track results
    success_count = 0
    failure_count = 0
    failed_payloads = []
    
    for idx, payload in enumerate(gear_payloads):
        result = await processor._er_client.send_gear_to_buoy_api(payload)
        if result.get("status") == "success":
            success_count += 1
            logger.info(f"Successfully sent gear set {idx + 1}/{len(gear_payloads)} to Buoy API")
        else:
            failure_count += 1
            error_info = result.get("error") or result.get("response", "Unknown error")
            logger.error(
                f"Failed to send gear set {idx + 1}/{len(gear_payloads)} with payload \"{json.dumps(payload, default=str)}\" to Buoy API: {error_info}"
            )
            failed_payloads.append({"index": idx, "error": error_info})
    
    # Log activity with success/failure counts
    log_level = LogLevel.INFO if failure_count == 0 else LogLevel.WARNING
    title = (
        f"Processed {len(gear_payloads)} gear sets: "
        f"{success_count} successful, {failure_count} failed"
    )
    await log_action_activity(
        integration_id=integration.id,
        action_id="pull_edgetech",
        level=log_level,
        title=title,
        data={"total": len(gear_payloads), "success": success_count, "failures": failure_count},
    )
    
    return {
        "total": len(gear_payloads),
        "success": success_count,
        "failures": failure_count,
        "failed_payloads": failed_payloads if failed_payloads else None,
    }


# --- Main Handler Functions ---


async def action_auth(
    integration: Integration, action_config: EdgeTechAuthConfiguration
) -> Dict:
    """
    Execute an auth action for the given integration.

    Attempts to fetch a token from EdgeTech and returns a dict indicating
    whether the credentials are valid.
    """
    logger.info(
        f"Executing auth action with integration {integration} and action_config {action_config}..."
    )
    try:
        edgetech_client = EdgeTechClient(auth_config=action_config, pull_config=None)
        await edgetech_client.get_token()
        return {"valid_credentials": True}
    except InvalidCredentials as e:
        logger.info(
            f"Invalid credentials for integration {integration}: {e.response_data}"
        )
        return {"valid_credentials": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Error authenticating with EdgeTech: {e}")
        return {"valid_credentials": None, "error": str(e)}


@activity_logger()
@crontab_schedule("*/3 * * * *")  # Run every 3 minutes
async def action_pull_edgetech_observations(
    integration: Integration, action_config: EdgeTechConfiguration
):
    """
    Pull observations from EdgeTech and send them to EarthRanger via the Buoy API.

    This action retrieves buoy data from EdgeTech within a configured time window and
    processes it to generate gear payloads for EarthRanger. The processor determines
    which gears need to be deployed, updated, or hauled based on explicit status flags
    from EdgeTech (isDeleted, isDeployed) rather than inferring state from data absence.

    Key behaviors:
    - Only processes buoys present in the current sync window
    - Haul events are generated only when EdgeTech explicitly marks a buoy as deleted or not deployed
    - Absence of a buoy from the sync window does NOT trigger a haul event
    - Location changes and status updates are tracked for existing gears

    Args:
        integration: The integration configuration
        action_config: Action-specific configuration including sync window duration

    Returns:
        Dictionary with processing results per destination including counts of extracted records
        and successful/failed gear payloads.
    """
    gundi_client = GundiClient()
    connection_details = await gundi_client.get_connection_details(integration.id)
    # Get the primary auth config from the integration.
    auth_config_data = find_config_for_action(
        configurations=integration.configurations, action_id="auth"
    )
    auth_config = EdgeTechAuthConfiguration.parse_obj(auth_config_data.data)
    edgetech_client = EdgeTechClient(auth_config=auth_config, pull_config=action_config)

    start_datetime = datetime.now(timezone.utc) - timedelta(
        minutes=action_config.minutes_to_sync
    )
    data = await edgetech_client.download_data(start_datetime=start_datetime)
    destination_result = {}
    for destination in connection_details.destinations:
        result = await process_destination(
            gundi_client, integration, data, destination, auth_config, start_datetime
        )
        destination_key = f"{destination.id}_{destination.name}"
        destination_result[destination_key] = {
            "records_extracted": len(data),
            "gear_payloads_total": result["total"],
            "gear_payloads_successful": result["success"],
            "gear_payloads_failed": result["failures"],
        }

    return destination_result
