import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp
import pydantic

from app.actions.buoy import BuoyClient
from app.actions.buoy.types import BuoyGear
from app.actions.edgetech.types import Buoy
from app.actions.utils import get_hashed_user_id

logger = logging.getLogger(__name__)


class EdgeTechProcessor:
    def __init__(
        self,
        data: List[Buoy],
        er_token: str,
        er_url: str,
        filters: Optional[dict] = None,
    ):
        """
        Initialize an EdgetTechProcessor instance.

        This constructor parses raw data records into Buoy objects, initializes an ER client for
        further processing, and sets up filters for processing buoy data.

        Args:
            data (List[Buoy]): A list of Buoy objects (parsed from raw data).
            er_token (str): Authentication token for the ER client.
            er_url (str): URL endpoint for the ER client.
            filters (dict, optional): A dictionary containing filter criteria (e.g., start_date and end_date).
                                      If not provided, default filters covering the last 180 days are used.
        """
        self._data = [Buoy.parse_obj(record) for record in data]
        self._er_client = BuoyClient(er_token, er_url)
        self._filters = filters or self._get_default_filters()
        self._prefix = "edgetech_"

    def _get_default_filters(self) -> Dict[str, Any]:
        """
        Generate default filter criteria for processing buoy data.

        By default, this method defines a time window starting 30 minutes before the current UTC time.

        Returns:
            Dict[str, Any]: A dictionary with 'start_datetime' keys defining the filter window.
        """
        start_datetime = datetime.now(timezone.utc) - timedelta(minutes=30)
        return {"start_datetime": start_datetime}

    async def send_gear_to_buoy_api(self, gear_payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send gear payload to the Buoy API POST endpoint.

        Args:
            gear_payload: The gear payload in the format expected by /api/v2/gears/

        Returns:
            Dict containing the API response
        """
        url = f"{self._er_client.er_site}api/v1/gears/"
        headers = {
            "Authorization": f"Bearer {self._er_client.er_token}",
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
                logger.exception(f"Exception while sending gear to Buoy API: {e}")
                return {"status": "error", "error": str(e)}

    def _create_gear_payload(
        self,
        buoy: Buoy,
        owner_id: str,
        device_status: str,
        end_unit_buoy: Optional[Buoy] = None,
        set_id: Optional[str] = None,
        include_initial_deployment: bool = True,
    ) -> Dict[str, Any]:
        """
        Create a gear payload directly from Buoy data.

        Args:
            buoy: The main Buoy object
            owner_id: Owner/user ID for the gear set
            device_status: Status of the device (deployed/hauled)
            end_unit_buoy: Optional second buoy for two-unit lines
            set_id: Optional gear set ID (auto-generated if not provided)
            include_initial_deployment: Whether to include initial_deployment_date

        Returns:
            Dict in the format expected by /api/v2/gears/ POST endpoint
        """
        hashed_user_id = get_hashed_user_id(buoy.userId)
        
        # Generate set_id if not provided
        if not set_id:
            set_id = f"{buoy.serialNumber}_{hashed_user_id}"
        
        # Determine deployment type
        deployment_type = "trawl" if end_unit_buoy else "single"
        
        # Create devices list
        devices = []
        
        # Main device
        main_device_id = f"{buoy.serialNumber}_{hashed_user_id}"
        if end_unit_buoy:
            main_device_id = f"{main_device_id}_A"
        
        last_updated = buoy.currentState.lastUpdated.isoformat()
        
        main_device = {
            "mfr_device_id": main_device_id,
            "mfr_id": "edgetech",
            "last_deployed": last_updated,
            "last_updated": last_updated,
            "device_status": device_status,
            "positioning_type": "gps",
            "release_type": "acoustic",
            "location": {
                "latitude": buoy.currentState.latDeg,
                "longitude": buoy.currentState.lonDeg,
            },
        }
        
        # Add raw data
        raw_data = buoy.dict()
        raw_data.pop("changeRecords", None)
        main_device["device_additional_data"] = raw_data
        
        devices.append(main_device)
        
        # End unit device (if two-unit line)
        if end_unit_buoy:
            end_device_id = f"{end_unit_buoy.serialNumber}_{hashed_user_id}_B"
            end_last_updated = end_unit_buoy.currentState.lastUpdated.isoformat()
            
            end_device = {
                "mfr_device_id": end_device_id,
                "mfr_id": "edgetech",
                "last_deployed": end_last_updated,
                "last_updated": end_last_updated,
                "device_status": device_status,
                "positioning_type": "gps",
                "release_type": "acoustic",
                "location": {
                    "latitude": end_unit_buoy.currentState.latDeg,
                    "longitude": end_unit_buoy.currentState.lonDeg,
                },
            }
            
            end_raw_data = end_unit_buoy.dict()
            end_raw_data.pop("changeRecords", None)
            end_device["device_additional_data"] = end_raw_data
            
            devices.append(end_device)
        
        # Build payload
        payload = {
            "owner_id": owner_id,
            "deployment_type": deployment_type,
            "set_id": set_id,
            "set_display_id": set_id,
            "devices_in_set": len(devices),
            "devices": devices,
        }
        
        # Add initial_deployment_date only for new deployments
        if include_initial_deployment and device_status == "deployed":
            payload["initial_deployment_date"] = buoy.currentState.lastUpdated.isoformat()
        
        return payload

    def _create_haul_payload(
        self,
        er_gear: BuoyGear,
        owner_id: str,
    ) -> Dict[str, Any]:
        """
        Create a haul payload from an existing ER gear.

        Args:
            er_gear: The existing gear from ER
            owner_id: Owner/user ID

        Returns:
            Dict in the format expected by /api/v2/gears/ POST endpoint
        """
        devices = []
        
        for device in er_gear.devices:
            haul_device = {
                "mfr_device_id": device.device_id,
                "mfr_id": "edgetech",
                "last_deployed": device.last_deployed.isoformat() if device.last_deployed else device.last_updated.isoformat(),
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "device_status": "hauled",
                "location": {
                    "latitude": device.location.latitude,
                    "longitude": device.location.longitude,
                },
            }
            devices.append(haul_device)
        
        payload = {
            "owner_id": owner_id,
            "deployment_type": er_gear.type,
            "set_id": er_gear.display_id,
            "devices": devices,
        }
        
        return payload

    def _should_skip_buoy(self, record: Buoy) -> Tuple[bool, Optional[str]]:
        """
        Determine if a buoy record should be skipped and why.

        Args:
            record (Buoy): The buoy record to check.

        Returns:
            Tuple[bool, Optional[str]]: A tuple containing:
                - bool: True if the record should be skipped, False otherwise
                - Optional[str]: The reason for skipping, or None if not skipped
        """
        if record.currentState.isDeleted:
            return (
                True,
                f"Skipping deleted buoy record with serial number {record.serialNumber}. Last updated at {record.currentState.lastUpdated}.",
            )

        if not record.currentState.isDeployed:
            return (
                True,
                f"Skipping buoy record with serial number {record.serialNumber} that is not deployed. Last updated at {record.currentState.lastUpdated}.",
            )

        if not record.has_location:
            return (
                True,
                f"Skipping buoy record with serial number {record.serialNumber} that has no location data. Last updated at {record.currentState.lastUpdated}.",
            )

        return False, None

    def _filter_edgetech_buoys_data(self, data: List[Buoy]) -> List[Buoy]:
        """
        Filter buoy data records based on their current state.

        Iterates over the parsed buoy data and selects records whose current state is not deleted
        and is deployed. If a record is deleted or not deployed, it is skipped.

        Returns:
            List[Buoy]: A list of Buoy objects that satisfy the filter criteria.
        """
        filtered_data: List[Buoy] = []
        skipped_serial_numbers: Set[str] = set()

        for record in data:
            should_skip, skip_reason = self._should_skip_buoy(record)

            if should_skip:
                logger.warning(skip_reason)
                skipped_serial_numbers.add(record.serialNumber)
                continue

            filtered_data.append(record)

        return filtered_data

    def _get_latest_buoy_states(self, data: List[Buoy]) -> List[Buoy]:
        """
        Retrieve the latest buoy states from the parsed data.

        Returns:
            List[Buoy]: A list of Buoy objects representing the latest states.
        """
        latest: Dict[str, Buoy] = {}

        for record in data:
            key = f"{record.serialNumber}{record.userId}"
            prev = latest.get(key)
            if (
                prev is None
                or record.currentState.lastUpdated > prev.currentState.lastUpdated
            ):
                latest[key] = record

        return list(latest.values())

    async def _identify_buoys(
        self,
        er_gears_devices_id_to_gear: Dict[str, BuoyGear],
        serial_number_to_edgetech_buoy: Dict[str, Buoy],
    ) -> Tuple[Set[str], Set[str], Set[str]]:
        """
        Determines which buoys need to be inserted (deployed) or updated (deployed or hauled)

        This method checks the existing ER subjects against the latest buoy states and categorizes them into:
        - `to_deploy`: Buoys that need to be inserted into the ER system.
        - `to_haul`: Buoys that need to be updated in the ER system.
        - `to_update`: Buoys that are already in the ER system and need to be updated.

        Args:
            er_subjects_name_to_subject_mapping (Dict[str, Any]): A mapping of ER subject names to their corresponding subjects.
            serial_number_to_edgetech_buoy (Dict[str, Any]): A mapping of serial numbers to Edgetech buoy objects.
        Returns:
            Tuple[Set[str], Set[str], Set[str]]: A tuple containing two sets:
                - `to_deploy`: Serial numbers of buoys that need to be set as deployed.
                - `to_haul`: Serial numbers of buoys that need to be set as hauled.
                - `to_update`: Serial numbers of buoys that are already in the ER system and need to be updated.
        """
        to_deploy: Set[str] = set()
        to_haul: Set[str] = set()
        to_update: Set[str] = set()

        for serial_number_user_id in serial_number_to_edgetech_buoy.keys():
            serial_number, hashed_user_id = serial_number_user_id.split("/", 2)
            primary_subject_name = f"{serial_number}_{hashed_user_id}_A"
            standard_subject_name = f"{serial_number}_{hashed_user_id}"

            if (
                primary_subject_name not in er_gears_devices_id_to_gear
                and standard_subject_name not in er_gears_devices_id_to_gear
            ):
                to_deploy.add(serial_number_user_id)
            else:
                # If the buoy is already in the ER system, we check if it needs to be updated.
                er_gear = er_gears_devices_id_to_gear.get(
                    primary_subject_name
                ) or er_gears_devices_id_to_gear.get(standard_subject_name)

                edgetech_buoy = serial_number_to_edgetech_buoy[serial_number_user_id]

                # Check if the buoy's location has changed.
                edgetech_buoy_current_location = (
                    edgetech_buoy.currentState.latDeg, edgetech_buoy.currentState.lonDeg
                )
                er_gear_current_location = [
                    (device.location.latitude, device.location.longitude) for device in er_gear.devices
                ]
                location_changed = edgetech_buoy_current_location not in er_gear_current_location
                
                # Check if EdgeTech data is newer than ER data
                edgetech_last_updated = edgetech_buoy.currentState.lastUpdated
                er_last_updated = er_gear.last_updated
                has_newer_data = edgetech_last_updated > er_last_updated
                
                if location_changed or has_newer_data:
                    to_update.add(serial_number_user_id)

        for device_id_user_id in er_gears_devices_id_to_gear.keys():
            logger.info(f'device_id_user_id: {device_id_user_id}')
            device_id, user_id = device_id_user_id.split("_", 1)

            device_id = device_id.replace(self._prefix, "")
            device_serial_number = device_id.split("_")[0]
            expected_key_main = f"{device_serial_number}/{user_id}"
            if expected_key_main.endswith("_A") or expected_key_main.endswith("_B"):
                expected_key_main = f"{device_serial_number}/{user_id}"[:-2]

            if expected_key_main not in serial_number_to_edgetech_buoy:
                to_haul.add(device_id_user_id)

        logger.info(f"Buoys to deploy: {to_deploy}")
        logger.info(f"Buoys to haul: {to_haul}")
        logger.info(f"Buoys to update: {to_update}")

        return to_deploy, to_haul, to_update

    async def process(self) -> List[Dict[str, Any]]:
        """
        Process buoy data to generate gear payloads for the Buoy API.

        This asynchronous method performs the following steps:
            1. Retrieves and filters the latest buoy states grouped by serial number.
            2. Fetches existing ER gears from the ER client.
            3. Maps ER gears by device ID and categorizes buoy states into:
                - Deploy: Buoys not yet present in ER.
                - Update: Buoys present in ER that need to be updated.
                - Haul: Buoys that need to be retrieved.
            4. Creates gear payloads directly for each operation.

        Returns:
            List[dict]: A list of gear payloads ready to be sent to the Buoy API.
        """
        edgetech_deployed_buoys = self._get_latest_buoy_states(self._data)
        edgetech_deployed_buoys = self._filter_edgetech_buoys_data(edgetech_deployed_buoys)

        serial_number_to_edgetech_buoy = {
            f"{buoy.serialNumber}/{get_hashed_user_id(buoy.userId)}": buoy
            for buoy in edgetech_deployed_buoys
        }

        er_gears = await self._er_client.get_er_gears(params={"page_size": 10000})

        er_gears_devices_id_to_gear = {
            device.device_id: gear
            for gear in er_gears
            for device in gear.devices
        }

        to_deploy, to_haul, to_update = await self._identify_buoys(
            er_gears_devices_id_to_gear,
            serial_number_to_edgetech_buoy,
        )

        gear_payloads = []

        # Process deployments (new gear sets)
        for serial_number_user_id in to_deploy:
            edgetech_buoy = serial_number_to_edgetech_buoy[serial_number_user_id]
            
            # Extract owner_id from buoy data
            owner_id = edgetech_buoy.userId
            
            try:
                # Get end unit buoy if this is a two-unit line
                end_unit_buoy = None
                if edgetech_buoy.currentState.isTwoUnitLine:
                    end_unit_buoy_key = f"{edgetech_buoy.currentState.endUnit}/{get_hashed_user_id(edgetech_buoy.userId)}"
                    end_unit_buoy = serial_number_to_edgetech_buoy.get(end_unit_buoy_key)
                    
                    if not end_unit_buoy:
                        logger.warning(
                            "End unit buoy %s not found for serial number %s, skipping deployment.",
                            edgetech_buoy.currentState.endUnit,
                            serial_number_user_id,
                        )
                        continue
                    
                    if edgetech_buoy.currentState.startUnit:
                        # This record is for the end unit, skip it (will be handled by start unit)
                        continue

                payload = self._create_gear_payload(
                    buoy=edgetech_buoy,
                    owner_id=owner_id,
                    device_status="deployed",
                    end_unit_buoy=end_unit_buoy,
                    include_initial_deployment=True,
                )
                gear_payloads.append(payload)
                logger.info(f"Created deployment payload for {serial_number_user_id}")
                
            except Exception as e:
                logger.exception(
                    "Failed to create gear payload for deployment %s. Error: %s",
                    serial_number_user_id,
                    str(e),
                )

        # Process updates (existing gear sets with location changes)
        for serial_number_user_id in to_update:
            edgetech_buoy = serial_number_to_edgetech_buoy[serial_number_user_id]
            edgetech_buoy_lat = edgetech_buoy.currentState.latDeg
            edgetech_buoy_long = edgetech_buoy.currentState.lonDeg

            primary_device_name = f"{serial_number_user_id.replace('/', '_')}_A"
            single_device_name = f"{serial_number_user_id.replace('/', '_')}"
            er_gear = er_gears_devices_id_to_gear.get(primary_device_name) or er_gears_devices_id_to_gear.get(single_device_name)
            
            if not er_gear:
                logger.warning(f"ER gear not found for {serial_number_user_id}, skipping update.")
                continue
            
            # Find device location in ER gear
            er_device_lat = None
            er_device_long = None
            for er_device in er_gear.devices:
                if er_device.device_id == primary_device_name or er_device.device_id == single_device_name:
                    er_device_lat = er_device.location.latitude
                    er_device_long = er_device.location.longitude
                    break
            
            if er_device_lat == edgetech_buoy_lat and er_device_long == edgetech_buoy_long:
                # No change in location, skip update
                logger.info(
                    "No change in location for buoy %s, skipping update.",
                    serial_number_user_id,
                )
                continue

            # Extract owner_id
            owner_id = edgetech_buoy.userId

            try:
                # Get end unit buoy if this is a two-unit line
                end_unit_buoy = None
                if edgetech_buoy.currentState.isTwoUnitLine:
                    if edgetech_buoy.currentState.endUnit:
                        end_unit_buoy_key = f"{edgetech_buoy.currentState.endUnit}/{get_hashed_user_id(edgetech_buoy.userId)}"
                        end_unit_buoy = serial_number_to_edgetech_buoy.get(end_unit_buoy_key)
                        
                        if not end_unit_buoy:
                            logger.warning(
                                "End unit buoy %s not found for serial number %s, skipping update.",
                                edgetech_buoy.currentState.endUnit,
                                serial_number_user_id,
                            )
                            continue
                    
                    if edgetech_buoy.currentState.startUnit:
                        # This record is for the end unit, skip it
                        continue

                payload = self._create_gear_payload(
                    buoy=edgetech_buoy,
                    owner_id=owner_id,
                    device_status="deployed",
                    end_unit_buoy=end_unit_buoy,
                    set_id=er_gear.display_id,
                    include_initial_deployment=False,
                )
                gear_payloads.append(payload)
                logger.info(f"Created update payload for {serial_number_user_id}")
                
            except Exception as e:
                logger.exception(
                    "Failed to create gear payload for update %s. Error: %s",
                    serial_number_user_id,
                    str(e),
                )

        # Process hauls (gear sets to be retrieved)
        for device_id_user_id in to_haul:
            # Check if the device exists in ER
            if device_id_user_id not in er_gears_devices_id_to_gear:
                logger.warning(
                    "No ER gear found for device %s, skipping haul.",
                    device_id_user_id,
                )
                continue

            er_gear = er_gears_devices_id_to_gear[device_id_user_id]
            
            # Extract owner_id from the gear or use a default
            owner_id = "edgetech_integration"  # Default, can be extracted from gear if available

            try:
                payload = self._create_haul_payload(
                    er_gear=er_gear,
                    owner_id=owner_id,
                )
                gear_payloads.append(payload)
                logger.info(f"Created haul payload for {device_id_user_id}")
                
            except Exception as e:
                logger.exception(
                    "Failed to create haul payload for %s. Error: %s",
                    device_id_user_id,
                    str(e),
                )

        logger.info(
            "Generated %d gear payload(s):\n%s",
            len(gear_payloads),
            json.dumps(gear_payloads, indent=4, default=str),
        )

        return gear_payloads
